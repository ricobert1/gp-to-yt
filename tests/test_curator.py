"""Tests for photos2yt.curator (Task 7) and `photos2yt curate`.

No real API calls: a FakeYouTube records every call and fails the test on any
resource other than playlists/playlistItems (so search.list can never be used).
Every rules file lives under tmp_path; nothing is written inside the package.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httplib2
import pytest
from googleapiclient.errors import HttpError

from photos2yt import cli
from photos2yt.config import Config, PathsConfig, QuotaConfig, YouTubeConfig
from photos2yt.curator import (
    PlaylistRule,
    RulesError,
    build_plan,
    curate_dry_run,
    load_rules,
    resolve_rules_path,
    run_curation,
    _match_rule,
)
from photos2yt.db import (
    VideoRecord,
    get_connection,
    get_playlist,
    get_playlist_memberships,
    get_video,
    init_db,
    upsert_playlist,
    record_playlist_item_added,
)
from photos2yt.quota import QuotaTracker

PACKAGE_RULES = Path(__file__).resolve().parent.parent / "photos2yt" / "rules.yaml"
FROZEN_NOW = datetime(2026, 3, 1, 20, 0, tzinfo=timezone.utc)

XMAS_RULES = """\
playlists:
  - name: "Christmas Day Through the Years"
    privacy: unlisted
    match:
      any:
        - {local_month: 12, local_day: 25}
"""


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def package_rules_untouched():
    """No test may create or change photos2yt/rules.yaml."""
    def snapshot():
        return PACKAGE_RULES.read_bytes() if PACKAGE_RULES.exists() else None
    before = snapshot()
    yield
    assert snapshot() == before, "a test wrote to photos2yt/rules.yaml"


def make_config(tmp_path: Path, *, unit_limit: int = 10_000, rules: Optional[Path] = None,
                config_path: Optional[Path] = None) -> Config:
    return Config(
        paths=PathsConfig(
            staging_dir=tmp_path,
            db_path=tmp_path / "queue.db",
            credentials=tmp_path / "credentials.json",
            token=tmp_path / "token.json",
            rules=rules,
        ),
        timezone="America/New_York",
        youtube=YouTubeConfig(
            privacy="unlisted", category_id="22", made_for_kids=False,
            title_template="{date} - {title}", default_title="Family Video",
        ),
        quota=QuotaConfig(
            daily_unit_limit=unit_limit, upload_cost_units=1600,
            playlist_insert_cost=50, playlist_item_insert_cost=50,
        ),
        video_extensions=(".mp4",),
        config_path=config_path,
    )


@pytest.fixture
def config(tmp_path) -> Config:
    return make_config(tmp_path)


@pytest.fixture
def conn():
    c = init_db(get_connection(":memory:"))
    yield c
    c.close()


@pytest.fixture
def tracker(conn, config) -> QuotaTracker:
    return QuotaTracker(conn, config, clock=lambda: FROZEN_NOW)


def write_rules(tmp_path: Path, text: str, name: str = "rules.yaml") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def add_video(conn, *, taken_utc: str, local_date: str, title: str = "Video",
              status: str = "UPLOADED", yt_id: Optional[str] = "auto",
              tags: Optional[list[str]] = None) -> int:
    key = f"{taken_utc}|{title}|{local_date}|{status}"
    sha = hashlib.sha256(key.encode()).hexdigest()
    cur = conn.execute(
        "INSERT INTO videos (source_path, sha256, size_bytes, status, created_at, updated_at, "
        "taken_utc, taken_local_date, title, tags_json, youtube_video_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (f"/staging/{sha[:12]}.mp4", sha, 1024, status, "2024-01-01T00:00:00Z",
         "2024-01-01T00:00:00Z", taken_utc, local_date, title,
         json.dumps(tags) if tags is not None else None,
         None if yt_id is None else (f"yt-{sha[:8]}" if yt_id == "auto" else yt_id)),
    )
    conn.commit()
    return cur.lastrowid


def record(conn, video_id: int) -> VideoRecord:
    return get_video(conn, video_id)


def http_error(status: int, reason: Optional[str], message: str = "error") -> HttpError:
    errors = [{"reason": reason, "domain": "youtube", "message": message}] if reason else []
    body = json.dumps({"error": {"code": status, "message": message, "errors": errors}})
    return HttpError(httplib2.Response({"status": status}), body.encode())


class FakeRequest:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class FakeYouTube:
    """Records calls. ``item_script`` maps a YouTube video id to a list of
    outcomes (exceptions to raise, or None for success), consumed in order."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.item_script: dict[str, list[Any]] = {}
        self.playlist_script: list[Any] = []
        self._next_pl = 0

    def __getattr__(self, name):  # search(), videos(), channels(), ... are forbidden
        raise AssertionError(f"curator must not use youtube.{name}()")

    def playlists(self):
        fake = self

        class _Playlists:
            def insert(self, part, body):
                def run():
                    fake.calls.append(("playlists.insert", {"part": part, "body": body}))
                    if fake.playlist_script:
                        outcome = fake.playlist_script.pop(0)
                        if isinstance(outcome, BaseException):
                            raise outcome
                    fake._next_pl += 1
                    return {"id": f"PL{fake._next_pl}"}
                return FakeRequest(run)

            def __getattr__(self, name):
                raise AssertionError(f"curator must not use playlists().{name}()")
        return _Playlists()

    def playlistItems(self):
        fake = self

        class _Items:
            def insert(self, part, body):
                def run():
                    fake.calls.append(("playlistItems.insert", {"part": part, "body": body}))
                    vid = body["snippet"]["resourceId"]["videoId"]
                    script = fake.item_script.get(vid)
                    if script:
                        outcome = script.pop(0)
                        if isinstance(outcome, BaseException):
                            raise outcome
                    return {"id": f"item-{vid}"}
                return FakeRequest(run)

            def __getattr__(self, name):
                raise AssertionError(f"curator must not use playlistItems().{name}()")
        return _Items()

    def item_inserts(self) -> list[tuple[str, str, int]]:
        """(playlistId, videoId, position) per playlistItems.insert attempt."""
        out = []
        for name, kw in self.calls:
            if name == "playlistItems.insert":
                s = kw["body"]["snippet"]
                out.append((s["playlistId"], s["resourceId"]["videoId"], s["position"]))
        return out


