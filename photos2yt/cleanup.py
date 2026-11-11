"""Clean up local originals that are safely on YouTube (`photos2yt cleanup`).

Default action is to MOVE files out of the staging folder into
``paths.uploaded_dir`` (default: ``<staging_dir>-uploaded`` next to it),
keeping their sub-folders. ``--delete --yes`` deletes them permanently instead.
Google Photos itself cannot be cleaned up by any API; this only touches the
local Takeout copies.

A video's file is eligible only if ALL of these hold:
  * the row is UPLOADED or PLAYLISTED and has a YouTube video id;
  * ``photos2yt reconcile`` saw it on YouTube within ``--max-age-days``
    (default 7), i.e. ``verified_at`` is recent;
  * it has no open reconcile note (missing on YouTube, recordingDate or
    uploadStatus problem) in ``last_error``;
  * the file still exists, lies inside ``staging_dir`` and has the size
    recorded at ingest.
Duplicate files (SKIPPED rows whose ``duplicate_of`` is an eligible row) go
with their original. A sidecar JSON is moved only when every row that uses it
is eligible. Every move/delete is appended to ``cleanup_log.csv`` next to the
database. The database is only read, never changed.
"""

from __future__ import annotations

import csv
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from photos2yt.config import Config
from photos2yt.db import RECONCILE_NOTE_PREFIX, VideoRecord, VideoStatus

DEFAULT_MAX_AGE_DAYS = 7
LOG_NAME = "cleanup_log.csv"
LOG_COLUMNS = ("time_utc", "action", "row_id", "youtube_video_id", "source", "destination")

Printer = Callable[[str], None]


class CleanupError(RuntimeError):
    """Cleanup cannot run (old database, bad destination, ...)."""


def uploaded_dir(config: Config, override: Optional[Path] = None) -> Path:
    if override is not None:
        return Path(override).resolve()
    if config.paths.uploaded_dir is not None:
        return Path(config.paths.uploaded_dir).resolve()
    staging = Path(config.paths.staging_dir).resolve()
    return staging.parent / f"{staging.name}-uploaded"


@dataclass
class FileAction:
    source: Path
    destination: Optional[Path]  # None when deleting
    row_id: Optional[int]        # None for a sidecar
    youtube_video_id: str
    size: int
    why: str                     # "uploaded" / "duplicate of row N" / "sidecar"


@dataclass
class CleanupPlan:
    target_dir: Optional[Path]
    delete: bool
    actions: list[FileAction] = field(default_factory=list)
    not_eligible: list[tuple[VideoRecord, str]] = field(default_factory=list)
    problems: list[tuple[VideoRecord, str]] = field(default_factory=list)
    already_gone: int = 0

    @property
    def videos(self) -> list[FileAction]:
        return [a for a in self.actions if a.row_id is not None]

    @property
    def total_bytes(self) -> int:
        return sum(a.size for a in self.actions)


def _has_verified_at(conn) -> bool:
    return any(r[1] == "verified_at" for r in conn.execute("PRAGMA table_info(videos)"))


def _parse_utc(value: str) -> Optional[datetime]:
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.relative_to(folder)
        return True
    except ValueError:
        return False


def _eligibility(video: VideoRecord, cutoff: datetime, now: datetime) -> Optional[str]:
    """None if the uploaded row is confirmed safe, else the reason it is not."""
    if not video.youtube_video_id:
        return "no YouTube video id stored"
    if video.last_error and video.last_error.startswith(RECONCILE_NOTE_PREFIX):
        return f"open reconcile problem: {video.last_error}"
    if not video.verified_at:
        return "not confirmed by reconcile yet (run `photos2yt reconcile`)"
    when = _parse_utc(video.verified_at)
    if when is None:
        return f"unreadable verified_at {video.verified_at!r}"
    if when < cutoff:
        days = (now - when).days
        return f"last confirmed by reconcile {days} day(s) ago (run `photos2yt reconcile`)"
    return None


