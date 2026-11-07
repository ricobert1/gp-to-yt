"""Tests for SQLite queue, schema migrations, and state machine transitions."""

from __future__ import annotations

import itertools
from pathlib import Path
import sqlite3
import pytest

from photos2yt.db import (
    LEGAL_TRANSITIONS,
    InvalidStateTransitionError,
    VideoNotFoundError,
    VideoRecord,
    VideoStatus,
    add_playlist_membership,
    get_connection,
    get_playlist,
    get_playlist_memberships,
    get_quota_log,
    get_video,
    get_video_by_sha256,
    get_videos_by_status,
    init_db,
    is_valid_transition,
    mark_playlist_membership_added,
    transition_video_status,
    upsert_playlist,
    upsert_quota_log,
    validate_transition,
    insert_video,
)


@pytest.fixture
def memory_db() -> sqlite3.Connection:
    """Fixture providing an initialized in-memory database."""
    conn = get_connection(":memory:")
    init_db(conn)
    return conn


@pytest.fixture
def file_db(tmp_path: Path) -> sqlite3.Connection:
    """Fixture providing an initialized file-based database."""
    db_file = tmp_path / "test_queue.db"
    conn = get_connection(db_file)
    init_db(conn)
    return conn


# --- Acceptance Test: State Machine Transition Matrix ---

ALL_STATUSES = list(VideoStatus)
ALL_STATUS_PAIRS = list(itertools.product(ALL_STATUSES, ALL_STATUSES))


def test_transition_matrix_counts():
    """Verify that expected legal transitions match the design specification."""
    legal_pairs = [
        (curr, tgt) for curr in ALL_STATUSES for tgt in LEGAL_TRANSITIONS[curr]
    ]
    # Expected legal transitions:
    # PENDING -> UPLOADING, SKIPPED, FAILED (3)
    # UPLOADING -> UPLOADED, PENDING, FAILED (3)
    # UPLOADED -> PLAYLISTED (1)
    # PLAYLISTED -> none (0)
    # FAILED -> PENDING (1)
    # SKIPPED -> PENDING (1)
    assert len(legal_pairs) == 9
    assert len(ALL_STATUS_PAIRS) == 36


@pytest.mark.parametrize("current_status,target_status", ALL_STATUS_PAIRS)
def test_all_transitions_matrix(current_status: VideoStatus, target_status: VideoStatus):
    """Exhaustively test every single transition pair (both legal and illegal)."""
    is_legal = target_status in LEGAL_TRANSITIONS[current_status]

    assert is_valid_transition(current_status, target_status) is is_legal
    assert is_valid_transition(current_status.value, target_status.value) is is_legal

    if is_legal:
        # validate_transition should not raise
        validate_transition(current_status, target_status)
        validate_transition(current_status.value, target_status.value)
    else:
        with pytest.raises(InvalidStateTransitionError):
            validate_transition(current_status, target_status)
        with pytest.raises(InvalidStateTransitionError):
            validate_transition(current_status.value, target_status.value)


def test_validate_transition_invalid_status_strings():
    """Invalid status values should raise InvalidStateTransitionError."""
    with pytest.raises(InvalidStateTransitionError, match="Invalid current status"):
        validate_transition("NON_EXISTENT", VideoStatus.PENDING)

    with pytest.raises(InvalidStateTransitionError, match="Invalid target status"):
        validate_transition(VideoStatus.PENDING, "INVALID_TARGET")


# --- Acceptance Test: In-Database State Transitions ---

