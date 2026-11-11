"""Database schema, queue management, and state machine for photos2yt."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
import sqlite3
from typing import Any, Optional, Sequence


class VideoStatus(str, Enum):
    """Lifecycle status values for a video in the processing queue."""
    PENDING = "PENDING"
    UPLOADING = "UPLOADING"
    UPLOADED = "UPLOADED"
    PLAYLISTED = "PLAYLISTED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


# Legal status transitions matrix:
# PENDING -> UPLOADING, SKIPPED, FAILED
# UPLOADING -> UPLOADED, PENDING (retry/interrupted/quota), FAILED
# UPLOADED -> PLAYLISTED
# FAILED -> PENDING (re-queued/retry)
# SKIPPED -> PENDING (manual un-skip/re-queue)
# PLAYLISTED -> (terminal state, no outbound transitions)
LEGAL_TRANSITIONS: dict[VideoStatus, frozenset[VideoStatus]] = {
    VideoStatus.PENDING: frozenset({
        VideoStatus.UPLOADING,
        VideoStatus.SKIPPED,
        VideoStatus.FAILED,
    }),
    VideoStatus.UPLOADING: frozenset({
        VideoStatus.UPLOADED,
        VideoStatus.PENDING,
        VideoStatus.FAILED,
    }),
    VideoStatus.UPLOADED: frozenset({
        VideoStatus.PLAYLISTED,
    }),
    VideoStatus.PLAYLISTED: frozenset(),
    VideoStatus.FAILED: frozenset({
        VideoStatus.PENDING,
    }),
    VideoStatus.SKIPPED: frozenset({
        VideoStatus.PENDING,
    }),
}


class DatabaseError(Exception):
    """Base exception for photos2yt database operations."""
    pass


class InvalidStateTransitionError(DatabaseError):
    """Raised when an illegal video status transition is attempted."""
    pass


class VideoNotFoundError(DatabaseError):
    """Raised when a video record cannot be found in the database."""
    pass


def _iso_now() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


def is_valid_transition(current: VideoStatus | str, target: VideoStatus | str) -> bool:
    """Check if transitioning from current to target status is permitted."""
    try:
        curr_enum = VideoStatus(current)
        tgt_enum = VideoStatus(target)
    except ValueError:
        return False
    return tgt_enum in LEGAL_TRANSITIONS.get(curr_enum, frozenset())


def validate_transition(current: VideoStatus | str, target: VideoStatus | str) -> None:
    """Validate that transition from current to target status is legal.
    
    Raises:
        InvalidStateTransitionError: If the transition is illegal or status is invalid.
    """
    try:
        curr_enum = VideoStatus(current)
    except ValueError:
        raise InvalidStateTransitionError(f"Invalid current status: {current!r}")

    try:
        tgt_enum = VideoStatus(target)
    except ValueError:
        raise InvalidStateTransitionError(f"Invalid target status: {target!r}")

    if tgt_enum not in LEGAL_TRANSITIONS.get(curr_enum, frozenset()):
        raise InvalidStateTransitionError(
            f"Illegal state transition: cannot transition from {curr_enum.value} to {tgt_enum.value}"
        )


@dataclass(frozen=True)
class VideoRecord:
    """Represents a row in the `videos` table."""
    id: int
    source_path: str
    sha256: str
    size_bytes: int
    status: VideoStatus
    created_at: str
    updated_at: str
    sidecar_path: Optional[str] = None
    duration_s: Optional[float] = None
    taken_utc: Optional[str] = None
    taken_local_date: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    tags_json: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    attempts: int = 0
    last_error: Optional[str] = None
    upload_session_uri: Optional[str] = None
    youtube_video_id: Optional[str] = None
    actual_privacy: Optional[str] = None
    duplicate_of: Optional[int] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row | dict[str, Any]) -> VideoRecord:
        data = dict(row)
        return cls(
            id=data["id"],
            source_path=data["source_path"],
            sha256=data["sha256"],
            size_bytes=data["size_bytes"],
            status=VideoStatus(data["status"]),
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            sidecar_path=data.get("sidecar_path"),
            duration_s=data.get("duration_s"),
            taken_utc=data.get("taken_utc"),
            taken_local_date=data.get("taken_local_date"),
            title=data.get("title"),
            description=data.get("description"),
            tags_json=data.get("tags_json"),
            lat=data.get("lat"),
            lon=data.get("lon"),
            attempts=data.get("attempts", 0),
            last_error=data.get("last_error"),
            upload_session_uri=data.get("upload_session_uri"),
            youtube_video_id=data.get("youtube_video_id"),
            actual_privacy=data.get("actual_privacy"),
            duplicate_of=data.get("duplicate_of"),
        )


SCHEMA_VERSION = 3

MIGRATIONS: dict[int, list[str]] = {
    1: [
        """
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path TEXT NOT NULL,
            sidecar_path TEXT,
            sha256 TEXT NOT NULL UNIQUE,
            size_bytes INTEGER NOT NULL,
            duration_s REAL,
            taken_utc TEXT,
            taken_local_date TEXT,
            title TEXT,
            description TEXT,
            tags_json TEXT,
            lat REAL,
            lon REAL,
            status TEXT NOT NULL CHECK (status IN ('PENDING', 'UPLOADING', 'UPLOADED', 'PLAYLISTED', 'FAILED', 'SKIPPED')),
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            upload_session_uri TEXT,
            youtube_video_id TEXT,
            actual_privacy TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """,
        "CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);",
        "CREATE INDEX IF NOT EXISTS idx_videos_taken_utc ON videos(taken_utc);",
        """
        CREATE TABLE IF NOT EXISTS quota_log (
            date_pacific TEXT PRIMARY KEY,
            units_used INTEGER NOT NULL DEFAULT 0,
            uploads_used INTEGER NOT NULL DEFAULT 0
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS playlists (
            name TEXT PRIMARY KEY,
            youtube_playlist_id TEXT NOT NULL
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS playlist_membership (
            video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            playlist_name TEXT NOT NULL REFERENCES playlists(name) ON DELETE CASCADE,
            added INTEGER NOT NULL DEFAULT 0,
            position INTEGER,
            PRIMARY KEY (video_id, playlist_name)
        );
        """,
        "CREATE INDEX IF NOT EXISTS idx_playlist_membership_video ON playlist_membership(video_id);",
        "CREATE INDEX IF NOT EXISTS idx_playlist_membership_playlist ON playlist_membership(playlist_name);",
    ],
    2: [
        """
        CREATE TABLE videos_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path TEXT NOT NULL UNIQUE,
            sidecar_path TEXT,
            sha256 TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            duration_s REAL,
            taken_utc TEXT,
            taken_local_date TEXT,
            title TEXT,
            description TEXT,
            tags_json TEXT,
            lat REAL,
            lon REAL,
            status TEXT NOT NULL CHECK (status IN ('PENDING', 'UPLOADING', 'UPLOADED', 'PLAYLISTED', 'FAILED', 'SKIPPED')),
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            upload_session_uri TEXT,
            youtube_video_id TEXT,
            actual_privacy TEXT,
            duplicate_of INTEGER REFERENCES videos(id),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """,
        """
        INSERT INTO videos_new (
            id, source_path, sidecar_path, sha256, size_bytes, duration_s,
            taken_utc, taken_local_date, title, description, tags_json,
            lat, lon, status, attempts, last_error, upload_session_uri,
            youtube_video_id, actual_privacy, duplicate_of, created_at, updated_at
        )
        SELECT
            v.id,
            v.source_path,
            v.sidecar_path,
            CASE WHEN instr(v.sha256, ':dup:') > 0 THEN substr(v.sha256, 1, instr(v.sha256, ':dup:') - 1) ELSE v.sha256 END,
            v.size_bytes,
            v.duration_s,
            v.taken_utc,
            v.taken_local_date,
            v.title,
            v.description,
            v.tags_json,
            v.lat,
            v.lon,
            v.status,
            v.attempts,
            v.last_error,
            v.upload_session_uri,
            v.youtube_video_id,
            v.actual_privacy,
            CASE WHEN instr(v.sha256, ':dup:') > 0 THEN (
                SELECT orig.id FROM videos orig
                WHERE orig.sha256 = substr(v.sha256, 1, instr(v.sha256, ':dup:') - 1)
                LIMIT 1
            ) ELSE NULL END,
            v.created_at,
            v.updated_at
        FROM videos v;
        """,
        "DROP TABLE videos;",
        "ALTER TABLE videos_new RENAME TO videos;",
        "CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);",
        "CREATE INDEX IF NOT EXISTS idx_videos_taken_utc ON videos(taken_utc);",
        "CREATE INDEX IF NOT EXISTS idx_videos_sha256 ON videos(sha256);",
        "CREATE INDEX IF NOT EXISTS idx_videos_duplicate_of ON videos(duplicate_of);",
    ],
    3: [
        # Add exhausted_at column to quota_log.
        # Nullable TEXT stores an ISO-8601 UTC timestamp of when quota was
        # marked exhausted for that Pacific date.  The migration version guard
        # in init_db ensures this ALTER runs at most once per database.
        """
        ALTER TABLE quota_log ADD COLUMN exhausted_at TEXT;
        """,
    ],
}


def get_connection(db_path: str | Path) -> sqlite3.Connection:
    """Open or create a SQLite database connection with row factory and PRAGMA settings."""
    db_str = str(db_path)
    if db_str != ":memory:":
        Path(db_path).resolve().parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_str)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    if db_str != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL;")
    return conn


def init_db(conn_or_path: sqlite3.Connection | str | Path) -> sqlite3.Connection:
    """Initialize database tables and run pending migrations idempotently.
    
    Can be called repeatedly on new or existing databases without altering existing data.
    """
    if isinstance(conn_or_path, (str, Path)):
        conn = get_connection(conn_or_path)
    else:
        conn = conn_or_path

    fk_cursor = conn.execute("PRAGMA foreign_keys;")
    fk_enabled = bool(fk_cursor.fetchone()[0])
    if fk_enabled:
        conn.execute("PRAGMA foreign_keys = OFF;")

    try:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                """
            )

            cursor = conn.cursor()
            cursor.execute("SELECT version FROM schema_migrations ORDER BY version ASC")
            applied = {row[0] for row in cursor.fetchall()}

            for version in sorted(MIGRATIONS.keys()):
                if version not in applied:
                    for statement in MIGRATIONS[version]:
                        conn.execute(statement)
                    conn.execute(
                        "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                        (version, _iso_now()),
                    )
    finally:
        if fk_enabled:
            conn.execute("PRAGMA foreign_keys = ON;")

    return conn


# --- CRUD & Transition Helpers ---

def insert_video(
    conn: sqlite3.Connection,
    *,
    source_path: str,
    sha256: str,
    size_bytes: int,
    status: VideoStatus | str = VideoStatus.PENDING,
    sidecar_path: Optional[str] = None,
    duration_s: Optional[float] = None,
    taken_utc: Optional[str] = None,
    taken_local_date: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    tags_json: Optional[str] = None,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    last_error: Optional[str] = None,
    duplicate_of: Optional[int] = None,
) -> int:
    """Insert a new video record into the queue.
    
    Returns:
        The newly inserted video record id.
    """
    status_str = VideoStatus(status).value
    now = _iso_now()
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO videos (
                source_path, sidecar_path, sha256, size_bytes, duration_s,
                taken_utc, taken_local_date, title, description, tags_json,
                lat, lon, status, attempts, last_error, upload_session_uri,
                youtube_video_id, actual_privacy, duplicate_of, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, NULL, NULL, NULL, ?, ?, ?)
            """,
            (
                source_path,
                sidecar_path,
                sha256,
                size_bytes,
                duration_s,
                taken_utc,
                taken_local_date,
                title,
                description,
                tags_json,
                lat,
                lon,
                status_str,
                last_error,
                duplicate_of,
                now,
                now,
            ),
        )
        return int(cursor.lastrowid)


def get_video(conn: sqlite3.Connection, video_id: int) -> Optional[VideoRecord]:
    """Retrieve a single video record by its primary key ID."""
    cursor = conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,))
    row = cursor.fetchone()
    if row is None:
        return None
    return VideoRecord.from_row(row)


def get_video_by_source_path(conn: sqlite3.Connection, source_path: str) -> Optional[VideoRecord]:
    """Retrieve a single video record by its source_path."""
    cursor = conn.execute("SELECT * FROM videos WHERE source_path = ?", (source_path,))
    row = cursor.fetchone()
    if row is None:
        return None
    return VideoRecord.from_row(row)


def get_video_by_sha256(conn: sqlite3.Connection, sha256: str) -> Optional[VideoRecord]:
    """Retrieve a video record by SHA-256 hash, returning the canonical record if duplicates exist."""
    cursor = conn.execute(
        "SELECT * FROM videos WHERE sha256 = ? ORDER BY (duplicate_of IS NOT NULL), id ASC LIMIT 1",
        (sha256,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return VideoRecord.from_row(row)


def get_canonical_video_by_sha256(conn: sqlite3.Connection, sha256: str) -> Optional[VideoRecord]:
    """Retrieve the canonical video record for a given SHA-256 hash."""
    cursor = conn.execute(
        "SELECT * FROM videos WHERE sha256 = ? AND duplicate_of IS NULL ORDER BY id ASC LIMIT 1",
        (sha256,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return VideoRecord.from_row(row)


def get_videos_by_sha256(conn: sqlite3.Connection, sha256: str) -> list[VideoRecord]:
    """Retrieve all video records matching a SHA-256 hash."""
    cursor = conn.execute(
        "SELECT * FROM videos WHERE sha256 = ? ORDER BY (duplicate_of IS NOT NULL), id ASC",
        (sha256,),
    )
    return [VideoRecord.from_row(row) for row in cursor.fetchall()]


def get_videos_by_status(
    conn: sqlite3.Connection,
    status: VideoStatus | str,
    *,
    limit: Optional[int] = None,
) -> list[VideoRecord]:
    """Retrieve videos with a specific status, ordered by taken_utc ascending (nulls last)."""
    status_str = VideoStatus(status).value
    query = """
        SELECT * FROM videos
        WHERE status = ?
        ORDER BY (taken_utc IS NULL), taken_utc ASC, id ASC
    """
    params: list[Any] = [status_str]
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)

    cursor = conn.execute(query, params)
    return [VideoRecord.from_row(row) for row in cursor.fetchall()]


def transition_video_status(
    conn: sqlite3.Connection,
    video_id: int,
    new_status: VideoStatus | str,
    *,
    error: Optional[str] = None,
    upload_session_uri: Optional[str] = None,
    clear_session_uri: bool = False,
    youtube_video_id: Optional[str] = None,
    actual_privacy: Optional[str] = None,
    increment_attempts: bool = False,
) -> VideoRecord:
    """Transition a video to a new status, verifying the legality of the transition.
    
    Raises:
        VideoNotFoundError: If no video with video_id exists.
        InvalidStateTransitionError: If the transition is not allowed.
    """
    target_enum = VideoStatus(new_status)
    now = _iso_now()

    with conn:
        cursor = conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,))
        row = cursor.fetchone()
        if row is None:
            raise VideoNotFoundError(f"Video with id {video_id} not found")

        current_enum = VideoStatus(row["status"])
        validate_transition(current_enum, target_enum)

        # Build update fields dynamically
        updates = ["status = ?", "updated_at = ?"]
        params: list[Any] = [target_enum.value, now]

        if increment_attempts:
            updates.append("attempts = attempts + 1")

        if error is not None:
            updates.append("last_error = ?")
            params.append(error)

        if clear_session_uri:
            updates.append("upload_session_uri = NULL")
        elif upload_session_uri is not None:
            updates.append("upload_session_uri = ?")
            params.append(upload_session_uri)

        if youtube_video_id is not None:
            updates.append("youtube_video_id = ?")
            params.append(youtube_video_id)

        if actual_privacy is not None:
            updates.append("actual_privacy = ?")
            params.append(actual_privacy)

        params.append(video_id)
        sql = f"UPDATE videos SET {', '.join(updates)} WHERE id = ?"
        conn.execute(sql, params)

        cursor = conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,))
        updated_row = cursor.fetchone()
        return VideoRecord.from_row(updated_row)


def update_upload_session(
    conn: sqlite3.Connection,
    video_id: int,
    session_uri: Optional[str],
) -> None:
    """Persist (or clear, when ``session_uri`` is None) the resumable upload
    session URI for a video.

    The resumable upload protocol returns the session URI (Location header)
    as soon as the upload session is created.  Persisting it lets a crashed
    or interrupted run resume the transfer instead of starting over.

    Only rows currently in UPLOADING status are touched; other statuses are
    left alone (the session URI is owned by the upload lifecycle and is
    cleared explicitly on completion or failure via transition_video_status).
    The URI itself is never logged.

    Raises:
        VideoNotFoundError: If no video with video_id exists.
    """
    with conn:
        cursor = conn.execute("SELECT id FROM videos WHERE id = ?", (video_id,))
        if cursor.fetchone() is None:
            raise VideoNotFoundError(f"Video with id {video_id} not found")

        conn.execute(
            """
            UPDATE videos
            SET upload_session_uri = ?, updated_at = ?
            WHERE id = ? AND status = 'UPLOADING'
            """,
            (session_uri, _iso_now(), video_id),
        )


def update_video_verification(
    conn: sqlite3.Connection,
    video_id: int,
    *,
    actual_privacy: Optional[str],
) -> VideoRecord:
    """Store the post-upload read-back result (actual privacy) without changing status.

    Only valid for videos that have been uploaded (UPLOADED or PLAYLISTED).

    Raises:
        VideoNotFoundError: If no video with video_id exists.
        DatabaseError: If the video has not been uploaded.
    """
    with conn:
        row = conn.execute(
            "SELECT status FROM videos WHERE id = ?", (video_id,)
        ).fetchone()
        if row is None:
            raise VideoNotFoundError(f"Video with id {video_id} not found")
        if row["status"] not in (VideoStatus.UPLOADED.value, VideoStatus.PLAYLISTED.value):
            raise DatabaseError(
                f"Cannot store verification for video {video_id}: status is {row['status']}"
            )
        conn.execute(
            "UPDATE videos SET actual_privacy = ?, updated_at = ? WHERE id = ?",
            (actual_privacy, _iso_now(), video_id),
        )
        updated = conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
        return VideoRecord.from_row(updated)


def get_video_by_youtube_id(
    conn: sqlite3.Connection, youtube_video_id: str
) -> Optional[VideoRecord]:
    """Retrieve the video record that holds a given YouTube video id, if any."""
    row = conn.execute(
        "SELECT * FROM videos WHERE youtube_video_id = ? ORDER BY id ASC LIMIT 1",
        (youtube_video_id,),
    ).fetchone()
    if row is None:
        return None
    return VideoRecord.from_row(row)


def get_readonly_connection(db_path: str | Path) -> sqlite3.Connection:
    """Open an existing database strictly read-only (never creates or migrates it).

    Used by --dry-run. Raises FileNotFoundError if the database file does not exist.
    """
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"Database not found: {path}")
    # Even mode=ro creates -wal/-shm side files for a WAL database. When no WAL
    # file exists (the normal state after a clean close) everything is in the
    # main file, so open it immutable: SQLite then creates no files at all.
    # If a -wal file is present, it holds data not yet checkpointed, so use
    # plain read-only mode to see it (its side files already exist).
    wal = path.with_name(path.name + "-wal")
    flags = "mode=ro" if wal.exists() else "mode=ro&immutable=1"
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?{flags}", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# --- Quota Helpers ---

def get_quota_log(conn: sqlite3.Connection, date_pacific: str) -> Optional[sqlite3.Row]:
    """Retrieve the quota log row for a specific Pacific date (YYYY-MM-DD)."""
    cursor = conn.execute(
        "SELECT * FROM quota_log WHERE date_pacific = ?",
        (date_pacific,),
    )
    return cursor.fetchone()


def upsert_quota_log(
    conn: sqlite3.Connection,
    date_pacific: str,
    *,
    units_used: int = 0,
    uploads_used: int = 0,
) -> None:
    """Insert or increment quota usage for a given Pacific date."""
    with conn:
        conn.execute(
            """
            INSERT INTO quota_log (date_pacific, units_used, uploads_used)
            VALUES (?, ?, ?)
            ON CONFLICT(date_pacific) DO UPDATE SET
                units_used = units_used + excluded.units_used,
                uploads_used = uploads_used + excluded.uploads_used
            """,
            (date_pacific, units_used, uploads_used),
        )


def set_quota_exhausted(
    conn: sqlite3.Connection,
    date_pacific: str,
    exhausted_at: Optional[str],
) -> None:
    """Set or clear the exhausted_at timestamp on today's quota_log row.

    Ensures the row exists first (via INSERT OR IGNORE), then sets exhausted_at.
    Pass None to clear the exhausted flag (e.g., after quota resets).
    """
    with conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO quota_log (date_pacific, units_used, uploads_used)
            VALUES (?, 0, 0)
            """,
            (date_pacific,),
        )
        conn.execute(
            "UPDATE quota_log SET exhausted_at = ? WHERE date_pacific = ?",
            (exhausted_at, date_pacific),
        )


