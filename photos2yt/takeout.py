"""Takeout ingest, sidecar matching, ffprobe extraction, and ingest reporting."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
from typing import Any, Optional, Sequence

from photos2yt.config import Config
from photos2yt.db import (
    VideoRecord,
    VideoStatus,
    get_connection,
    get_video_by_sha256,
    init_db,
    insert_video,
)
from photos2yt.metadata import (
    compute_dates,
    compute_dates_from_datetime,
    format_description,
    format_tags,
    format_title,
    parse_sidecar,
)


class TakeoutError(Exception):
    """Base exception for takeout ingest operations."""
    pass


class FFprobeNotFoundError(TakeoutError):
    """Raised when ffprobe executable is not found in PATH."""
    pass


class FFprobeExecutionError(TakeoutError):
    """Raised when ffprobe command fails on a video file."""
    pass


@dataclass(frozen=True)
class VideoProbeResult:
    """Result of probing a video with ffprobe."""
    duration_s: Optional[float] = None
    creation_time: Optional[datetime] = None


@dataclass(frozen=True)
class SidecarMatch:
    """Result of sidecar matching."""
    path: Optional[Path]
    tier: str
    confidence: str  # "high", "low", "none"


@dataclass
class IngestItemResult:
    """Report record for a single processed video file."""
    source_path: str
    status: str
    matcher_tier: str
    confidence: str
    sidecar_path: Optional[str]
    date_source: str
    taken_utc: Optional[str]
    taken_local_date: Optional[str]
    duration_s: Optional[float]
    is_long_file: bool
    sha256: str
    size_bytes: int
    title: Optional[str]
    notes: Optional[str] = None


@dataclass
class IngestSummary:
    """Summary statistics for an ingest run."""
    total_scanned: int = 0
    new_pending: int = 0
    skipped: int = 0
    already_present: int = 0
    matched_by_tier: dict[str, int] = field(default_factory=dict)
    date_source_counts: dict[str, int] = field(default_factory=dict)
    unmatched_count: int = 0
    duplicate_count: int = 0
    long_file_count: int = 0
    report_path: Optional[Path] = None
    items: list[IngestItemResult] = field(default_factory=list)


def check_ffprobe_available() -> bool:
    """Check if ffprobe binary is available in the current environment."""
    return shutil.which("ffprobe") is not None


def probe_video(video_path: Path, ffprobe_bin: str = "ffprobe") -> VideoProbeResult:
    """Probe video file using ffprobe to extract duration and creation_time.

    Raises:
        FFprobeNotFoundError: If ffprobe binary is not found in PATH.
        FFprobeExecutionError: If ffprobe exits with non-zero status.
    """
    if not check_ffprobe_available():
        raise FFprobeNotFoundError(
            "ffprobe is not installed or not found in PATH. "
            "ffprobe is required for video duration and metadata extraction."
        )

    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-show_entries",
        "format=duration:format_tags=creation_time:stream=duration:stream_tags=creation_time",
        "-of",
        "json",
        str(video_path),
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        raise FFprobeNotFoundError(
            "ffprobe is not installed or not found in PATH. "
            "ffprobe is required for video duration and metadata extraction."
        )

    if proc.returncode != 0:
        raise FFprobeExecutionError(
            f"ffprobe failed for '{video_path}' (code {proc.returncode}): {proc.stderr.strip()}"
        )

    try:
        data = json.loads(proc.stdout)
    except Exception as err:
        raise FFprobeExecutionError(f"Failed to parse ffprobe JSON output for '{video_path}': {err}")

    # Extract duration
    duration_s: Optional[float] = None
    format_dict = data.get("format") or {}
    streams_list = data.get("streams") or []

    raw_duration = format_dict.get("duration")
    if raw_duration is not None:
        try:
            duration_s = float(raw_duration)
        except (ValueError, TypeError):
            pass

    if duration_s is None and streams_list:
        for stream in streams_list:
            stream_dur = stream.get("duration")
            if stream_dur is not None:
                try:
                    duration_s = float(stream_dur)
                    break
                except (ValueError, TypeError):
                    continue

    # Extract creation_time tag
    creation_time: Optional[datetime] = None
    raw_creation = (format_dict.get("tags") or {}).get("creation_time")
    if not raw_creation and streams_list:
        for stream in streams_list:
            raw_creation = (stream.get("tags") or {}).get("creation_time")
            if raw_creation:
                break

    if raw_creation:
        creation_str = str(raw_creation).strip()
        # Handle ISO-8601 strings, e.g. 2020-12-24T20:00:00.000000Z
        if creation_str.endswith("Z"):
            creation_str = creation_str[:-1] + "+00:00"
        try:
            parsed_dt = datetime.fromisoformat(creation_str)
            if parsed_dt.tzinfo is None:
                parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
            creation_time = parsed_dt
        except (ValueError, TypeError):
            pass

    return VideoProbeResult(duration_s=duration_s, creation_time=creation_time)


def compute_sha256(path: Path, chunk_size: int = 65536) -> str:
    """Compute SHA-256 hash of a file streamed in chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _match_tiers_1_to_4(video_path: Path) -> SidecarMatch:
    """Internal helper to match tiers 1 through 4 for a given video path."""
    dir_path = video_path.parent
    if not dir_path.is_dir():
        return SidecarMatch(None, "unmatched", "none")

    filename = video_path.name
    stem = video_path.stem
    ext = video_path.suffix

    # Tier 1: <file>.supplemental-metadata.json
    cand1 = dir_path / f"{filename}.supplemental-metadata.json"
    if cand1.is_file():
        return SidecarMatch(cand1, "tier_1_supplemental_metadata", "high")
    cand1_stem = dir_path / f"{stem}.supplemental-metadata.json"
    if cand1_stem.is_file():
        return SidecarMatch(cand1_stem, "tier_1_supplemental_metadata", "high")

    # Tier 2: <file>.json
    cand2 = dir_path / f"{filename}.json"
    if cand2.is_file():
        return SidecarMatch(cand2, "tier_2_json", "high")
    cand2_stem = dir_path / f"{stem}.json"
    if cand2_stem.is_file():
        return SidecarMatch(cand2_stem, "tier_2_json", "high")

    # Tier 3: Truncated-name variants
    # (sidecar base name cut to ~46 chars; handle truncation of 'supplemental-metadata' too)
    try:
        json_files = list(dir_path.glob("*.json"))
    except Exception:
        json_files = []

    for jf in json_files:
        j_name = jf.name
        j_stem = jf.stem

        # Truncation of 'supplemental-metadata'
        # e.g., name.mp4.supplemental-meta.json, name.mp4.supple.json
        if (
            j_name.startswith(f"{filename}.supplemental")
            or j_name.startswith(f"{stem}.supplemental")
            or j_name.startswith(f"{filename}.supp")
            or j_name.startswith(f"{stem}.supp")
        ):
            return SidecarMatch(jf, "tier_3_truncated", "high")

        # Long filename base truncated to ~40-50 chars
        for prefix_len in (46, 47, 48, 49, 50, 45, 44, 43, 42, 41, 40):
            if len(filename) >= prefix_len:
                prefix = filename[:prefix_len]
                if j_name.startswith(prefix) or j_stem.startswith(prefix):
                    return SidecarMatch(jf, "tier_3_truncated", "high")
            if len(stem) >= prefix_len:
                prefix = stem[:prefix_len]
                if j_name.startswith(prefix) or j_stem.startswith(prefix):
                    return SidecarMatch(jf, "tier_3_truncated", "high")

    # Tier 4: Duplicate-suffix handling: name(1).mp4 <-> name.mp4(1).json
    # Examples: name(1).mp4 <-> name.mp4(1).json, name(1).mp4.json, name(1).json
    dup_match = re.search(r"^(.*?)(?:\s*\((?P<idx>\d+)\))(?P<ext>\.[^.]+)$", filename)
    if dup_match:
        base_name = dup_match.group(1)
        idx = dup_match.group("idx")
        file_ext = dup_match.group("ext")

        candidates = [
            f"{base_name}{file_ext}({idx}).json",
            f"{base_name}{file_ext}({idx}).supplemental-metadata.json",
            f"{base_name} ({idx}){file_ext}.json",
            f"{base_name}({idx}){file_ext}.json",
            f"{base_name}({idx}).json",
            f"{base_name} ({idx}).json",
            f"{base_name}({idx}){file_ext}.supplemental-metadata.json",
            f"{base_name}({idx}).supplemental-metadata.json",
        ]
        for cand in candidates:
            p = dir_path / cand
            if p.is_file():
                return SidecarMatch(p, "tier_4_duplicate_suffix", "high")

    return SidecarMatch(None, "unmatched", "none")