@pytest.mark.parametrize("current_status,target_status", [
    (VideoStatus.PENDING, VideoStatus.UPLOADING),
    (VideoStatus.PENDING, VideoStatus.SKIPPED),
    (VideoStatus.PENDING, VideoStatus.FAILED),
    (VideoStatus.UPLOADING, VideoStatus.UPLOADED),
    (VideoStatus.UPLOADING, VideoStatus.PENDING),
    (VideoStatus.UPLOADING, VideoStatus.FAILED),
    (VideoStatus.UPLOADED, VideoStatus.PLAYLISTED),
    (VideoStatus.FAILED, VideoStatus.PENDING),
    (VideoStatus.SKIPPED, VideoStatus.PENDING),
])
def test_database_legal_transitions(memory_db: sqlite3.Connection, current_status: VideoStatus, target_status: VideoStatus):
    """Test every legal transition applied to a real DB record."""
    vid_id = insert_video(
        memory_db,
        source_path=f"/staging/{current_status.value}_{target_status.value}.mp4",
        sha256=f"sha_{current_status.value}_{target_status.value}",
        size_bytes=1024,
        status=current_status,
    )

    updated = transition_video_status(
        memory_db,
        vid_id,
        target_status,
        error="test error" if target_status == VideoStatus.FAILED else None,
        upload_session_uri="https://yt.example.com/resumable" if target_status == VideoStatus.UPLOADING else None,
        youtube_video_id="yt_12345" if target_status == VideoStatus.UPLOADED else None,
        actual_privacy="unlisted" if target_status == VideoStatus.UPLOADED else None,
        increment_attempts=(target_status == VideoStatus.UPLOADING),
    )

    assert updated.status == target_status
    assert updated.updated_at is not None

    if target_status == VideoStatus.UPLOADING:
        assert updated.attempts == 1
        assert updated.upload_session_uri == "https://yt.example.com/resumable"
    if target_status == VideoStatus.UPLOADED:
        assert updated.youtube_video_id == "yt_12345"
        assert updated.actual_privacy == "unlisted"
    if target_status == VideoStatus.FAILED:
        assert updated.last_error == "test error"


@pytest.mark.parametrize("current_status,target_status", [
    pair for pair in ALL_STATUS_PAIRS if pair[1] not in LEGAL_TRANSITIONS[pair[0]]
])
def test_database_illegal_transitions_rejected(
    memory_db: sqlite3.Connection,
    current_status: VideoStatus,
    target_status: VideoStatus,
):
    """Test that every illegal transition is rejected and leaves DB row untouched."""
    vid_id = insert_video(
        memory_db,
        source_path=f"/staging/illegal_{current_status.value}_{target_status.value}.mp4",
        sha256=f"sha_illegal_{current_status.value}_{target_status.value}",
        size_bytes=1024,
        status=current_status,
    )

    original_rec = get_video(memory_db, vid_id)
    assert original_rec is not None
    assert original_rec.status == current_status

    with pytest.raises(InvalidStateTransitionError):
        transition_video_status(memory_db, vid_id, target_status)

    # Check record was not modified
    current_rec = get_video(memory_db, vid_id)
    assert current_rec is not None
    assert current_rec.status == current_status


def test_transition_video_not_found(memory_db: sqlite3.Connection):
    """Attempting to transition a non-existent video ID raises VideoNotFoundError."""
    with pytest.raises(VideoNotFoundError, match="Video with id 999999 not found"):
        transition_video_status(memory_db, 999999, VideoStatus.UPLOADING)


def test_transition_clear_session_uri(memory_db: sqlite3.Connection):
    """Clearing upload session URI works during transition."""
    vid_id = insert_video(
        memory_db,
        source_path="/staging/sample.mp4",
        sha256="sha_sample_clear",
        size_bytes=2048,
        status=VideoStatus.PENDING,
    )
    # Transition to UPLOADING with session URI
    transition_video_status(
        memory_db,
        vid_id,
        VideoStatus.UPLOADING,
        upload_session_uri="https://session.uri",
    )
    video = get_video(memory_db, vid_id)
    assert video.upload_session_uri == "https://session.uri"

    # Transition to UPLOADED with clear_session_uri=True
    transition_video_status(
        memory_db,
        vid_id,
        VideoStatus.UPLOADED,
        clear_session_uri=True,
        youtube_video_id="abc123vid",
    )
    updated = get_video(memory_db, vid_id)
    assert updated.upload_session_uri is None
    assert updated.youtube_video_id == "abc123vid"