# --- Playlist Helpers ---

def upsert_playlist(
    conn: sqlite3.Connection,
    name: str,
    youtube_playlist_id: str,
) -> None:
    """Insert or update a playlist entry."""
    with conn:
        conn.execute(
            """
            INSERT INTO playlists (name, youtube_playlist_id)
            VALUES (?, ?)
            ON CONFLICT(name) DO UPDATE SET
                youtube_playlist_id = excluded.youtube_playlist_id
            """,
            (name, youtube_playlist_id),
        )


def get_playlist(conn: sqlite3.Connection, name: str) -> Optional[sqlite3.Row]:
    """Get playlist by name."""
    cursor = conn.execute("SELECT * FROM playlists WHERE name = ?", (name,))
    return cursor.fetchone()


def add_playlist_membership(
    conn: sqlite3.Connection,
    video_id: int,
    playlist_name: str,
    *,
    added: bool = False,
    position: Optional[int] = None,
) -> None:
    """Add or update a video's membership in a playlist."""
    with conn:
        conn.execute(
            """
            INSERT INTO playlist_membership (video_id, playlist_name, added, position)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(video_id, playlist_name) DO UPDATE SET
                added = excluded.added,
                position = excluded.position
            """,
            (video_id, playlist_name, 1 if added else 0, position),
        )


