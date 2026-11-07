"""Tests for takeout ingest, tiered sidecar matching, ffprobe mocking, idempotency, and CSV reporting."""

import csv
from datetime import datetime, timezone
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from photos2yt.cli import main
from photos2yt.config import (
    Config,
    PathsConfig,
    QuotaConfig,
    YouTubeConfig,
)
from photos2yt.db import (
    VideoRecord,
    VideoStatus,
    get_connection,
    get_video_by_sha256,
    init_db,
    transition_video_status,
)
from photos2yt.takeout import (
    FFprobeExecutionError,
    FFprobeNotFoundError,
    VideoProbeResult,
    compute_sha256,
    find_sidecar,
    ingest,
    probe_video,
    walk_staging_dir,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "synthetic_takeout"


@pytest.fixture
def test_config(tmp_path: Path) -> Config:
    """Create a test Config pointing to a temporary database and the synthetic fixtures."""
    db_path = tmp_path / "test_queue.db"
    return Config(
        paths=PathsConfig(
            staging_dir=FIXTURES_DIR,
            db_path=db_path,
            credentials=tmp_path / "credentials.json",
            token=tmp_path / "token.json",
        ),
        timezone="America/New_York",
        youtube=YouTubeConfig(
            privacy="unlisted",
            category_id="22",
            made_for_kids=False,
            title_template="{date} - {title}",
            default_title="Family Video",
        ),
        quota=QuotaConfig(
            daily_unit_limit=10000,
            upload_cost_units=1600,
            playlist_insert_cost=50,
            playlist_item_insert_cost=50,
        ),
        video_extensions=(".mp4", ".mov", ".mkv"),
    )


# --- 1. Sidecar Matcher Tier Unit Tests ---

def test_tier_1_supplemental_metadata_matching():
    """Tier 1: <file>.supplemental-metadata.json match."""
    video = FIXTURES_DIR / "tier1" / "tier1_clip.mp4"
    match = find_sidecar(video)
    assert match.path is not None
    assert match.tier == "tier_1_supplemental_metadata"
    assert match.confidence == "high"
    assert match.path.name == "tier1_clip.mp4.supplemental-metadata.json"


def test_tier_2_json_matching():
    """Tier 2: <file>.json match."""
    video = FIXTURES_DIR / "tier2" / "holiday_trip.mp4"
    match = find_sidecar(video)
    assert match.path is not None
    assert match.tier == "tier_2_json"
    assert match.confidence == "high"
    assert match.path.name == "holiday_trip.mp4.json"


def test_tier_3_truncated_name_matching():
    """Tier 3: Truncated-name variants (~46 chars base / truncated supplemental-metadata)."""
    # Truncated long base name
    long_video = FIXTURES_DIR / "tier3" / "very_long_synthetic_filename_exceeding_forty_six_characters_test.mp4"
    match = find_sidecar(long_video)
    assert match.path is not None
    assert match.tier == "tier_3_truncated"
    assert match.confidence == "high"
    assert match.path.name == "very_long_synthetic_filename_exceeding_forty_s.json"

    # Truncated 'supplemental-metadata' suffix
    supp_video = FIXTURES_DIR / "tier3" / "clip_truncated_supplemental.mp4"
    match_supp = find_sidecar(supp_video)
    assert match_supp.path is not None
    assert match_supp.tier == "tier_3_truncated"
    assert match_supp.confidence == "high"
    assert match_supp.path.name == "clip_truncated_supplemental.mp4.supple.json"


def test_tier_4_duplicate_suffix_matching():
    """Tier 4: Duplicate-suffix handling: name(1).mp4 <-> name.mp4(1).json."""
    video = FIXTURES_DIR / "tier4" / "duplicate_indexed(1).mp4"
    match = find_sidecar(video)
    assert match.path is not None
    assert match.tier == "tier_4_duplicate_suffix"
    assert match.confidence == "high"
    assert match.path.name == "duplicate_indexed.mp4(1).json"


def test_tier_5_edited_inherits_original():
    """Tier 5: -edited files inherit the original's sidecar."""
    video = FIXTURES_DIR / "tier5" / "edited_clip-edited.mp4"
    match = find_sidecar(video)
    assert match.path is not None
    assert match.tier == "tier_5_edited_original"
    assert match.confidence == "high"
    assert match.path.name == "edited_clip.mp4.supplemental-metadata.json"


def test_tier_6_fuzzy_fallback():
    """Tier 6: Fuzzy fallback within same directory (recorded as low confidence)."""
    video = FIXTURES_DIR / "tier6" / "fuzzy_event_recording_sample.mp4"
    match = find_sidecar(video)
    assert match.path is not None
    assert match.tier == "tier_6_fuzzy"
    assert match.confidence == "low"
    assert match.path.name == "fuzzy_event_recording.json"


def test_unmatched_sidecar():
    """Missing sidecar returns unmatched with confidence none."""
    video = FIXTURES_DIR / "missing" / "missing_sidecar_video.mp4"
    match = find_sidecar(video)
    assert match.path is None
    assert match.tier == "unmatched"
    assert match.confidence == "none"


# --- 2. FFprobe Mocking & Probing Tests ---

def test_ffprobe_missing_fails_clearly():
    """If ffprobe is not installed, fail with a clear message rather than guessing."""
    with patch("photos2yt.takeout.check_ffprobe_available", return_value=False):
        with pytest.raises(FFprobeNotFoundError) as exc_info:
            probe_video(Path("/tmp/some_video.mp4"))
        assert "ffprobe is not installed or not found in PATH" in str(exc_info.value)


def test_probe_video_parses_json_output():
    """Mock ffprobe JSON output parsing duration and creation_time."""
    mock_json = {
        "format": {
            "duration": "123.456",
            "tags": {
                "creation_time": "2020-12-24T20:00:00.000000Z"
            }
        }
    }
    with patch("photos2yt.takeout.check_ffprobe_available", return_value=True):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(mock_json), stderr="")
            res = probe_video(Path("/tmp/test.mp4"))

    assert res.duration_s == pytest.approx(123.456)
    assert res.creation_time == datetime(2020, 12, 24, 20, 0, 0, tzinfo=timezone.utc)


