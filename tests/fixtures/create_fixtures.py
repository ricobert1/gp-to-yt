"""Generate synthetic fixtures for testing photos2yt takeout ingest."""

import json
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "synthetic_takeout"


def create_fixtures():
    # 1. Tier 1: supplemental-metadata.json (Dec 24 8 PM Eastern edge case)
    t1 = FIXTURES_DIR / "tier1"
    t1.mkdir(parents=True, exist_ok=True)
    (t1 / "tier1_clip.mp4").write_bytes(b"synthetic_tier1_clip_bytes_data_12345")
    (t1 / "tier1_clip.mp4.supplemental-metadata.json").write_text(
        json.dumps({
            "title": "Christmas Eve Unwrapping.mp4",
            "description": "Family unwrapping gifts <at home>",
            "photoTakenTime": {
                "timestamp": "1608858000",
                "formatted": "Dec 25, 2020, 1:00:00 AM UTC"
            },
            "geoData": {
                "latitude": 40.7128,
                "longitude": -74.0060,
                "altitude": 10.0
            }
        }, indent=2),
        encoding="utf-8"
    )

    # 2. Tier 2: <file>.json
    t2 = FIXTURES_DIR / "tier2"
    t2.mkdir(parents=True, exist_ok=True)
    (t2 / "holiday_trip.mp4").write_bytes(b"synthetic_tier2_holiday_bytes_data_67890")
    (t2 / "holiday_trip.mp4.json").write_text(
        json.dumps({
            "title": "Holiday Trip",
            "description": "Holiday vacation footage",
            "photoTakenTime": {
                "timestamp": "1625097600",
                "formatted": "Jul 1, 2021, 12:00:00 AM UTC"
            },
            "geoData": {
                "latitude": 0.0,
                "longitude": 0.0,
                "altitude": 0.0
            }
        }, indent=2),
        encoding="utf-8"
    )

    # 3. Tier 3: Truncated-name variants
    t3 = FIXTURES_DIR / "tier3"
    t3.mkdir(parents=True, exist_ok=True)
    (t3 / "very_long_synthetic_filename_exceeding_forty_six_characters_test.mp4").write_bytes(
        b"synthetic_tier3_long_bytes_data_abcde"
    )
    (t3 / "very_long_synthetic_filename_exceeding_forty_s.json").write_text(
        json.dumps({
            "title": "Long Filename Truncated Video",
            "photoTakenTime": {
                "timestamp": "1640995200"
            }
        }, indent=2),
        encoding="utf-8"
    )
    (t3 / "clip_truncated_supplemental.mp4").write_bytes(b"synthetic_tier3_supp_bytes_data_fghij")
    (t3 / "clip_truncated_supplemental.mp4.supple.json").write_text(
        json.dumps({
            "title": "Truncated Supplemental Video",
            "photoTakenTime": {
                "timestamp": "1641081600"
            }
        }, indent=2),
        encoding="utf-8"
    )

    # 4. Tier 4: Duplicate-suffix handling (name(1).mp4 <-> name.mp4(1).json)
    t4 = FIXTURES_DIR / "tier4"
    t4.mkdir(parents=True, exist_ok=True)
    (t4 / "duplicate_indexed(1).mp4").write_bytes(b"synthetic_tier4_indexed_bytes_data_klmno")
    (t4 / "duplicate_indexed.mp4(1).json").write_text(
        json.dumps({
            "title": "Duplicate Suffix Video",
            "photoTakenTime": {
                "timestamp": "1641168000"
            }
        }, indent=2),
        encoding="utf-8"
    )

    # 5. Tier 5: -edited files inherit original sidecar
    t5 = FIXTURES_DIR / "tier5"
    t5.mkdir(parents=True, exist_ok=True)
    (t5 / "edited_clip-edited.mp4").write_bytes(b"synthetic_tier5_edited_bytes_data_pqrst")
    (t5 / "edited_clip.mp4.supplemental-metadata.json").write_text(
        json.dumps({
            "title": "Original Clip Sidecar",
            "description": "Edited video inheriting original sidecar",
            "photoTakenTime": {
                "timestamp": "1641254400"
            }
        }, indent=2),
        encoding="utf-8"
    )

    # 6. Tier 6: Fuzzy fallback within the same directory
    t6 = FIXTURES_DIR / "tier6"
    t6.mkdir(parents=True, exist_ok=True)
    (t6 / "fuzzy_event_recording_sample.mp4").write_bytes(b"synthetic_tier6_fuzzy_bytes_data_uvwxy")
    (t6 / "fuzzy_event_recording.json").write_text(
        json.dumps({
            "title": "Fuzzy Fallback Event",
            "photoTakenTime": {
                "timestamp": "1641340800"
            }
        }, indent=2),
        encoding="utf-8"
    )

    # 7. Missing sidecar (falls back to ffprobe creation_time or mtime)
    t7 = FIXTURES_DIR / "missing"
    t7.mkdir(parents=True, exist_ok=True)
    (t7 / "missing_sidecar_video.mp4").write_bytes(b"synthetic_missing_sidecar_bytes_data_zzzzz")

    # 8. Duplicate files (exact same sha256 content across two files)
    t8 = FIXTURES_DIR / "duplicates"
    t8.mkdir(parents=True, exist_ok=True)
    dup_bytes = b"EXACT_IDENTICAL_CONTENT_HASH_FOR_DUPLICATE_DETECTION"
    (t8 / "original_sample.mp4").write_bytes(dup_bytes)
    (t8 / "copy_sample.mp4").write_bytes(dup_bytes)
    (t8 / "original_sample.mp4.json").write_text(
        json.dumps({
            "title": "Original Duplicate Test Sample",
            "photoTakenTime": {
                "timestamp": "1641427200"
            }
        }, indent=2),
        encoding="utf-8"
    )

    # 9. Long video (> 15 minutes)
    t9 = FIXTURES_DIR / "long_video"
    t9.mkdir(parents=True, exist_ok=True)
    (t9 / "long_duration_video.mp4").write_bytes(b"synthetic_long_video_bytes_data_15minplus")
    (t9 / "long_duration_video.mp4.json").write_text(
        json.dumps({
            "title": "Long Video Over 15 Minutes",
            "photoTakenTime": {
                "timestamp": "1641513600"
            }
        }, indent=2),
        encoding="utf-8"
    )


if __name__ == "__main__":
    create_fixtures()