def find_sidecar(video_path: Path) -> SidecarMatch:
    """Find matching sidecar file using the tiered matching strategy.

    Tier 1: <file>.supplemental-metadata.json
    Tier 2: <file>.json
    Tier 3: Truncated-name variants (~46 chars base / truncated supplemental-metadata)
    Tier 4: Duplicate-suffix handling: name(1).mp4 <-> name.mp4(1).json
    Tier 5: "-edited" files inherit the original's sidecar
    Tier 6: Fuzzy fallback within the same directory (recorded as low confidence)

    Returns:
        SidecarMatch with matched path, tier name, and confidence.
    """
    # Try tiers 1-4 first
    match = _match_tiers_1_to_4(video_path)
    if match.path is not None:
        return match

    dir_path = video_path.parent
    stem = video_path.stem
    ext = video_path.suffix

    # Tier 5: "-edited" files inherit the original's sidecar
    edited_pattern = re.compile(r"[-_](edited|EDITED)(.*)$", re.IGNORECASE)
    if edited_pattern.search(stem):
        orig_stem = edited_pattern.sub(r"\2", stem)
        orig_video_path = dir_path / f"{orig_stem}{ext}"
        orig_match = _match_tiers_1_to_4(orig_video_path)
        if orig_match.path is not None:
            return SidecarMatch(orig_match.path, "tier_5_edited_original", "high")

    # Tier 6: Fuzzy fallback within the same directory (low confidence)
    if dir_path.is_dir():
        best_match: Optional[Path] = None
        best_ratio = 0.0

        try:
            json_files = list(dir_path.glob("*.json"))
        except Exception:
            json_files = []

        for jf in json_files:
            j_stem = jf.stem
            # Clean common suffixes like .supplemental-metadata for fair comparison
            clean_j_stem = j_stem.replace(".supplemental-metadata", "").replace(".supplemental", "")
            ratio = difflib.SequenceMatcher(None, stem.lower(), clean_j_stem.lower()).ratio()
            if ratio >= 0.6 and ratio > best_ratio:
                best_ratio = ratio
                best_match = jf

        if best_match is not None:
            return SidecarMatch(best_match, "tier_6_fuzzy", "low")

    return SidecarMatch(None, "unmatched", "none")