class Factory:
    def __init__(self, youtube):
        self.youtube = youtube
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.youtube


def run(conn, config, tracker, youtube, rules, **kw):
    lines: list[str] = []
    factory = Factory(youtube)
    summary = run_curation(conn, config, tracker, factory, rules, out=lines.append,
                           sleep=lambda s: None, **kw)
    return summary, factory, lines


def xmas_rules(tmp_path) -> dict[str, PlaylistRule]:
    return load_rules(write_rules(tmp_path, XMAS_RULES))


def units_today(tracker: QuotaTracker, config: Config) -> int:
    return config.quota.daily_unit_limit - tracker.remaining()


# ---------------------------------------------------------------------------
# Rule matching
# ---------------------------------------------------------------------------

def _video(**kw) -> VideoRecord:
    base = dict(id=1, source_path="/s/a.mp4", sha256="x", size_bytes=1, status="UPLOADED",
                created_at="t", updated_at="t")
    base.update(kw)
    return VideoRecord.from_row(base)


XMAS = PlaylistRule("Xmas", "unlisted", {"any": [{"local_month": 12, "local_day": 25}]})


@pytest.mark.parametrize("local_date,expected", [
    ("2024-12-25", True), ("2024-01-01", False), ("2024-12-24", False), ("2024-12-31", False),
    (None, False), ("garbage", False),
])
def test_date_rule_uses_local_date(local_date, expected):
    assert (_match_rule(XMAS, _video(taken_local_date=local_date)) is not None) == expected


def test_dec24_8pm_eastern_does_not_match_dec25_rule():
    # 8 PM EST on Dec 24 is Dec 25 03:00 UTC; the local date decides.
    v = _video(taken_utc="2024-12-25T01:00:00Z", taken_local_date="2024-12-24")
    assert _match_rule(XMAS, v) is None


def test_dec25_morning_eastern_matches():
    v = _video(taken_utc="2024-12-25T15:00:00Z", taken_local_date="2024-12-25")
    assert _match_rule(XMAS, v) is not None


def test_dec24_edge_with_dates_computed_by_ingest():
    """8 PM Eastern on Dec 24 (= Dec 25 01:00 UTC) gets local date Dec 24 from the
    ingest date helper, and so does not match the Dec 25 rule."""
    from photos2yt.metadata import compute_dates
    epoch = datetime(2024, 12, 25, 1, 0, tzinfo=timezone.utc).timestamp()
    taken_utc, local_date, _ = compute_dates(epoch, "America/New_York")
    assert local_date == "2024-12-24"
    assert _match_rule(XMAS, _video(taken_utc=taken_utc, taken_local_date=local_date)) is None