# --- Acceptance Test: Schema Migrates Idempotently ---

def test_schema_migration_idempotent_memory():
    """Running init_db multiple times on an in-memory DB succeeds without error."""
    conn = get_connection(":memory:")
    init_db(conn)
    # Second run
    init_db(conn)
    # Third run
    init_db(conn)

    cursor = conn.cursor()
    cursor.execute("SELECT version FROM schema_migrations")
    versions = [row[0] for row in cursor.fetchall()]
    assert versions == [1]


def test_schema_migration_idempotent_file(tmp_path: Path):
    """Running init_db multiple times on a disk file preserves existing rows and schema."""
    db_path = tmp_path / "idempotent_test.db"

    # 1. Initial creation
    conn1 = init_db(db_path)
    insert_video(
        conn1,
        source_path="/staging/v1.mp4",
        sha256="sha111",
        size_bytes=1000,
        status=VideoStatus.PENDING,
    )
    upsert_quota_log(conn1, "2026-11-07", units_used=1600, uploads_used=1)
    upsert_playlist(conn1, "Holiday 2026", "PL_12345")
    add_playlist_membership(conn1, 1, "Holiday 2026", added=True, position=0)
    conn1.close()

    # 2. Run init_db again on the existing file
    conn2 = init_db(db_path)

    # Verify existing video data remains intact
    video = get_video(conn2, 1)
    assert video is not None
    assert video.source_path == "/staging/v1.mp4"
    assert video.sha256 == "sha111"
    assert video.status == VideoStatus.PENDING

    # Verify quota log remains intact
    quota = get_quota_log(conn2, "2026-11-07")
    assert quota is not None
    assert quota["units_used"] == 1600
    assert quota["uploads_used"] == 1

    # Verify playlist remains intact
    pl = get_playlist(conn2, "Holiday 2026")
    assert pl is not None
    assert pl["youtube_playlist_id"] == "PL_12345"

    memberships = get_playlist_memberships(conn2, playlist_name="Holiday 2026")
    assert len(memberships) == 1
    assert memberships[0]["video_id"] == 1
    assert memberships[0]["added"] == 1

    # 3. Third run on already open connection
    init_db(conn2)
    conn2.close()


# --- Database Constraints & Integrity Tests ---

def test_unique_sha256_constraint(memory_db: sqlite3.Connection):
    """Duplicate SHA256 hashes must raise sqlite3.IntegrityError."""
    insert_video(
        memory_db,
        source_path="/staging/a.mp4",
        sha256="duplicate_sha",
        size_bytes=100,
    )
    with pytest.raises(sqlite3.IntegrityError):
        insert_video(
            memory_db,
            source_path="/staging/b.mp4",
            sha256="duplicate_sha",
            size_bytes=200,
        )