def build_plan(conn, config: Config, *, max_age_days: int = DEFAULT_MAX_AGE_DAYS,
               delete: bool = False, dest: Optional[Path] = None,
               now: Optional[datetime] = None) -> CleanupPlan:
    """Work out what cleanup would do. Only reads ``conn`` and the file system."""
    if not _has_verified_at(conn):
        raise CleanupError(
            "This database has no verified_at column yet. Run `photos2yt reconcile` once "
            "(it upgrades the database and confirms which videos are on YouTube)."
        )
    staging = Path(config.paths.staging_dir).resolve()
    target = None if delete else uploaded_dir(config, dest)
    if target is not None and (_inside(target, staging) or _inside(staging, target)):
        raise CleanupError(
            f"Destination {target} must not be inside the staging folder {staging} "
            "(or contain it): ingest would find the moved files again."
        )
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max_age_days)
    plan = CleanupPlan(target_dir=target, delete=delete)

    rows = [VideoRecord.from_row(r) for r in conn.execute("SELECT * FROM videos ORDER BY id")]
    by_id = {v.id: v for v in rows}

    eligible: dict[int, str] = {}  # row id -> why
    for v in rows:
        if v.status not in (VideoStatus.UPLOADED, VideoStatus.PLAYLISTED):
            continue
        reason = _eligibility(v, cutoff, now)
        if reason is None:
            eligible[v.id] = "uploaded"
        else:
            plan.not_eligible.append((v, reason))
    for v in rows:
        if v.duplicate_of is not None and v.duplicate_of in eligible and v.id not in eligible:
            eligible[v.id] = f"duplicate of row {v.duplicate_of}"

    for row_id, why in eligible.items():
        v = by_id[row_id]
        canonical = by_id.get(v.duplicate_of) if v.duplicate_of else v
        yt_id = (canonical.youtube_video_id if canonical else None) or ""
        src = Path(v.source_path)
        if not src.exists():
            plan.already_gone += 1
            continue
        src = src.resolve()
        if not _inside(src, staging):
            plan.problems.append((v, f"file is outside the staging folder: {src}"))
            continue
        size = src.stat().st_size
        if size != v.size_bytes:
            plan.problems.append((v, f"file size changed since ingest ({v.size_bytes} -> {size} bytes)"))
            continue
        destination = None if delete else target / src.relative_to(staging)
        if destination is not None and destination.exists():
            plan.problems.append((v, f"destination already exists: {destination}"))
            continue
        plan.actions.append(FileAction(src, destination, v.id, yt_id, size, why))

    moving = {a.row_id for a in plan.actions}
    users: dict[str, set[int]] = {}
    for v in rows:
        if v.sidecar_path:
            users.setdefault(v.sidecar_path, set()).add(v.id)
    for sidecar, row_ids in users.items():
        if not row_ids & moving or not row_ids <= set(eligible):
            continue  # nothing of it moves, or a row still needs it
        path = Path(sidecar)
        if not path.exists():
            continue
        path = path.resolve()
        if not _inside(path, staging):
            continue
        destination = None if delete else target / path.relative_to(staging)
        if destination is not None and destination.exists():
            continue
        plan.actions.append(FileAction(path, destination, None, "", path.stat().st_size, "sidecar"))
    return plan


def format_plan(plan: CleanupPlan, *, dry_run: bool) -> str:
    verb = "delete" if plan.delete else "move"
    head = "[DRY RUN] Would" if dry_run else "Will"
    lines = []
    if plan.delete:
        lines.append(f"{head} PERMANENTLY DELETE these originals:")
    else:
        lines.append(f"{head} move originals to {plan.target_dir}:")
    for a in plan.videos:
        lines.append(f"  row {a.row_id}  {a.youtube_video_id}  {a.size / 1_000_000:.1f} MB  "
                     f"{a.source.name}  ({a.why})")
    sidecars = [a for a in plan.actions if a.row_id is None]
    lines.append(f"  {len(plan.videos)} video file(s) and {len(sidecars)} sidecar(s) to {verb}, "
                 f"{plan.total_bytes / 1_000_000_000:.2f} GB.")
    if plan.already_gone:
        lines.append(f"  {plan.already_gone} eligible file(s) already gone from staging.")
    if plan.problems:
        lines.append(f"  Left alone ({len(plan.problems)}):")
        for v, why in plan.problems:
            lines.append(f"    row {v.id}  {Path(v.source_path).name}: {why}")
    if plan.not_eligible:
        lines.append(f"  Uploaded but not confirmed safe ({len(plan.not_eligible)}):")
        for v, why in plan.not_eligible[:20]:
            lines.append(f"    row {v.id}  {Path(v.source_path).name}: {why}")
        if len(plan.not_eligible) > 20:
            lines.append(f"    ... and {len(plan.not_eligible) - 20} more")
    return "\n".join(lines)


@dataclass
class CleanupSummary:
    done: int = 0
    failed: int = 0
    bytes_done: int = 0
    errors: list[str] = field(default_factory=list)


def apply_plan(plan: CleanupPlan, log_path: Path, *, out: Printer = print,
               now: Optional[datetime] = None) -> CleanupSummary:
    """Move or delete the planned files, logging each one to ``log_path``."""
    summary = CleanupSummary()
    new_log = not log_path.exists()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        if new_log:
            writer.writerow(LOG_COLUMNS)
        for a in plan.actions:
            try:
                if plan.delete:
                    a.source.unlink()
                    action = "deleted"
                else:
                    a.destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(a.source), str(a.destination))
                    action = "moved"
            except OSError as exc:
                summary.failed += 1
                message = f"could not {'delete' if plan.delete else 'move'} {a.source}: {exc}"
                summary.errors.append(message)
                out(f"  FAILED: {message}")
                continue
            stamp = (now or datetime.now(timezone.utc)).isoformat()
            writer.writerow([stamp, action, a.row_id if a.row_id is not None else "",
                             a.youtube_video_id, str(a.source),
                             str(a.destination) if a.destination else ""])
            f.flush()
            summary.done += 1
            summary.bytes_done += a.size
    out(f"Cleanup done: {summary.done} file(s) {'deleted' if plan.delete else 'moved'} "
        f"({summary.bytes_done / 1_000_000_000:.2f} GB), {summary.failed} failed. Log: {log_path}")
    return summary
