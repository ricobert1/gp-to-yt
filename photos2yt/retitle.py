"""Recompute titles of videos not yet uploaded (`photos2yt retitle`).

Titles are built once, at ingest. After changing youtube.title_template (or
default_title) in config.yaml, this rebuilds them with the same rules ingest
uses: Google Photos caption first, then a meaningful file name, else
default_title, put through the template.

Only rows that are PENDING, FAILED or SKIPPED and have no saved upload session
are changed. Rows uploading, uploaded or playlisted keep their titles: those
are on YouTube already (rename them in Studio) or, for UPLOADING, crash
recovery needs the exact title it sent. Database only; no API calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from photos2yt.config import Config
from photos2yt.db import RETITLABLE_STATUSES, VideoRecord, update_video_title
from photos2yt.metadata import format_title, parse_sidecar

Printer = Callable[[str], None]


@dataclass
class RetitlePlan:
    changes: list[tuple[VideoRecord, str]] = field(default_factory=list)
    unchanged: int = 0
    skipped: list[tuple[VideoRecord, str]] = field(default_factory=list)  # (row, reason)


def _local_dt(taken_utc: Optional[str], config: Config) -> Optional[datetime]:
    if not taken_utc:
        return None
    raw = taken_utc.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(config.tzinfo)


def new_title_for(video: VideoRecord, config: Config) -> tuple[Optional[str], Optional[str]]:
    """Return (title, None) or (None, reason it cannot be rebuilt)."""
    dt_local = _local_dt(video.taken_utc, config)
    if dt_local is None:
        return None, "no usable taken_utc"
    raw_title = caption = None
    if video.sidecar_path:
        meta = parse_sidecar(video.sidecar_path)
        if meta is None:
            # Without the sidecar the caption is unknown; keep the old title
            # rather than silently dropping a caption-based one.
            return None, f"sidecar not readable: {video.sidecar_path}"
        raw_title, caption = meta.title, meta.description
    title = format_title(
        template=config.youtube.title_template,
        default_title=config.youtube.default_title,
        raw_title=raw_title,
        taken_local_date=video.taken_local_date or dt_local.strftime("%Y-%m-%d"),
        dt_local=dt_local,
        video_stem=Path(video.source_path).stem,
        caption=caption,
    )
    return title, None


def build_retitle_plan(conn, config: Config) -> RetitlePlan:
    """Work out new titles. Only reads from ``conn``."""
    placeholders = ", ".join("?" for _ in RETITLABLE_STATUSES)
    rows = conn.execute(
        f"SELECT * FROM videos WHERE status IN ({placeholders}) "
        "AND upload_session_uri IS NULL ORDER BY taken_utc ASC, id ASC",
        RETITLABLE_STATUSES,
    ).fetchall()
    plan = RetitlePlan()
    for row in rows:
        video = VideoRecord.from_row(row)
        title, reason = new_title_for(video, config)
        if title is None:
            plan.skipped.append((video, reason or "unknown"))
        elif title == video.title:
            plan.unchanged += 1
        else:
            plan.changes.append((video, title))
    return plan


def print_plan(plan: RetitlePlan, out: Printer, *, dry_run: bool) -> None:
    prefix = "[DRY RUN] Would retitle" if dry_run else "Retitled"
    for video, title in plan.changes:
        out(f"  row {video.id}: {video.title!r} -> {title!r}")
    for video, reason in plan.skipped:
        out(f"  row {video.id}: kept {video.title!r} ({reason})")
    out(f"{prefix} {len(plan.changes)} video(s); {plan.unchanged} already up to date, "
        f"{len(plan.skipped)} kept as is.")


def apply_retitle(conn, plan: RetitlePlan) -> int:
    """Write the planned titles. Returns how many rows changed."""
    return sum(1 for video, title in plan.changes if update_video_title(conn, video.id, title))