def test_month_only_and_year_conditions():
    december = PlaylistRule("Dec", "unlisted", {"any": [{"local_month": 12}]})
    y2023 = PlaylistRule("2023", "unlisted", {"any": [{"local_year": 2023}]})
    assert _match_rule(december, _video(taken_local_date="2021-12-03")) is not None
    assert _match_rule(december, _video(taken_local_date="2021-11-30")) is None
    assert _match_rule(y2023, _video(taken_local_date="2023-06-01")) is not None
    assert _match_rule(y2023, _video(taken_local_date="2024-06-01")) is None


def test_fields_within_one_condition_are_anded():
    rule = PlaylistRule("r", "unlisted", {"any": [{"local_month": 12, "title_contains_any": ["tree"]}]})
    assert _match_rule(rule, _video(taken_local_date="2020-12-01", title="The tree")) is not None
    assert _match_rule(rule, _video(taken_local_date="2020-11-01", title="The tree")) is None
    assert _match_rule(rule, _video(taken_local_date="2020-12-01", title="Snow")) is None


def test_whole_word_case_insensitive_title_and_tags():
    title_rule = PlaylistRule("t", "unlisted", {"any": [{"title_contains_any": ["CHRISTMAS"]}]})
    assert _match_rule(title_rule, _video(title="My Christmas morning")) is not None
    assert _match_rule(title_rule, _video(title="Christmassy decorations")) is None
    tag_rule = PlaylistRule("g", "unlisted", {"any": [{"tags_contain_any": ["Christmas Day"]}]})
    assert _match_rule(tag_rule, _video(tags_json='["christmas day", "family"]')) is not None
    assert _match_rule(tag_rule, _video(tags_json='["christmassy"]')) is None
    assert _match_rule(tag_rule, _video(tags_json=None)) is None


def test_any_and_all():
    any_rule = PlaylistRule("a", "unlisted", {"any": [{"local_month": 1, "local_day": 1},
                                                      {"local_month": 12, "local_day": 25}]})
    all_rule = PlaylistRule("b", "unlisted", {"all": [{"local_month": 12, "local_day": 25},
                                                      {"title_contains_any": ["Christmas"]}]})
    v = _video(taken_local_date="2024-12-25", title="Christmas video")
    assert _match_rule(any_rule, v) is not None
    assert _match_rule(all_rule, v) is not None
    assert _match_rule(all_rule, _video(taken_local_date="2024-12-25", title="Other")) is None


# ---------------------------------------------------------------------------
# Rules validation and path resolution
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,needle", [
    ('playlists:\n  - {name: "A", privacy: unlisted, match: {any: [{local_month: 1}]}}\n'
     '  - {name: "A", privacy: unlisted, match: {any: [{local_month: 1}]}}\n', "duplicate"),
    ("playlists:\n  - {name: t, privacy: unlisted, match: {any: [{unknown_field: 1}]}}\n",
     "unexpected field name"),
    ("playlists:\n  - {name: t, privacy: unlisted, match: {any: [{}]}}\n", "no recognized fields"),
    ("playlists:\n  - {name: t, privacy: unlisted, match: {any: [{local_month: 13}]}}\n",
     "invalid 'local_month'"),
    ("playlists:\n  - {name: t, privacy: unlisted, match: {all: [{local_day: 32}]}}\n",
     "invalid 'local_day'"),
    ("playlists:\n  - {name: t, privacy: unlisted, match: {any: [{local_month: true}]}}\n",
     "invalid 'local_month'"),
    ("playlists:\n  - {name: 12345, privacy: unlisted}\n", "must be a non-empty string"),
    ('playlists:\n  - {name: "", privacy: unlisted}\n', "must be a non-empty string"),
    ("playlists:\n  - {name: t, privacy: confidential}\n", "invalid 'privacy'"),
    ("playlists:\n  - {name: t, privacy: unlisted}\n", "invalid 'match'"),
    ("playlists:\n  - {name: t, privacy: unlisted, match: {some: []}}\n", "unknown key"),
    ("playlists:\n  - {name: t, privacy: unlisted, match: {any: []}}\n", "non-empty list"),
    ("playlists:\n  - {name: t, privacy: unlisted, match: {any: [{title_contains_any: x}]}}\n",
     "title_contains_any"),
    ("playlists: 5\n", "must be a list"),
])
def test_rules_validation(tmp_path, text, needle):
    with pytest.raises(RulesError) as exc:
        load_rules(write_rules(tmp_path, text))
    assert needle.lower() in str(exc.value).lower()


def test_rules_load_ok_and_empty(tmp_path):
    rules = load_rules(write_rules(tmp_path, XMAS_RULES))
    assert list(rules) == ["Christmas Day Through the Years"]
    assert rules["Christmas Day Through the Years"].privacy == "unlisted"
    assert load_rules(write_rules(tmp_path, "playlists: []\n", "empty.yaml")) == {}
    assert load_rules(write_rules(tmp_path, "", "blank.yaml")) == {}


