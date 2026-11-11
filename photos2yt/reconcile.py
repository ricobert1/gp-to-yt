"""Reconcile the local database with YouTube (Task 8).

``photos2yt reconcile`` checks every row that has a YouTube video id and is
UPLOADED or PLAYLISTED, using ``videos.list`` with up to 50 ids per call
(1 unit per call). Never search.list: the local DB stays the source of truth.

For each video it:
  * refreshes ``actual_privacy`` from ``status.privacyStatus``;
  * flags a privacy that differs from ``youtube.privacy`` in config (the
    private-upload lock, AGENT_RULES fact 1) or that changed since last time;
  * re-checks ``recordingDetails.recordingDate`` against ``taken_utc`` (UTC
    date part), because right after upload YouTube sometimes omits it while
    the video is processing;
  * flags an ``uploadStatus`` of deleted/failed/rejected.

A video id that ``videos.list`` does not return is *missing* (deleted or
removed on YouTube). Its row keeps its status (there is no legal transition
out of UPLOADED/PLAYLISTED for this, and the row is the record of what was
uploaded); a note starting with ``[reconcile`` is written to ``last_error``
instead, which ``photos2yt report`` lists under "needs attention". The
recordingDate and upload-status problems are noted the same way. A later
clean check clears the note. YouTube itself is never modified.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from googleapiclient.errors import HttpError
from tenacity import RetryError, Retrying, retry_if_exception, stop_after_attempt

from photos2yt import ECHOED_TO_TERMINAL
from photos2yt.config import Config
from photos2yt.db import (
    RECONCILE_NOTE_PREFIX,
    VideoRecord,
    mark_verified,
    set_reconcile_note,
    update_video_verification,
)
from photos2yt.quota import QuotaTracker
from photos2yt.uploader import (
    DEFAULT_MAX_ATTEMPTS,
    PRIVATE_LOCK_EXPLANATION,
    VERIFY_COST_UNITS,
    _format_recording_date,
    _is_retryable,
    _retry_wait,
    _safe_error_text,
    parse_http_error,
)

logger = logging.getLogger(__name__)

Printer = Callable[[str], None]

BATCH_SIZE = 50  # videos.list accepts at most 50 ids per call
LIST_PARTS = "status,processingDetails,recordingDetails"
LIST_COST_UNITS = VERIFY_COST_UNITS  # videos.list, same call the uploader verifies with
RECONCILED_STATUSES = ("UPLOADED", "PLAYLISTED")
BAD_UPLOAD_STATUSES = frozenset({"deleted", "failed", "rejected"})


def reconcile_candidates(conn) -> list[VideoRecord]:
    """Rows that should exist on YouTube: UPLOADED or PLAYLISTED with a video id."""
    rows = conn.execute(
        f"""
        SELECT * FROM videos
        WHERE status IN ({", ".join("?" for _ in RECONCILED_STATUSES)})
          AND youtube_video_id IS NOT NULL AND youtube_video_id != ''
        ORDER BY id ASC
        """,
        RECONCILED_STATUSES,
    ).fetchall()
    return [VideoRecord.from_row(r) for r in rows]


def batches(videos: list[VideoRecord], size: int = BATCH_SIZE) -> list[list[VideoRecord]]:
    """Split rows into batches of at most ``size`` distinct video ids."""
    out: list[list[VideoRecord]] = []
    current: list[VideoRecord] = []
    ids: set[str] = set()
    for v in videos:
        if v.youtube_video_id not in ids and len(ids) == size:
            out.append(current)
            current, ids = [], set()
        current.append(v)
        ids.add(v.youtube_video_id)
    if current:
        out.append(current)
    return out


def estimated_units(groups: list[list[VideoRecord]]) -> int:
    return len(groups) * LIST_COST_UNITS


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

def format_dry_run(conn, config: Config, tracker: Optional[QuotaTracker] = None) -> str:
    """Describe what a real run would check. Only reads from ``conn``."""
    videos = reconcile_candidates(conn)
    groups = batches(videos)
    lines = ["[DRY RUN] Reconcile plan"]
    if not videos:
        lines.append("  Nothing to reconcile: no UPLOADED or PLAYLISTED rows with a video id.")
        return "\n".join(lines)
    for v in videos:
        lines.append(
            f"  row {v.id}  {v.youtube_video_id}  {v.status.value}  "
            f"privacy={v.actual_privacy or '?'}  {v.taken_local_date or '?'}  {v.title or ''}"
        )
    units = estimated_units(groups)
    lines.append(
        f"  {len(videos)} video(s) to check in {len(groups)} videos.list call(s) "
        f"(up to {BATCH_SIZE} ids each)."
    )
    lines.append(f"  Estimated quota: {units} unit(s) ({LIST_COST_UNITS} per call).")
    if tracker is not None:
        try:
            exhausted = tracker.is_exhausted()
        except (IndexError, KeyError):  # old schema without exhausted_at
            exhausted = False
        if exhausted:
            lines.append("  Today's quota is marked exhausted: a real run would stop at once.")
        else:
            remaining = tracker.remaining()
            lines.append(f"  Remaining quota today (local counter): {remaining} units.")
            if units > remaining:
                lines.append("  Not enough for every call: a real run would stop early.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

class _QuotaStop(Exception):
    """Stop the run (local counter says no, or quotaExceeded)."""


@dataclass
class Attention:
    video: VideoRecord
    problem: str


@dataclass
class ReconcileSummary:
    checked: int = 0
    present: int = 0
    missing: int = 0
    privacy_changed: int = 0
    failed_batches: int = 0
    unchecked: int = 0
    api_calls: int = 0
    quota_stopped: bool = False
    attention: list[Attention] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _recording_date_problem(video: VideoRecord, item: dict) -> Optional[str]:
    expected = _format_recording_date(video.taken_utc)
    if not expected:
        return None
    actual = (item.get("recordingDetails") or {}).get("recordingDate")
    if not actual:
        processing = (item.get("processingDetails") or {}).get("processingStatus")
        if processing and processing != "succeeded":
            return (f"recordingDate missing (video still processing: {processing}; "
                    f"expected {expected[:10]})")
        return f"recordingDate missing (expected {expected[:10]})"
    normalized = _format_recording_date(actual) or actual
    # YouTube may keep only the date part, so compare the UTC date.
    if normalized[:10] != expected[:10]:
        return f"recordingDate differs (expected {expected[:10]}, YouTube has {actual})"
    return None


class _Reconciler:
    def __init__(self, conn, config: Config, tracker: QuotaTracker,
                 youtube_factory: Callable[[], Any], out: Printer,
                 sleep: Optional[Callable[[float], None]], max_attempts: int,
                 today: str, now_iso: str) -> None:
        self.conn = conn
        self.config = config
        self.tracker = tracker
        self._factory = youtube_factory
        self._youtube = None
        self.out = out
        self.sleep = sleep
        self.max_attempts = max_attempts
        self.today = today
        self.now_iso = now_iso
        self.summary = ReconcileSummary()

    @property
    def youtube(self):
        if self._youtube is None:
            self._youtube = self._factory()
        return self._youtube

    def _list(self, ids: list[str]) -> dict:
        """One videos.list call: quota check, retries, quota record."""
        cost = LIST_COST_UNITS
        if not self.tracker.can_afford(cost):
            raise _QuotaStop(f"not enough quota left today for videos.list ({cost} unit)")
        kwargs = {"sleep": self.sleep} if self.sleep is not None else {}
        retrying = Retrying(
            retry=retry_if_exception(_is_retryable),
            wait=_retry_wait,
            stop=stop_after_attempt(self.max_attempts),
            reraise=False,
            **kwargs,
        )

        def attempt():
            self.summary.api_calls += 1
            return self.youtube.videos().list(part=LIST_PARTS, id=",".join(ids)).execute()

        try:
            response = retrying(attempt)
        except RetryError as exc:
            if isinstance(exc.last_attempt.exception(), HttpError):
                self.tracker.record(cost)
            raise
        except HttpError as exc:
            if parse_http_error(exc).reason == "quotaExceeded":
                self.tracker.mark_exhausted()
                raise _QuotaStop("YouTube returned quotaExceeded") from exc
            self.tracker.record(cost)
            raise
        self.tracker.record(cost)
        return response if isinstance(response, dict) else {}

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

    def _flag(self, video: VideoRecord, problem: str) -> None:
        self.summary.attention.append(Attention(video, problem))
        message = f"  ATTENTION row {video.id} ({video.youtube_video_id}): {problem}"
        self.out(message)
        logger.warning(message.strip(), extra={ECHOED_TO_TERMINAL: True})

    def _note(self, problems: list[str]) -> str:
        return f"{RECONCILE_NOTE_PREFIX} {self.today}] " + "; ".join(problems)

    def check_batch(self, group: list[VideoRecord]) -> None:
        ids = list(dict.fromkeys(v.youtube_video_id for v in group))
        try:
            response = self._list(ids)
        except (HttpError, RetryError) as exc:
            message = (f"videos.list failed for {len(ids)} id(s) "
                       f"(rows {group[0].id}..{group[-1].id}): {self._error_text(exc)}")
            self.summary.failed_batches += 1
            self.summary.unchecked += len(group)
            self.summary.errors.append(message)
            logger.error(message, extra={ECHOED_TO_TERMINAL: True})
            self.out(f"  FAILED: {message}")
            return

        items: dict[str, dict] = {}
        for item in response.get("items") or []:
            if isinstance(item, dict) and item.get("id"):
                items[item["id"]] = item

        requested = self.config.youtube.privacy
        for video in group:
            self.summary.checked += 1
            item = items.get(video.youtube_video_id)
            if item is None:
                self.summary.missing += 1
                problem = "missing on YouTube (not returned by videos.list; deleted or removed?)"
                self._flag(video, problem)
                set_reconcile_note(self.conn, video.id, self._note([problem]))
                continue

            self.summary.present += 1
            mark_verified(self.conn, video.id, self.now_iso)
            status = item.get("status") or {}
            actual = status.get("privacyStatus")
            if actual and actual != video.actual_privacy:
                if video.actual_privacy:
                    self.summary.privacy_changed += 1
                    self.out(f"  row {video.id} ({video.youtube_video_id}): privacy changed "
                             f"from '{video.actual_privacy}' to '{actual}'.")
                update_video_verification(self.conn, video.id, actual_privacy=actual)
            if actual and actual != requested:
                self._flag(video, f"privacy is '{actual}', config requests '{requested}' "
                                  "(private-upload lock or changed in Studio?)")

            noted: list[str] = []
            upload_status = status.get("uploadStatus")
            if upload_status in BAD_UPLOAD_STATUSES:
                reason = status.get("rejectionReason") or status.get("failureReason")
                noted.append(f"uploadStatus is '{upload_status}'"
                             + (f" ({reason})" if reason else ""))
            date_problem = _recording_date_problem(video, item)
            if date_problem:
                noted.append(date_problem)
            for problem in noted:
                self._flag(video, problem)
            set_reconcile_note(self.conn, video.id, self._note(noted) if noted else None)


def run_reconcile(
    conn,
    config: Config,
    tracker: QuotaTracker,
    youtube_factory: Callable[[], Any],
    *,
    out: Printer = print,
    sleep: Optional[Callable[[float], None]] = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    clock: Optional[Callable[[], datetime]] = None,
) -> ReconcileSummary:
    """Check every UPLOADED/PLAYLISTED video id with batched videos.list.

    ``youtube_factory`` is called only when the first call is needed, so a
    run with nothing to check needs no credentials.
    """
    now = clock() if clock is not None else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    today = now.astimezone(timezone.utc).date().isoformat()

    videos = reconcile_candidates(conn)
    runner = _Reconciler(conn, config, tracker, youtube_factory, out, sleep, max_attempts, today,
                         now.astimezone(timezone.utc).isoformat())
    s = runner.summary
    if not videos:
        out("Nothing to reconcile: no UPLOADED or PLAYLISTED rows with a video id.")
        return s

    groups = batches(videos)
    out(f"Reconciling {len(videos)} video(s) in {len(groups)} videos.list call(s).")
    done = 0
    try:
        if tracker.is_exhausted():
            raise _QuotaStop("today's quota is already marked exhausted")
        for group in groups:
            runner.check_batch(group)
            done += 1
    except _QuotaStop as stop:
        s.quota_stopped = True
        s.unchecked += sum(len(g) for g in groups[done:])
        out(f"Stopping: {stop}. {s.unchecked} video(s) not checked; run reconcile again "
            f"after the quota resets (in about {tracker.seconds_until_reset() / 3600:.1f} h).")

    out(f"Reconcile done: {s.checked} checked, {s.present} present, {s.missing} missing, "
        f"{s.privacy_changed} privacy change(s), {len(s.attention)} item(s) need attention, "
        f"{s.failed_batches} failed call(s), {s.unchecked} not checked.")
    if any("private-upload lock" in a.problem for a in s.attention):
        out(f"  Note: {PRIVATE_LOCK_EXPLANATION}")
    return s
