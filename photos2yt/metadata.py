"""Video metadata parsing, date conversion, and title/description/tag formatting."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any, Optional, Sequence
import zoneinfo


@dataclass(frozen=True)
class SidecarMetadata:
    """Metadata extracted from a Google Photos JSON sidecar."""
    title: Optional[str] = None
    description: Optional[str] = None
    timestamp: Optional[float] = None
    lat: Optional[float] = None
    lon: Optional[float] = None


def parse_sidecar(json_path: str | Path) -> Optional[SidecarMetadata]:
    """Parse a Google Photos JSON sidecar file.

    Extracts:
    - photoTakenTime.timestamp (or creationTime.timestamp)
    - geoData (latitude/longitude; treats 0.0, 0.0 as no location)
    - description
    - title
    """
    path = Path(json_path)
    if not path.is_file():
        return None

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except Exception:
        return None

    if not isinstance(data, dict):
        return None

    # Title
    raw_title = data.get("title")
    title = str(raw_title).strip() if raw_title is not None else None
    if title == "":
        title = None

    # Description
    raw_desc = data.get("description")
    description = str(raw_desc).strip() if raw_desc is not None else None
    if description == "":
        description = None

    # Timestamp from photoTakenTime or creationTime
    timestamp: Optional[float] = None
    for time_key in ("photoTakenTime", "creationTime"):
        time_dict = data.get(time_key)
        if isinstance(time_dict, dict):
            ts_val = time_dict.get("timestamp")
            if ts_val is not None:
                try:
                    parsed_ts = float(str(ts_val).strip())
                    if parsed_ts > 0:
                        timestamp = parsed_ts
                        break
                except (ValueError, TypeError):
                    continue

    # GeoData
    lat: Optional[float] = None
    lon: Optional[float] = None
    geo_dict = data.get("geoData")
    if isinstance(geo_dict, dict):
        raw_lat = geo_dict.get("latitude")
        raw_lon = geo_dict.get("longitude")
        if raw_lat is not None and raw_lon is not None:
            try:
                lat_float = float(raw_lat)
                lon_float = float(raw_lon)
                # Google Photos sets (0.0, 0.0) when no location is recorded
                if not (abs(lat_float) < 1e-6 and abs(lon_float) < 1e-6):
                    lat = lat_float
                    lon = lon_float
            except (ValueError, TypeError):
                pass

    return SidecarMetadata(
        title=title,
        description=description,
        timestamp=timestamp,
        lat=lat,
        lon=lon,
    )


def compute_dates(epoch_seconds: float, tz_name: str) -> tuple[str, str, datetime]:
    """Convert a UTC epoch timestamp into taken_utc, taken_local_date, and local datetime.

    Args:
        epoch_seconds: Seconds since Unix epoch in UTC.
        tz_name: IANA timezone name (e.g. 'America/New_York').

    Returns:
        tuple of (taken_utc ISO string, taken_local_date YYYY-MM-DD, dt_local datetime)
    """
    dt_utc = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
    zone = zoneinfo.ZoneInfo(tz_name)
    dt_local = dt_utc.astimezone(zone)

    taken_utc = dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    taken_local_date = dt_local.strftime("%Y-%m-%d")
    return taken_utc, taken_local_date, dt_local


def compute_dates_from_datetime(dt: datetime, tz_name: str) -> tuple[str, str, datetime]:
    """Convert a datetime (naive assumed UTC or aware) to taken_utc, taken_local_date, and dt_local."""
    if dt.tzinfo is None:
        dt_utc = dt.replace(tzinfo=timezone.utc)
    else:
        dt_utc = dt.astimezone(timezone.utc)

    zone = zoneinfo.ZoneInfo(tz_name)
    dt_local = dt_utc.astimezone(zone)

    taken_utc = dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    taken_local_date = dt_local.strftime("%Y-%m-%d")
    return taken_utc, taken_local_date, dt_local


def format_title(
    template: str,
    default_title: str,
    raw_title: Optional[str],
    taken_local_date: str,
    dt_local: datetime,
    video_stem: Optional[str] = None,
) -> str:
    """Format and sanitize video title according to config template and YouTube constraints.

    YouTube constraint: Title <= 100 characters.
    """
    candidate: Optional[str] = None
    if raw_title is not None and raw_title.strip():
        cand = raw_title.strip()
        # If raw_title includes a file extension like .mp4, strip it for clean display
        cand_path = Path(cand)
        if cand_path.suffix.lower() in {".mp4", ".mov", ".mkv", ".m4v", ".avi", ".3gp", ".json"}:
            cand = cand_path.stem
        candidate = cand

    if not candidate:
        if video_stem and video_stem.strip():
            candidate = video_stem.strip()
        else:
            candidate = default_title

    # Template mapping
    mapping = {
        "date": taken_local_date,
        "title": candidate,
        "year": f"{dt_local.year}",
        "month": f"{dt_local.month:02d}",
        "day": f"{dt_local.day:02d}",
    }

    try:
        formatted = template.format(**mapping)
    except KeyError:
        # Fallback if unknown placeholder in template
        formatted = f"{taken_local_date} - {candidate}"

    # Sanitize control characters and normalize whitespace
    sanitized = re.sub(r"[\r\n\t]+", " ", formatted)
    sanitized = re.sub(r"\s+", " ", sanitized).strip()

    if not sanitized:
        sanitized = default_title

    # Enforce YouTube <= 100 characters
    return sanitized[:100]


def format_description(
    raw_description: Optional[str],
    dt_local: datetime,
) -> str:
    """Format and sanitize video description according to YouTube constraints.

    YouTube constraints:
    - No '<' or '>' characters
    - Total bytes <= 5,000 bytes (UTF-8)
    Hashtags appended: #family #<year> #<month>
    """
    hashtags = f"#family #{dt_local.year} #{dt_local.month:02d}"

    cleaned = ""
    if raw_description:
        # Strip forbidden angle brackets
        cleaned = raw_description.replace("<", "").replace(">", "").strip()

    if cleaned:
        full_text = f"{cleaned}\n\n{hashtags}"
    else:
        full_text = hashtags

    # Enforce <= 5,000 bytes UTF-8
    encoded = full_text.encode("utf-8")
    if len(encoded) > 5000:
        # Truncate at 5000 bytes safely without broken UTF-8 codepoints
        full_text = encoded[:5000].decode("utf-8", errors="ignore")

    return full_text


def format_tags(
    dt_local: datetime,
    extra_tags: Optional[Sequence[str]] = None,
) -> list[str]:
    """Generate tags according to YouTube constraints.

    YouTube constraints:
    - Total length of comma-joined tags <= 500 characters.
    """
    base_tags = ["family", str(dt_local.year), f"{dt_local.year}-{dt_local.month:02d}"]
    all_tags: list[str] = []

    # Combine base tags + extra tags with deduplication while preserving order
    seen: set[str] = set()
    for tag in base_tags + (list(extra_tags) if extra_tags else []):
        t = str(tag).strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            all_tags.append(t)

    # Ensure total character length <= 500 chars
    result: list[str] = []
    current_length = 0
    for tag in all_tags:
        added_len = len(tag) if not result else (len(tag) + 1)
        if current_length + added_len <= 500:
            result.append(tag)
            current_length += added_len
        else:
            break

    return result
