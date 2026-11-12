"""Unpack Google Takeout zips into the staging folder (`photos2yt unpack`).

Looks in ``paths.takeout_inbox`` (default: ``gp-to-yt-inbox`` next to
staging_dir) for ``takeout-*.zip`` files and extracts only videos (the
config's ``video_extensions``) and ``.json`` sidecars into
``staging_dir/<export name>/``, keeping the paths inside the zip.

* All parts of one export (``takeout-20261112T101500Z-001.zip``, ``-002.zip``,
  ...) go into the same folder, because a video and its sidecar can be in
  different parts and ingest matches sidecars within a folder.
* Free disk space is checked before each zip; nothing is overwritten (an
  existing file of the same size counts as already there, a different one is
  reported and skipped); entries with absolute paths or ``..`` are refused.
* Each file is written to a temporary name and renamed when complete, so a
  failure never leaves a half-written video that ingest could pick up.
* A zip that unpacked without errors is moved to ``<inbox>/unpacked/`` so it is
  never processed twice (you delete it from there when you like); every zip is
  logged in ``unpack_log.csv`` next to the database. A zip that failed stays in
  the inbox and is retried on the next run.

Google Takeout has no API, so requesting and downloading the export stays
manual; save the zips into the inbox (not ~/Downloads: macOS blocks scheduled
jobs from reading Downloads).
"""

from __future__ import annotations

import csv
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

from photos2yt.config import Config

LOG_NAME = "unpack_log.csv"
LOG_COLUMNS = ("time_utc", "zip", "status", "extracted", "already_there", "conflicts",
               "bytes", "destination", "error")
DONE_DIR = "unpacked"
DISK_MARGIN_BYTES = 1_000_000_000  # keep at least 1 GB free
_PART_RE = re.compile(r"^(takeout-.+?)(?:-\d{3})?\.zip$", re.IGNORECASE)

Printer = Callable[[str], None]


class UnpackError(RuntimeError):
    """A zip could not be unpacked (bad zip, unsafe path, no disk space)."""


def inbox_dir(config: Config, override: Optional[Path] = None) -> Path:
    if override is not None:
        return Path(override).expanduser().resolve()
    if config.paths.takeout_inbox is not None:
        return Path(config.paths.takeout_inbox).resolve()
    return Path(config.paths.staging_dir).resolve().parent / "gp-to-yt-inbox"


def export_name(zip_name: str) -> str:
    """'takeout-20261112T101500Z-001.zip' -> 'takeout-20261112T101500Z'."""
    m = _PART_RE.match(zip_name)
    return m.group(1) if m else Path(zip_name).stem


def find_zips(inbox: Path) -> list[Path]:
    if not inbox.is_dir():
        return []
    return sorted(p for p in inbox.iterdir()
                  if p.is_file() and p.name.lower().startswith("takeout-")
                  and p.suffix.lower() == ".zip")


def _wanted(name: str, video_exts: tuple[str, ...]) -> bool:
    lower = name.lower()
    return lower.endswith(".json") or any(lower.endswith(ext) for ext in video_exts)


def _safe_relative(name: str) -> PurePosixPath:
    rel = PurePosixPath(name.replace("\\", "/"))
    if rel.is_absolute() or ".." in rel.parts or (rel.parts and rel.parts[0].endswith(":")):
        raise UnpackError(f"unsafe path inside zip: {name!r}")
    return rel


@dataclass
class ZipPlan:
    path: Path
    destination: Path
    members: list[zipfile.ZipInfo] = field(default_factory=list)
    videos: int = 0
    sidecars: int = 0
    bytes: int = 0
    error: Optional[str] = None


def plan_zip(zip_path: Path, staging: Path, video_exts: tuple[str, ...]) -> ZipPlan:
    plan = ZipPlan(zip_path, staging / export_name(zip_path.name))
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                if info.is_dir() or not _wanted(info.filename, video_exts):
                    continue
                _safe_relative(info.filename)
                plan.members.append(info)
                plan.bytes += info.file_size
                if info.filename.lower().endswith(".json"):
                    plan.sidecars += 1
                else:
                    plan.videos += 1
    except (zipfile.BadZipFile, OSError) as exc:
        plan.error = f"not a readable zip ({exc}); re-download it"
    except UnpackError as exc:
        plan.error = str(exc)
    return plan


@dataclass
class ZipResult:
    extracted: int = 0
    already_there: int = 0
    conflicts: list[str] = field(default_factory=list)
    bytes: int = 0


