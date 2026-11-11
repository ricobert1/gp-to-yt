"""Upload a single queued video to YouTube (Task 6, part 1: uploader core).

Scope of this module (part 1):
  * build the videos.insert request body from a DB row and config,
  * run a real resumable upload (MediaFileUpload + next_chunk loop),
  * persist the resumable session URI as soon as it exists,
  * retry individual chunks on transient errors with tenacity (5xx, network
    errors, and rate limiting: 429 / 403 rateLimitExceeded, honouring
    Retry-After when the server sends it),
  * classify API errors by the ``reason`` in the HttpError JSON body,
  * drive the row through PENDING -> UPLOADING -> UPLOADED / PENDING / FAILED,
  * record quota only after a successful upload.

Part 2 adds: post-upload verification read-back (private lock, recordingDate),
crash recovery for rows stuck in UPLOADING (resume a saved session, or adopt
an already-uploaded video found in the channel's uploads playlist), the
``photos2yt upload`` run loop, and a read-only dry run.

Secrets: this module never logs or stores OAuth tokens. The resumable session
URI is stored in the DB (needed for resume) but is never logged, and
``last_error`` is built from the API's status/reason/message only, never from
the request URI.
"""

from __future__ import annotations

import http.client
import json
import logging
import socket
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Optional

import httplib2
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from tenacity import (
    RetryCallState,
    RetryError,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from photos2yt import ECHOED_TO_TERMINAL
from photos2yt.config import Config
from photos2yt.db import (
    VideoRecord,
    VideoStatus,
    get_readonly_connection,
    get_video_by_youtube_id,
    get_videos_by_status,
    transition_video_status,
    update_upload_session,
    update_video_verification,
)
from photos2yt.quota import QuotaTracker

logger = logging.getLogger(__name__)

# The chunk size default lives in config (youtube.upload_chunk_size).
DEFAULT_MAX_ATTEMPTS = 5  # per chunk, including the first try
UPLOAD_PARTS = "snippet,status,recordingDetails"

RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
# 403s that mean "slow down", not "you may not". quotaExceeded is deliberately
# NOT here: it is the authoritative daily stop signal.
RATE_LIMIT_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})
# Upper bound on a server-requested Retry-After wait, so one bad header cannot
# stall a run indefinitely. Retries remain bounded by max_attempts.
MAX_RETRY_AFTER_SECONDS = 300.0

# Network-level failures worth retrying. Deliberately NOT bare OSError, so a
# missing/unreadable source file (FileNotFoundError, PermissionError) is not
# mistaken for a transient network problem.
NETWORK_ERRORS: tuple[type[BaseException], ...] = (
    ConnectionError,
    TimeoutError,
    socket.gaierror,
    ssl.SSLError,
    http.client.HTTPException,
    httplib2.HttpLib2Error,
)

# Statuses that mean a saved resumable session is expired or unusable.
SESSION_GONE_STATUS_CODES = frozenset({400, 404, 410})

_MAX_ERROR_LEN = 500


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class UploaderError(Exception):
    """Base class for uploader errors."""


class QuotaExhaustedError(UploaderError):
    """API returned quotaExceeded. The tracker is marked exhausted; stop the run."""


class UploadLimitExceededError(UploaderError):
    """Channel-level upload cap hit (uploadLimitExceeded). Not API quota; stop the run."""


class UploadRetriesExhaustedError(UploaderError):
    """A chunk kept failing with transient errors after all retry attempts."""


class SessionGoneError(UploaderError):
    """A saved resumable session can no longer be used (expired / invalid)."""


# ---------------------------------------------------------------------------
# Request body
# ---------------------------------------------------------------------------

def _format_recording_date(taken_utc: Optional[str]) -> Optional[str]:
    """Return taken_utc as 'YYYY-MM-DDTHH:MM:SSZ', or None if absent/unparseable."""
    if not taken_utc:
        return None
    raw = taken_utc.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"  # fromisoformat on 3.10 does not accept 'Z'
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        logger.warning("Unparseable taken_utc %r; omitting recordingDate", taken_utc)
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_tags(tags_json: Optional[str]) -> list[str]:
    if not tags_json:
        return []
    try:
        tags = json.loads(tags_json)
    except (TypeError, ValueError):
        logger.warning("Invalid tags_json; uploading without tags")
        return []
    if not isinstance(tags, list):
        return []
    return [str(t) for t in tags if str(t).strip()]


