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


# File names cameras and phones generate; they say nothing about the video.
_CAMERA_NAME_RE = re.compile(
    r"""^(?:
        (?:IMG|VID|MVI|MOV|DSC[FN]?|PXL|GOPR|GP|GH|GX|DJI|MAH|CIMG|SAM|WP|PANO|MOVIE|CLIP|C)
            [\s_\-]*\d[\w\-]*
      | VID[_\-]\d{8}[_\-]\w+                      # VID-20201224-WA0001
      | P\d{7}                                     # P1010001 (Panasonic)
      | \d{8}[_\-]?\d{6}\w*                         # 20201224_181500
      | \d{4}-\d{2}-\d{2}[\s_T]\d{2}[.:\-]\d{2}[.:\-]\d{2}.*   # 2020-12-24 18.15.00
      | (?:Screen[\s_]?Recording|RPReplay)[\s_\-].*
      | [0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}
      | [\d\s_\-.()]+                                  # only digits/punctuation
      | trim\.[\w\-]+                               # iOS trim.<id>
    )(?:\s*\(\d+\))?(?:-edited)?$""",
    re.IGNORECASE | re.VERBOSE,
)
_VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".m4v", ".avi", ".3gp", ".json"}
MAX_TITLE_LEN = 100


def is_meaningless_name(name: Optional[str]) -> bool:
    """True for empty names and camera-generated file names (IMG_1234, PXL_..., etc.)."""
    if name is None or not name.strip():
        return True
    return bool(_CAMERA_NAME_RE.match(name.strip()))


def _strip_video_suffix(name: str) -> str:
    p = Path(name)
    return p.stem if p.suffix.lower() in _VIDEO_SUFFIXES else name


def _clean_title_text(text: str) -> str:
    text = text.replace("<", "").replace(">", "")
    text = re.sub(r"[\r\n\t]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def caption_from_description(description: Optional[str]) -> Optional[str]:
    """First non-empty line of a Google Photos description, cleaned; None if none."""
    if not description:
        return None
    for line in description.splitlines():
        cleaned = _clean_title_text(line)
        if cleaned:
            return cleaned
    return None


def choose_title_subject(
    raw_title: Optional[str],
    video_stem: Optional[str],
    default_title: str,
    caption: Optional[str] = None,
) -> str:
    """What the video is called, before the template adds dates.

    Order: the Google Photos caption (description's first line), then the
    sidecar title or file name if it is meaningful, else ``default_title``.
    """
    caption_text = caption_from_description(caption)
    if caption_text:
        return caption_text
    for name in (raw_title, video_stem):
        if name and name.strip():
            candidate = _clean_title_text(_strip_video_suffix(name.strip()))
            if candidate and not is_meaningless_name(candidate):
                return candidate
    return default_title


def _format_time(dt_local: datetime) -> str:
    hour = dt_local.hour % 12 or 12
    return f"{hour}:{dt_local.minute:02d} {'AM' if dt_local.hour < 12 else 'PM'}"


def format_title(
    template: str,
    default_title: str,
    raw_title: Optional[str],
    taken_local_date: str,
    dt_local: datetime,
    video_stem: Optional[str] = None,
    caption: Optional[str] = None,
) -> str:
    """Build the YouTube title from the config template.

    ``{title}`` is the caption, a meaningful file name, or ``default_title``
    (see choose_title_subject). Placeholders, all in local time:
    {title} {default_title} {date} (YYYY-MM-DD) {year} {month} (01-12)
    {day} (01-31) {day_num} (1-31) {month_name} (December) {month_short} (Dec)
    {weekday} (Thursday) {weekday_short} (Thu) {time} (6:15 PM).

    YouTube limits: at most 100 characters, no '<' or '>'. A long {title} is
    shortened so the rest of the template (e.g. the date) still fits.
    """
    subject = choose_title_subject(raw_title, video_stem, default_title, caption)
    mapping = {
        "date": taken_local_date,
        "default_title": default_title,
        "year": f"{dt_local.year}",
        "month": f"{dt_local.month:02d}",
        "day": f"{dt_local.day:02d}",
        "day_num": f"{dt_local.day}",
        "month_name": dt_local.strftime("%B"),
        "month_short": dt_local.strftime("%b"),
        "weekday": dt_local.strftime("%A"),
        "weekday_short": dt_local.strftime("%a"),
        "time": _format_time(dt_local),
    }

    def render(title_text: str) -> str:
        try:
            out = template.format(title=title_text, **mapping)
        except (KeyError, IndexError, ValueError):
            out = f"{taken_local_date} - {title_text}"
        return _clean_title_text(out)

    formatted = render(subject)
    if len(formatted) > MAX_TITLE_LEN:
        room = MAX_TITLE_LEN - (len(render("X")) - 1)  # "X": keeps spacing exact
        if room > 0:
            formatted = render(subject[:room].rstrip())
    if not formatted:
        formatted = _clean_title_text(default_title)
    return formatted[:MAX_TITLE_LEN]


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
