"""Upload a single queued video to YouTube (Task 6, part 1: uploader core).

Scope of this module (part 1):
  * build the videos.insert request body from a DB row and config,
  * run a real resumable upload (MediaFileUpload + next_chunk loop),
  * persist the resumable session URI as soon as it exists,
  * retry individual chunks on transient errors with tenacity,
  * classify API errors by the ``reason`` in the HttpError JSON body,
  * drive the row through PENDING -> UPLOADING -> UPLOADED / PENDING / FAILED,
  * record quota only after a successful upload.

Not here yet (part 2): post-upload verification read-back (private lock),
crash recovery / resuming a stored session URI, and the CLI run loop.

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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import httplib2
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from tenacity import (
    RetryError,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from photos2yt.config import Config
from photos2yt.db import (
    VideoRecord,
    VideoStatus,
    transition_video_status,
    update_upload_session,
)
from photos2yt.quota import QuotaTracker

logger = logging.getLogger(__name__)

DEFAULT_CHUNKSIZE = 8 * 1024 * 1024  # 8 MiB; must be a multiple of 256 KiB
DEFAULT_MAX_ATTEMPTS = 5  # per chunk, including the first try
UPLOAD_PARTS = "snippet,status,recordingDetails"

RETRYABLE_STATUS_CODES = frozenset({500, 502, 503, 504})

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
        return int(getattr(exc.resp, "status", 0) or 0) in RETRYABLE_STATUS_CODES
    return isinstance(exc, NETWORK_ERRORS)


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
    chunksize: int = DEFAULT_CHUNKSIZE,
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
      * transient errors (5xx / network) beyond ``max_attempts`` for one chunk:
        row FAILED with last_error, UploadRetriesExhaustedError raised.

    Args:
        conn: SQLite connection (init_db applied).
        config: Loaded Config (youtube.* and quota.upload_cost_units are used).
        tracker: QuotaTracker for this connection.
        youtube: Authorized YouTube API client (googleapiclient resource).
        video: The row to upload; must currently be PENDING in the DB.
        chunksize: Resumable chunk size in bytes (default 8 MiB).
        max_attempts: Attempts per chunk, including the first (>= 1).
        sleep: Sleep function used between retries (injectable for tests).
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

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

    request = youtube.videos().insert(
        part=UPLOAD_PARTS,
        body=build_request_body(video, config),
        media_body=media,
    )

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
        wait=wait_exponential(multiplier=1, min=1, max=60),
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