def test_example_rules_file_is_valid():
    example = PACKAGE_RULES.with_name("rules.yaml.example")
    assert load_rules(example) == {}


def test_resolve_rules_path(tmp_path):
    cfg_file = tmp_path / "conf" / "config.yaml"
    default = make_config(tmp_path, config_path=cfg_file)
    assert resolve_rules_path(default) == (tmp_path / "conf" / "rules.yaml", False)
    from_config = make_config(tmp_path, rules=tmp_path / "mine.yaml", config_path=cfg_file)
    assert resolve_rules_path(from_config) == (tmp_path / "mine.yaml", True)
    override = tmp_path / "cli.yaml"
    assert resolve_rules_path(from_config, str(override)) == (override.resolve(), True)


def test_config_loader_reads_paths_rules(tmp_path):
    from photos2yt.config import load_config
    example = (Path(__file__).resolve().parent.parent / "config.yaml.example").read_text()
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(example.replace("rules: rules.yaml", "rules: my_rules.yaml"))
    assert load_config(cfg_file).paths.rules == (tmp_path / "my_rules.yaml").resolve()
    no_rules = "\n".join(l for l in example.splitlines() if not l.strip().startswith("rules:"))
    cfg_file.write_text(no_rules)
    assert load_config(cfg_file).paths.rules is None


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def test_plan_orders_by_date_and_assigns_positions(conn, config, tmp_path):
    ids = [add_video(conn, taken_utc=f"2023-12-25T{h:02d}:00:00Z", local_date="2023-12-25",
                     title=f"V{h}") for h in (8, 9, 10)]
    late_older = add_video(conn, taken_utc="2023-12-25T06:00:00Z", local_date="2023-12-25",
                           title="late")
    plan = build_plan(conn, xmas_rules(tmp_path), config)
    assert plan.playlists_to_create == ["Christmas Day Through the Years"]
    assert [(v, p) for _, v, p in plan.videos_to_add] == [
        (late_older, 0), (ids[0], 1), (ids[1], 2), (ids[2], 3)]
    assert plan.estimated_cost == 50 + 4 * 50


def test_plan_ignores_pending_failed_and_rows_without_youtube_id(conn, config, tmp_path):
    good = add_video(conn, taken_utc="2024-12-25T18:00:00Z", local_date="2024-12-25")
    add_video(conn, taken_utc="2024-12-25T18:00:00Z", local_date="2024-12-25", title="p",
              status="PENDING", yt_id=None)
    add_video(conn, taken_utc="2024-12-25T18:00:00Z", local_date="2024-12-25", title="f",
              status="FAILED", yt_id=None)
    add_video(conn, taken_utc="2024-12-25T19:00:00Z", local_date="2024-12-25", title="noid",
              yt_id=None)
    plan = build_plan(conn, xmas_rules(tmp_path), config)
    assert [v for _, v, _ in plan.videos_to_add] == [good]


def test_plan_for_existing_playlist_inserts_late_older_video_at_its_date(conn, config, tmp_path):
    a = add_video(conn, taken_utc="2020-12-25T10:00:00Z", local_date="2020-12-25", title="a")
    c = add_video(conn, taken_utc="2022-12-25T10:00:00Z", local_date="2022-12-25", title="c")
    upsert_playlist(conn, "Christmas Day Through the Years", "PLx")
    record_playlist_item_added(conn, a, "Christmas Day Through the Years", 0)
    record_playlist_item_added(conn, c, "Christmas Day Through the Years", 1)
    b = add_video(conn, taken_utc="2021-12-25T10:00:00Z", local_date="2021-12-25", title="b")
    plan = build_plan(conn, xmas_rules(tmp_path), config)
    assert plan.playlists_to_create == []
    assert plan.videos_to_add == [("Christmas Day Through the Years", b, 1)]
    assert plan.estimated_cost == 50


def test_dry_run_writes_nothing(conn, config, tmp_path):
    add_video(conn, taken_utc="2024-12-25T18:00:00Z", local_date="2024-12-25")
    before = list(conn.iterdump())
    out: list[str] = []
    plan = curate_dry_run(config, write_rules(tmp_path, XMAS_RULES), conn, out=out.append)
    assert len(plan.videos_to_add) == 1
    assert list(conn.iterdump()) == before
    assert "would create" in out[0]


# ---------------------------------------------------------------------------
# Running (non-dry-run)
# ---------------------------------------------------------------------------

