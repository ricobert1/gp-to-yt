"""Manifest and summary report (Task 8). Reads the database only.

``photos2yt report`` writes two files into the output directory:

* ``manifest.csv``: one row per video that is on YouTube (UPLOADED or
  PLAYLISTED with a video id): original filename, video id, URL
  (``https://youtu.be/<id>``), privacy (``actual_privacy`` as last read back
  from YouTube), playlists (those with ``playlist_membership.added = 1``),
  then status, local date, full source path and any attention note.
* ``summary.txt``: counts by status, quota used today (local counter, Pacific
  day) and the items needing attention. The same text is printed.

Nothing here calls the API; run ``photos2yt reconcile`` first for fresh
privacy and missing-video information.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePath
from typing import Callable, Optional

from photos2yt.config import Config
from photos2yt.db import RECONCILE_NOTE_PREFIX, VideoRecord, VideoStatus, get_quota_log
from photos2yt.quota import _pacific_today

MANIFEST_NAME = "manifest.csv"
SUMMARY_NAME = "summary.txt"
MANIFEST_COLUMNS = (
    "original_filename",
    "video_id",
    "url",
    "privacy",
    "playlists",
    "status",
    "taken_local_date",
    "source_path",
    "attention",
)
ON_YOUTUBE = (VideoStatus.UPLOADED, VideoStatus.PLAYLISTED)


def video_url(video_id: str) -> str:
    return f"https://youtu.be/{video_id}"


def _filename(source_path: str) -> str:
    # Paths may come from another OS (e.g. ingested on Windows, reported on the NAS).
    name = PurePath(source_path).name
    return name.rsplit("\\", 1)[-1] if "\\" in name else name


@dataclass
class AttentionItem:
    row_id: int
    filename: str
    problem: str


@dataclass
class Report:
    status_counts: dict[str, int]
    duplicates_skipped: int
    quota_date: str
    units_used: int
    unit_limit: int
    uploads_used: int
    upload_limit: Optional[int]
    quota_exhausted: bool
    manifest_rows: list[dict[str, str]] = field(default_factory=list)
    attention: list[AttentionItem] = field(default_factory=list)


def _playlists_by_video(conn) -> dict[int, list[str]]:
    rows = conn.execute(
        """
        SELECT pm.video_id, p.name
        FROM playlist_membership pm
        JOIN playlists p ON p.name = pm.playlist_name
        WHERE pm.added = 1
        ORDER BY p.name ASC
        """
    ).fetchall()
    out: dict[int, list[str]] = {}
    for r in rows:
        out.setdefault(r["video_id"], []).append(r["name"])
    return out


def _pending_adds(conn) -> dict[int, list[str]]:
    rows = conn.execute(
        "SELECT video_id, playlist_name FROM playlist_membership WHERE added = 0 "
        "ORDER BY playlist_name ASC"
    ).fetchall()
    out: dict[int, list[str]] = {}
    for r in rows:
        out.setdefault(r["video_id"], []).append(r["playlist_name"])
    return out


def _problems(video: VideoRecord, config: Config, pending: dict[int, list[str]]) -> list[str]:
    """Why a row needs a human to look at it (empty list = fine)."""
    problems: list[str] = []
    status = video.status
    if status == VideoStatus.FAILED:
        problems.append(f"FAILED: {video.last_error or 'no error recorded'}")
    elif status == VideoStatus.UPLOADING:
        problems.append("upload interrupted (UPLOADING); the next `upload` run recovers it")
    elif status == VideoStatus.SKIPPED and video.duplicate_of is None:
        problems.append(f"SKIPPED: {video.last_error or 'no reason recorded'}")
    elif status in ON_YOUTUBE:
        if not video.youtube_video_id:
            problems.append(f"{status.value} but no YouTube video id stored")
        if video.last_error and video.last_error.startswith(RECONCILE_NOTE_PREFIX):
            problems.append(video.last_error)
        requested = config.youtube.privacy
        if video.actual_privacy is None:
            problems.append("privacy not verified yet (run `reconcile`)")
        elif video.actual_privacy != requested:
            problems.append(
                f"privacy is '{video.actual_privacy}', config requests '{requested}'"
            )
        for name in pending.get(video.id, []):
            problems.append(f"playlist add to '{name}' not done yet (run `curate`)")
    return problems


def build_report(conn, config: Config, *, clock: Optional[Callable[[], datetime]] = None) -> Report:
    """Collect everything for the manifest and summary. Only reads ``conn``."""
    counts = {s.value: 0 for s in VideoStatus}
    for r in conn.execute("SELECT status, COUNT(*) AS n FROM videos GROUP BY status"):
        counts[r["status"]] = r["n"]
    dups = conn.execute(
        "SELECT COUNT(*) FROM videos WHERE status = 'SKIPPED' AND duplicate_of IS NOT NULL"
    ).fetchone()[0]

    quota_date = _pacific_today(clock)
    q = get_quota_log(conn, quota_date)
    exhausted = bool(q is not None and "exhausted_at" in q.keys() and q["exhausted_at"])
    report = Report(
        status_counts=counts,
        duplicates_skipped=dups,
        quota_date=quota_date,
        units_used=q["units_used"] if q is not None else 0,
        unit_limit=config.quota.daily_unit_limit,
        uploads_used=q["uploads_used"] if q is not None else 0,
        upload_limit=config.quota.daily_upload_limit,
        quota_exhausted=exhausted,
    )

    playlists = _playlists_by_video(conn)
    pending = _pending_adds(conn)
    videos = [
        VideoRecord.from_row(r)
        for r in conn.execute(
            "SELECT * FROM videos ORDER BY (taken_utc IS NULL), taken_utc ASC, id ASC"
        )
    ]
    for v in videos:
        problems = _problems(v, config, pending)
        filename = _filename(v.source_path)
        for p in problems:
            report.attention.append(AttentionItem(v.id, filename, p))
        if v.status in ON_YOUTUBE and v.youtube_video_id:
            report.manifest_rows.append({
                "original_filename": filename,
                "video_id": v.youtube_video_id,
                "url": video_url(v.youtube_video_id),
                "privacy": v.actual_privacy or "",
                "playlists": "; ".join(playlists.get(v.id, [])),
                "status": v.status.value,
                "taken_local_date": v.taken_local_date or "",
                "source_path": v.source_path,
                "attention": " | ".join(problems),
            })
    return report


def format_summary(report: Report) -> str:
    lines = ["photos2yt report", "", "Videos by status:"]
    total = 0
    for status, n in report.status_counts.items():
        total += n
        extra = ""
        if status == VideoStatus.SKIPPED.value and report.duplicates_skipped:
            extra = f"  ({report.duplicates_skipped} duplicate(s) of another file)"
        lines.append(f"  {status:<11} {n}{extra}")
    lines.append(f"  {'TOTAL':<11} {total}")
    lines.append(f"  On YouTube (in manifest): {len(report.manifest_rows)}")
    lines.append("")
    lines.append(f"Quota today ({report.quota_date}, Pacific; local counter):")
    lines.append(f"  units used: {report.units_used} of {report.unit_limit}")
    uploads = f"  uploads:    {report.uploads_used}"
    if report.upload_limit is not None:
        uploads += f" of {report.upload_limit}"
    lines.append(uploads)
    if report.quota_exhausted:
        lines.append("  marked exhausted (YouTube returned quotaExceeded today)")
    lines.append("")
    if report.attention:
        lines.append(f"Needs attention ({len(report.attention)}):")
        for a in report.attention:
            lines.append(f"  row {a.row_id}  {a.filename}: {a.problem}")
    else:
        lines.append("Needs attention: nothing.")
    return "\n".join(lines)


def write_manifest(report: Report, path: Path) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=MANIFEST_COLUMNS)
        writer.writeheader()
        writer.writerows(report.manifest_rows)


def write_report(conn, config: Config, out_dir: Path, *,
                 clock: Optional[Callable[[], datetime]] = None) -> tuple[Report, Path, Path]:
    """Build the report and write manifest.csv and summary.txt into ``out_dir``."""
    report = build_report(conn, config, clock=clock)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = out_dir / MANIFEST_NAME
    summary = out_dir / SUMMARY_NAME
    write_manifest(report, manifest)
    summary.write_text(format_summary(report) + "\n", encoding="utf-8")
    return report, manifest, summary
