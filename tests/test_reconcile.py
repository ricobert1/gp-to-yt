"""Tests for photos2yt.reconcile and `photos2yt reconcile` (Task 8).

No real API calls: FakeYouTube answers videos.list from an in-memory table and
fails the test on any other resource (so search.list can never be used).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httplib2
import pytest
from googleapiclient.errors import HttpError

from photos2yt import cli
from photos2yt.config import Config, PathsConfig, QuotaConfig, YouTubeConfig
from photos2yt.db import (
    RECONCILE_NOTE_PREFIX,
    get_connection,
    get_quota_log,
    get_video,
    init_db,
)
from photos2yt.quota import QuotaTracker, _pacific_today
from photos2yt.reconcile import BATCH_SIZE, batches, reconcile_candidates, run_reconcile

FROZEN_NOW = datetime(2026, 11, 11, 20, 0, tzinfo=timezone.utc)


def make_config(tmp_path: Path, *, unit_limit: int = 10_000, privacy: str = "unlisted") -> Config:
    return Config(
        paths=PathsConfig(
            staging_dir=tmp_path,
            db_path=tmp_path / "queue.db",
            credentials=tmp_path / "credentials.json",
            token=tmp_path / "token.json",
        ),
        timezone="America/New_York",
        youtube=YouTubeConfig(
            privacy=privacy, category_id="22", made_for_kids=False,
            title_template="{date} - {title}", default_title="Family Video",
        ),
        quota=QuotaConfig(
            daily_unit_limit=unit_limit, upload_cost_units=1600,
            playlist_insert_cost=50, playlist_item_insert_cost=50,
        ),
        video_extensions=(".mp4",),
        config_path=tmp_path / "config.yaml",
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


def add_video(conn, *, yt_id: Optional[str], status: str = "UPLOADED",
              taken_utc: str = "2020-12-24T23:15:00Z", privacy: Optional[str] = "unlisted",
              last_error: Optional[str] = None, name: Optional[str] = None) -> int:
    key = f"{yt_id}|{status}|{name}|{taken_utc}"
    sha = hashlib.sha256(key.encode()).hexdigest()
    cur = conn.execute(
        "INSERT INTO videos (source_path, sha256, size_bytes, status, created_at, updated_at, "
        "taken_utc, taken_local_date, title, youtube_video_id, actual_privacy, last_error) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (f"/staging/{name or sha[:12]}.mp4", sha, 1024, status, "2024-01-01T00:00:00Z",
         "2024-01-01T00:00:00Z", taken_utc, taken_utc[:10], "Video", yt_id, privacy, last_error),
    )
    conn.commit()
    return cur.lastrowid


def http_error(status: int, reason: Optional[str], message: str = "error") -> HttpError:
    errors = [{"reason": reason, "domain": "youtube", "message": message}] if reason else []
    body = json.dumps({"error": {"code": status, "message": message, "errors": errors}})
    return HttpError(httplib2.Response({"status": status}), body.encode())


def item(vid: str, *, privacy: str = "unlisted", recording_date: Optional[str] = "2020-12-24T23:15:00Z",
         processing: str = "succeeded", upload_status: str = "processed") -> dict:
    out: dict[str, Any] = {
        "id": vid,
        "status": {"privacyStatus": privacy, "uploadStatus": upload_status},
        "processingDetails": {"processingStatus": processing},
    }
    if recording_date is not None:
        out["recordingDetails"] = {"recordingDate": recording_date}
    return out


class FakeYouTube:
    """videos.list over ``table`` (id -> item). ``script`` holds one outcome per
    call (exception to raise, or None to answer normally), consumed in order."""

    def __init__(self, table: Optional[dict[str, dict]] = None):
        self.table = dict(table or {})
        self.calls: list[dict] = []
        self.script: list[Any] = []

    def __getattr__(self, name):
        raise AssertionError(f"reconcile must not use youtube.{name}()")

    def videos(self):
        fake = self

        class _Videos:
            def list(self, part, id, **kw):
                assert not kw, f"unexpected videos.list args {kw}"
                ids = id.split(",")
                assert 1 <= len(ids) <= BATCH_SIZE

                class _Req:
                    def execute(self_inner):
                        fake.calls.append({"part": part, "ids": ids})
                        if fake.script:
                            outcome = fake.script.pop(0)
                            if isinstance(outcome, BaseException):
                                raise outcome
                        return {"items": [fake.table[i] for i in ids if i in fake.table]}
                return _Req()

            def __getattr__(self, name):
                raise AssertionError(f"reconcile must not use videos().{name}()")
        return _Videos()


class Factory:
    def __init__(self, youtube):
        self.youtube = youtube
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.youtube


def run(conn, config, tracker, youtube, **kw):
    lines: list[str] = []
    factory = Factory(youtube)
    summary = run_reconcile(conn, config, tracker, factory, out=lines.append,
                            sleep=lambda s: None, clock=lambda: FROZEN_NOW, **kw)
    return summary, factory, lines


def units_today(conn) -> int:
    row = get_quota_log(conn, _pacific_today(lambda: FROZEN_NOW))
    return row["units_used"] if row else 0


# ---------------------------------------------------------------------------


def test_candidates_are_uploaded_and_playlisted_with_ids(conn):
    a = add_video(conn, yt_id="A", status="UPLOADED")
    b = add_video(conn, yt_id="B", status="PLAYLISTED")
    add_video(conn, yt_id=None, status="PENDING", name="p")
    add_video(conn, yt_id="X", status="FAILED", name="f")
    add_video(conn, yt_id=None, status="UPLOADED", name="noid")
    assert [v.id for v in reconcile_candidates(conn)] == [a, b]


def test_all_present_refreshes_privacy_and_flags_nothing(conn, config, tracker):
    a = add_video(conn, yt_id="A", privacy=None)
    b = add_video(conn, yt_id="B", status="PLAYLISTED")
    yt = FakeYouTube({"A": item("A"), "B": item("B")})
    s, factory, lines = run(conn, config, tracker, yt)
    assert (s.checked, s.present, s.missing, s.failed_batches) == (2, 2, 0, 0)
    assert s.attention == []
    assert len(yt.calls) == 1 and yt.calls[0]["ids"] == ["A", "B"]
    assert yt.calls[0]["part"] == "status,processingDetails,recordingDetails"
    assert get_video(conn, a).actual_privacy == "unlisted"
    assert get_video(conn, b).status.value == "PLAYLISTED"
    assert units_today(conn) == 1


def test_missing_id_keeps_status_and_sets_note(conn, config, tracker):
    a = add_video(conn, yt_id="A")
    gone = add_video(conn, yt_id="GONE", status="PLAYLISTED")
    yt = FakeYouTube({"A": item("A")})
    s, _, lines = run(conn, config, tracker, yt)
    assert s.missing == 1 and s.present == 1
    row = get_video(conn, gone)
    assert row.status.value == "PLAYLISTED"  # no state transition, row not deleted
    assert row.last_error.startswith(f"{RECONCILE_NOTE_PREFIX} 2026-11-11]")
    assert "missing on YouTube" in row.last_error
    assert get_video(conn, a).last_error is None
    assert any("GONE" in line and "missing" in line for line in lines)


def test_note_cleared_when_video_is_fine_again_but_other_errors_kept(conn, config, tracker):
    a = add_video(conn, yt_id="A", last_error=f"{RECONCILE_NOTE_PREFIX} 2026-11-01] missing on YouTube")
    b = add_video(conn, yt_id="B", last_error="HTTP 500 backendError: old upload attempt")
    yt = FakeYouTube({"A": item("A"), "B": item("B")})
    run(conn, config, tracker, yt)
    assert get_video(conn, a).last_error is None
    assert get_video(conn, b).last_error == "HTTP 500 backendError: old upload attempt"


def test_privacy_changed_and_differs_from_config(conn, config, tracker):
    a = add_video(conn, yt_id="A", privacy="unlisted")
    yt = FakeYouTube({"A": item("A", privacy="private")})
    s, _, lines = run(conn, config, tracker, yt)
    assert get_video(conn, a).actual_privacy == "private"
    assert s.privacy_changed == 1
    assert len(s.attention) == 1 and "privacy is 'private'" in s.attention[0].problem
    assert any("changed from 'unlisted' to 'private'" in line for line in lines)
    # privacy is derivable from actual_privacy, so it is not written to last_error
    assert get_video(conn, a).last_error is None


def test_recording_date_missing_and_different_are_noted(conn, config, tracker):
    miss = add_video(conn, yt_id="M")
    proc = add_video(conn, yt_id="P", name="proc")
    diff = add_video(conn, yt_id="D", name="diff")
    date_only = add_video(conn, yt_id="OK", name="ok")
    yt = FakeYouTube({
        "M": item("M", recording_date=None),
        "P": item("P", recording_date=None, processing="processing"),
        "D": item("D", recording_date="2019-01-01T00:00:00Z"),
        "OK": item("OK", recording_date="2020-12-24T00:00:00Z"),  # time part dropped: fine
    })
    s, _, _ = run(conn, config, tracker, yt)
    assert "recordingDate missing (expected 2020-12-24)" in get_video(conn, miss).last_error
    assert "still processing: processing" in get_video(conn, proc).last_error
    assert "recordingDate differs" in get_video(conn, diff).last_error
    assert get_video(conn, date_only).last_error is None
    assert len(s.attention) == 3


def test_rejected_upload_status_is_noted(conn, config, tracker):
    a = add_video(conn, yt_id="A")
    it = item("A", upload_status="rejected")
    it["status"]["rejectionReason"] = "duplicate"
    run(conn, config, tracker, FakeYouTube({"A": it}))
    assert "uploadStatus is 'rejected' (duplicate)" in get_video(conn, a).last_error


def test_batches_of_at_most_50_ids(conn, config, tracker):
    ids = [f"V{i:03d}" for i in range(120)]
    for vid in ids:
        add_video(conn, yt_id=vid, name=vid)
    yt = FakeYouTube({vid: item(vid) for vid in ids})
    s, _, _ = run(conn, config, tracker, yt)
    assert [len(c["ids"]) for c in yt.calls] == [50, 50, 20]
    assert [i for c in yt.calls for i in c["ids"]] == ids
    assert s.checked == 120 and s.present == 120
    assert units_today(conn) == 3


def test_batches_dedupe_repeated_ids():
    from photos2yt.db import VideoRecord
    vids = [VideoRecord.from_row(dict(id=i, source_path=f"/{i}", sha256="x", size_bytes=1,
                                      status="UPLOADED", created_at="t", updated_at="t",
                                      youtube_video_id="SAME" if i < 3 else f"V{i}"))
            for i in range(53)]
    groups = batches(vids)
    assert all(len({v.youtube_video_id for v in g}) <= 50 for g in groups)
    assert sum(len(g) for g in groups) == 53


def test_quota_exceeded_stops_cleanly(conn, config, tracker):
    for i in range(60):
        add_video(conn, yt_id=f"V{i}", name=str(i))
    yt = FakeYouTube({f"V{i}": item(f"V{i}") for i in range(60)})
    yt.script = [http_error(403, "quotaExceeded")]
    s, _, lines = run(conn, config, tracker, yt)
    assert s.quota_stopped and s.failed_batches == 0
    assert s.checked == 0 and s.unchecked == 60
    assert len(yt.calls) == 1  # never retried, second batch never attempted
    assert tracker.is_exhausted()
    assert any("Stopping" in line for line in lines)
    # nothing was marked missing
    assert all(get_video(conn, i).last_error is None for i in range(1, 61))


def test_local_quota_limit_stops_before_calling(conn, tmp_path):
    config = make_config(tmp_path, unit_limit=1)
    tracker = QuotaTracker(conn, config, clock=lambda: FROZEN_NOW)
    for i in range(51):
        add_video(conn, yt_id=f"V{i}", name=str(i))
    yt = FakeYouTube({f"V{i}": item(f"V{i}") for i in range(51)})
    s, _, _ = run(conn, config, tracker, yt)
    assert len(yt.calls) == 1 and s.checked == 50 and s.unchecked == 1 and s.quota_stopped


def test_transient_error_is_retried_and_hard_error_fails_batch(conn, config, tracker):
    for i in range(51):
        add_video(conn, yt_id=f"V{i}", name=str(i))
    yt = FakeYouTube({f"V{i}": item(f"V{i}") for i in range(51)})
    yt.script = [http_error(503, "backendError"), None, http_error(400, "badRequest")]
    s, _, _ = run(conn, config, tracker, yt)
    assert len(yt.calls) == 3
    assert s.checked == 50 and s.failed_batches == 1 and s.unchecked == 1
    assert s.missing == 0  # a failed call never marks anything missing


def test_nothing_to_check_needs_no_client(conn, config, tracker):
    add_video(conn, yt_id=None, status="PENDING")
    s, factory, _ = run(conn, config, tracker, FakeYouTube())
    assert factory.calls == 0 and s.checked == 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _file_db(config: Config):
    c = init_db(get_connection(config.paths.db_path))
    return c


def test_dry_run_is_read_only_and_needs_no_credentials(tmp_path, config, capsys, monkeypatch):
    c = _file_db(config)
    for i in range(51):
        add_video(c, yt_id=f"V{i}", name=str(i))
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    db = config.paths.db_path
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    files_before = sorted(p.name for p in tmp_path.iterdir())

    def boom(*a, **k):
        raise AssertionError("dry run must not load credentials")
    monkeypatch.setattr(cli, "get_credentials", boom)
    monkeypatch.setattr(cli, "get_youtube_service", boom)

    assert cli.run_reconcile_command(config, dry_run=True) == 0
    out = capsys.readouterr().out
    assert "51 video(s) to check in 2 videos.list call(s)" in out
    assert "Estimated quota: 2 unit(s)" in out
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == files_before


def test_dry_run_missing_db_is_not_created(config, capsys):
    assert cli.run_reconcile_command(config, dry_run=True) == 0
    assert not config.paths.db_path.exists()
    assert "No database" in capsys.readouterr().out


def test_real_run_missing_db_is_not_created(config, capsys):
    assert cli.run_reconcile_command(config, youtube_factory=Factory(FakeYouTube())) == 0
    assert not config.paths.db_path.exists()


def test_cli_run_and_exit_codes(config, capsys):
    c = _file_db(config)
    a = add_video(c, yt_id="A")
    add_video(c, yt_id="GONE", name="gone")
    c.close()
    yt = FakeYouTube({"A": item("A")})
    assert cli.run_reconcile_command(config, youtube_factory=Factory(yt), sleep=lambda s: None) == 0
    c = get_connection(config.paths.db_path)
    assert get_video(c, a).actual_privacy == "unlisted"
    c.close()

    yt.script = [http_error(400, "badRequest")]
    assert cli.run_reconcile_command(config, youtube_factory=Factory(yt), sleep=lambda s: None) == 1


def test_cli_parser_has_dry_run():
    args = cli.create_parser().parse_args(["reconcile", "--dry-run"])
    assert args.subcommand == "reconcile" and args.dry_run


def test_reconcile_records_verified_at_only_for_present_videos(conn, config, tracker):
    a = add_video(conn, yt_id="A")
    gone = add_video(conn, yt_id="GONE", name="gone")
    run(conn, config, tracker, FakeYouTube({"A": item("A")}))
    assert get_video(conn, a).verified_at == FROZEN_NOW.isoformat()
    assert get_video(conn, gone).verified_at is None