def test_probe_video_nonzero_exit_raises_execution_error():
    """Non-zero exit code from ffprobe raises FFprobeExecutionError."""
    with patch("photos2yt.takeout.check_ffprobe_available", return_value=True):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Corrupt video stream")
            with pytest.raises(FFprobeExecutionError) as exc_info:
                probe_video(Path("/tmp/corrupt.mp4"))
            assert "Corrupt video stream" in str(exc_info.value)


# --- 3. Full Ingest Integration Tests (Mocked FFprobe) ---

def _mock_probe_for_fixtures(video_path: Path) -> VideoProbeResult:
    """Mock helper returning duration and creation_time based on filename."""
    name = video_path.name
    if "long_duration" in name:
        # Long file > 15 minutes (960 seconds)
        return VideoProbeResult(duration_s=960.0, creation_time=datetime(2021, 6, 1, 10, 0, 0, tzinfo=timezone.utc))
    elif "missing" in name:
        # Missing sidecar: provide creation_time via ffprobe to test fallback
        return VideoProbeResult(duration_s=30.0, creation_time=datetime(2021, 7, 10, 15, 30, 0, tzinfo=timezone.utc))
    return VideoProbeResult(duration_s=45.0, creation_time=None)


@patch("photos2yt.takeout.check_ffprobe_available", return_value=True)
@patch("photos2yt.takeout.probe_video", side_effect=_mock_probe_for_fixtures)
def test_full_ingest_synthetic_fixtures(mock_probe, mock_avail, test_config: Config, tmp_path: Path):
    """Full ingest run on synthetic fixtures covering all tiers, timezone edge, duplicates, and CSV report."""
    report_file = tmp_path / "test_ingest_report.csv"
    summary = ingest(config=test_config, dry_run=False, report_path=report_file)

    # 1. Verify summary counts
    assert summary.total_scanned == 11
    assert summary.duplicate_count == 1
    assert summary.skipped == 1
    assert summary.new_pending == 10
    assert summary.long_file_count == 1

    # Verify matcher tiers present
    assert "tier_1_supplemental_metadata" in summary.matched_by_tier
    assert "tier_2_json" in summary.matched_by_tier
    assert "tier_3_truncated" in summary.matched_by_tier
    assert "tier_4_duplicate_suffix" in summary.matched_by_tier
    assert "tier_5_edited_original" in summary.matched_by_tier
    assert "tier_6_fuzzy" in summary.matched_by_tier
    assert summary.unmatched_count >= 1

    # 2. Verify Dec 24 8 PM Eastern edge case in DB
    conn = get_connection(test_config.paths.db_path)
    cursor = conn.execute("SELECT * FROM videos WHERE source_path LIKE '%tier1_clip.mp4'")
    row_t1 = cursor.fetchone()
    assert row_t1 is not None
    assert row_t1["taken_utc"] == "2020-12-25T01:00:00Z"
    assert row_t1["taken_local_date"] == "2020-12-24"
    assert row_t1["status"] == "PENDING"
    assert row_t1["lat"] == pytest.approx(40.7128)
    assert row_t1["lon"] == pytest.approx(-74.0060)

    # 3. Verify duplicate handling in DB
    # One of the two identical files must be marked SKIPPED with duplicate_of set
    cursor = conn.execute("SELECT * FROM videos WHERE status = 'SKIPPED'")
    skipped_rows = cursor.fetchall()
    assert len(skipped_rows) == 1
    dup_row = skipped_rows[0]
    assert "Duplicate of" in dup_row["last_error"]
    assert "duplicates" in dup_row["source_path"]
    assert dup_row["duplicate_of"] is not None

    canonical_row = conn.execute("SELECT * FROM videos WHERE id = ?", (dup_row["duplicate_of"],)).fetchone()
    assert canonical_row is not None
    assert canonical_row["status"] == "PENDING"
    assert canonical_row["duplicate_of"] is None
    assert dup_row["sha256"] == canonical_row["sha256"]
    assert len(dup_row["sha256"]) == 64
    assert ":dup:" not in dup_row["sha256"]
    assert dup_row["last_error"] == f"Duplicate of {canonical_row['source_path']}"

    # 4. Verify fallback date source used for missing sidecar
    cursor = conn.execute("SELECT * FROM videos WHERE source_path LIKE '%missing_sidecar_video.mp4'")
    row_missing = cursor.fetchone()
    assert row_missing is not None
    assert row_missing["taken_utc"] == "2021-07-10T15:30:00Z"
    assert row_missing["taken_local_date"] == "2021-07-10"

    # 5. Verify CSV report file
    assert report_file.is_file()
    with open(report_file, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    assert len(rows) == 11
    # Check headers
    expected_headers = {
        "source_path", "status", "matcher_tier", "confidence",
        "sidecar_path", "date_source", "taken_utc", "taken_local_date",
        "duration_s", "is_long_file", "sha256", "size_bytes", "title", "notes"
    }
    assert expected_headers.issubset(reader.fieldnames)

    # Find the long file in report
    long_items = [r for r in rows if r["is_long_file"] == "True"]
    assert len(long_items) == 1
    assert "long_duration_video.mp4" in long_items[0]["source_path"]
    assert float(long_items[0]["duration_s"]) > 900.0

    conn.close()


# --- 4. Idempotency Tests ---

@patch("photos2yt.takeout.check_ffprobe_available", return_value=True)
@patch("photos2yt.takeout.probe_video", side_effect=_mock_probe_for_fixtures)
def test_ingest_idempotency_and_preserve_advanced_status(mock_probe, mock_avail, test_config: Config, tmp_path: Path):
    """Re-running ingest must not create duplicate rows or reset the status of rows already past PENDING."""
    report1 = tmp_path / "report1.csv"
    report2 = tmp_path / "report2.csv"

    # Run 1: initial ingest
    s1 = ingest(config=test_config, dry_run=False, report_path=report1)
    assert s1.new_pending == 10

    conn = get_connection(test_config.paths.db_path)
    cursor = conn.execute("SELECT count(*) FROM videos")
    total_db_rows_after_run1 = cursor.fetchone()[0]

    # Advance one video's status to UPLOADED
    cursor = conn.execute("SELECT id FROM videos WHERE status = 'PENDING' LIMIT 1")
    vid_id = cursor.fetchone()["id"]
    # Legal transitions: PENDING -> UPLOADING -> UPLOADED
    transition_video_status(conn, vid_id, VideoStatus.UPLOADING)
    transition_video_status(conn, vid_id, VideoStatus.UPLOADED, youtube_video_id="TEST_YT_123")
    conn.close()

    # Run 2: re-run ingest on the exact same directory
    s2 = ingest(config=test_config, dry_run=False, report_path=report2)
    assert s2.new_pending == 0  # No new videos queued
    assert s2.already_present == 11  # All files recognized as already present in database

    # Verify database: no duplicate rows created
    conn = get_connection(test_config.paths.db_path)
    cursor = conn.execute("SELECT count(*) FROM videos")
    total_db_rows_after_run2 = cursor.fetchone()[0]
    assert total_db_rows_after_run2 == total_db_rows_after_run1

    # Verify that the advanced video status was NOT reset to PENDING
    cursor = conn.execute("SELECT status, youtube_video_id FROM videos WHERE id = ?", (vid_id,))
    row = cursor.fetchone()
    assert row["status"] == VideoStatus.UPLOADED.value
    assert row["youtube_video_id"] == "TEST_YT_123"
    conn.close()


@patch("photos2yt.takeout.check_ffprobe_available", return_value=True)
@patch("photos2yt.takeout.probe_video", side_effect=_mock_probe_for_fixtures)
def test_rerun_ingest_twice_with_duplicates_identical_counts_and_statuses(mock_probe, mock_avail, test_config: Config, tmp_path: Path):
    """Re-running ingest twice with duplicates present yields identical row counts and statuses."""
    # First ingest run
    s1 = ingest(config=test_config, dry_run=False, report_path=tmp_path / "rep1.csv")
    assert s1.total_scanned == 11
    assert s1.new_pending == 10
    assert s1.skipped == 1

    conn = get_connection(test_config.paths.db_path)
    rows_after_run1 = conn.execute(
        "SELECT id, source_path, sha256, status, duplicate_of, last_error FROM videos ORDER BY id ASC"
    ).fetchall()
    counts_run1 = conn.execute("SELECT status, count(*) FROM videos GROUP BY status ORDER BY status ASC").fetchall()
    conn.close()

    assert len(rows_after_run1) == 11
    # Statuses: 10 PENDING, 1 SKIPPED
    status_dict_1 = {r[0]: r[1] for r in counts_run1}
    assert status_dict_1 == {"PENDING": 10, "SKIPPED": 1}

    # Second ingest run on exact same directory
    s2 = ingest(config=test_config, dry_run=False, report_path=tmp_path / "rep2.csv")
    assert s2.total_scanned == 11
    assert s2.new_pending == 0
    assert s2.already_present == 11

    conn = get_connection(test_config.paths.db_path)
    rows_after_run2 = conn.execute(
        "SELECT id, source_path, sha256, status, duplicate_of, last_error FROM videos ORDER BY id ASC"
    ).fetchall()
    counts_run2 = conn.execute("SELECT status, count(*) FROM videos GROUP BY status ORDER BY status ASC").fetchall()
    conn.close()

    # Identical row counts and statuses
    assert len(rows_after_run2) == len(rows_after_run1) == 11
    assert counts_run2 == counts_run1
    for r1, r2 in zip(rows_after_run1, rows_after_run2):
        assert tuple(r1) == tuple(r2)


@patch("photos2yt.takeout.check_ffprobe_available", return_value=True)
def test_deterministic_canonical_choice(mock_avail, tmp_path: Path):
    """The first path in sorted order keeps the real row (PENDING, duplicate_of=None); later files become SKIPPED."""
    staging = tmp_path / "staging"
    staging.mkdir()

    # Create 3 video files with identical content
    content = b"fake identical video bytes"
    p_z = staging / "z_last.mp4"
    p_a = staging / "a_first.mp4"
    p_m = staging / "m_middle.mp4"

    p_z.write_bytes(content)
    p_a.write_bytes(content)
    p_m.write_bytes(content)

    cfg = Config(
        paths=PathsConfig(
            staging_dir=staging,
            db_path=tmp_path / "deterministic.db",
            credentials=tmp_path / "c.json",
            token=tmp_path / "t.json",
        ),
        timezone="America/New_York",
        youtube=YouTubeConfig(
            privacy="unlisted",
            category_id="22",
            made_for_kids=False,
            title_template="{title}",
            default_title="Test",
        ),
        quota=QuotaConfig(
            daily_unit_limit=10000,
            upload_cost_units=1600,
            playlist_insert_cost=50,
            playlist_item_insert_cost=50,
        ),
        video_extensions=(".mp4",),
    )

    with patch("photos2yt.takeout.probe_video", return_value=VideoProbeResult(duration_s=10.0)):
        summary = ingest(cfg, dry_run=False, report_path=tmp_path / "det_report.csv")

    assert summary.total_scanned == 3
    assert summary.new_pending == 1
    assert summary.skipped == 2
    assert summary.duplicate_count == 2

    conn = get_connection(cfg.paths.db_path)
    rows = conn.execute(
        "SELECT id, source_path, sha256, status, duplicate_of, last_error FROM videos ORDER BY source_path ASC"
    ).fetchall()
    conn.close()

    assert len(rows) == 3
    # Sorted order of paths: a_first.mp4, m_middle.mp4, z_last.mp4
    row_a, row_m, row_z = rows[0], rows[1], rows[2]

    # a_first.mp4 is canonical
    assert row_a["source_path"] == str(p_a)
    assert row_a["status"] == "PENDING"
    assert row_a["duplicate_of"] is None
    canonical_id = row_a["id"]

    # m_middle.mp4 is duplicate of a_first.mp4
    assert row_m["source_path"] == str(p_m)
    assert row_m["status"] == "SKIPPED"
    assert row_m["duplicate_of"] == canonical_id
    assert row_m["last_error"] == f"Duplicate of {str(p_a)}"

    # z_last.mp4 is duplicate of a_first.mp4
    assert row_z["source_path"] == str(p_z)
    assert row_z["status"] == "SKIPPED"
    assert row_z["duplicate_of"] == canonical_id
    assert row_z["last_error"] == f"Duplicate of {str(p_a)}"

    # All share the exact same 64-character sha256
    assert row_a["sha256"] == row_m["sha256"] == row_z["sha256"]
    assert len(row_a["sha256"]) == 64
    assert ":dup:" not in row_a["sha256"]


@patch("photos2yt.takeout.check_ffprobe_available", return_value=True)
def test_duplicate_canonical_file_disappears(mock_avail, tmp_path: Path):
    """If a previously ingested duplicate's canonical file disappears from disk, do nothing special; keep data consistent."""
    staging = tmp_path / "disappear_staging"
    staging.mkdir()

    content = b"sample video bytes duplicate test"
    p_orig = staging / "1_original.mp4"
    p_copy = staging / "2_copy.mp4"
    p_orig.write_bytes(content)
    p_copy.write_bytes(content)

    cfg = Config(
        paths=PathsConfig(
            staging_dir=staging,
            db_path=tmp_path / "disappear.db",
            credentials=tmp_path / "c.json",
            token=tmp_path / "t.json",
        ),
        timezone="America/New_York",
        youtube=YouTubeConfig(
            privacy="unlisted",
            category_id="22",
            made_for_kids=False,
            title_template="{title}",
            default_title="Test",
        ),
        quota=QuotaConfig(
            daily_unit_limit=10000,
            upload_cost_units=1600,
            playlist_insert_cost=50,
            playlist_item_insert_cost=50,
        ),
        video_extensions=(".mp4",),
    )

    with patch("photos2yt.takeout.probe_video", return_value=VideoProbeResult(duration_s=15.0)):
        ingest(cfg, dry_run=False, report_path=tmp_path / "rep1.csv")

    conn = get_connection(cfg.paths.db_path)
    orig_row = conn.execute("SELECT * FROM videos WHERE source_path = ?", (str(p_orig),)).fetchone()
    copy_row = conn.execute("SELECT * FROM videos WHERE source_path = ?", (str(p_copy),)).fetchone()
    assert orig_row["status"] == "PENDING"
    assert copy_row["status"] == "SKIPPED"
    assert copy_row["duplicate_of"] == orig_row["id"]
    conn.close()

    # Canonical file disappears from disk
    p_orig.unlink()

    # Re-run ingest
    with patch("photos2yt.takeout.probe_video", return_value=VideoProbeResult(duration_s=15.0)):
        s2 = ingest(cfg, dry_run=False, report_path=tmp_path / "rep2.csv")

    # Ingest scanned the 1 remaining file and recognized it as already present
    assert s2.total_scanned == 1
    assert s2.already_present == 1

    # Database records remain completely consistent
    conn = get_connection(cfg.paths.db_path)
    orig_after = conn.execute("SELECT * FROM videos WHERE source_path = ?", (str(p_orig),)).fetchone()
    copy_after = conn.execute("SELECT * FROM videos WHERE source_path = ?", (str(p_copy),)).fetchone()
    conn.close()

    assert orig_after is not None
    assert orig_after["status"] == "PENDING"
    assert copy_after is not None
    assert copy_after["status"] == "SKIPPED"
    assert copy_after["duplicate_of"] == orig_after["id"]
    assert copy_after["last_error"] == f"Duplicate of {str(p_orig)}"


# --- 5. Dry-Run Tests ---

@patch("photos2yt.takeout.check_ffprobe_available", return_value=True)
@patch("photos2yt.takeout.probe_video", side_effect=_mock_probe_for_fixtures)
def test_ingest_dry_run_leaves_database_untouched(mock_probe, mock_avail, test_config: Config, tmp_path: Path):
    """Running ingest with dry_run=True scans, reports, but leaves DB completely empty (writes nothing)."""
    report_file = tmp_path / "dry_run_report.csv"
    summary = ingest(config=test_config, dry_run=True, report_path=report_file)

    assert summary.total_scanned == 11
    assert summary.new_pending == 10
    assert summary.duplicate_count == 1
    assert summary.skipped == 1
    assert report_file.is_file()

    # DB file should either not exist or have 0 rows in videos table
    if test_config.paths.db_path.is_file():
        conn = get_connection(test_config.paths.db_path)
        cursor = conn.execute("SELECT count(*) FROM videos")
        count = cursor.fetchone()[0]
        assert count == 0
        conn.close()

    # Also test running dry_run on an existing populated database writes nothing
    # 1. Non-dry-run ingest to populate
    ingest(config=test_config, dry_run=False, report_path=tmp_path / "populated.csv")
    conn = get_connection(test_config.paths.db_path)
    count_before = conn.execute("SELECT count(*) FROM videos").fetchone()[0]
    mtime_before = test_config.paths.db_path.stat().st_mtime
    conn.close()

    # 2. Dry run against populated database
    summary2 = ingest(config=test_config, dry_run=True, report_path=tmp_path / "dry_populated.csv")
    assert summary2.total_scanned == 11
    assert summary2.already_present == 11

    conn = get_connection(test_config.paths.db_path)
    count_after = conn.execute("SELECT count(*) FROM videos").fetchone()[0]
    conn.close()
    assert count_after == count_before


# --- 6. CLI Ingest Command Tests ---

@patch("photos2yt.takeout.check_ffprobe_available", return_value=True)
@patch("photos2yt.takeout.probe_video", side_effect=_mock_probe_for_fixtures)
def test_cli_ingest_dry_run_and_report_args(mock_probe, mock_avail, test_config: Config, tmp_path: Path):
    """CLI photos2yt ingest works with --dry-run and --report."""
    cfg_file = tmp_path / "test_config.yaml"
    cfg_file.write_text(f"""
paths:
  staging_dir: "{FIXTURES_DIR}"
  db_path: "{tmp_path / 'cli_test.db'}"
  credentials: "{tmp_path / 'creds.json'}"
  token: "{tmp_path / 'token.json'}"
timezone: America/New_York
youtube:
  privacy: unlisted
  category_id: "22"
  made_for_kids: false
  title_template: "{{date}} - {{title}}"
  default_title: "Family Video"
quota:
  daily_unit_limit: 10000
  upload_cost_units: 1600
  playlist_insert_cost: 50
  playlist_item_insert_cost: 50
video_extensions: [.mp4, .mov]
""", encoding="utf-8")

    cli_report = tmp_path / "cli_report.csv"
    code = main(["-c", str(cfg_file), "ingest", "--dry-run", "--report", str(cli_report)])
    assert code == 0
    assert cli_report.is_file()