def test_status_check_constraint(memory_db: sqlite3.Connection):
    """Direct SQL insertion of an unknown status string violates the CHECK constraint."""
    with pytest.raises(sqlite3.IntegrityError):
        memory_db.execute(
            """
            INSERT INTO videos (
                source_path, sha256, size_bytes, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("/staging/c.mp4", "sha_c", 50, "BOGUS_STATUS", "2026-11-07", "2026-11-07"),
        )


def test_foreign_key_cascade_video_delete(memory_db: sqlite3.Connection):
    """Deleting a video cascades and removes playlist_membership rows."""
    vid_id = insert_video(
        memory_db,
        source_path="/staging/casc.mp4",
        sha256="sha_casc",
        size_bytes=500,
    )
    upsert_playlist(memory_db, "Cascade Playlist", "PL_casc")
    add_playlist_membership(memory_db, vid_id, "Cascade Playlist", added=False)

    assert len(get_playlist_memberships(memory_db, video_id=vid_id)) == 1

    # Delete video
    with memory_db:
        memory_db.execute("DELETE FROM videos WHERE id = ?", (vid_id,))

    # Membership should be automatically deleted
    assert len(get_playlist_memberships(memory_db, video_id=vid_id)) == 0


def test_foreign_key_playlist_constraint(memory_db: sqlite3.Connection):
    """Adding membership to a non-existent playlist violates foreign key constraint."""
    vid_id = insert_video(
        memory_db,
        source_path="/staging/fk.mp4",
        sha256="sha_fk",
        size_bytes=300,
    )
    with pytest.raises(sqlite3.IntegrityError):
        add_playlist_membership(memory_db, vid_id, "NonExistentPlaylist")


# --- Query Helpers & Ordering Tests ---

def test_get_videos_by_status_order(memory_db: sqlite3.Connection):
    """Pending videos should be returned ordered by taken_utc ascending."""
    insert_video(
        memory_db,
        source_path="/staging/video_b.mp4",
        sha256="sha_b",
        size_bytes=100,
        taken_utc="2021-06-15T10:00:00Z",
    )
    insert_video(
        memory_db,
        source_path="/staging/video_a.mp4",
        sha256="sha_a",
        size_bytes=100,
        taken_utc="2020-01-01T08:00:00Z",
    )
    insert_video(
        memory_db,
        source_path="/staging/video_c.mp4",
        sha256="sha_c",
        size_bytes=100,
        taken_utc="2022-12-25T18:00:00Z",
    )

    pending = get_videos_by_status(memory_db, VideoStatus.PENDING)
    assert len(pending) == 3
    assert [v.sha256 for v in pending] == ["sha_a", "sha_b", "sha_c"]

    # Test limit
    limited = get_videos_by_status(memory_db, VideoStatus.PENDING, limit=2)
    assert len(limited) == 2
    assert [v.sha256 for v in limited] == ["sha_a", "sha_b"]


def test_get_video_by_sha256(memory_db: sqlite3.Connection):
    """Retrieve video by SHA-256 works correctly."""
    vid_id = insert_video(
        memory_db,
        source_path="/staging/unique.mp4",
        sha256="sha_unique_test",
        size_bytes=1024,
        title="Sample Video",
    )
    rec = get_video_by_sha256(memory_db, "sha_unique_test")
    assert rec is not None
    assert rec.id == vid_id
    assert rec.title == "Sample Video"

    assert get_video_by_sha256(memory_db, "sha_does_not_exist") is None


def test_quota_log_upsert_increment(memory_db: sqlite3.Connection):
    """Quota usage accumulates correctly via ON CONFLICT DO UPDATE."""
    date_pac = "2026-11-07"
    upsert_quota_log(memory_db, date_pac, units_used=1600, uploads_used=1)
    row1 = get_quota_log(memory_db, date_pac)
    assert row1["units_used"] == 1600
    assert row1["uploads_used"] == 1

    # Increment again
    upsert_quota_log(memory_db, date_pac, units_used=1600, uploads_used=1)
    row2 = get_quota_log(memory_db, date_pac)
    assert row2["units_used"] == 3200
    assert row2["uploads_used"] == 2


def test_playlist_membership_update_added(memory_db: sqlite3.Connection):
    """Marking playlist membership as added modifies the row."""
    vid_id = insert_video(
        memory_db,
        source_path="/staging/mem.mp4",
        sha256="sha_mem",
        size_bytes=500,
        status=VideoStatus.UPLOADED,
    )
    upsert_playlist(memory_db, "Favorites", "PL_fav")
    add_playlist_membership(memory_db, vid_id, "Favorites", added=False, position=5)

    memberships = get_playlist_memberships(memory_db, playlist_name="Favorites")
    assert len(memberships) == 1
    assert memberships[0]["added"] == 0
    assert memberships[0]["position"] == 5

    mark_playlist_membership_added(memory_db, vid_id, "Favorites", added=True)
    updated = get_playlist_memberships(memory_db, playlist_name="Favorites")
    assert updated[0]["added"] == 1