def walk_staging_dir(staging_dir: Path, video_extensions: Sequence[str]) -> list[Path]:
    """Recursively find all video files in staging_dir matching allowed extensions.

    Never modifies, moves, or deletes any files.
    """
    allowed_exts = {ext.lower() for ext in video_extensions}
    results: list[Path] = []

    if not staging_dir.is_dir():
        return results

    for root, _, files in os.walk(staging_dir):
        for f in files:
            p = Path(root) / f
            if p.suffix.lower() in allowed_exts:
                results.append(p)

    results.sort(key=lambda p: str(p))
    return results


def write_ingest_report(report_path: Path, items: list[IngestItemResult]) -> None:
    """Write ingest results to a CSV report file."""
    report_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "source_path",
        "status",
        "matcher_tier",
        "confidence",
        "sidecar_path",
        "date_source",
        "taken_utc",
        "taken_local_date",
        "duration_s",
        "is_long_file",
        "sha256",
        "size_bytes",
        "title",
        "notes",
    ]

    with open(report_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for item in items:
            writer.writerow({
                "source_path": item.source_path,
                "status": item.status,
                "matcher_tier": item.matcher_tier,
                "confidence": item.confidence,
                "sidecar_path": item.sidecar_path or "",
                "date_source": item.date_source,
                "taken_utc": item.taken_utc or "",
                "taken_local_date": item.taken_local_date or "",
                "duration_s": f"{item.duration_s:.3f}" if item.duration_s is not None else "",
                "is_long_file": str(item.is_long_file),
                "sha256": item.sha256,
                "size_bytes": item.size_bytes,
                "title": item.title or "",
                "notes": item.notes or "",
            })


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    """Check if a table exists in the database."""
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
        (table_name,),
    )
    return cursor.fetchone() is not None