def _xmas_videos(conn) -> list[int]:
    # Inserted out of date order on purpose.
    return [
        add_video(conn, taken_utc="2022-12-25T15:00:00Z", local_date="2022-12-25", title="2022"),
        add_video(conn, taken_utc="2020-12-25T15:00:00Z", local_date="2020-12-25", title="2020"),
        add_video(conn, taken_utc="2021-12-25T15:00:00Z", local_date="2021-12-25", title="2021"),
    ]


def test_run_creates_playlist_adds_in_date_order_and_marks_playlisted(conn, config, tracker, tmp_path):
    v2022, v2020, v2021 = _xmas_videos(conn)
    other = add_video(conn, taken_utc="2021-07-04T15:00:00Z", local_date="2021-07-04", title="July")
    yt = FakeYouTube()
    summary, _, _ = run(conn, config, tracker, yt, xmas_rules(tmp_path))

    assert yt.calls[0] == ("playlists.insert", {
        "part": "snippet,status",
        "body": {"snippet": {"title": "Christmas Day Through the Years"},
                 "status": {"privacyStatus": "unlisted"}}})
    assert get_playlist(conn, "Christmas Day Through the Years")["youtube_playlist_id"] == "PL1"
    yid = {i: record(conn, i).youtube_video_id for i in (v2020, v2021, v2022)}
    assert yt.item_inserts() == [("PL1", yid[v2020], 0), ("PL1", yid[v2021], 1),
                                 ("PL1", yid[v2022], 2)]
    assert all(kw["part"] == "snippet" for n, kw in yt.calls if n == "playlistItems.insert")

    rows = {(r["video_id"], r["added"], r["position"])
            for r in get_playlist_memberships(conn, playlist_name="Christmas Day Through the Years")}
    assert rows == {(v2020, 1, 0), (v2021, 1, 1), (v2022, 1, 2)}
    for i in (v2020, v2021, v2022):
        assert record(conn, i).status.value == "PLAYLISTED"
    assert record(conn, other).status.value == "UPLOADED"  # matches no rule

    assert summary.playlists_created == ["Christmas Day Through the Years"]
    assert (summary.added, summary.failed, summary.marked_playlisted) == (3, 0, 3)
    assert units_today(tracker, config) == 50 + 3 * 50


def test_rerun_is_idempotent_and_makes_zero_api_calls(conn, config, tracker, tmp_path):
    _xmas_videos(conn)
    rules = xmas_rules(tmp_path)
    run(conn, config, tracker, FakeYouTube(), rules)
    used = units_today(tracker, config)

    yt = FakeYouTube()
    summary, factory, _ = run(conn, config, tracker, yt, rules)
    assert yt.calls == []
    assert factory.calls == 0  # no client (so no credentials) needed
    assert summary.api_calls == 0 and summary.added == 0 and summary.failed == 0
    assert units_today(tracker, config) == used


def test_existing_playlist_is_not_recreated_and_late_older_video_goes_to_its_date(
        conn, config, tracker, tmp_path):
    v2022, v2020, v2021 = _xmas_videos(conn)
    rules = xmas_rules(tmp_path)
    run(conn, config, tracker, FakeYouTube(), rules)

    older = add_video(conn, taken_utc="2019-12-25T15:00:00Z", local_date="2019-12-25", title="2019")
    middle = add_video(conn, taken_utc="2021-12-25T20:00:00Z", local_date="2021-12-25", title="2021b")
    yt = FakeYouTube()
    summary, _, _ = run(conn, config, tracker, yt, rules)
    assert [c[0] for c in yt.calls] == ["playlistItems.insert"] * 2
    ins = yt.item_inserts()
    assert ins == [("PL1", record(conn, older).youtube_video_id, 0),
                   ("PL1", record(conn, middle).youtube_video_id, 3)]
    positions = {r["video_id"]: r["position"]
                 for r in get_playlist_memberships(conn, playlist_name="Christmas Day Through the Years")}
    assert positions == {older: 0, v2020: 1, v2021: 2, middle: 3, v2022: 4}
    assert summary.marked_playlisted == 2


