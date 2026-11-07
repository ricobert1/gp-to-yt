"""Unit tests for metadata parsing, local-date conversion, and formatting."""

from datetime import datetime, timezone
import json
from pathlib import Path
import pytest

from photos2yt.metadata import (
    SidecarMetadata,
    compute_dates,
    compute_dates_from_datetime,
    format_description,
    format_tags,
    format_title,
    parse_sidecar,
)


def test_parse_sidecar_full(tmp_path: Path):
    """Parse a complete sidecar file with timestamp, geoData, title, description."""
    json_path = tmp_path / "video.mp4.supplemental-metadata.json"
    data = {
        "title": "Family Picnic",
        "description": "Fun day in the park <sunny>",
        "photoTakenTime": {
            "timestamp": "1625097600",
            "formatted": "Jul 1, 2021, 12:00:00 AM UTC"
        },
        "geoData": {
            "latitude": 37.7749,
            "longitude": -122.4194,
            "altitude": 15.0
        }
    }
    json_path.write_text(json.dumps(data), encoding="utf-8")

    meta = parse_sidecar(json_path)
    assert meta is not None
    assert meta.title == "Family Picnic"
    assert meta.description == "Fun day in the park <sunny>"
    assert meta.timestamp == 1625097600.0
    assert meta.lat == pytest.approx(37.7749)
    assert meta.lon == pytest.approx(-122.4194)


def test_parse_sidecar_zero_geodata(tmp_path: Path):
    """Google Photos treats (0.0, 0.0) as no location; lat and lon must be None."""
    json_path = tmp_path / "zero_geo.json"
    data = {
        "title": "Clip",
        "photoTakenTime": {"timestamp": "1600000000"},
        "geoData": {
            "latitude": 0.0,
            "longitude": 0.0,
            "altitude": 0.0
        }
    }
    json_path.write_text(json.dumps(data), encoding="utf-8")

    meta = parse_sidecar(json_path)
    assert meta is not None
    assert meta.lat is None
    assert meta.lon is None


def test_parse_sidecar_missing_and_corrupt(tmp_path: Path):
    """Missing files or corrupt JSON return None."""
    assert parse_sidecar(tmp_path / "nonexistent.json") is None

    corrupt_file = tmp_path / "corrupt.json"
    corrupt_file.write_text("{this is not valid json}", encoding="utf-8")
    assert parse_sidecar(corrupt_file) is None


def test_dec_24_8pm_eastern_edge_case():
    """Video taken at 8 PM America/New_York on Dec 24 must have local date Dec 24 even though UTC is Dec 25."""
    # Dec 24, 2020 at 20:00 EST (UTC-5) is Dec 25, 2020 at 01:00 UTC
    # 2020-12-25 01:00:00 UTC epoch timestamp = 1608858000
    epoch = 1608858000.0
    tz = "America/New_York"

    taken_utc, taken_local_date, dt_local = compute_dates(epoch, tz)

    assert taken_utc == "2020-12-25T01:00:00Z"
    assert taken_local_date == "2020-12-24"
    assert dt_local.year == 2020
    assert dt_local.month == 12
    assert dt_local.day == 24
    assert dt_local.hour == 20


def test_compute_dates_from_datetime():
    """Converting aware and naive datetimes properly computes UTC and local date."""
    # 2022-07-04 18:00:00 EDT (UTC-4) = 22:00:00 UTC
    dt = datetime(2022, 7, 4, 22, 0, 0, tzinfo=timezone.utc)
    taken_utc, taken_local_date, dt_local = compute_dates_from_datetime(dt, "America/New_York")

    assert taken_utc == "2022-07-04T22:00:00Z"
    assert taken_local_date == "2022-07-04"
    assert dt_local.hour == 18


def test_format_title_template_and_stem_fallback():
    """Title template formats with {date} and {title}, stripping .mp4 extensions."""
    dt = datetime(2020, 12, 24, 20, 0, 0)
    title = format_title(
        template="{date} - {title}",
        default_title="Family Video",
        raw_title="Christmas Eve.mp4",
        taken_local_date="2020-12-24",
        dt_local=dt,
    )
    assert title == "2020-12-24 - Christmas Eve"

    # Fallback to default_title when title is empty
    title_default = format_title(
        template="{date} - {title}",
        default_title="Family Video",
        raw_title=None,
        taken_local_date="2020-12-24",
        dt_local=dt,
        video_stem=None,
    )
    assert title_default == "2020-12-24 - Family Video"


def test_format_title_length_limit_and_sanitization():
    """Title <= 100 characters and control characters removed."""
    dt = datetime(2021, 1, 1)
    long_raw = "A" * 150
    title = format_title(
        template="{title}",
        default_title="Default",
        raw_title=long_raw,
        taken_local_date="2021-01-01",
        dt_local=dt,
    )
    assert len(title) == 100
    assert title == "A" * 100

    # Newlines sanitized
    dirty = "Line 1\nLine 2\r\nLine 3"
    sanitized = format_title(
        template="{title}",
        default_title="Default",
        raw_title=dirty,
        taken_local_date="2021-01-01",
        dt_local=dt,
    )
    assert "\n" not in sanitized
    assert "\r" not in sanitized
    assert sanitized == "Line 1 Line 2 Line 3"


def test_format_description_hashtags_and_strip_angle_brackets():
    """Description must strip < and >, append #family #<year> #<month>."""
    dt = datetime(2020, 12, 24)
    raw = "Kids opening <presents> under the tree > fun"
    desc = format_description(raw, dt)

    assert "<" not in desc
    assert ">" not in desc
    assert "Kids opening presents under the tree  fun" in desc
    assert "#family #2020 #12" in desc


def test_format_description_byte_limit():
    """Description must be <= 5,000 bytes in UTF-8."""
    dt = datetime(2023, 5, 1)
    huge_text = "Emoji 🎉 and characters " + "x" * 6000
    desc = format_description(huge_text, dt)

    encoded = desc.encode("utf-8")
    assert len(encoded) <= 5000


def test_format_tags_limit():
    """Tags total joined length must be <= 500 characters."""
    dt = datetime(2020, 12, 24)
    tags = format_tags(dt, extra_tags=["vacation", "christmas", "holidays"])
    assert "family" in tags
    assert "2020" in tags
    assert "2020-12" in tags
    assert "vacation" in tags

    # Many long tags
    many_tags = [f"tag_number_{i:04d}_with_long_name" for i in range(100)]
    capped_tags = format_tags(dt, extra_tags=many_tags)
    joined = ",".join(capped_tags)
    assert len(joined) <= 500
