"""Tests for photos2yt.uploader (Task 6, part 1).

No real API calls and no real uploads: the YouTube client is a mock whose
videos().insert() returns a FakeRequest that scripts next_chunk() results.
Errors are real googleapiclient.errors.HttpError objects with JSON bodies.
The DB is real (in-memory SQLite) and the QuotaTracker is real with a frozen
clock, so assertions are on actual rows and quota_log values.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import httplib2
import pytest
from googleapiclient.errors import HttpError, ResumableUploadError
from googleapiclient.http import MediaFileUpload

from photos2yt.config import Config, PathsConfig, QuotaConfig, YouTubeConfig
from photos2yt.db import (
    InvalidStateTransitionError,
    VideoStatus,
    get_connection,
    get_quota_log,
    get_video,
    init_db,
    insert_video,
    transition_video_status,
    update_upload_session,
)
from photos2yt.quota import QuotaTracker
from photos2yt.uploader import (
    MAX_UNVERIFIED_DURATION_S,
    dry_run,
    recover_stuck_uploads,
    run_uploads,
    verify_upload,
    QuotaExhaustedError,
    UploadLimitExceededError,
    UploadRetriesExhaustedError,
    build_request_body,
    upload_one,
)

SESSION_URI = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&upload_id=SECRET-SESSION-123"
FROZEN_NOW = datetime(2026, 6, 15, 18, 0, 0, tzinfo=timezone.utc)
PACIFIC_DAY = "2026-06-15"
UPLOAD_COST = 1600


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_http_error(
    status: int,
    reason: str | None,
    message: str = "error",
    *,
    retry_after: str | None = None,
    cls: type[HttpError] = HttpError,
) -> HttpError:
    errors = [{"reason": reason, "domain": "youtube", "message": message}] if reason else []
    body = {"error": {"code": status, "message": message, "errors": errors}}
    headers = {"status": str(status)}
    if retry_after is not None:
        headers["retry-after"] = retry_after
    return cls(
        httplib2.Response(headers),
        json.dumps(body).encode("utf-8"),
        uri=SESSION_URI,
    )


class FakeStatus:
    def __init__(self, progress: float) -> None:
        self._p = progress

    def progress(self) -> float:
        return self._p


class FakeRequest:
    """Stands in for googleapiclient's HttpRequest during a resumable upload.

    ``script`` items are either an exception to raise or a (status, response)
    tuple to return. Like the real client, resumable_uri becomes available
    during the first next_chunk() call (the initiating POST), before the
    chunk itself is sent, so it is set even if that first call then fails.
    """

    def __init__(self, script, *, uri: str | None = SESSION_URI, on_call=None) -> None:
        self.script = list(script)
        self.calls = 0
        self.resumable_uri = None
        self._uri = uri
        self._on_call = on_call

    def next_chunk(self):
        self.calls += 1
        if self._on_call:
            self._on_call(self.calls)
        if self.calls == 1 and self._uri is not None:
            self.resumable_uri = self._uri
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def make_youtube(request: FakeRequest) -> MagicMock:
    youtube = MagicMock()
    youtube.videos.return_value.insert.return_value = request
    return youtube


def insert_kwargs(youtube: MagicMock) -> dict:
    return youtube.videos.return_value.insert.call_args.kwargs


SUCCESS = (None, {"id": "yt-abc123"})


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        paths=PathsConfig(
            staging_dir=tmp_path,
            db_path=tmp_path / "queue.db",
            credentials=tmp_path / "credentials.json",
            token=tmp_path / "token.json",
        ),
        timezone="America/New_York",
        youtube=YouTubeConfig(
            privacy="unlisted",
            category_id="22",
            made_for_kids=False,
            title_template="{date} - {title}",
            default_title="Family Video",
        ),
        quota=QuotaConfig(
            daily_unit_limit=10_000,
            upload_cost_units=UPLOAD_COST,
            playlist_insert_cost=50,
            playlist_item_insert_cost=50,
            daily_upload_limit=None,
        ),
        video_extensions=(".mp4",),
    )


@pytest.fixture
def conn():
    c = get_connection(":memory:")
    init_db(c)
    yield c
    c.close()


@pytest.fixture
def tracker(conn, config) -> QuotaTracker:
    return QuotaTracker(conn, config, clock=lambda: FROZEN_NOW)


@pytest.fixture
def video_file(tmp_path: Path) -> Path:
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"\x00" * 1024)
    return p


@pytest.fixture
def make_video(conn, video_file):
    counter = {"n": 0}

    def _make(**overrides):
        counter["n"] += 1
        # source_path is UNIQUE in the schema: one file per row.
        path = video_file if counter["n"] == 1 else video_file.with_name(f"clip{counter['n']}.mp4")
        if not path.exists():
            path.write_bytes(b"\x00" * 1024)
        fields = dict(
            source_path=str(path),
            sha256=f"sha-{counter['n']}",
            size_bytes=1024,
            taken_utc="2024-12-25T01:00:00Z",
            taken_local_date="2024-12-24",
            title="2024-12-24 - Christmas Eve",
            description="Opening presents #family #2024 #12",
            tags_json=json.dumps(["family", "2024"]),
            lat=40.7128,
            lon=-74.0060,
        )
        fields.update(overrides)
        vid = insert_video(conn, **fields)
        return get_video(conn, vid)

    return _make


def no_sleep(_seconds: float) -> None:
    return None


def quota_units(conn) -> tuple[int, int]:
    row = get_quota_log(conn, PACIFIC_DAY)
    if row is None:
        return (0, 0)
    return (row["units_used"], row["uploads_used"])


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------

def test_success_marks_uploaded_stores_id_and_records_quota_once(
    conn, config, tracker, make_video
):
    video = make_video()
    request = FakeRequest([(FakeStatus(0.5), None), SUCCESS])
    youtube = make_youtube(request)

    result = upload_one(conn, config, tracker, youtube, video, sleep=no_sleep)

    row = get_video(conn, video.id)
    assert result.status == VideoStatus.UPLOADED
    assert row.status == VideoStatus.UPLOADED
    assert row.youtube_video_id == "yt-abc123"
    assert row.attempts == 1
    assert row.last_error is None
    assert quota_units(conn) == (UPLOAD_COST, 1)
    assert request.calls == 2
    youtube.videos.return_value.insert.assert_called_once()


def test_session_uri_persisted_as_soon_as_available(conn, config, tracker, make_video):
    video = make_video()
    seen_in_db: list = []

    def on_call(n):
        # Before call n runs, record what the DB holds for the session URI.
        row = get_video(conn, video.id)
        seen_in_db.append((n, row.status, row.upload_session_uri))

    request = FakeRequest(
        [(FakeStatus(0.33), None), (FakeStatus(0.66), None), SUCCESS], on_call=on_call
    )
    upload_one(conn, config, tracker, make_youtube(request), video, sleep=no_sleep)

    # Before the first request: UPLOADING, no URI yet.
    assert seen_in_db[0] == (1, VideoStatus.UPLOADING, None)
    # Right after the first next_chunk exposed the URI, it is in the DB.
    assert seen_in_db[1] == (2, VideoStatus.UPLOADING, SESSION_URI)
    assert seen_in_db[2] == (3, VideoStatus.UPLOADING, SESSION_URI)
    # Session finished: cleared on success.
    assert get_video(conn, video.id).upload_session_uri is None


def test_session_uri_persisted_even_if_first_chunk_fails(
    conn, config, tracker, make_video
):
    video = make_video()
    request = FakeRequest([make_http_error(400, "invalidMetadata")])

    upload_one(conn, config, tracker, make_youtube(request), video, sleep=no_sleep)

    row = get_video(conn, video.id)
    assert row.status == VideoStatus.FAILED
    assert row.upload_session_uri == SESSION_URI


def test_status_is_uploading_before_first_request(conn, config, tracker, make_video):
    video = make_video()
    statuses = []
    request = FakeRequest(
        [SUCCESS],
        on_call=lambda n: statuses.append(get_video(conn, video.id).status),
    )
    upload_one(conn, config, tracker, make_youtube(request), video, sleep=no_sleep)
    assert statuses == [VideoStatus.UPLOADING]


def test_non_pending_row_is_rejected_without_api_call(
    conn, config, tracker, make_video
):
    video = make_video()
    transition_video_status(conn, video.id, VideoStatus.FAILED, error="earlier")
    request = FakeRequest([SUCCESS])
    youtube = make_youtube(request)

    with pytest.raises(InvalidStateTransitionError):
        upload_one(conn, config, tracker, youtube, video, sleep=no_sleep)

    assert request.calls == 0
    assert get_video(conn, video.id).status == VideoStatus.FAILED
    assert quota_units(conn) == (0, 0)


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

def test_quota_exceeded_marks_exhausted_returns_to_pending_and_raises(
    conn, config, tracker, make_video
):
    video = make_video()
    request = FakeRequest([make_http_error(403, "quotaExceeded", "quota exceeded")])

    with pytest.raises(QuotaExhaustedError):
        upload_one(conn, config, tracker, make_youtube(request), video, sleep=no_sleep)

    row = get_video(conn, video.id)
    assert row.status == VideoStatus.PENDING
    assert row.youtube_video_id is None
    assert "quotaExceeded" in row.last_error
    assert tracker.is_exhausted() is True
    assert get_quota_log(conn, PACIFIC_DAY)["exhausted_at"] is not None
    assert quota_units(conn) == (0, 0)
    assert request.calls == 1  # not retried


def test_upload_limit_exceeded_returns_to_pending_without_marking_quota(
    conn, config, tracker, make_video
):
    video = make_video()
    request = FakeRequest([make_http_error(400, "uploadLimitExceeded", "limit")])

    with pytest.raises(UploadLimitExceededError):
        upload_one(conn, config, tracker, make_youtube(request), video, sleep=no_sleep)

    row = get_video(conn, video.id)
    assert row.status == VideoStatus.PENDING
    assert "uploadLimitExceeded" in row.last_error
    assert tracker.is_exhausted() is False
    log = get_quota_log(conn, PACIFIC_DAY)
    assert log is None or log["exhausted_at"] is None
    assert quota_units(conn) == (0, 0)
    assert request.calls == 1


@pytest.mark.parametrize(
    "status,reason",
    [
        (403, "forbidden"),
        (403, None),  # 403 with no reason in the body
        (400, "invalidMetadata"),
        (400, None),
    ],
)
def test_other_4xx_marks_failed_with_last_error(
    conn, config, tracker, make_video, status, reason
):
    video = make_video()
    request = FakeRequest([make_http_error(status, reason, "Request rejected")])

    result = upload_one(
        conn, config, tracker, make_youtube(request), video, sleep=no_sleep
    )

    row = get_video(conn, video.id)
    assert result.status == VideoStatus.FAILED
    assert row.status == VideoStatus.FAILED
    assert row.last_error.startswith(f"HTTP {status}")
    assert "Request rejected" in row.last_error
    if reason:
        assert reason in row.last_error
    assert tracker.is_exhausted() is False
    assert quota_units(conn) == (0, 0)
    assert request.calls == 1  # 4xx is not retried


def test_last_error_contains_no_session_uri_or_secrets(
    conn, config, tracker, make_video
):
    video = make_video()
    request = FakeRequest([make_http_error(403, "forbidden", "nope")])
    upload_one(conn, config, tracker, make_youtube(request), video, sleep=no_sleep)

    err = get_video(conn, video.id).last_error
    assert "SECRET-SESSION-123" not in err
    assert "upload_id" not in err
    assert "googleapis.com" not in err


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------

def test_503_is_retried_then_succeeds(conn, config, tracker, make_video):
    video = make_video()
    sleeps: list[float] = []
    request = FakeRequest(
        [
            (FakeStatus(0.5), None),
            make_http_error(503, "backendError"),
            make_http_error(503, "backendError"),
            SUCCESS,
        ]
    )

    result = upload_one(
        conn, config, tracker, make_youtube(request), video,
        max_attempts=5, sleep=sleeps.append,
    )

    assert result.status == VideoStatus.UPLOADED
    assert get_video(conn, video.id).youtube_video_id == "yt-abc123"
    assert request.calls == 4
    assert len(sleeps) == 2  # backed off twice
    assert sleeps[1] >= sleeps[0]  # exponential, non-decreasing
    assert quota_units(conn) == (UPLOAD_COST, 1)  # recorded exactly once
    assert get_video(conn, video.id).attempts == 1  # one upload attempt, chunk retries


@pytest.mark.parametrize("status", [500, 502, 504])
def test_other_5xx_codes_are_retried(conn, config, tracker, make_video, status):
    video = make_video()
    request = FakeRequest([make_http_error(status, "backendError"), SUCCESS])
    result = upload_one(
        conn, config, tracker, make_youtube(request), video, sleep=no_sleep
    )
    assert result.status == VideoStatus.UPLOADED
    assert request.calls == 2


def test_network_error_is_retried_then_succeeds(conn, config, tracker, make_video):
    video = make_video()
    request = FakeRequest([ConnectionResetError("reset"), TimeoutError(), SUCCESS])
    result = upload_one(
        conn, config, tracker, make_youtube(request), video, sleep=no_sleep
    )
    assert result.status == VideoStatus.UPLOADED
    assert request.calls == 3
    assert quota_units(conn) == (UPLOAD_COST, 1)


def test_503_retries_are_bounded_then_fail(conn, config, tracker, make_video):
    video = make_video()
    sleeps: list[float] = []
    request = FakeRequest([make_http_error(503, "backendError")] * 10)

    with pytest.raises(UploadRetriesExhaustedError):
        upload_one(
            conn, config, tracker, make_youtube(request), video,
            max_attempts=3, sleep=sleeps.append,
        )

    row = get_video(conn, video.id)
    assert request.calls == 3
    assert len(sleeps) == 2
    assert row.status == VideoStatus.FAILED
    assert "503" in row.last_error
    assert row.youtube_video_id is None
    assert quota_units(conn) == (0, 0)
    assert tracker.is_exhausted() is False
    # Session kept so part 2 (crash recovery) can resume it.
    assert row.upload_session_uri == SESSION_URI


def test_retry_is_per_chunk_not_whole_file(conn, config, tracker, make_video):
    """Each chunk gets its own retry budget; progress is not restarted."""
    video = make_video()
    err = make_http_error(503, "backendError")
    request = FakeRequest(
        [err, (FakeStatus(0.33), None), err, (FakeStatus(0.66), None), err, SUCCESS]
    )
    youtube = make_youtube(request)
    # max_attempts=2 would be exceeded by 3 failures if the budget were per file.
    result = upload_one(
        conn, config, tracker, youtube, video, max_attempts=2, sleep=no_sleep
    )
    assert result.status == VideoStatus.UPLOADED
    assert request.calls == 6
    # insert() built once; only next_chunk is repeated (no restart of the file).
    youtube.videos.return_value.insert.assert_called_once()


@pytest.mark.parametrize(
    "status,reason",
    [(403, "rateLimitExceeded"), (403, "userRateLimitExceeded"), (429, "rateLimitExceeded"), (429, None)],
)
def test_rate_limit_errors_are_retried(conn, config, tracker, make_video, status, reason):
    video = make_video()
    request = FakeRequest([make_http_error(status, reason, "slow down"), SUCCESS])
    result = upload_one(
        conn, config, tracker, make_youtube(request), video, sleep=no_sleep
    )
    assert result.status == VideoStatus.UPLOADED
    assert request.calls == 2
    assert tracker.is_exhausted() is False
    assert quota_units(conn) == (UPLOAD_COST, 1)


def test_rate_limit_retries_are_bounded_then_fail(conn, config, tracker, make_video):
    video = make_video()
    request = FakeRequest([make_http_error(403, "rateLimitExceeded")] * 5)
    with pytest.raises(UploadRetriesExhaustedError):
        upload_one(
            conn, config, tracker, make_youtube(request), video,
            max_attempts=3, sleep=no_sleep,
        )
    row = get_video(conn, video.id)
    assert request.calls == 3
    assert row.status == VideoStatus.FAILED
    assert "rateLimitExceeded" in row.last_error
    assert tracker.is_exhausted() is False
    assert quota_units(conn) == (0, 0)


def test_retry_after_header_is_honoured(conn, config, tracker, make_video):
    video = make_video()
    sleeps: list[float] = []
    request = FakeRequest(
        [
            make_http_error(429, "rateLimitExceeded", retry_after="17"),
            make_http_error(503, "backendError", retry_after="42"),
            make_http_error(503, "backendError"),  # no header -> exponential
            SUCCESS,
        ]
    )
    upload_one(
        conn, config, tracker, make_youtube(request), video, sleep=sleeps.append
    )
    assert sleeps[0] == 17.0
    assert sleeps[1] == 42.0
    assert 1.0 <= sleeps[2] <= 60.0


def test_retry_after_is_capped(conn, config, tracker, make_video):
    from photos2yt.uploader import MAX_RETRY_AFTER_SECONDS

    video = make_video()
    sleeps: list[float] = []
    request = FakeRequest([make_http_error(429, None, retry_after="86400"), SUCCESS])
    upload_one(
        conn, config, tracker, make_youtube(request), video, sleep=sleeps.append
    )
    assert sleeps == [MAX_RETRY_AFTER_SECONDS]


def test_quota_exceeded_on_session_initiation(conn, config, tracker, make_video):
    """The client raises ResumableUploadError (an HttpError) if the initiating POST fails."""
    video = make_video()
    err = make_http_error(403, "quotaExceeded", "quota", cls=ResumableUploadError)
    request = FakeRequest([err], uri=None)
    with pytest.raises(QuotaExhaustedError):
        upload_one(conn, config, tracker, make_youtube(request), video, sleep=no_sleep)
    row = get_video(conn, video.id)
    assert row.status == VideoStatus.PENDING
    assert row.upload_session_uri is None
    assert tracker.is_exhausted() is True
    assert request.calls == 1


def test_missing_source_file_marks_failed(conn, config, tracker, make_video, tmp_path):
    video = make_video(source_path=str(tmp_path / "gone.mp4"))
    request = FakeRequest([SUCCESS])
    result = upload_one(
        conn, config, tracker, make_youtube(request), video, sleep=no_sleep
    )
    assert result.status == VideoStatus.FAILED
    assert "Cannot open source file" in get_video(conn, video.id).last_error
    assert request.calls == 0
    assert quota_units(conn) == (0, 0)


def test_response_without_id_marks_failed(conn, config, tracker, make_video):
    video = make_video()
    request = FakeRequest([(None, {"kind": "youtube#video"})])
    result = upload_one(
        conn, config, tracker, make_youtube(request), video, sleep=no_sleep
    )
    assert result.status == VideoStatus.FAILED
    assert quota_units(conn) == (0, 0)


# ---------------------------------------------------------------------------
# Request body and media
# ---------------------------------------------------------------------------

def test_insert_called_with_parts_body_and_resumable_media(
    conn, config, tracker, make_video, video_file
):
    video = make_video()
    youtube = make_youtube(FakeRequest([SUCCESS]))
    upload_one(conn, config, tracker, youtube, video, sleep=no_sleep)

    kwargs = insert_kwargs(youtube)
    assert kwargs["part"] == "snippet,status,recordingDetails"

    body = kwargs["body"]
    assert body["snippet"]["categoryId"] == "22"
    assert body["snippet"]["title"] == "2024-12-24 - Christmas Eve"
    assert body["snippet"]["description"] == "Opening presents #family #2024 #12"
    assert body["snippet"]["tags"] == ["family", "2024"]
    assert "recordingDate" not in body["snippet"]
    assert body["status"] == {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False}
    assert body["recordingDetails"]["recordingDate"] == "2024-12-25T01:00:00Z"
    assert body["recordingDetails"]["location"] == {"latitude": 40.7128, "longitude": -74.006}

    media = kwargs["media_body"]
    assert isinstance(media, MediaFileUpload)
    assert media.resumable() is True
    assert media.chunksize() == config.youtube.upload_chunk_size == 8 * 1024 * 1024


def test_chunksize_comes_from_config(conn, config, tracker, make_video):
    cfg = replace(config, youtube=replace(config.youtube, upload_chunk_size=512 * 1024))
    video = make_video()
    youtube = make_youtube(FakeRequest([SUCCESS]))
    upload_one(conn, cfg, tracker, youtube, video, sleep=no_sleep)
    assert insert_kwargs(youtube)["media_body"].chunksize() == 512 * 1024


def test_chunksize_argument_overrides_config(conn, config, tracker, make_video):
    video = make_video()
    youtube = make_youtube(FakeRequest([SUCCESS]))
    upload_one(
        conn, config, tracker, youtube, video, chunksize=256 * 1024, sleep=no_sleep
    )
    assert insert_kwargs(youtube)["media_body"].chunksize() == 256 * 1024


@pytest.mark.parametrize(
    "lat,lon",
    [(0.0, 0.0), (None, None), (40.7, None), (None, -74.0), (0.0, -74.0), (40.7, 0.0)],
)
def test_no_location_when_lat_lon_zero_or_missing(
    conn, config, tracker, make_video, lat, lon
):
    video = make_video(lat=lat, lon=lon)
    youtube = make_youtube(FakeRequest([SUCCESS]))
    upload_one(conn, config, tracker, youtube, video, sleep=no_sleep)

    rd = insert_kwargs(youtube)["body"]["recordingDetails"]
    assert "location" not in rd
    assert rd["recordingDate"] == "2024-12-25T01:00:00Z"


def test_recording_date_normalized_from_offset_timestamp(config, make_video):
    video = make_video(taken_utc="2024-12-24T20:00:00-05:00")
    body = build_request_body(video, config)
    assert body["recordingDetails"]["recordingDate"] == "2024-12-25T01:00:00Z"


def test_default_title_and_privacy_from_config(conn, tracker, make_video, config):
    cfg = replace(
        config,
        youtube=replace(config.youtube, privacy="private", category_id="19", made_for_kids=False),
    )
    video = make_video(title=None, tags_json=None, taken_utc=None)
    body = build_request_body(video, cfg)
    assert body["snippet"]["title"] == "Family Video"
    assert body["snippet"]["categoryId"] == "19"
    assert body["snippet"]["tags"] == []
    assert body["status"]["privacyStatus"] == "private"
    assert "recordingDate" not in body["recordingDetails"]


# ===========================================================================
# Part 2: verification, crash recovery, run loop, dry run, CLI
# ===========================================================================

def verify_response(vid, privacy="unlisted", recording_date="2024-12-25T01:00:00Z"):
    item = {
        "id": vid,
        "status": {"privacyStatus": privacy},
        "processingDetails": {"processingStatus": "processing"},
        "recordingDetails": {},
    }
    if recording_date is not None:
        item["recordingDetails"]["recordingDate"] = recording_date
    return {"items": [item]}


def make_api(requests=(), *, verify=None, uploads_playlist="UU-chan", playlist_pages=None):
    """Mock YouTube client.

    requests: FakeRequest per videos().insert() call, in order.
    verify: callable(video_id) -> response dict, or raises; default = matching privacy/date.
    playlist_pages: list of playlistItems.list responses (or an exception to raise).
    """
    yt = MagicMock()
    pending = list(requests)
    yt.videos.return_value.insert.side_effect = lambda **kw: pending.pop(0)

    def _videos_list(part, id):
        call = MagicMock()
        fn = verify or (lambda vid: verify_response(vid))
        call.execute.side_effect = lambda: fn(id)
        return call

    yt.videos.return_value.list.side_effect = _videos_list

    channels = {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": uploads_playlist}}}]}
    yt.channels.return_value.list.return_value.execute.return_value = channels

    pages = list(playlist_pages or [{"items": []}])

    def _pl_list(**kwargs):
        call = MagicMock()
        page = pages.pop(0)
        if isinstance(page, BaseException):
            call.execute.side_effect = page
        else:
            call.execute.return_value = page
        return call

    yt.playlistItems.return_value.list.side_effect = _pl_list
    return yt


def playlist_item(video_id, title):
    return {"snippet": {"title": title, "resourceId": {"kind": "youtube#video", "videoId": video_id}}}


def insert_count(yt):
    return yt.videos.return_value.insert.call_count


class Out:
    def __init__(self):
        self.lines = []

    def __call__(self, text):
        self.lines.append(text)

    @property
    def text(self):
        return "\n".join(self.lines)


def make_uploaded(conn, make_video, yt_id="yt-abc123", **kw):
    v = make_video(**kw)
    transition_video_status(conn, v.id, VideoStatus.UPLOADING)
    return transition_video_status(conn, v.id, VideoStatus.UPLOADED, youtube_video_id=yt_id)


def make_stuck(conn, make_video, session_uri=None, **kw):
    v = make_video(**kw)
    transition_video_status(conn, v.id, VideoStatus.UPLOADING, increment_attempts=True)
    if session_uri:
        update_upload_session(conn, v.id, session_uri)
    return get_video(conn, v.id)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def test_privacy_mismatch_warns_loudly_and_stores_actual(
    conn, config, tracker, make_video, caplog
):
    video = make_uploaded(conn, make_video)
    yt = make_api(verify=lambda vid: verify_response(vid, privacy="private"))
    out = Out()

    with caplog.at_level(logging.WARNING, logger="photos2yt.uploader"):
        result = verify_upload(conn, config, tracker, yt, video, out=out)

    assert result.privacy_mismatch is True
    row = get_video(conn, video.id)
    assert row.actual_privacy == "private"
    assert row.status == VideoStatus.UPLOADED
    assert "PRIVATE-UPLOAD LOCK" in out.text
    assert "requested 'unlisted', YouTube reports 'private'" in out.text
    assert "July 2020" in out.text and "YouTube Studio" in out.text
    assert any("PRIVATE-UPLOAD LOCK" in r.getMessage() for r in caplog.records)
    assert quota_units(conn) == (1, 0)  # videos.list = 1 unit, recorded
    yt.videos.return_value.list.assert_called_once_with(
        part="status,processingDetails,recordingDetails", id="yt-abc123"
    )


def test_matching_privacy_no_warning(conn, config, tracker, make_video, caplog):
    video = make_uploaded(conn, make_video)
    out = Out()
    with caplog.at_level(logging.WARNING, logger="photos2yt.uploader"):
        result = verify_upload(conn, config, tracker, make_api(), video, out=out)
    assert result.privacy_mismatch is False
    assert result.recording_date_problem is None
    assert get_video(conn, video.id).actual_privacy == "unlisted"
    assert "PRIVATE-UPLOAD LOCK" not in out.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize(
    "failure",
    [make_http_error(500, "backendError"), make_http_error(403, "forbidden"), ConnectionResetError()],
)
def test_verification_failure_leaves_row_uploaded(conn, config, tracker, make_video, failure):
    video = make_uploaded(conn, make_video)

    def boom(vid):
        raise failure

    out = Out()
    result = verify_upload(conn, config, tracker, make_api(verify=boom), video, out=out)
    row = get_video(conn, video.id)
    assert result.error
    assert row.status == VideoStatus.UPLOADED
    assert row.youtube_video_id == "yt-abc123"
    assert row.actual_privacy is None
    assert "upload itself succeeded" in out.text


def test_verification_video_not_returned_leaves_row_uploaded(conn, config, tracker, make_video):
    video = make_uploaded(conn, make_video)
    result = verify_upload(conn, config, tracker, make_api(verify=lambda v: {"items": []}), video, out=Out())
    assert result.error
    assert get_video(conn, video.id).status == VideoStatus.UPLOADED


@pytest.mark.parametrize(
    "returned,problem",
    [(None, True), ("2023-01-01T00:00:00Z", True), ("2024-12-25T00:00:00Z", False),
     ("2024-12-25T01:00:00Z", False)],
)
def test_recording_date_warning(conn, config, tracker, make_video, returned, problem):
    video = make_uploaded(conn, make_video)
    out = Out()
    yt = make_api(verify=lambda vid: verify_response(vid, recording_date=returned))
    result = verify_upload(conn, config, tracker, yt, video, out=out)
    assert (result.recording_date_problem is not None) is problem
    assert ("recordingDate" in out.text) is problem


# ---------------------------------------------------------------------------
# Crash recovery
# ---------------------------------------------------------------------------

def test_recovery_resumes_saved_session(conn, config, tracker, make_video):
    video = make_stuck(conn, make_video, session_uri=SESSION_URI)
    primed = {}

    def on_call(n):
        primed["uri"] = request.resumable_uri
        primed["error_state"] = request._in_error_state

    request = FakeRequest([SUCCESS], uri=None, on_call=on_call)
    request._in_error_state = False  # attribute the real client has
    yt = make_api([request])

    summary = recover_stuck_uploads(conn, config, tracker, yt, sleep=no_sleep, out=Out())

    # The request was pointed at the saved session and told to query it first.
    assert primed == {"uri": SESSION_URI, "error_state": True}
    row = get_video(conn, video.id)
    assert summary.resumed == 1
    assert row.status == VideoStatus.UPLOADED
    assert row.youtube_video_id == "yt-abc123"
    assert row.upload_session_uri is None
    assert row.actual_privacy == "unlisted"  # verification ran
    assert quota_units(conn) == (UPLOAD_COST + 1, 1)
    yt.playlistItems.return_value.list.assert_not_called()


@pytest.mark.parametrize("status", [404, 410])
def test_recovery_session_gone_requeues_and_clears_session(
    conn, config, tracker, make_video, status
):
    video = make_stuck(conn, make_video, session_uri=SESSION_URI)
    request = FakeRequest([make_http_error(status, "notFound")], uri=None)
    request._in_error_state = False
    yt = make_api([request])

    summary = recover_stuck_uploads(conn, config, tracker, yt, sleep=no_sleep, out=Out())

    row = get_video(conn, video.id)
    assert summary.requeued == 1
    assert row.status == VideoStatus.PENDING
    assert row.upload_session_uri is None
    assert row.youtube_video_id is None
    # It checked the uploads playlist before re-queuing (never search.list).
    yt.playlistItems.return_value.list.assert_called_once()
    yt.search.assert_not_called()


def test_recovery_without_session_not_found_requeues(conn, config, tracker, make_video):
    video = make_stuck(conn, make_video)
    yt = make_api(playlist_pages=[{"items": [playlist_item("other", "Some other title")]}])
    summary = recover_stuck_uploads(conn, config, tracker, yt, sleep=no_sleep, out=Out())
    row = get_video(conn, video.id)
    assert summary.requeued == 1
    assert row.status == VideoStatus.PENDING
    assert insert_count(yt) == 0
    yt.channels.return_value.list.assert_called_once_with(part="contentDetails", mine=True)
    assert quota_units(conn) == (2, 0)  # channels.list + one playlistItems page


def test_recovery_adopts_already_uploaded_video(conn, config, tracker, make_video):
    # Another DB row already owns yt-known with the same title: must not be adopted.
    make_uploaded(conn, make_video, yt_id="yt-known")
    video = make_stuck(conn, make_video)
    title = video.title
    yt = make_api(
        playlist_pages=[
            {"items": [playlist_item("yt-known", title), playlist_item("x1", "Different")],
             "nextPageToken": "p2"},
            {"items": [playlist_item("yt-orphan", title)]},
        ]
    )

    summary = recover_stuck_uploads(conn, config, tracker, yt, sleep=no_sleep, out=Out())

    row = get_video(conn, video.id)
    assert summary.adopted == 1
    assert row.status == VideoStatus.UPLOADED
    assert row.youtube_video_id == "yt-orphan"
    assert row.actual_privacy == "unlisted"  # verification ran
    assert insert_count(yt) == 0  # no duplicate upload
    assert yt.playlistItems.return_value.list.call_count == 2
    assert quota_units(conn) == (1 + 2 + 1, 0)  # channels + 2 pages + verify; no upload cost


def test_recovery_after_session_gone_adopts_instead_of_duplicate(conn, config, tracker, make_video):
    video = make_stuck(conn, make_video, session_uri=SESSION_URI)
    request = FakeRequest([make_http_error(404, "notFound")], uri=None)
    request._in_error_state = False
    yt = make_api([request], playlist_pages=[{"items": [playlist_item("yt-done", video.title)]}])

    summary = recover_stuck_uploads(conn, config, tracker, yt, sleep=no_sleep, out=Out())

    row = get_video(conn, video.id)
    assert summary.adopted == 1
    assert row.status == VideoStatus.UPLOADED
    assert row.youtube_video_id == "yt-done"
    assert row.upload_session_uri is None


def test_recovery_ambiguous_match_marks_failed(conn, config, tracker, make_video):
    video = make_stuck(conn, make_video)
    yt = make_api(playlist_pages=[{"items": [playlist_item("a", video.title), playlist_item("b", video.title)]}])
    summary = recover_stuck_uploads(conn, config, tracker, yt, sleep=no_sleep, out=Out())
    row = get_video(conn, video.id)
    assert summary.failed == 1
    assert row.status == VideoStatus.FAILED
    assert "several uploaded videos" in row.last_error


def test_recovery_check_unavailable_leaves_row_uploading(conn, config, tracker, make_video):
    video = make_stuck(conn, make_video)
    yt = make_api(playlist_pages=[make_http_error(500, "backendError")])
    summary = recover_stuck_uploads(conn, config, tracker, yt, sleep=no_sleep, out=Out())
    assert summary.deferred == 1
    assert get_video(conn, video.id).status == VideoStatus.UPLOADING


def test_recovery_deferred_when_quota_exhausted(conn, config, tracker, make_video):
    video = make_stuck(conn, make_video, session_uri=SESSION_URI)
    tracker.mark_exhausted()
    yt = make_api()
    summary = recover_stuck_uploads(conn, config, tracker, yt, sleep=no_sleep, out=Out())
    assert summary.deferred == 1
    assert get_video(conn, video.id).status == VideoStatus.UPLOADING
    yt.channels.assert_not_called()
    assert insert_count(yt) == 0


def test_run_recovers_then_uploads_pending(conn, config, tracker, make_video):
    stuck = make_stuck(conn, make_video)
    pending = make_video(title="Second")
    yt = make_api(
        [FakeRequest([(None, {"id": "yt-new"})])],
        playlist_pages=[{"items": [playlist_item("yt-old", stuck.title)]}],
    )
    summary = run_uploads(conn, config, tracker, yt, out=Out(), retry_sleep=no_sleep)
    assert summary.recovery.adopted == 1
    assert summary.uploaded == 1
    assert get_video(conn, stuck.id).youtube_video_id == "yt-old"
    assert get_video(conn, pending.id).youtube_video_id == "yt-new"
    assert insert_count(yt) == 1


# ---------------------------------------------------------------------------
# Run loop
# ---------------------------------------------------------------------------

def success_requests(n):
    return [FakeRequest([(None, {"id": f"yt-{i}"})]) for i in range(n)]


def test_limit_and_order(conn, config, tracker, make_video):
    c = make_video(title="C", taken_utc="2022-01-01T00:00:00Z")
    a = make_video(title="A", taken_utc="2020-01-01T00:00:00Z")
    b = make_video(title="B", taken_utc="2021-01-01T00:00:00Z")
    yt = make_api(success_requests(3))

    summary = run_uploads(conn, config, tracker, yt, limit=2, out=Out(), retry_sleep=no_sleep)

    assert summary.uploaded == 2
    assert insert_count(yt) == 2
    assert get_video(conn, a.id).youtube_video_id == "yt-0"
    assert get_video(conn, b.id).youtube_video_id == "yt-1"
    assert get_video(conn, c.id).status == VideoStatus.PENDING
    # 2 uploads + 2 verifications
    assert quota_units(conn) == (2 * UPLOAD_COST + 2, 2)


def test_same_timestamp_ordered_by_id(conn, config, tracker, make_video):
    first = make_video(title="first")
    make_video(title="second")
    yt = make_api(success_requests(1))
    run_uploads(conn, config, tracker, yt, limit=1, out=Out(), retry_sleep=no_sleep)
    assert get_video(conn, first.id).status == VideoStatus.UPLOADED


def test_over_15_minutes_skipped(conn, config, tracker, make_video):
    long_v = make_video(title="Long", duration_s=MAX_UNVERIFIED_DURATION_S + 1)
    ok = make_video(title="Exactly 15", duration_s=MAX_UNVERIFIED_DURATION_S)
    yt = make_api(success_requests(1))

    summary = run_uploads(conn, config, tracker, yt, limit=1, out=Out(), retry_sleep=no_sleep)

    row = get_video(conn, long_v.id)
    assert row.status == VideoStatus.SKIPPED
    assert "15 min" in row.last_error
    assert summary.skipped == 1
    # Skipped row did not consume the --limit slot.
    assert get_video(conn, ok.id).status == VideoStatus.UPLOADED
    assert insert_count(yt) == 1


def test_over_15_minutes_uploaded_when_channel_verified(conn, config, tracker, make_video):
    cfg = replace(config, youtube=replace(config.youtube, channel_verified=True))
    long_v = make_video(duration_s=3600)
    yt = make_api(success_requests(1))
    run_uploads(conn, cfg, tracker, yt, out=Out(), retry_sleep=no_sleep)
    assert get_video(conn, long_v.id).status == VideoStatus.UPLOADED


def test_cannot_afford_stops_run(conn, config, tracker, make_video):
    from photos2yt.db import upsert_quota_log

    v = make_video()
    upsert_quota_log(conn, PACIFIC_DAY, units_used=config.quota.daily_unit_limit - UPLOAD_COST + 1)
    yt = make_api(success_requests(1))
    sleeps = []

    summary = run_uploads(conn, config, tracker, yt, sleep=sleeps.append, out=Out(), retry_sleep=no_sleep)

    assert summary.stopped_reason and "quota" in summary.stopped_reason
    assert insert_count(yt) == 0
    assert sleeps == []  # no --wait: no sleeping
    assert get_video(conn, v.id).status == VideoStatus.PENDING


def test_quota_exhausted_error_stops_run(conn, config, tracker, make_video):
    first = make_video(title="1", taken_utc="2020-01-01T00:00:00Z")
    second = make_video(title="2", taken_utc="2021-01-01T00:00:00Z")
    yt = make_api([FakeRequest([make_http_error(403, "quotaExceeded")])] + success_requests(1))
    out = Out()

    summary = run_uploads(conn, config, tracker, yt, out=out, retry_sleep=no_sleep)

    assert "quota exhausted" in summary.stopped_reason
    assert "Stopping: quota exhausted" in out.text
    assert insert_count(yt) == 1
    assert get_video(conn, first.id).status == VideoStatus.PENDING
    assert get_video(conn, second.id).status == VideoStatus.PENDING
    assert tracker.is_exhausted()


def test_upload_limit_error_stops_run(conn, config, tracker, make_video):
    make_video(title="1", taken_utc="2020-01-01T00:00:00Z")
    second = make_video(title="2", taken_utc="2021-01-01T00:00:00Z")
    yt = make_api([FakeRequest([make_http_error(400, "uploadLimitExceeded")])] + success_requests(1))

    summary = run_uploads(conn, config, tracker, yt, out=Out(), retry_sleep=no_sleep)

    assert "upload limit" in summary.stopped_reason
    assert insert_count(yt) == 1
    assert get_video(conn, second.id).status == VideoStatus.PENDING
    assert not tracker.is_exhausted()


def test_one_failing_video_does_not_abort_others(conn, config, tracker, make_video):
    bad = make_video(title="bad", taken_utc="2020-01-01T00:00:00Z")
    flaky = make_video(title="flaky", taken_utc="2021-01-01T00:00:00Z")
    good = make_video(title="good", taken_utc="2022-01-01T00:00:00Z")
    weird = make_video(title="weird", taken_utc="2023-01-01T00:00:00Z")
    good2 = make_video(title="good2", taken_utc="2024-01-01T00:00:00Z")
    yt = make_api([
        FakeRequest([make_http_error(400, "invalidMetadata")]),
        FakeRequest([make_http_error(503, "backendError")] * 10),
        FakeRequest([(None, {"id": "yt-good"})]),
        FakeRequest([ValueError("unexpected")]),
        FakeRequest([(None, {"id": "yt-good2"})]),
    ])

    summary = run_uploads(conn, config, tracker, yt, out=Out(), retry_sleep=no_sleep, max_attempts=2)

    assert summary.failed == 3
    assert summary.uploaded == 2
    assert summary.stopped_reason is None
    assert get_video(conn, bad.id).status == VideoStatus.FAILED
    assert get_video(conn, flaky.id).status == VideoStatus.FAILED
    assert get_video(conn, good.id).youtube_video_id == "yt-good"
    assert get_video(conn, weird.id).status == VideoStatus.UPLOADING  # recovered next run
    assert get_video(conn, good2.id).youtube_video_id == "yt-good2"


def test_wait_sleeps_with_injected_sleep_until_reset(conn, config, make_video):
    from datetime import timedelta

    from photos2yt.db import upsert_quota_log

    now = [FROZEN_NOW]
    tracker = QuotaTracker(conn, config, clock=lambda: now[0])
    upsert_quota_log(conn, PACIFIC_DAY, units_used=config.quota.daily_unit_limit)
    v = make_video()
    expected = tracker.seconds_until_reset()
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        now[0] = now[0] + timedelta(seconds=seconds + 1)  # past Pacific midnight

    yt = make_api(success_requests(1))
    summary = run_uploads(conn, config, tracker, yt, wait=True, sleep=fake_sleep, out=Out(),
                          retry_sleep=no_sleep)

    assert sleeps == [expected]
    assert summary.uploaded == 1
    assert get_video(conn, v.id).status == VideoStatus.UPLOADED


def test_wait_gives_up_if_still_unaffordable(conn, config, tracker, make_video):
    from photos2yt.db import upsert_quota_log

    upsert_quota_log(conn, PACIFIC_DAY, units_used=config.quota.daily_unit_limit)
    make_video()
    sleeps = []
    yt = make_api(success_requests(1))
    summary = run_uploads(conn, config, tracker, yt, wait=True, sleep=sleeps.append, out=Out())
    assert len(sleeps) == 1  # frozen clock: no reset happened
    assert summary.stopped_reason
    assert insert_count(yt) == 0


# ---------------------------------------------------------------------------
# Dry run and CLI
# ---------------------------------------------------------------------------

def _dir_snapshot(d: Path):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in d.iterdir() if p.is_file()}


def test_dry_run_missing_db_creates_nothing(config, tmp_path):
    out = Out()
    db_path = config.paths.db_path
    assert not db_path.exists()
    plan = dry_run(config, out=out)
    assert plan.database_missing
    assert not db_path.exists()
    assert not Path(str(db_path) + "-wal").exists()
    assert "nothing to upload" in out.text


def test_dry_run_touches_nothing(config, tmp_path, video_file):
    file_conn = init_db(get_connection(config.paths.db_path))
    insert_video(file_conn, source_path=str(video_file), sha256="a", size_bytes=1,
                 title="Beach day", taken_utc="2020-01-01T00:00:00Z")
    insert_video(file_conn, source_path=str(video_file) + "2", sha256="b", size_bytes=1,
                 title="Birthday", taken_utc="2021-01-01T00:00:00Z")
    insert_video(file_conn, source_path=str(video_file) + "3", sha256="c", size_bytes=1,
                 title="Long one", duration_s=MAX_UNVERIFIED_DURATION_S + 60)
    file_conn.close()
    before = _dir_snapshot(tmp_path)

    out = Out()
    plan = dry_run(config, limit=None, out=out)

    assert _dir_snapshot(tmp_path) == before  # no DB change, no new -wal/-shm files
    assert len(plan.to_upload) == 2 and len(plan.to_skip) == 1
    assert plan.estimated_units == 2 * (UPLOAD_COST + 1)
    assert "Would upload 2 video(s)" in out.text
    assert "'unlisted'" in out.text
    assert "Beach day" in out.text and "Birthday" in out.text
    assert "would SKIP" in out.text
    assert str(2 * (UPLOAD_COST + 1)) in out.text


def test_dry_run_respects_limit(config, video_file):
    file_conn = init_db(get_connection(config.paths.db_path))
    for i in range(3):
        insert_video(file_conn, source_path=f"{video_file}{i}", sha256=f"s{i}", size_bytes=1, title=f"T{i}")
    file_conn.close()
    plan = dry_run(config, limit=1, out=Out())
    assert [v.title for v in plan.to_upload] == ["T0"]


def _write_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "paths:\n"
        f"  staging_dir: {tmp_path}\n"
        "  db_path: queue.db\n"
        "  credentials: credentials.json\n"
        "  token: token.json\n"
        "timezone: America/New_York\n"
        "youtube:\n"
        "  privacy: unlisted\n"
        "  category_id: '22'\n"
        "  made_for_kids: false\n"
        "  title_template: '{date} - {title}'\n"
        "  default_title: Family Video\n"
        "quota:\n"
        "  daily_unit_limit: 10000\n"
        "  upload_cost_units: 1600\n"
        "  playlist_insert_cost: 50\n"
        "  playlist_item_insert_cost: 50\n"
        "video_extensions: [.mp4]\n"
    )
    return cfg


def test_cli_dry_run_uses_no_credentials_and_creates_no_db(tmp_path, monkeypatch, capsys):
    import photos2yt.cli as cli

    def forbidden(*a, **k):
        raise AssertionError("dry run must not touch credentials or the API")

    monkeypatch.setattr(cli, "get_credentials", forbidden)
    monkeypatch.setattr(cli, "get_youtube_service", forbidden)
    cfg = _write_config(tmp_path)

    rc = cli.main(["-c", str(cfg), "upload", "--dry-run", "--limit", "5"])

    assert rc == 0
    assert not (tmp_path / "queue.db").exists()
    assert "[DRY RUN]" in capsys.readouterr().out


def test_cli_upload_uses_get_credentials_and_exit_codes(tmp_path, monkeypatch, video_file):
    import photos2yt.cli as cli
    from photos2yt.config import load_config

    cfg_path = _write_config(tmp_path)
    config = load_config(cfg_path)
    c = init_db(get_connection(config.paths.db_path))
    insert_video(c, source_path=str(video_file), sha256="z", size_bytes=1, title="One")
    c.close()

    calls = {}
    yt = make_api(success_requests(1))
    monkeypatch.setattr(cli, "get_credentials", lambda config: calls.setdefault("creds", "CREDS"))
    monkeypatch.setattr(cli, "get_youtube_service",
                        lambda config, credentials: calls.setdefault("svc", credentials) and yt)

    rc = cli.main(["-c", str(cfg_path), "upload", "--limit", "1"])

    assert rc == 0
    assert calls == {"creds": "CREDS", "svc": "CREDS"}
    c = get_connection(config.paths.db_path)
    assert get_video(c, 1).status == VideoStatus.UPLOADED
    c.close()

    # A failing video makes the exit code non-zero.
    c = get_connection(config.paths.db_path)
    insert_video(c, source_path=str(video_file) + "b", sha256="y", size_bytes=1, title="Two")
    c.close()
    yt2 = make_api([FakeRequest([make_http_error(400, "invalidMetadata")])])
    assert cli.run_upload_command(config, youtube=yt2) == 1


def test_no_secrets_in_output(conn, config, tracker, make_video):
    make_stuck(conn, make_video, session_uri=SESSION_URI)
    make_video(title="other")
    request = FakeRequest([make_http_error(404, "notFound")], uri=None)
    request._in_error_state = False
    out = Out()
    run_uploads(conn, config, tracker, make_api([request] + success_requests(1)),
                out=out, retry_sleep=no_sleep)
    assert "SECRET-SESSION-123" not in out.text
    assert "upload_id" not in out.text
    assert not os.environ.get("NEVER_SET_TOKEN")  # sanity: no token in env used