def test_failed_add_does_not_block_others_and_is_retried_next_run(conn, config, tracker, tmp_path):
    v2022, v2020, v2021 = _xmas_videos(conn)
    rules = xmas_rules(tmp_path)
    yt = FakeYouTube()
    bad = record(conn, v2020).youtube_video_id
    yt.item_script[bad] = [http_error(400, "invalidVideo", "bad video")]
    summary, _, lines = run(conn, config, tracker, yt, rules)

    assert summary.failed == 1 and summary.added == 2
    # 2020 failed, so 2021 is inserted at 0 and 2022 at 1 (positions follow real state).
    yid = {i: record(conn, i).youtube_video_id for i in (v2020, v2021, v2022)}
    assert yt.item_inserts() == [("PL1", yid[v2020], 0), ("PL1", yid[v2021], 0),
                                 ("PL1", yid[v2022], 1)]
    m = {r["video_id"]: (r["added"], r["position"])
         for r in get_playlist_memberships(conn, playlist_name="Christmas Day Through the Years")}
    assert m[v2020][0] == 0
    assert record(conn, v2020).status.value == "UPLOADED"
    assert record(conn, v2021).status.value == "PLAYLISTED"
    assert any("invalidVideo" in line for line in lines)
    # The rejected request still counts against quota (conservative).
    assert units_today(tracker, config) == 50 + 3 * 50

    yt2 = FakeYouTube()
    summary2, _, _ = run(conn, config, tracker, yt2, rules)
    assert yt2.item_inserts() == [("PL1", yid[v2020], 0)]
    assert summary2.added == 1 and summary2.failed == 0
    positions = {r["video_id"]: r["position"]
                 for r in get_playlist_memberships(conn, playlist_name="Christmas Day Through the Years")}
    assert positions == {v2020: 0, v2021: 1, v2022: 2}
    assert record(conn, v2020).status.value == "PLAYLISTED"


def test_transient_errors_are_retried(conn, config, tracker, tmp_path):
    (v,) = [add_video(conn, taken_utc="2024-12-25T15:00:00Z", local_date="2024-12-25")]
    yt = FakeYouTube()
    yt.item_script[record(conn, v).youtube_video_id] = [http_error(503, "backendError"), None]
    summary, _, _ = run(conn, config, tracker, yt, xmas_rules(tmp_path))
    assert summary.added == 1 and summary.failed == 0
    assert len(yt.item_inserts()) == 2


def test_retries_exhausted_counts_as_failure(conn, config, tracker, tmp_path):
    v = add_video(conn, taken_utc="2024-12-25T15:00:00Z", local_date="2024-12-25")
    yt = FakeYouTube()
    yt.item_script[record(conn, v).youtube_video_id] = [http_error(500, "backendError")] * 10
    summary, _, _ = run(conn, config, tracker, yt, xmas_rules(tmp_path), max_attempts=3)
    assert summary.failed == 1 and summary.added == 0
    assert len(yt.item_inserts()) == 3
    assert record(conn, v).status.value == "UPLOADED"


def test_quota_exceeded_marks_exhausted_and_stops(conn, config, tracker, tmp_path):
    v2022, v2020, v2021 = _xmas_videos(conn)
    yt = FakeYouTube()
    yt.item_script[record(conn, v2021).youtube_video_id] = [http_error(403, "quotaExceeded")]
    summary, _, _ = run(conn, config, tracker, yt, xmas_rules(tmp_path))
    assert summary.quota_stopped and summary.failed == 0 and summary.added == 1
    assert tracker.is_exhausted()
    assert len(yt.item_inserts()) == 2  # 2020 ok, 2021 quotaExceeded, 2022 never tried
    assert record(conn, v2020).status.value == "PLAYLISTED"
    assert record(conn, v2022).status.value == "UPLOADED"

    # While exhausted, a further run makes no calls at all.
    yt2 = FakeYouTube()
    summary2, factory, _ = run(conn, config, tracker, yt2, xmas_rules(tmp_path))
    assert yt2.calls == [] and factory.calls == 0 and summary2.quota_stopped


def test_local_quota_counter_stops_before_calling(tmp_path):
    config = make_config(tmp_path, unit_limit=120)  # playlist (50) + 1 add (50), not 2
    conn = init_db(get_connection(":memory:"))
    tracker = QuotaTracker(conn, config, clock=lambda: FROZEN_NOW)
    _xmas_videos(conn)
    yt = FakeYouTube()
    summary, _, _ = run(conn, config, tracker, yt, xmas_rules(tmp_path))
    assert summary.quota_stopped
    assert [c[0] for c in yt.calls] == ["playlists.insert", "playlistItems.insert"]
    assert summary.added == 1
    assert not tracker.is_exhausted()  # local stop does not mark the day exhausted