def _has_location(lat: Optional[float], lon: Optional[float]) -> bool:
    if lat is None or lon is None:
        return False
    try:
        return float(lat) != 0.0 and float(lon) != 0.0
    except (TypeError, ValueError):
        return False


def build_request_body(video: VideoRecord, config: Config) -> dict[str, Any]:
    """Build the videos.insert body for one DB row (part=snippet,status,recordingDetails)."""
    yt = config.youtube
    body: dict[str, Any] = {
        "snippet": {
            "title": video.title or yt.default_title,
            "description": video.description or "",
            "tags": _parse_tags(video.tags_json),
            "categoryId": str(yt.category_id),
        },
        "status": {
            "privacyStatus": yt.privacy,
            "selfDeclaredMadeForKids": bool(yt.made_for_kids),
        },
        "recordingDetails": {},
    }

    recording_date = _format_recording_date(video.taken_utc)
    if recording_date is not None:
        body["recordingDetails"]["recordingDate"] = recording_date

    if _has_location(video.lat, video.lon):
        body["recordingDetails"]["location"] = {
            "latitude": float(video.lat),  # type: ignore[arg-type]
            "longitude": float(video.lon),  # type: ignore[arg-type]
        }

    return body


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ApiErrorInfo:
    status: int
    reason: Optional[str]
    message: Optional[str]


def parse_http_error(err: HttpError) -> ApiErrorInfo:
    """Extract status code, first error reason and message from an HttpError's JSON body."""
    status = int(getattr(err.resp, "status", 0) or 0)
    reason: Optional[str] = None
    message: Optional[str] = None
    content = err.content
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    try:
        payload = json.loads(content) if content else {}
    except (TypeError, ValueError):
        payload = {}
    error_obj = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error_obj, dict):
        message = error_obj.get("message")
        errors = error_obj.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            reason = errors[0].get("reason")
    return ApiErrorInfo(status=status, reason=reason, message=message)


def _safe_error_text(info: ApiErrorInfo) -> str:
    """Error text for last_error: status, reason, API message. No URIs, no tokens."""
    parts = [f"HTTP {info.status}"]
    if info.reason:
        parts.append(info.reason)
    text = " ".join(parts)
    if info.message:
        text += f": {info.message}"
    return text[:_MAX_ERROR_LEN]


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, HttpError):
        info = parse_http_error(exc)
        if info.status in RETRYABLE_STATUS_CODES:
            return True
        return info.status == 403 and info.reason in RATE_LIMIT_REASONS
    return isinstance(exc, NETWORK_ERRORS)


def _retry_after_seconds(exc: Optional[BaseException]) -> Optional[float]:
    """Seconds requested by a Retry-After header (delta-seconds or HTTP-date), if any."""
    if not isinstance(exc, HttpError) or exc.resp is None:
        return None
    raw = exc.resp.get("retry-after")
    if raw is None:
        return None
    raw = str(raw).strip()
    try:
        seconds = float(raw)
    except ValueError:
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - datetime.now(timezone.utc)).total_seconds()
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_SECONDS)


_exponential_wait = wait_exponential(multiplier=1, min=1, max=60)


def _retry_wait(retry_state: RetryCallState) -> float:
    """Use the server's Retry-After when given, otherwise exponential backoff."""
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    requested = _retry_after_seconds(exc)
    if requested is not None:
        return requested
    return _exponential_wait(retry_state)


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