def get_playlist_memberships(
    conn: sqlite3.Connection,
    *,
    video_id: Optional[int] = None,
    playlist_name: Optional[str] = None,
) -> list[sqlite3.Row]:
    """Query playlist memberships by video_id, playlist_name, or all."""
    query = "SELECT * FROM playlist_membership WHERE 1=1"
    params: list[Any] = []
    if video_id is not None:
        query += " AND video_id = ?"
        params.append(video_id)
    if playlist_name is not None:
        query += " AND playlist_name = ?"
        params.append(playlist_name)
    query += " ORDER BY playlist_name ASC, position ASC, video_id ASC"

    cursor = conn.execute(query, params)
    return cursor.fetchall()


def mark_playlist_membership_added(
    conn: sqlite3.Connection,
    video_id: int,
    playlist_name: str,
    added: bool = True,
) -> None:
    """Mark a video as added (or unadded) in a playlist."""
    with conn:
        conn.execute(
            """
            UPDATE playlist_membership
            SET added = ?
            WHERE video_id = ? AND playlist_name = ?
            """,
            (1 if added else 0, video_id, playlist_name),
        )


def record_playlist_item_added(
    conn: sqlite3.Connection,
    video_id: int,
    playlist_name: str,
    position: int,
) -> None:
    """Record that a video was inserted into a playlist at ``position``.

    In one transaction: every other added member of the playlist at or after
    ``position`` moves down by one (mirroring what YouTube does to the
    playlist), then this video's membership row is upserted with added=1.
    """
    with conn:
        conn.execute(
            """
            UPDATE playlist_membership
            SET position = position + 1
            WHERE playlist_name = ? AND added = 1 AND video_id != ?
              AND position IS NOT NULL AND position >= ?
            """,
            (playlist_name, video_id, position),
        )
        conn.execute(
            """
            INSERT INTO playlist_membership (video_id, playlist_name, added, position)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(video_id, playlist_name) DO UPDATE SET
                added = 1,
                position = excluded.position
            """,
            (video_id, playlist_name, position),
        )