def ingest(
    config: Config,
    dry_run: bool = False,
    report_path: Optional[Path] = None,
) -> IngestSummary:
    """Ingest videos and sidecars from staging_dir into the queue database.

    - Verifies ffprobe availability (fails clearly if not installed)
    - Walks staging_dir recursively
    - Matches sidecars using 6-tier matcher
    - Computes streaming SHA-256
    - Handles duplicates (marks SKIPPED)
    - Flags videos > 15 minutes
    - Idempotent: re-running does not duplicate rows or reset status past PENDING
    - Writes ingest_report.csv
    - Never mutates staging files (read-only)
    """
    if not check_ffprobe_available():
        raise FFprobeNotFoundError(
            "ffprobe is not installed or not found in PATH. "
            "ffprobe is required for video duration and metadata extraction."
        )

    staging_dir = config.paths.staging_dir
    video_files = walk_staging_dir(staging_dir, config.video_extensions)

    summary = IngestSummary()
    summary.total_scanned = len(video_files)

    # Resolve report path
    if report_path is None:
        report_path = Path("ingest_report.csv").resolve()
    summary.report_path = report_path

    # Connect to DB (even in dry-run, we query existing state without mutating)
    if dry_run and not config.paths.db_path.is_file():
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        has_videos_table = False
    else:
        conn = get_connection(config.paths.db_path)
        if not dry_run:
            init_db(conn)
            has_videos_table = True
        else:
            has_videos_table = _table_exists(conn, "videos")

    dry_run_seen_shas: dict[str, str] = {}

    for video_path in video_files:
        size_bytes = video_path.stat().st_size
        sha256 = compute_sha256(video_path)

        # Probe duration and creation time via ffprobe
        probe_res = probe_video(video_path)
        duration_s = probe_res.duration_s
        is_long = (duration_s is not None and duration_s > 900.0)
        if is_long:
            summary.long_file_count += 1

        # Match sidecar
        sidecar_match = find_sidecar(video_path)
        matcher_tier = sidecar_match.tier
        confidence = sidecar_match.confidence
        sidecar_path = sidecar_match.path

        summary.matched_by_tier[matcher_tier] = summary.matched_by_tier.get(matcher_tier, 0) + 1
        if matcher_tier == "unmatched":
            summary.unmatched_count += 1

        # Extract metadata
        sidecar_meta = parse_sidecar(sidecar_path) if sidecar_path else None

        taken_utc: Optional[str] = None
        taken_local_date: Optional[str] = None
        dt_local: Optional[datetime] = None
        date_source = "none"

        # Determine date source: sidecar -> ffprobe creation_time -> mtime
        if sidecar_meta and sidecar_meta.timestamp is not None:
            taken_utc, taken_local_date, dt_local = compute_dates(
                sidecar_meta.timestamp, config.timezone
            )
            date_source = "sidecar"
        elif probe_res.creation_time is not None:
            taken_utc, taken_local_date, dt_local = compute_dates_from_datetime(
                probe_res.creation_time, config.timezone
            )
            date_source = "ffprobe"
        else:
            mtime = video_path.stat().st_mtime
            taken_utc, taken_local_date, dt_local = compute_dates(mtime, config.timezone)
            date_source = "mtime"

        summary.date_source_counts[date_source] = summary.date_source_counts.get(date_source, 0) + 1

        # Metadata fields
        meta_title = sidecar_meta.title if sidecar_meta else None
        meta_desc = sidecar_meta.description if sidecar_meta else None
        lat = sidecar_meta.lat if sidecar_meta else None
        lon = sidecar_meta.lon if sidecar_meta else None

        title = format_title(
            template=config.youtube.title_template,
            default_title=config.youtube.default_title,
            raw_title=meta_title,
            taken_local_date=taken_local_date or "",
            dt_local=dt_local or datetime.now(config.tzinfo),
            video_stem=video_path.stem,
            caption=meta_desc,
        )

        description = format_description(
            raw_description=meta_desc,
            dt_local=dt_local or datetime.now(config.tzinfo),
        )

        tags = format_tags(dt_local or datetime.now(config.tzinfo))
        tags_json = json.dumps(tags)

        # Check DB state
        # 1. Look rows up by source_path first so re-running ingest never creates a second row
        #    for the same file and never resets the status of rows that are past PENDING.
        row_by_path = None
        if has_videos_table:
            cursor = conn.execute("SELECT * FROM videos WHERE source_path = ?", (str(video_path),))
            row_by_path = cursor.fetchone()

        item_status = VideoStatus.PENDING.value
        notes: Optional[str] = None

        if row_by_path is not None:
            # Idempotent re-run on this exact file path
            current_status = row_by_path["status"]
            item_status = current_status
            summary.already_present += 1
            notes = f"Already in database with status {current_status}"
            # Preserve existing row; do not reset status past PENDING
        else:
            # 2. Check for duplicate SHA-256 hash among existing videos
            existing_canonical = None
            if has_videos_table:
                cursor = conn.execute(
                    "SELECT * FROM videos WHERE sha256 = ? ORDER BY (duplicate_of IS NOT NULL), id ASC LIMIT 1",
                    (sha256,),
                )
                existing_canonical = cursor.fetchone()

            if existing_canonical is not None:
                # A different file path has the exact same SHA-256 hash (Duplicate!)
                summary.duplicate_count += 1
                summary.skipped += 1
                item_status = VideoStatus.SKIPPED.value
                canonical_path = existing_canonical["source_path"]
                canonical_id = existing_canonical["id"]
                notes = f"Duplicate of {canonical_path}"

                if not dry_run:
                    insert_video(
                        conn,
                        source_path=str(video_path),
                        sidecar_path=str(sidecar_path) if sidecar_path else None,
                        sha256=sha256,
                        size_bytes=size_bytes,
                        duration_s=duration_s,
                        taken_utc=taken_utc,
                        taken_local_date=taken_local_date,
                        title=title,
                        description=description,
                        tags_json=tags_json,
                        lat=lat,
                        lon=lon,
                        status=VideoStatus.SKIPPED,
                        duplicate_of=canonical_id,
                        last_error=notes,
                    )
            elif dry_run and sha256 in dry_run_seen_shas:
                # In dry-run mode, check if we previously saw this hash earlier in the run
                summary.duplicate_count += 1
                summary.skipped += 1
                item_status = VideoStatus.SKIPPED.value
                canonical_path = dry_run_seen_shas[sha256]
                notes = f"Duplicate of {canonical_path}"
            else:
                # Brand new canonical unique video
                if dry_run:
                    dry_run_seen_shas[sha256] = str(video_path)
                summary.new_pending += 1
                item_status = VideoStatus.PENDING.value

                if not dry_run:
                    insert_video(
                        conn,
                        source_path=str(video_path),
                        sidecar_path=str(sidecar_path) if sidecar_path else None,
                        sha256=sha256,
                        size_bytes=size_bytes,
                        duration_s=duration_s,
                        taken_utc=taken_utc,
                        taken_local_date=taken_local_date,
                        title=title,
                        description=description,
                        tags_json=tags_json,
                        lat=lat,
                        lon=lon,
                        status=VideoStatus.PENDING,
                        duplicate_of=None,
                    )

        if is_long:
            long_note = "Duration exceeds 15 minutes (requires verified YouTube channel)"
            notes = f"{notes}; {long_note}" if notes else long_note

        item_result = IngestItemResult(
            source_path=str(video_path),
            status=item_status,
            matcher_tier=matcher_tier,
            confidence=confidence,
            sidecar_path=str(sidecar_path) if sidecar_path else None,
            date_source=date_source,
            taken_utc=taken_utc,
            taken_local_date=taken_local_date,
            duration_s=duration_s,
            is_long_file=is_long,
            sha256=sha256,
            size_bytes=size_bytes,
            title=title,
            notes=notes,
        )
        summary.items.append(item_result)

    conn.close()

    # Write report CSV
    write_ingest_report(report_path, summary.items)
    return summary