def upload_one(
    conn,
    config: Config,
    tracker: QuotaTracker,
    youtube,
    video: VideoRecord,
    *,
    chunksize: Optional[int] = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> VideoRecord:
    """Upload one PENDING video and return its updated DB record.

    Outcomes:
      * success: row UPLOADED with youtube_video_id; quota recorded once.
      * quotaExceeded: tracker marked exhausted, row back to PENDING,
        QuotaExhaustedError raised.
      * uploadLimitExceeded: row back to PENDING, UploadLimitExceededError
        raised (API quota is NOT marked exhausted).
      * any other 4xx (or no video id in the response): row FAILED with
        last_error; the FAILED record is returned (the run may continue).
      * transient errors (5xx / 429 / 403 rate limit / network) beyond
        ``max_attempts`` for one chunk:
        row FAILED with last_error, UploadRetriesExhaustedError raised.

    Args:
        conn: SQLite connection (init_db applied).
        config: Loaded Config (youtube.* and quota.upload_cost_units are used).
        tracker: QuotaTracker for this connection.
        youtube: Authorized YouTube API client (googleapiclient resource).
        video: The row to upload; must currently be PENDING in the DB.
        chunksize: Resumable chunk size in bytes; defaults to
            config.youtube.upload_chunk_size.
        max_attempts: Attempts per chunk, including the first (>= 1).
        sleep: Sleep function used between retries (injectable for tests).
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    if chunksize is None:
        chunksize = config.youtube.upload_chunk_size

    # PENDING -> UPLOADING before any request is made.
    video = transition_video_status(
        conn, video.id, VideoStatus.UPLOADING, increment_attempts=True
    )

    try:
        media = MediaFileUpload(video.source_path, chunksize=chunksize, resumable=True)
    except OSError as exc:
        error = f"Cannot open source file: {type(exc).__name__}: {exc.strerror or exc}"
        logger.error("Video %s: %s", video.id, error)
        return transition_video_status(
            conn, video.id, VideoStatus.FAILED, error=error[:_MAX_ERROR_LEN]
        )

    return _run_upload(
        conn, config, tracker, youtube, video, media,
        max_attempts=max_attempts, sleep=sleep,
    )


def _run_upload(
    conn,
    config: Config,
    tracker: QuotaTracker,
    youtube,
    video: VideoRecord,
    media: MediaFileUpload,
    *,
    max_attempts: int,
    sleep: Callable[[float], None],
    resume_uri: Optional[str] = None,
) -> VideoRecord:
    """Drive a resumable upload for a row that is already UPLOADING.

    Shared by upload_one (fresh session) and crash recovery (``resume_uri``
    set: the saved session is queried for its received byte range first and
    the upload continues from there; if it already completed, the server
    returns the finished video).

    Raises SessionGoneError (resume only) when the saved session is no
    longer valid; the row is left UPLOADING for the caller to handle.
    """
    request = youtube.videos().insert(
        part=UPLOAD_PARTS,
        body=build_request_body(video, config),
        media_body=media,
    )
    if resume_uri is not None:
        _prime_resume(request, resume_uri)

    persisted_uri: Optional[str] = video.upload_session_uri

    def _persist_session_uri() -> None:
        nonlocal persisted_uri
        uri = getattr(request, "resumable_uri", None)
        if uri and uri != persisted_uri:
            update_upload_session(conn, video.id, uri)
            persisted_uri = uri
            logger.info("Video %s: resumable session started and saved", video.id)

    def _next_chunk():
        # The session URI can appear even when the chunk PUT after the
        # initiating POST fails, so persist it whether or not this raises.
        try:
            return request.next_chunk()
        finally:
            _persist_session_uri()

    retrying = Retrying(
        retry=retry_if_exception(_is_retryable),
        wait=_retry_wait,
        stop=stop_after_attempt(max_attempts),
        sleep=sleep,
        reraise=False,
        before_sleep=lambda rs: logger.warning(
            "Video %s: transient error (%s), retrying chunk (attempt %d/%d)",
            video.id,
            type(rs.outcome.exception()).__name__,
            rs.attempt_number,
            max_attempts,
        ),
    )

    response = None
    try:
        while response is None:
            # Retry the chunk, not the whole file: each next_chunk() call is
            # wrapped individually; googleapiclient resumes from the server's
            # recorded offset after a failed chunk.
            status, response = retrying(_next_chunk)
            if status is not None:
                logger.info(
                    "Video %s: uploaded %d%%", video.id, int(status.progress() * 100)
                )
    except RetryError as exc:
        last = exc.last_attempt.exception()
        if isinstance(last, HttpError):
            error = _safe_error_text(parse_http_error(last))
        else:
            error = f"Network error: {type(last).__name__}"
        error = f"Gave up after {max_attempts} attempts: {error}"[:_MAX_ERROR_LEN]
        logger.error("Video %s: %s", video.id, error)
        transition_video_status(conn, video.id, VideoStatus.FAILED, error=error)
        raise UploadRetriesExhaustedError(error) from last
    except HttpError as exc:
        if resume_uri is not None:
            info = parse_http_error(exc)
            if info.status in SESSION_GONE_STATUS_CODES and info.reason not in (
                "quotaExceeded",
                "uploadLimitExceeded",
            ):
                raise SessionGoneError(_safe_error_text(info)) from exc
        return _handle_http_error(conn, tracker, video, exc)

    video_id = response.get("id") if isinstance(response, dict) else None
    if not video_id:
        error = "Upload finished but response contained no video id"
        logger.error("Video %s: %s", video.id, error)
        return transition_video_status(conn, video.id, VideoStatus.FAILED, error=error)

    updated = transition_video_status(
        conn,
        video.id,
        VideoStatus.UPLOADED,
        youtube_video_id=str(video_id),
        clear_session_uri=True,
    )
    # Quota only after a confirmed successful upload.
    tracker.record(config.quota.upload_cost_units, uploads=1)
    logger.info("Video %s: uploaded as YouTube id %s", video.id, video_id)
    return updated


def _prime_resume(request, resume_uri: str) -> None:
    """Point a fresh insert request at a saved session and make the next
    next_chunk() first ask the server how many bytes it already has.

    googleapiclient has no public "resume this URI" API. Setting the public
    ``resumable_uri`` attribute skips session creation; the private
    ``_in_error_state`` flag makes next_chunk() send the protocol's status
    query (PUT, Content-Range: bytes */size) instead of blindly re-sending
    from byte 0. If the library ever drops that attribute we refuse to
    resume (SessionGoneError) rather than risk a corrupt upload.
    """
    if not hasattr(request, "_in_error_state"):
        raise SessionGoneError("client library does not support resuming a saved session")
    request.resumable_uri = resume_uri
    request._in_error_state = True


def _handle_http_error(
    conn, tracker: QuotaTracker, video: VideoRecord, exc: HttpError
) -> VideoRecord:
    """Classify a non-retryable HttpError by its JSON reason and update the row."""
    info = parse_http_error(exc)
    error = _safe_error_text(info)

    if info.reason == "quotaExceeded":
        tracker.mark_exhausted()
        transition_video_status(conn, video.id, VideoStatus.PENDING, error=error)
        logger.error("Video %s: API quota exhausted; stopping run", video.id)
        raise QuotaExhaustedError(error) from exc

    if info.reason == "uploadLimitExceeded":
        transition_video_status(conn, video.id, VideoStatus.PENDING, error=error)
        logger.error("Video %s: channel upload limit reached; stopping run", video.id)
        raise UploadLimitExceededError(error) from exc

    logger.error("Video %s: upload failed: %s", video.id, error)
    return transition_video_status(conn, video.id, VideoStatus.FAILED, error=error)


# ===========================================================================
# Part 2: verification, crash recovery, run loop, dry run
# ===========================================================================

VERIFY_PARTS = "status,processingDetails,recordingDetails"
VERIFY_COST_UNITS = 1  # videos.list
LIST_COST_UNITS = 1  # channels.list / playlistItems.list, per page
MAX_UNVERIFIED_DURATION_S = 15 * 60  # platform rule: > 15 min needs a verified channel
# How many pages (50 items each) of the uploads playlist to scan, newest first,
# when looking for a video that a crashed run may already have uploaded.
DEFAULT_ADOPTION_MAX_PAGES = 4

PRIVATE_LOCK_EXPLANATION = (
    "Unverified API projects created after 28 July 2020 have all uploads forced to "
    "private; pass the YouTube API compliance audit, or change the privacy manually "
    "in YouTube Studio."
)

Printer = Callable[[str], None]


@dataclass
class VerificationResult:
    actual_privacy: Optional[str] = None
    privacy_mismatch: bool = False
    recording_date_problem: Optional[str] = None
    # recordingDate absent while the video is still processing (not a problem).
    recording_date_pending: bool = False
    error: Optional[str] = None


def _warn(out: Printer, message: str) -> None:
    """Print a warning once and log it; the record is marked as already printed
    so the CLI's stderr log handler does not show it a second time."""
    out(message)
    logger.warning(message, extra={ECHOED_TO_TERMINAL: True})


def verify_upload(
    conn,
    config: Config,
    tracker: QuotaTracker,
    youtube,
    video: VideoRecord,
    *,
    out: Printer = print,
) -> VerificationResult:
    """Read back an uploaded video and store its actual privacy.

    Never raises and never changes the row's status: a verification problem
    must not undo or fail an upload that already succeeded.
    """
    result = VerificationResult()
    try:
        response = youtube.videos().list(part=VERIFY_PARTS, id=video.youtube_video_id).execute()
        tracker.record(VERIFY_COST_UNITS)
    except HttpError as exc:
        result.error = _safe_error_text(parse_http_error(exc))
    except Exception as exc:  # network etc.; verification is best effort
        result.error = f"{type(exc).__name__}"
    if result.error:
        _warn(out, f"WARNING: could not verify video {video.youtube_video_id} (row {video.id}): "
                   f"{result.error}. The upload itself succeeded.")
        return result

    items = response.get("items") if isinstance(response, dict) else None
    if not items:
        result.error = "video not returned by videos.list"
        _warn(out, f"WARNING: video {video.youtube_video_id} (row {video.id}) was not returned "
                   "by videos.list; it may still be processing. The upload itself succeeded.")
        return result

    item = items[0]
    requested = config.youtube.privacy
    actual = (item.get("status") or {}).get("privacyStatus")
    result.actual_privacy = actual
    try:
        update_video_verification(conn, video.id, actual_privacy=actual)
    except Exception as exc:
        result.error = f"could not store actual_privacy: {type(exc).__name__}"
        logger.warning("Video %s: %s", video.id, result.error)

    if actual != requested:
        result.privacy_mismatch = True
        bar = "!" * 78
        _warn(out, "\n".join([
            bar,
            "!!! PRIVATE-UPLOAD LOCK: privacy does not match what was requested",
            f"!!! video {video.youtube_video_id} (row {video.id}): requested "
            f"'{requested}', YouTube reports '{actual}'.",
            "!!! This project is probably subject to the private-upload lock.",
            f"!!! {PRIVATE_LOCK_EXPLANATION}",
            bar,
        ]))

    processing = (item.get("processingDetails") or {}).get("processingStatus")
    expected_date = _format_recording_date(video.taken_utc)
    actual_date = (item.get("recordingDetails") or {}).get("recordingDate")
    if expected_date:
        if not actual_date and processing != "succeeded":
            # Right after upload, while YouTube is still processing the video,
            # recordingDetails can be absent even though the date was stored
            # (seen on a real upload, 2026-11-11). Not a failure; no extra
            # videos.list call is spent re-checking.
            result.recording_date_pending = True
            message = (f"recordingDate for video {video.youtube_video_id} (row {video.id}) "
                       f"not visible yet (video still processing: "
                       f"{processing or 'status unknown'}); check it later in YouTube Studio.")
            out(message)
            logger.info(message, extra={ECHOED_TO_TERMINAL: True})
        elif not actual_date:
            result.recording_date_problem = "missing"
        else:
            normalized = _format_recording_date(actual_date) or actual_date
            # YouTube may keep only the date part, so compare the UTC date.
            if normalized[:10] != expected_date[:10]:
                result.recording_date_problem = f"differs: expected {expected_date}, got {actual_date}"
        if result.recording_date_problem:
            _warn(out, f"WARNING: recordingDate for video {video.youtube_video_id} (row {video.id}) "
                       f"did not stick ({result.recording_date_problem}).")

    if processing:
        logger.info("Video %s: processingStatus=%s", video.id, processing)
    return result


# ---------------------------------------------------------------------------
# Crash recovery
# ---------------------------------------------------------------------------

@dataclass
class RecoverySummary:
    resumed: int = 0
    adopted: int = 0
    requeued: int = 0
    failed: int = 0
    deferred: int = 0


def _uploads_playlist_id(youtube, tracker: QuotaTracker) -> Optional[str]:
    response = youtube.channels().list(part="contentDetails", mine=True).execute()
    tracker.record(LIST_COST_UNITS)
    items = response.get("items") or []
    if not items:
        return None
    return ((items[0].get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")


def find_existing_upload(
    conn,
    config: Config,
    tracker: QuotaTracker,
    youtube,
    video: VideoRecord,
    *,
    max_pages: int = DEFAULT_ADOPTION_MAX_PAGES,
) -> tuple[str, Optional[str]]:
    """Look in the channel's uploads playlist for a video this row already produced.

    Uses channels.list + playlistItems.list only (never search.list). Matches
    on the exact title that would be sent, ignoring videos already in the DB.

    Returns (outcome, video_id) where outcome is one of:
      "found" (exactly one match), "none", "ambiguous" (several matches),
      "unavailable" (could not check: quota or API error).
    """
    title = build_request_body(video, config)["snippet"]["title"]
    try:
        if not tracker.can_afford(LIST_COST_UNITS):
            return ("unavailable", None)
        playlist_id = _uploads_playlist_id(youtube, tracker)
        if not playlist_id:
            return ("none", None)
        matches: list[str] = []
        page_token: Optional[str] = None
        for _ in range(max_pages):
            if not tracker.can_afford(LIST_COST_UNITS):
                return ("unavailable", None)
            kwargs: dict[str, Any] = dict(part="snippet", playlistId=playlist_id, maxResults=50)
            if page_token:
                kwargs["pageToken"] = page_token
            response = youtube.playlistItems().list(**kwargs).execute()
            tracker.record(LIST_COST_UNITS)
            for item in response.get("items") or []:
                snippet = item.get("snippet") or {}
                vid = (snippet.get("resourceId") or {}).get("videoId")
                if vid and snippet.get("title") == title and get_video_by_youtube_id(conn, vid) is None:
                    if vid not in matches:
                        matches.append(vid)
            page_token = response.get("nextPageToken")
            if not page_token:
                break
    except HttpError as exc:
        logger.warning("Video %s: uploads playlist check failed: %s",
                       video.id, _safe_error_text(parse_http_error(exc)))
        return ("unavailable", None)
    except Exception as exc:
        logger.warning("Video %s: uploads playlist check failed: %s", video.id, type(exc).__name__)
        return ("unavailable", None)

    if len(matches) == 1:
        return ("found", matches[0])
    if len(matches) > 1:
        return ("ambiguous", None)
    return ("none", None)


def recover_stuck_uploads(
    conn,
    config: Config,
    tracker: QuotaTracker,
    youtube,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
    out: Printer = print,
    adoption_max_pages: int = DEFAULT_ADOPTION_MAX_PAGES,
) -> RecoverySummary:
    """Handle rows left in UPLOADING by a crashed or killed run.

    For each such row:
      1. If a session URI was saved, try to resume it (best effort). A session
         that had actually finished returns the video, so no duplicate.
      2. If there is no session, or it is gone (404/410/400), look for the
         video in the channel's uploads playlist. Exactly one title match not
         already in the DB is adopted (UPLOADED + verification).
      3. Otherwise the row goes back to PENDING with the session cleared, to
         be uploaded from scratch. If several videos match, the row is marked
         FAILED for manual review instead of risking a duplicate. If the check
         could not run (quota / API error), the row stays UPLOADING and is
         retried next run.

    QuotaExhaustedError / UploadLimitExceededError propagate (stop the run).
    """
    summary = RecoverySummary()
    stuck = get_videos_by_status(conn, VideoStatus.UPLOADING)
    if stuck and tracker.is_exhausted():
        summary.deferred = len(stuck)
        out(f"{len(stuck)} interrupted upload(s) left for next run: today's quota is exhausted.")
        return summary
    for video in stuck:
        out(f"Recovering interrupted upload: row {video.id} ({video.title or video.source_path})")

        if video.upload_session_uri:
            try:
                media = MediaFileUpload(
                    video.source_path, chunksize=config.youtube.upload_chunk_size, resumable=True
                )
            except OSError as exc:
                error = f"Cannot open source file: {type(exc).__name__}: {exc.strerror or exc}"
                transition_video_status(conn, video.id, VideoStatus.FAILED, error=error[:_MAX_ERROR_LEN],
                                        clear_session_uri=True)
                summary.failed += 1
                continue
            try:
                result = _run_upload(
                    conn, config, tracker, youtube, video, media,
                    max_attempts=max_attempts, sleep=sleep,
                    resume_uri=video.upload_session_uri,
                )
            except SessionGoneError as exc:
                logger.info("Video %s: saved session unusable (%s)", video.id, exc)
            except UploadRetriesExhaustedError:
                summary.failed += 1
                continue
            else:
                if result.status == VideoStatus.UPLOADED:
                    summary.resumed += 1
                    out(f"  resumed and finished: YouTube id {result.youtube_video_id}")
                    verify_upload(conn, config, tracker, youtube, result, out=out)
                else:
                    summary.failed += 1
                continue

        # No usable session: before re-uploading, make sure it is not already there.
        outcome, found_id = find_existing_upload(
            conn, config, tracker, youtube, video, max_pages=adoption_max_pages
        )
        if outcome == "found":
            adopted = transition_video_status(
                conn, video.id, VideoStatus.UPLOADED,
                youtube_video_id=found_id, clear_session_uri=True,
            )
            summary.adopted += 1
            out(f"  already on YouTube as {found_id}; adopted instead of re-uploading")
            verify_upload(conn, config, tracker, youtube, adopted, out=out)
        elif outcome == "ambiguous":
            transition_video_status(
                conn, video.id, VideoStatus.FAILED, clear_session_uri=True,
                error="Recovery: several uploaded videos match this title; "
                      "resolve manually to avoid a duplicate",
            )
            summary.failed += 1
            _warn(out, f"WARNING: row {video.id}: several uploaded videos match its title; "
                       "marked FAILED for manual review.")
        elif outcome == "unavailable":
            summary.deferred += 1
            _warn(out, f"WARNING: row {video.id}: could not check for an existing upload; "
                       "leaving it UPLOADING to retry recovery next run.")
        else:
            transition_video_status(conn, video.id, VideoStatus.PENDING, clear_session_uri=True)
            summary.requeued += 1
            out("  not found on YouTube; re-queued as PENDING")
    return summary


# ---------------------------------------------------------------------------
# Run loop
# ---------------------------------------------------------------------------

@dataclass
class RunSummary:
    uploaded: int = 0
    failed: int = 0
    skipped: int = 0
    privacy_mismatches: int = 0
    stopped_reason: Optional[str] = None
    recovery: RecoverySummary = field(default_factory=RecoverySummary)


def _too_long(video: VideoRecord, config: Config) -> bool:
    return (
        not config.youtube.channel_verified
        and video.duration_s is not None
        and video.duration_s > MAX_UNVERIFIED_DURATION_S
    )


def run_uploads(
    conn,
    config: Config,
    tracker: QuotaTracker,
    youtube,
    *,
    limit: Optional[int] = None,
    wait: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    out: Printer = print,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    retry_sleep: Callable[[float], None] = time.sleep,
) -> RunSummary:
    """Recover interrupted uploads, then upload PENDING rows in upload order
    (youtube.upload_order: largest file first, or (taken_utc, id)).

    ``sleep`` is used only for --wait (sleeping until the quota reset);
    ``retry_sleep`` is the backoff sleep between chunk retries.
    """
    summary = RunSummary()
    cost = config.quota.upload_cost_units

    try:
        summary.recovery = recover_stuck_uploads(
            conn, config, tracker, youtube, max_attempts=max_attempts, sleep=retry_sleep, out=out
        )
    except QuotaExhaustedError:
        summary.stopped_reason = "quota exhausted (quotaExceeded from YouTube)"
    except UploadLimitExceededError:
        summary.stopped_reason = "channel upload limit reached (uploadLimitExceeded)"
    if summary.stopped_reason:
        out(f"Stopping: {summary.stopped_reason}.")
        return summary

    attempted = 0
    for video in _pending_in_upload_order(conn, config):
        if limit is not None and attempted >= limit:
            break

        if _too_long(video, config):
            minutes = video.duration_s / 60  # type: ignore[operator]
            transition_video_status(
                conn, video.id, VideoStatus.SKIPPED,
                error=f"Too long for an unverified channel ({minutes:.1f} min > 15 min); "
                      "set youtube.channel_verified: true once the channel is verified",
            )
            summary.skipped += 1
            out(f"Skipped row {video.id} ({video.title}): {minutes:.1f} min exceeds 15 min")
            continue

        if not tracker.can_afford(cost, uploads=1):
            if not wait:
                summary.stopped_reason = "local quota budget for today is used up"
                break
            delay = tracker.seconds_until_reset()
            out(f"Quota budget used up; waiting {delay:.0f}s until the Pacific-midnight reset.")
            sleep(delay)
            if not tracker.can_afford(cost, uploads=1):
                summary.stopped_reason = "quota still unavailable after waiting for reset"
                break

        attempted += 1
        out(f"Uploading row {video.id}: {video.title or video.source_path}")
        try:
            result = upload_one(
                conn, config, tracker, youtube, video, max_attempts=max_attempts, sleep=retry_sleep
            )
        except QuotaExhaustedError:
            summary.stopped_reason = "quota exhausted (quotaExceeded from YouTube)"
            break
        except UploadLimitExceededError:
            summary.stopped_reason = "channel upload limit reached (uploadLimitExceeded)"
            break
        except UploadRetriesExhaustedError as exc:
            summary.failed += 1
            out(f"  FAILED: {exc}")
            continue
        except Exception as exc:  # one bad video must not abort the run
            summary.failed += 1
            text = _safe_error_text(parse_http_error(exc)) if isinstance(exc, HttpError) \
                else type(exc).__name__
            logger.exception("Video %s: unexpected error during upload", video.id)
            out(f"  FAILED (unexpected {text}); row will be recovered next run if left UPLOADING")
            continue

        if result.status == VideoStatus.UPLOADED:
            summary.uploaded += 1
            out(f"  uploaded: https://youtu.be/{result.youtube_video_id}")
            check = verify_upload(conn, config, tracker, youtube, result, out=out)
            if check.privacy_mismatch:
                summary.privacy_mismatches += 1
        else:
            summary.failed += 1
            out(f"  FAILED: {result.last_error}")

    if summary.stopped_reason:
        out(f"Stopping: {summary.stopped_reason}.")
    out(f"Done: {summary.uploaded} uploaded, {summary.failed} failed, {summary.skipped} skipped.")
    return summary


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

@dataclass
class DryRunPlan:
    to_upload: list[VideoRecord] = field(default_factory=list)
    to_skip: list[VideoRecord] = field(default_factory=list)
    interrupted: int = 0
    estimated_units: int = 0
    database_missing: bool = False


def _pending_in_upload_order(conn, config: Config) -> list[VideoRecord]:
    """PENDING rows in the order uploads (and the dry run) take them."""
    largest = getattr(config.youtube, "upload_order", "largest_first") == "largest_first"
    return get_videos_by_status(conn, VideoStatus.PENDING, largest_first=largest)


def dry_run(config: Config, *, limit: Optional[int] = None, out: Printer = print) -> DryRunPlan:
    """Print what an upload run would do. Touches neither the API nor the database
    (opens it read-only; never creates a missing database file)."""
    plan = DryRunPlan()
    db_path = config.paths.db_path
    try:
        conn = get_readonly_connection(db_path)
    except FileNotFoundError:
        plan.database_missing = True
        out(f"[DRY RUN] No database at {db_path}; nothing to upload (run 'photos2yt ingest' first).")
        return plan

    try:
        plan.interrupted = len(get_videos_by_status(conn, VideoStatus.UPLOADING))
        for video in _pending_in_upload_order(conn, config):
            if _too_long(video, config):
                plan.to_skip.append(video)
                continue
            if limit is not None and len(plan.to_upload) >= limit:
                continue
            plan.to_upload.append(video)
    finally:
        conn.close()

    per_video = config.quota.upload_cost_units + VERIFY_COST_UNITS
    plan.estimated_units = per_video * len(plan.to_upload)

    order = getattr(config.youtube, "upload_order", "largest_first").replace("_", " ")
    out(f"[DRY RUN] Would upload {len(plan.to_upload)} video(s), requested privacy "
        f"'{config.youtube.privacy}', order: {order}.")
    for video in plan.to_upload:
        title = build_request_body(video, config)["snippet"]["title"]
        out(f"  - row {video.id} ({video.size_bytes / 1_000_000:.1f} MB): {title}")
    for video in plan.to_skip:
        out(f"  - would SKIP row {video.id} ({video.title}): longer than 15 min, channel not verified")
    if plan.interrupted:
        out(f"  {plan.interrupted} interrupted upload(s) would be recovered first "
            "(may use a few extra units).")
    out(f"  Estimated quota: {plan.estimated_units} units "
        f"({config.quota.upload_cost_units} per upload + {VERIFY_COST_UNITS} per verification); "
        f"daily limit {config.quota.daily_unit_limit}.")
    return plan