def test_playlist_create_failure_skips_only_that_playlist(conn, config, tracker, tmp_path):
    v = add_video(conn, taken_utc="2024-12-25T15:00:00Z", local_date="2024-12-25",
                  title="Christmas tree")
    rules = load_rules(write_rules(tmp_path, XMAS_RULES + """\
  - name: "Trees"
    privacy: private
    match:
      any:
        - {title_contains_any: ["tree"]}
"""))
    yt = FakeYouTube()
    yt.playlist_script = [http_error(400, "invalidPlaylistSnippet")]
    summary, _, _ = run(conn, config, tracker, yt, rules)
    assert summary.failed == 1
    assert get_playlist(conn, "Christmas Day Through the Years") is None
    assert get_playlist(conn, "Trees")["youtube_playlist_id"] == "PL1"
    assert yt.calls[1][1]["body"]["status"]["privacyStatus"] == "private"
    # In one of its two playlists only: not PLAYLISTED yet.
    assert record(conn, v).status.value == "UPLOADED"

    yt2 = FakeYouTube()
    run(conn, config, tracker, yt2, rules)
    assert [c[0] for c in yt2.calls] == ["playlists.insert", "playlistItems.insert"]
    assert record(conn, v).status.value == "PLAYLISTED"


def test_failure_shown_once_on_terminal_but_still_logged(conn, config, tracker, tmp_path, capsys):
    """_Runner._fail prints via out and logs; the log record must not be
    printed a second time on stderr (by logging.lastResort when no logging is
    configured, or by the CLI's stderr handler)."""
    add_video(conn, taken_utc="2024-12-25T15:00:00Z", local_date="2024-12-25",
              title="Christmas tree")
    rules = xmas_rules(tmp_path)
    root = logging.getLogger()
    pkg = logging.getLogger("photos2yt")
    saved_root, saved_pkg = root.handlers[:], pkg.handlers[:]
    seen: list[str] = []

    class Recorder(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    for with_cli_logging in (False, True):
        root.handlers = []  # pytest's own root handlers would hide lastResort
        pkg.handlers = [h for h in saved_pkg if isinstance(h, logging.NullHandler)]
        try:
            if with_cli_logging:
                cli.configure_logging()
                root.addHandler(Recorder())
            yt = FakeYouTube()
            yt.playlist_script = [http_error(400, "invalidPlaylistSnippet")]
            summary = run_curation(conn, config, tracker, Factory(yt), rules, out=print,
                                   sleep=lambda s: None)
            captured = capsys.readouterr()
        finally:
            root.handlers, pkg.handlers = saved_root, saved_pkg
        assert summary.failed == 1
        assert captured.out.count("could not create playlist") == 1
        assert "could not create playlist" not in captured.err
    assert sum("could not create playlist" in m for m in seen) == 1


def test_playlist_not_found_skips_rest_of_that_playlist(conn, config, tracker, tmp_path):
    _xmas_videos(conn)
    upsert_playlist(conn, "Christmas Day Through the Years", "PLgone")
    yt = FakeYouTube()
    first = conn.execute(
        "SELECT youtube_video_id FROM videos ORDER BY taken_utc LIMIT 1").fetchone()[0]
    yt.item_script[first] = [http_error(404, "playlistNotFound")]
    summary, _, _ = run(conn, config, tracker, yt, xmas_rules(tmp_path))
    assert summary.failed == 1 and len(yt.item_inserts()) == 1


def test_playlisted_video_matching_a_new_rule_is_added(conn, config, tracker, tmp_path):
    v = add_video(conn, taken_utc="2024-12-25T15:00:00Z", local_date="2024-12-25")
    run(conn, config, tracker, FakeYouTube(), xmas_rules(tmp_path))
    assert record(conn, v).status.value == "PLAYLISTED"
    rules = load_rules(write_rules(tmp_path, XMAS_RULES + """\
  - name: "2024"
    privacy: unlisted
    match:
      any:
        - {local_year: 2024}
""", "rules2.yaml"))
    yt = FakeYouTube()
    summary, _, _ = run(conn, config, tracker, yt, rules)
    assert [c[0] for c in yt.calls] == ["playlists.insert", "playlistItems.insert"]
    assert summary.added == 1
    assert record(conn, v).status.value == "PLAYLISTED"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _write_config(tmp_path: Path, extra_paths: str = "") -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"""\
paths:
  staging_dir: {tmp_path}
  db_path: queue.db
  credentials: credentials.json
  token: token.json
{extra_paths}timezone: America/New_York
youtube:
  privacy: unlisted
  category_id: "22"
  made_for_kids: false
  title_template: "{{date}} - {{title}}"
  default_title: "Family Video"
quota:
  daily_unit_limit: 10000
  upload_cost_units: 1600
  playlist_insert_cost: 50
  playlist_item_insert_cost: 50
  daily_upload_limit: null
video_extensions: [.mp4]
""")
    return cfg