def extract_zip(plan: ZipPlan, *, disk_free: Callable[[Path], int]) -> ZipResult:
    """Extract the planned members. Raises UnpackError on failure."""
    plan.destination.mkdir(parents=True, exist_ok=True)
    free = disk_free(plan.destination)
    if plan.bytes + DISK_MARGIN_BYTES > free:
        raise UnpackError(
            f"not enough disk space: needs {plan.bytes / 1e9:.1f} GB plus 1 GB spare, "
            f"{free / 1e9:.1f} GB free; run cleanup or free space, then it will be retried"
        )
    result = ZipResult()
    try:
        with zipfile.ZipFile(plan.path) as zf:
            for info in plan.members:
                target = plan.destination.joinpath(*_safe_relative(info.filename).parts)
                if target.exists():
                    if target.stat().st_size == info.file_size:
                        result.already_there += 1
                    else:
                        result.conflicts.append(str(target))
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(target.name + ".unpacking")
                try:
                    with zf.open(info) as src, open(tmp, "wb") as dst:
                        shutil.copyfileobj(src, dst, 8 * 1024 * 1024)
                    tmp.replace(target)
                finally:
                    if tmp.exists():
                        tmp.unlink()
                result.extracted += 1
                result.bytes += info.file_size
    except (zipfile.BadZipFile, OSError, EOFError) as exc:
        raise UnpackError(f"extraction failed ({exc}); re-download the zip") from exc
    return result


@dataclass
class UnpackSummary:
    zips_done: int = 0
    zips_failed: int = 0
    extracted: int = 0
    errors: list[str] = field(default_factory=list)


def _log(log_path: Path, row: list) -> None:
    new = not log_path.exists()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        if new:
            writer.writerow(LOG_COLUMNS)
        writer.writerow(row)


def run_unpack(config: Config, *, inbox: Optional[Path] = None, dry_run: bool = False,
               out: Printer = print, disk_free: Optional[Callable[[Path], int]] = None,
               now: Optional[Callable[[], datetime]] = None) -> UnpackSummary:
    staging = Path(config.paths.staging_dir).resolve()
    box = inbox_dir(config, inbox)
    summary = UnpackSummary()
    zips = find_zips(box)
    if not zips:
        out(f"No Takeout zips in {box}; nothing to unpack.")
        return summary
    disk_free = disk_free or (lambda p: shutil.disk_usage(p).free)
    clock = now or (lambda: datetime.now(timezone.utc))
    log_path = Path(config.paths.db_path).resolve().parent / LOG_NAME
    prefix = "[DRY RUN] " if dry_run else ""
    out(f"{prefix}{len(zips)} Takeout zip(s) in {box}:")
    for zip_path in zips:
        plan = plan_zip(zip_path, staging, tuple(config.video_extensions))
        head = (f"  {zip_path.name}: {plan.videos} video(s), {plan.sidecars} sidecar(s), "
                f"{plan.bytes / 1e9:.2f} GB -> {plan.destination}")
        if plan.error:
            summary.zips_failed += 1
            summary.errors.append(f"{zip_path.name}: {plan.error}")
            out(f"{head}\n    FAILED: {plan.error}")
            if not dry_run:
                _log(log_path, [clock().isoformat(), zip_path.name, "failed", 0, 0, 0, 0,
                                str(plan.destination), plan.error])
            continue
        out(head)
        if dry_run:
            continue
        try:
            result = extract_zip(plan, disk_free=disk_free)
        except UnpackError as exc:
            summary.zips_failed += 1
            summary.errors.append(f"{zip_path.name}: {exc}")
            out(f"    FAILED: {exc} (left in the inbox; retried next run)")
            _log(log_path, [clock().isoformat(), zip_path.name, "failed", 0, 0, 0, 0,
                            str(plan.destination), str(exc)])
            continue
        done_dir = box / DONE_DIR
        done_dir.mkdir(exist_ok=True)
        shutil.move(str(zip_path), str(done_dir / zip_path.name))
        summary.zips_done += 1
        summary.extracted += result.extracted
        out(f"    unpacked {result.extracted} file(s), {result.already_there} already there"
            + (f", {len(result.conflicts)} conflict(s) skipped" if result.conflicts else "")
            + f"; zip moved to {done_dir}")
        for c in result.conflicts[:10]:
            out(f"      different file already exists, kept: {c}")
        _log(log_path, [clock().isoformat(), zip_path.name, "unpacked", result.extracted,
                        result.already_there, len(result.conflicts), result.bytes,
                        str(plan.destination), ""])
    if not dry_run:
        out(f"Unpack done: {summary.zips_done} zip(s) unpacked, {summary.extracted} file(s) "
            f"extracted, {summary.zips_failed} failed. Log: {log_path}")
    return summary
