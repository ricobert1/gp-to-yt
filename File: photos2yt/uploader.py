"""YouTube video upload and resumption module."""
from __future__ import annotations

import time
from typing import Optional
from tenacity import retry, stop_after_attempt, wait_exponential
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials

from photos2yt.config import Config
from photos2yt.db import get_connection, get_video, transition_video_status
from photos2yt.quota import QuotaTracker
from photos2yt.auth import get_credentials

class UploaderError(Exception):
    """Base exception for uploader errors."""
    pass

class UploadLimitExceededError(UploaderError):
    """Raised when the upload limit is exceeded."""
    pass

class PrivateLockWarning(UploaderError):
    """Warning when the project is subject to the private-upload lock."""
    pass

def upload_video(video_id: int, config: Config, quota: QuotaTracker, sleep: Optional[Callable[[], None]] = None) -> None:
    """Upload a video to YouTube."""
    conn = get_connection(config.paths.db_path)
    video = get_video(conn, video_id)
    if video is None:
        raise ValueError(f"Video with id {video_id} not found")

    if video.duration_s > 900 and not config.youtube.get('verified_channel', False):
        transition_video_status(conn, video_id, VideoStatus.SKIPPED, reason="Video duration exceeds 15 minutes")
        return

    if not quota.can_afford(quota.cfg.quota.upload_cost_units, uploads=1):
        raise UploadLimitExceededError("Upload limit exceeded")

    youtube = build('youtube', 'v3', credentials=get_credentials())
    media = MediaFileUpload(video.source_path, chunksize=1024 * 1024 * 5, resumable=True)

    request = youtube.videos().insert(
        part="snippet,status,recordingDetails",
        body={
            "snippet": {
                "title": video.title,
                "description": video.description,
                "tags": video.tags_json.split(','),
                "categoryId": config.youtube['category_id'],
                "selfDeclaredMadeForKids": config.youtube.get('made_for_kids', False)
            },
            "status": {
                "privacyStatus": config.youtube['privacy'],
                "selfDeclaredMadeForKids": config.youtube.get('made_for_kids', False)
            },
            "recordingDetails": {
                "recordingDate": video.taken_utc
            }
        },
        media_body=media
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"Uploaded {int(status.progress() * 100)}%.")

    if response.get('error'):
        error = response['error']
        if error['code'] == 403 and 'quotaExceeded' in error['message']:
            quota.mark_exhausted()
            transition_video_status(conn, video_id, VideoStatus.PENDING)
            raise UploaderError("Quota exceeded")
        elif error['code'] == 403 and 'uploadLimitExceeded' in error['message']:
            transition_video_status(conn, video_id, VideoStatus.PENDING)
            raise UploadLimitExceededError("Upload limit exceeded")
        else:
            transition_video_status(conn, video_id, VideoStatus.FAILED, last_error=error['message'])
            raise UploaderError(f"Upload failed: {error['message']}")

    video.youtube_video_id = response['id']
    transition_video_status(conn, video_id, VideoStatus.UPLOADED)
    quota.record(quota.cfg.quota.upload_cost_units, uploads=1)

    video_record = youtube.videos().list(
        part="status,processingDetails,recordingDetails",
        id=video.youtube_video_id
    ).execute()

    video_record = video_record['items'][0]
    video.actual_privacy = video_record['status']['privacyStatus']
    if video.actual_privacy != config.youtube['privacy']:
        print(f"WARNING: Project is subject to the private-upload lock. Requested {config.youtube['privacy']}, got {video.actual_privacy}")
        raise PrivateLockWarning("Project is subject to the private-upload lock")

    if video_record['recordingDetails'].get('location'):
        video.lat = video_record['recordingDetails']['location']['latitude']
        video.lon = video_record['recordingDetails']['location']['longitude']

    transition_video_status(conn, video_id, VideoStatus.UPLOADED)

def resume_upload(video_id: int, config: Config, quota: QuotaTracker, sleep: Optional[Callable[[], None]] = None) -> None:
    """Resume an upload that was interrupted."""
    conn = get_connection(config.paths.db_path)
    video = get_video(conn, video_id)
    if video is None:
        raise ValueError(f"Video with id {video_id} not found")

    if video.upload_session_uri:
        youtube = build('youtube', 'v3', credentials=get_credentials())
        request = youtube.videos().insert(
            part="snippet,status,recordingDetails",
            body={
                "snippet": {
                    "title": video.title,
                    "description": video.description,
                    "tags": video.tags_json.split(','),
                    "categoryId": config.youtube['category_id'],
                    "selfDeclaredMadeForKids": config.youtube.get('made_for_kids', False)
                },
                "status": {
                    "privacyStatus": config.youtube['privacy'],
                    "selfDeclaredMadeForKids": config.youtube.get('made_for_kids', False)
                },
                "recordingDetails": {
                    "recordingDate": video.taken_utc
                }
            },
            media_body=MediaFileUpload(video.source_path, chunksize=1024 * 1024 * 5, resumable=True),
            media_body_resumable=video.upload_session_uri
        )

        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                print(f"Resumed {int(status.progress() * 100)}%.")

        if response.get('error'):
            error = response['error']
            if error['code'] == 403 and 'quotaExceeded' in error['message']:
                quota.mark_exhausted()
                transition_video_status(conn, video_id, VideoStatus.PENDING)
                raise UploaderError("Quota exceeded")
            elif error['code'] == 403 and 'uploadLimitExceeded' in error['message']:
                transition_video_status(conn, video_id, VideoStatus.PENDING)
                raise UploadLimitExceededError("Upload limit exceeded")
            else:
                transition_video_status(conn, video_id, VideoStatus.FAILED, last_error=error['message'])
                raise UploaderError(f"Upload failed: {error['message']}")

        video.youtube_video_id = response['id']
        transition_video_status(conn, video_id, VideoStatus.UPLOADED)
        quota.record(quota.cfg.quota.upload_cost_units, uploads=1)

        video_record = youtube.videos().list(
            part="status,processingDetails,recordingDetails",
            id=video.youtube_video_id
        ).execute()

        video_record = video_record['items'][0]
        video.actual_privacy = video_record['status']['privacyStatus']
        if video.actual_privacy != config.youtube['privacy']:
            print(f"WARNING: Project is subject to the private-upload lock. Requested {config.youtube['privacy']}, got {video.actual_privacy}")
            raise PrivateLockWarning("Project is subject to the private-upload lock")

        if video_record['recordingDetails'].get('location'):
            video.lat = video_record['recordingDetails']['location']['latitude']
            video.lon = video_record['recordingDetails']['location']['longitude']

        transition_video_status(conn, video_id, VideoStatus.UPLOADED)

def upload_videos(config: Config, quota: QuotaTracker, limit: Optional[int] = None, dry_run: bool = False, wait: bool = False, sleep: Optional[Callable[[], None]] = None) -> None:
    """Upload videos to YouTube."""
    conn = get_connection(config.paths.db_path)
    videos = get_videos_by_status(conn, VideoStatus.PENDING, limit=limit)
    for video in videos:
        try:
            if video.upload_session_uri:
                resume_upload(video.id, config, quota, sleep)
            else:
                upload_video(video.id, config, quota, sleep)
        except UploaderError as e:
            if dry_run:
                print(f"Would have failed to upload {video.title} due to {e}")
            else:
                print(f"Failed to upload {video.title} due to {e}")
                if wait:
                    sleep()
