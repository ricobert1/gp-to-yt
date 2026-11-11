"""Tests for photos2yt.uploader (Task 6, part 1).

No real API calls and no real uploads: the YouTube client is a mock whose
videos().insert() returns a FakeRequest that scripts next_chunk() results.
Errors are real googleapiclient.errors.HttpError objects with JSON bodies.
The DB is real (in-memory SQLite) and the QuotaTracker is real with a frozen
clock, so assertions are on actual rows and quota_log values.
"""

from __future__ import annotations

import json
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
)
from photos2yt.quota import QuotaTracker
from photos2yt.uploader import (
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
        fields = dict(
            source_path=str(video_file),
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
    from dataclasses import replace

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
    from dataclasses import replace

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