def _seed_db(path: Path) -> int:
    c = init_db(get_connection(path))
    vid = add_video(c, taken_utc="2024-12-25T15:00:00Z", local_date="2024-12-25", title="Xmas")
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    return vid


def _dir_state(d: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in d.iterdir() if p.is_file()}


def test_cli_dry_run_missing_db_creates_nothing(tmp_path, capsys):
    cfg = _write_config(tmp_path)
    write_rules(tmp_path, XMAS_RULES)
    assert cli.main(["-c", str(cfg), "curate", "--dry-run"]) == 0
    assert not (tmp_path / "queue.db").exists()
    assert "No database" in capsys.readouterr().out


def test_cli_dry_run_does_not_touch_or_migrate_db(tmp_path, capsys, monkeypatch):
    cfg = _write_config(tmp_path)
    write_rules(tmp_path, XMAS_RULES)
    _seed_db(tmp_path / "queue.db")
    # Simulate an older schema: drop the newest migration marker. A migrating
    # open would re-run it (and fail or change the file); a dry run must not.
    c = sqlite3.connect(tmp_path / "queue.db")
    c.execute("DELETE FROM schema_migrations WHERE version = 3")
    c.commit()
    c.close()
    before = _dir_state(tmp_path)

    def no_creds(*a, **k):
        raise AssertionError("dry run must not load credentials")
    monkeypatch.setattr(cli, "get_credentials", no_creds)

    assert cli.main(["-c", str(cfg), "curate", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Christmas Day Through the Years" in out and "position 0" in out
    assert _dir_state(tmp_path) == before


def test_cli_rules_option_and_config_path(tmp_path, capsys):
    cfg = _write_config(tmp_path, "  rules: custom_rules.yaml\n")
    _seed_db(tmp_path / "queue.db")
    # paths.rules set but missing -> error
    assert cli.main(["-c", str(cfg), "curate", "--dry-run"]) == 1
    write_rules(tmp_path, XMAS_RULES, "custom_rules.yaml")
    assert cli.main(["-c", str(cfg), "curate", "--dry-run"]) == 0
    assert "Christmas Day Through the Years" in capsys.readouterr().out
    other = write_rules(tmp_path, XMAS_RULES.replace("Christmas Day Through the Years", "Other"),
                        "other.yaml")
    assert cli.main(["-c", str(cfg), "curate", "--dry-run", "--rules", str(other)]) == 0
    assert "Playlist 'Other'" in capsys.readouterr().out
    assert cli.main(["-c", str(cfg), "curate", "--dry-run", "--rules",
                     str(tmp_path / "nope.yaml")]) == 1


def test_cli_default_rules_missing_is_not_an_error(tmp_path, capsys):
    cfg = _write_config(tmp_path)
    assert cli.main(["-c", str(cfg), "curate"]) == 0
    assert "nothing to curate" in capsys.readouterr().out
    assert not (tmp_path / "queue.db").exists()


def test_cli_invalid_rules_exit_1(tmp_path, capsys):
    cfg = _write_config(tmp_path)
    write_rules(tmp_path, "playlists:\n  - {name: t, privacy: nope}\n")
    assert cli.main(["-c", str(cfg), "curate", "--dry-run"]) == 1
    assert "invalid 'privacy'" in capsys.readouterr().err


def test_cli_real_run_with_fake_client(tmp_path, monkeypatch):
    cfg = _write_config(tmp_path)
    write_rules(tmp_path, XMAS_RULES)
    vid = _seed_db(tmp_path / "queue.db")
    from photos2yt.config import load_config
    config = load_config(cfg)
    yt = FakeYouTube()
    factory = Factory(yt)
    assert cli.run_curate_command(config, youtube_factory=factory, sleep=lambda s: None) == 0
    assert [c[0] for c in yt.calls] == ["playlists.insert", "playlistItems.insert"]
    c = get_connection(tmp_path / "queue.db")
    assert get_video(c, vid).status.value == "PLAYLISTED"
    c.close()

    # Re-run: nothing to do, the client factory (credentials) is never called.
    factory2 = Factory(FakeYouTube())
    assert cli.run_curate_command(config, youtube_factory=factory2) == 0
    assert factory2.calls == 0


def test_cli_real_run_failure_exit_code(tmp_path):
    cfg = _write_config(tmp_path)
    write_rules(tmp_path, XMAS_RULES)
    _seed_db(tmp_path / "queue.db")
    from photos2yt.config import load_config
    yt = FakeYouTube()
    yt.playlist_script = [http_error(400, "invalidPlaylistSnippet")]
    assert cli.run_curate_command(load_config(cfg), youtube_factory=Factory(yt),
                                  sleep=lambda s: None) == 1