# Statuses whose title may still be changed locally: not on YouTube yet and no
# upload in progress. UPLOADING is excluded on purpose: crash recovery finds an
# already-uploaded video by its exact title.
RETITLABLE_STATUSES = ("PENDING", "FAILED", "SKIPPED")


def update_video_title(conn: sqlite3.Connection, video_id: int, title: str) -> bool:
    """Set a row's title if it has not started uploading. Returns True if changed.

    Only rows in RETITLABLE_STATUSES with no saved resumable session are
    touched: a saved session already carries the old title to YouTube.
    """
    placeholders = ", ".join("?" for _ in RETITLABLE_STATUSES)
    with conn:
        cur = conn.execute(
            f"""
            UPDATE videos SET title = ?, updated_at = ?
            WHERE id = ? AND status IN ({placeholders})
              AND upload_session_uri IS NULL AND (title IS NULL OR title != ?)
            """,
            (title, _iso_now(), video_id, *RETITLABLE_STATUSES, title),
        )
    return cur.rowcount == 1


# --- Reconcile notes (Task 8) ---

# last_error on an UPLOADED/PLAYLISTED row is reused for problems found by
# `photos2yt reconcile` (missing on YouTube, recordingDate not right). Notes
# start with this prefix so a later clean reconcile clears only its own note.
RECONCILE_NOTE_PREFIX = "[reconcile"


def set_reconcile_note(conn: sqlite3.Connection, video_id: int, note: Optional[str]) -> bool:
    """Store (or clear, when ``note`` is None) a reconcile note in last_error.

    Never changes status. Only rows that are UPLOADED or PLAYLISTED are
    touched. Clearing removes last_error only if it is a reconcile note.
    Returns True if the row changed.
    """
    with conn:
        if note is None:
            cur = conn.execute(
                """
                UPDATE videos SET last_error = NULL, updated_at = ?
                WHERE id = ? AND status IN ('UPLOADED', 'PLAYLISTED')
                  AND last_error LIKE ? || '%'
                """,
                (_iso_now(), video_id, RECONCILE_NOTE_PREFIX),
            )
        else:
            cur = conn.execute(
                """
                UPDATE videos SET last_error = ?, updated_at = ?
                WHERE id = ? AND status IN ('UPLOADED', 'PLAYLISTED')
                  AND (last_error IS NULL OR last_error != ?)
                """,
                (note, _iso_now(), video_id, note),
            )
    return cur.rowcount == 1
