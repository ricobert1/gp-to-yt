"""Curator: put uploaded videos into playlists from declarative rules (Task 7).

Rules live in a YAML file (``paths.rules`` in config.yaml, default
``rules.yaml`` next to the config file, or ``photos2yt curate --rules PATH``):

    playlists:
      - name: "Christmas Day Through the Years"
        privacy: unlisted
        match:
          any:
            - {local_month: 12, local_day: 25}
            - {tags_contain_any: ["Christmas Day"]}

Matching uses ``taken_local_date`` (the local date from config.timezone),
never the UTC timestamp. Within one condition every field must hold; ``any``
needs at least one condition to hold, ``all`` needs every one. A rule with
both needs both.

The local DB is the source of truth: no search.list, no playlist reads.

Running (``run_curation``):
  1. For each rule (file order), ensure the playlist exists: look it up in the
     ``playlists`` table, else ``playlists.insert`` and store the id at once.
  2. Insert the rule's not-yet-added members in date order, (taken_utc, id).
     Each insert gets an explicit ``position`` = the number of members already
     added (per the DB) that sort before it, so the playlist ends up
     date-sorted, including late-arriving older videos. A full backfill is
     therefore positions 0, 1, 2, ...
  3. Each success is written to ``playlist_membership`` (added=1, position)
     right away; positions of later members shift by one, as on YouTube.
     A failed add leaves added=0 and is retried on the next run; it does not
     block the other adds.
  4. UPLOADED videos whose matching playlists are all done become PLAYLISTED.
     Videos that match no rule stay UPLOADED.

Quota: ``QuotaTracker.can_afford`` before every call; cost recorded after
each call that got a response. quotaExceeded marks the day exhausted and stops
the run. A re-run with nothing left to do makes zero API calls.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import yaml
from googleapiclient.errors import HttpError
from tenacity import RetryError, Retrying, retry_if_exception, stop_after_attempt

from photos2yt.config import Config
from photos2yt.db import (
    VideoRecord,
    VideoStatus,
    add_playlist_membership,
    get_playlist,
    record_playlist_item_added,
    transition_video_status,
    upsert_playlist,
)
from photos2yt.quota import QuotaTracker
from photos2yt.uploader import (
    DEFAULT_MAX_ATTEMPTS,
    _is_retryable,
    _retry_wait,
    _safe_error_text,
    parse_http_error,
)

logger = logging.getLogger(__name__)

Printer = Callable[[str], None]

VALID_PRIVACY = ("private", "unlisted", "public")
CONDITION_FIELDS = frozenset(
    {"local_month", "local_day", "local_year", "title_contains_any", "tags_contain_any"}
)
_INT_RANGES = {"local_month": (1, 12), "local_day": (1, 31), "local_year": (1800, 9999)}
MAX_PLAYLIST_TITLE = 150  # YouTube playlist title limit


# YouTube answers playlist writes made in quick succession (notably right
# after playlists.insert) with HTTP 409, reason SERVICE_UNAVAILABLE, "The
# operation was aborted." The write was not applied, so retrying is safe.
_ABORTED_409_REASONS = frozenset({"SERVICE_UNAVAILABLE"})


def _is_retryable_playlist_error(exc: BaseException) -> bool:
    """Uploader's transient errors, plus YouTube's aborted-409 on playlist writes."""
    if _is_retryable(exc):
        return True
    if isinstance(exc, HttpError):
        info = parse_http_error(exc)
        return info.status == 409 and info.reason in _ABORTED_409_REASONS
    return False


class RulesError(RuntimeError):
    """The rules file is missing, unreadable or invalid."""


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PlaylistRule:
    """One playlist: name, privacy for playlists.insert, and match conditions."""

    name: str
    privacy: str
    match: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    @property
    def any_conditions(self) -> Optional[list[dict[str, Any]]]:
        return self.match.get("any")

    @property
    def all_conditions(self) -> Optional[list[dict[str, Any]]]:
        return self.match.get("all")


def resolve_rules_path(config: Config, override: Optional[str | Path] = None) -> tuple[Path, bool]:
    """Return (rules path, explicitly_set).

    Order: CLI override, then ``paths.rules`` from config, then ``rules.yaml``
    next to the config file (or in the current directory if the config has no
    path). ``explicitly_set`` is False only for the last fallback.
    """
    if override is not None:
        return Path(override).expanduser().resolve(), True
    if config.paths.rules is not None:
        return Path(config.paths.rules), True
    base = config.config_path.parent if config.config_path else Path.cwd()
    return base / "rules.yaml", False


def _validate_condition(name: str, where: str, j: int, cond: Any) -> None:
    label = f"Playlist '{name}': condition {j} in '{where}'"
    if not isinstance(cond, dict):
        raise RulesError(f"{label} is not a mapping")
    if not cond:
        raise RulesError(f"{label} has no recognized fields (empty dict)")
    for key in cond:
        if key not in CONDITION_FIELDS:
            raise RulesError(
                f"{label} has unknown field {key!r}: unexpected field name "
                f"(allowed: {', '.join(sorted(CONDITION_FIELDS))})"
            )
    for key, (lo, hi) in _INT_RANGES.items():
        if key in cond:
            val = cond[key]
            if isinstance(val, bool) or not isinstance(val, int) or not lo <= val <= hi:
                raise RulesError(
                    f"{label} has invalid '{key}'={val!r}; must be an integer between {lo} and {hi}"
                )
    for key in ("title_contains_any", "tags_contain_any"):
        if key in cond:
            words = cond[key]
            if (
                not isinstance(words, list)
                or not words
                or not all(isinstance(w, str) and w.strip() for w in words)
            ):
                raise RulesError(
                    f"{label} has invalid '{key}' (expected a non-empty list of non-empty strings)"
                )


def load_rules(rules_file: Path) -> dict[str, PlaylistRule]:
    """Load and validate rules. Returns {name: rule} in file order. Raises RulesError."""
    try:
        with open(rules_file, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except FileNotFoundError:
        raise RulesError(f"Rules file not found: {rules_file}") from None
    except (yaml.YAMLError, OSError) as e:
        raise RulesError(f"Failed to read rules file {rules_file}: {e}") from None

    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise RulesError("Root of the rules file must be a mapping")
    playlists_raw = raw.get("playlists")
    if playlists_raw is None:
        return {}
    if not isinstance(playlists_raw, list):
        raise RulesError(
            f"Top-level 'playlists' must be a list (got {type(playlists_raw).__name__})"
        )

    rules: dict[str, PlaylistRule] = {}
    for i, item in enumerate(playlists_raw):
        if not isinstance(item, dict):
            raise RulesError(f"Each playlist must be a mapping; item {i} is not")
        name_val = item.get("name")
        if not isinstance(name_val, str) or not name_val.strip():
            raise RulesError(f"Playlist 'name' must be a non-empty string, got: {name_val!r}")
        name = name_val.strip()
        if len(name) > MAX_PLAYLIST_TITLE or "<" in name or ">" in name:
            raise RulesError(
                f"Playlist name {name!r} must be at most {MAX_PLAYLIST_TITLE} characters "
                "and contain no '<' or '>'"
            )
        if name in rules:
            raise RulesError(f"Duplicate playlist name: {name!r} appears more than once")

        privacy = item.get("privacy")
        if not isinstance(privacy, str) or privacy.strip().lower() not in VALID_PRIVACY:
            raise RulesError(
                f"Playlist '{name}' has invalid 'privacy': {privacy!r}; "
                f"must be one of: {', '.join(VALID_PRIVACY)}"
            )

        match = item.get("match")
        if not isinstance(match, dict):
            raise RulesError(f"Playlist '{name}' has invalid 'match': expected a mapping")
        unknown = set(match) - {"any", "all"}
        if unknown:
            raise RulesError(
                f"Playlist '{name}' has invalid 'match': unknown key(s) {sorted(unknown)}; "
                "use 'any' and/or 'all'"
            )
        if not match:
            raise RulesError(f"Playlist '{name}' has invalid 'match': needs 'any' or 'all'")
        for where in ("any", "all"):
            if where in match:
                conds = match[where]
                if not isinstance(conds, list) or not conds:
                    raise RulesError(
                        f"Playlist '{name}' has invalid 'match.{where}': expected a non-empty list"
                    )
                for j, cond in enumerate(conds):
                    _validate_condition(name, where, j, cond)

        rules[name] = PlaylistRule(name=name, privacy=privacy.strip().lower(), match=match)
    return rules


# Backwards-compatible name (Task 7A).
_build_playlist_rules = load_rules


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def _keyword_regex(keywords: list[str]) -> re.Pattern[str]:
    escaped = [re.escape(kw.strip()) for kw in keywords if kw.strip()]
    return re.compile(r"\b(" + "|".join(escaped) + r")\b", re.IGNORECASE)


def _match_title_keyword(keywords: list[str], video_title: str) -> bool:
    """Case-insensitive whole-word match of any keyword in the title."""
    if not keywords or not any(kw.strip() for kw in keywords):
        return False
    return bool(_keyword_regex(keywords).search(video_title or ""))


def _match_tags_keyword(keywords: list[str], video_tags_json: Optional[str]) -> bool:
    """Case-insensitive whole-word match of any keyword in any tag (tags are a JSON list)."""
    if not keywords or not video_tags_json or not any(kw.strip() for kw in keywords):
        return False
    try:
        tags = json.loads(video_tags_json)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(tags, list):
        return False
    pattern = _keyword_regex(keywords)
    return any(isinstance(t, str) and pattern.search(t) for t in tags)


def _local_date_parts(video: VideoRecord) -> Optional[tuple[int, int, int]]:
    """(year, month, day) from taken_local_date ('YYYY-MM-DD'), or None."""
    try:
        y, m, d = (int(p) for p in (video.taken_local_date or "").split("-"))
    except ValueError:
        return None
    return y, m, d


def _condition_holds(cond: dict[str, Any], video: VideoRecord) -> bool:
    """Every field in the condition must hold."""
    if any(k in cond for k in _INT_RANGES):
        parts = _local_date_parts(video)
        if parts is None:
            return False
        year, month, day = parts
        if "local_year" in cond and cond["local_year"] != year:
            return False
        if "local_month" in cond and cond["local_month"] != month:
            return False
        if "local_day" in cond and cond["local_day"] != day:
            return False
    if "title_contains_any" in cond and not _match_title_keyword(
        cond["title_contains_any"], video.title or ""
    ):
        return False
    if "tags_contain_any" in cond and not _match_tags_keyword(
        cond["tags_contain_any"], video.tags_json
    ):
        return False
    return True


def _format_condition(condition: dict[str, Any]) -> str:
    return ", ".join(f"{k}={condition[k]!r}" for k in sorted(condition)) or "(no constraints)"


def _match_rule(rule: PlaylistRule, video: VideoRecord) -> Optional[str]:
    """Return a description of why the rule matches the video, or None."""
    any_c, all_c = rule.any_conditions, rule.all_conditions
    if not any_c and not all_c:
        return None
    reasons: list[str] = []
    if any_c:
        hit = next((c for c in any_c if _condition_holds(c, video)), None)
        if hit is None:
            return None
        reasons.append(_format_condition(hit))
    if all_c:
        if not all(_condition_holds(c, video) for c in all_c):
            return None
        reasons.extend(_format_condition(c) for c in all_c)
    return "; ".join(reasons)


# ---------------------------------------------------------------------------
# Planning (read-only)
# ---------------------------------------------------------------------------

def _sort_key(video: VideoRecord) -> tuple[str, int]:
    return (video.taken_utc or "", video.id)


def _eligible_videos(conn) -> list[VideoRecord]:
    """UPLOADED/PLAYLISTED rows that have a YouTube id, in date order."""
    rows = conn.execute(
        "SELECT * FROM videos WHERE status IN ('UPLOADED', 'PLAYLISTED') "
        "AND youtube_video_id IS NOT NULL AND youtube_video_id != '' "
        "ORDER BY taken_utc ASC, id ASC"
    ).fetchall()
    return sorted((VideoRecord.from_row(r) for r in rows), key=_sort_key)


def _added_video_ids(conn, playlist_name: str) -> set[int]:
    rows = conn.execute(
        "SELECT video_id FROM playlist_membership WHERE playlist_name = ? AND added = 1",
        (playlist_name,),
    ).fetchall()
    return {r[0] for r in rows}


def _target_position(members: list[VideoRecord], added: set[int], video: VideoRecord) -> int:
    """Number of already-added members that sort before ``video``."""
    key = _sort_key(video)
    return sum(1 for m in members if m.id in added and _sort_key(m) < key)


@dataclass
class PlannedAdd:
    playlist_name: str
    video: VideoRecord
    position: int


@dataclass
class PlaylistPlan:
    rule: PlaylistRule
    youtube_playlist_id: Optional[str]  # None: playlists.insert needed
    members: list[VideoRecord]          # every matching video, date order
    already_added: set[int]
    to_add: list[PlannedAdd]

    @property
    def needs_create(self) -> bool:
        return self.youtube_playlist_id is None


@dataclass
class CurationPlan:
    playlists: list[PlaylistPlan] = field(default_factory=list)
    estimated_cost: int = 0
    note: Optional[str] = None

    @property
    def playlists_to_create(self) -> list[str]:
        return [p.rule.name for p in self.playlists if p.needs_create and p.to_add]

    @property
    def videos_to_add(self) -> list[tuple[str, int, int]]:
        """(playlist name, video id, target position) in execution order."""
        return [(a.playlist_name, a.video.id, a.position) for p in self.playlists for a in p.to_add]

    @property
    def is_empty(self) -> bool:
        return not self.videos_to_add


def build_plan(conn, rules: Optional[dict[str, PlaylistRule]], config: Optional[Config] = None) -> CurationPlan:
    """Work out what a run would do. Only reads from ``conn``.

    Planned positions assume every earlier add in the same run succeeds; the
    real run recomputes each position from what has actually been added.
    A playlist is only created when it has at least one video to add.
    """
    if not rules:
        return CurationPlan(note="No playlists configured in the rules file")

    videos = _eligible_videos(conn)
    plan = CurationPlan()
    for rule in rules.values():
        members = [v for v in videos if _match_rule(rule, v) is not None]
        row = get_playlist(conn, rule.name)
        added = _added_video_ids(conn, rule.name) if row is not None else set()
        simulated = set(added)
        to_add: list[PlannedAdd] = []
        for video in members:
            if video.id in added:
                continue
            to_add.append(PlannedAdd(rule.name, video, _target_position(members, simulated, video)))
            simulated.add(video.id)
        plan.playlists.append(
            PlaylistPlan(
                rule=rule,
                youtube_playlist_id=row["youtube_playlist_id"] if row is not None else None,
                members=members,
                already_added=added,
                to_add=to_add,
            )
        )
    if config is not None:
        plan.estimated_cost = (
            len(plan.playlists_to_create) * config.quota.playlist_insert_cost
            + len(plan.videos_to_add) * config.quota.playlist_item_insert_cost
        )
    return plan


def format_plan(plan: CurationPlan, config: Optional[Config] = None, tracker: Optional[QuotaTracker] = None) -> str:
    lines = ["[DRY RUN] Curation plan"]
    if plan.note:
        lines.append(f"  {plan.note}")
    for p in plan.playlists:
        if p.needs_create:
            state = f"would create ({p.rule.privacy})" if p.to_add else "not created yet, no videos"
        else:
            state = f"exists ({len(p.already_added)} added)"
        lines.append(f"  Playlist '{p.rule.name}': {state}, {len(p.to_add)} video(s) to add")
        for a in p.to_add:
            lines.append(
                f"    position {a.position}: row {a.video.id}  {a.video.taken_local_date or '?'}  "
                f"{a.video.title or ''}"
            )
    if plan.is_empty:
        lines.append("  Nothing to do: every matching video is already in its playlists.")
    else:
        lines.append(
            f"  {len(plan.playlists_to_create)} playlist(s) to create, "
            f"{len(plan.videos_to_add)} video add(s)."
        )
    if config is not None:
        lines.append(
            f"  Estimated quota: {plan.estimated_cost} units "
            f"({config.quota.playlist_insert_cost} per playlist, "
            f"{config.quota.playlist_item_insert_cost} per video add)."
        )
    if tracker is not None and not plan.is_empty:
        if tracker.is_exhausted():
            lines.append("  Today's quota is marked exhausted: a real run would stop at once.")
        else:
            remaining = tracker.remaining()
            lines.append(f"  Remaining quota today (local counter): {remaining} units.")
            if plan.estimated_cost > remaining:
                lines.append("  Not enough for the whole plan: a real run would stop early "
                             "and continue on a later run.")
    return "\n".join(lines)


def curate_plan(config: Optional[Config], rules_file: Optional[Path], conn) -> CurationPlan:
    """Load rules and build the plan on an open connection (read-only)."""
    if rules_file is None or not Path(rules_file).exists():
        return CurationPlan(note=f"No rules file found at {rules_file}")
    return build_plan(conn, load_rules(Path(rules_file)), config=config)


def curate_dry_run(config: Config, rules_file: Optional[Path], conn, out: Printer = print) -> CurationPlan:
    """Build and print the plan. Reads only; never writes to ``conn``."""
    plan = curate_plan(config, rules_file, conn)
    out(format_plan(plan, config))
    return plan


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

class _QuotaStop(Exception):
    """Stop the whole run (local counter says no, or quotaExceeded)."""


@dataclass
class CurationSummary:
    playlists_created: list[str] = field(default_factory=list)
    added: int = 0
    failed: int = 0
    marked_playlisted: int = 0
    api_calls: int = 0
    quota_stopped: bool = False
    errors: list[str] = field(default_factory=list)


class _Runner:
    def __init__(self, conn, config: Config, tracker: QuotaTracker,
                 youtube_factory: Callable[[], Any], out: Printer,
                 sleep: Optional[Callable[[float], None]], max_attempts: int) -> None:
        self.conn = conn
        self.config = config
        self.tracker = tracker
        self._factory = youtube_factory
        self._youtube = None
        self.out = out
        self.sleep = sleep
        self.max_attempts = max_attempts
        self.summary = CurationSummary()

    @property
    def youtube(self):
        if self._youtube is None:
            self._youtube = self._factory()
        return self._youtube

    def _call(self, make_request: Callable[[], Any], cost: int, what: str) -> dict:
        """Execute one API call: quota check, retries, quota record.

        Raises _QuotaStop on a local quota stop or quotaExceeded; HttpError or
        RetryError for other failures.
        """
        if not self.tracker.can_afford(cost):
            raise _QuotaStop(f"not enough quota left today for {what} ({cost} units)")
        kwargs = {"sleep": self.sleep} if self.sleep is not None else {}
        retrying = Retrying(
            retry=retry_if_exception(_is_retryable_playlist_error),
            wait=_retry_wait,
            stop=stop_after_attempt(self.max_attempts),
            reraise=False,
            **kwargs,
        )

        def attempt():
            self.summary.api_calls += 1
            return make_request().execute()

        try:
            response = retrying(attempt)
        except RetryError as exc:
            last = exc.last_attempt.exception()
            if isinstance(last, HttpError):
                self.tracker.record(cost)
            raise
        except HttpError as exc:
            info = parse_http_error(exc)
            if info.reason == "quotaExceeded":
                self.tracker.mark_exhausted()
                raise _QuotaStop(f"YouTube returned quotaExceeded during {what}") from exc
            # The request reached YouTube and was rejected; it may still be charged.
            self.tracker.record(cost)
            raise
        self.tracker.record(cost)
        return response

    @staticmethod
    def _error_text(exc: BaseException) -> str:
        if isinstance(exc, RetryError):
            last = exc.last_attempt.exception()
            inner = _safe_error_text(parse_http_error(last)) if isinstance(last, HttpError) \
                else f"network error: {type(last).__name__}"
            return f"gave up after retries: {inner}"
        if isinstance(exc, HttpError):
            return _safe_error_text(parse_http_error(exc))
        return f"{type(exc).__name__}: {exc}"

    def _fail(self, message: str) -> None:
        self.summary.failed += 1
        self.summary.errors.append(message)
        logger.error(message)
        self.out(f"  FAILED: {message}")

    def ensure_playlist(self, rule: PlaylistRule) -> Optional[str]:
        row = get_playlist(self.conn, rule.name)
        if row is not None:
            return row["youtube_playlist_id"]
        body = {
            "snippet": {"title": rule.name},
            "status": {"privacyStatus": rule.privacy},
        }
        try:
            response = self._call(
                lambda: self.youtube.playlists().insert(part="snippet,status", body=body),
                self.config.quota.playlist_insert_cost,
                f"creating playlist '{rule.name}'",
            )
        except (HttpError, RetryError) as exc:
            self._fail(f"could not create playlist '{rule.name}': {self._error_text(exc)}")
            return None
        playlist_id = response.get("id") if isinstance(response, dict) else None
        if not playlist_id:
            self._fail(f"playlists.insert for '{rule.name}' returned no id")
            return None
        upsert_playlist(self.conn, rule.name, playlist_id)
        self.summary.playlists_created.append(rule.name)
        self.out(f"Created playlist '{rule.name}' ({rule.privacy}).")
        return playlist_id

    def add_members(self, plan: PlaylistPlan, playlist_id: str) -> None:
        name = plan.rule.name
        added = _added_video_ids(self.conn, name)
        for video in plan.members:
            if video.id in added:
                continue
            position = _target_position(plan.members, added, video)
            add_playlist_membership(self.conn, video.id, name, added=False, position=position)
            body = {
                "snippet": {
                    "playlistId": playlist_id,
                    "position": position,
                    "resourceId": {"kind": "youtube#video", "videoId": video.youtube_video_id},
                }
            }
            try:
                self._call(
                    lambda: self.youtube.playlistItems().insert(part="snippet", body=body),
                    self.config.quota.playlist_item_insert_cost,
                    f"adding row {video.id} to '{name}'",
                )
            except (HttpError, RetryError) as exc:
                self._fail(f"could not add row {video.id} to '{name}': {self._error_text(exc)}")
                if isinstance(exc, HttpError) and parse_http_error(exc).reason == "playlistNotFound":
                    self.out(f"  Playlist '{name}' ({playlist_id}) no longer exists on YouTube; "
                             "skipping its remaining adds. Remove its row from the playlists "
                             "table to have it re-created.")
                    return
                continue
            record_playlist_item_added(self.conn, video.id, name, position)
            added.add(video.id)
            self.summary.added += 1
            self.out(f"Added row {video.id} to '{name}' at position {position}.")

    def mark_playlisted(self, rules: dict[str, PlaylistRule]) -> None:
        added_by_playlist = {name: _added_video_ids(self.conn, name) for name in rules}
        for video in _eligible_videos(self.conn):
            if video.status != VideoStatus.UPLOADED:
                continue
            matching = [r.name for r in rules.values() if _match_rule(r, video) is not None]
            if matching and all(video.id in added_by_playlist[n] for n in matching):
                transition_video_status(self.conn, video.id, VideoStatus.PLAYLISTED)
                self.summary.marked_playlisted += 1


def run_curation(
    conn,
    config: Config,
    tracker: QuotaTracker,
    youtube_factory: Callable[[], Any],
    rules: dict[str, PlaylistRule],
    *,
    out: Printer = print,
    sleep: Optional[Callable[[float], None]] = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> CurationSummary:
    """Create playlists, add members in date order, mark PLAYLISTED.

    ``youtube_factory`` is called only when the first API call is needed, so a
    run with nothing to do needs no credentials and makes no calls.
    """
    runner = _Runner(conn, config, tracker, youtube_factory, out, sleep, max_attempts)
    plan = build_plan(conn, rules, config=config)
    try:
        if not plan.is_empty and tracker.is_exhausted():
            raise _QuotaStop("today's quota is already marked exhausted")
        for p in plan.playlists:
            if not p.to_add:
                continue
            playlist_id = runner.ensure_playlist(p.rule)
            if playlist_id is None:
                continue
            runner.add_members(p, playlist_id)
    except _QuotaStop as stop:
        runner.summary.quota_stopped = True
        out(f"Stopping: {stop}. Remaining adds will run after the quota resets "
            f"(in about {tracker.seconds_until_reset() / 3600:.1f} h).")
    runner.mark_playlisted(rules)
    s = runner.summary
    out(f"Curation done: {len(s.playlists_created)} playlist(s) created, {s.added} video(s) added, "
        f"{s.failed} failure(s), {s.marked_playlisted} video(s) now PLAYLISTED.")
    return s
