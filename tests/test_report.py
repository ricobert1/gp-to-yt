"""Tests for photos2yt.report and `photos2yt report` (Task 8).

The manifest is generated from a seeded database. Every output file is
written under tmp_path; the report never calls the API.
"""

from __future__ import annotations

import csv
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pytest

from photos2yt import cli
from photos2yt.config import Config, PathsConfig, QuotaConfig, YouTubeConfig
from photos2yt.db import (
    RECONCILE_NOTE_PREFIX,
    get_connection,
    init_db,
    record_playlist_item_added,
    add_playlist_membership,
    set_quota_exhausted,
    upsert_playlist,
    upsert_quota_log,
)
from photos2yt.quota import _pacific_today
from photos2yt.report import (
    MANIFEST_COLUMNS,
    build_report,
    format_summary,
    write_report,
)

FROZEN_NOW = datetime(2026, 11, 11, 20, 0, tzinfo=timezone.utc)  # 12:00 PST, 2026-11-11
PACKAGE_DIR = Path(__file__).resolve().parent.parent / "photos2yt"


def make_config(tmp_path: Path) -> Config:
    return Config(
        paths=PathsConfig(
            staging_dir=tmp_path,
            db_path=tmp_path / "data" / "queue.db",
            credentials=tmp_path / "credentials.json",
            token=tmp_path / "token.json",
        ),
        timezone="America/New_York",
        youtube=YouTubeConfig(
            privacy="unlisted", category_id="22", made_for_kids=False,
            title_template="{date} - {title}", default_title="Family Video",
        ),
        quota=QuotaConfig(
            daily_unit_limit=10_000, upload_cost_units=1600,
            playlist_insert_cost=50, playlist_item_insert_cost=50,
            daily_upload_limit=6,
        ),
        video_extensions=(".mp4",),
        config_path=tmp_path / "config.yaml",
    )


@pytest.fixture
def config(tmp_path) -> Config:
    return make_config(tmp_path)


def add(conn, name: str, status: str, *, yt: Optional[str] = None, privacy: Optional[str] = None,
        taken: str = "2020-01-01T12:00:00Z", last_error: Optional[str] = None,
        duplicate_of: Optional[int] = None) -> int:
    sha = hashlib.sha256(name.encode()).hexdigest()
    cur = conn.execute(
        "INSERT INTO videos (source_path, sha256, size_bytes, status, created_at, updated_at, "
        "taken_utc, taken_local_date, title, youtube_video_id, actual_privacy, last_error, "
        "duplicate_of) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (f"/staging/Takeout/Google Photos/{name}", sha, 1024, status, "t", "t", taken,
         taken[:10], name, yt, privacy, last_error, duplicate_of),
    )
    conn.commit()
    return cur.lastrowid


@pytest.fixture
def seeded(config):
    """A database with one row in each interesting state."""
    conn = init_db(get_connection(config.paths.db_path))
    ids = {}
    ids["xmas"] = add(conn, "IMG_0001.mp4", "PLAYLISTED", yt="AAA", privacy="unlisted",
                      taken="2020-12-25T15:00:00Z")
    ids["both"] = add(conn, "VID_2.mp4", "PLAYLISTED", yt="BBB", privacy="unlisted",
                      taken="2021-12-25T15:00:00Z")
    ids["plain"] = add(conn, "beach.mp4", "UPLOADED", yt="CCC", privacy="unlisted",
                       taken="2019-07-04T18:00:00Z")
    ids["locked"] = add(conn, "locked.mp4", "UPLOADED", yt="DDD", privacy="private",
                        taken="2019-08-01T18:00:00Z")
    ids["gone"] = add(conn, "gone.mp4", "UPLOADED", yt="EEE", privacy="unlisted",
                      taken="2018-01-01T00:00:00Z",
                      last_error=f"{RECONCILE_NOTE_PREFIX} 2026-11-11] missing on YouTube")
    ids["unverified"] = add(conn, "new.mp4", "UPLOADED", yt="FFF", privacy=None,
                            taken="2022-02-02T00:00:00Z")
    ids["pending"] = add(conn, "pending.mp4", "PENDING")
    ids["failed"] = add(conn, "broken.mp4", "FAILED", last_error="HTTP 400 invalidVideo: bad file")
    ids["long"] = add(conn, "long.mp4", "SKIPPED", last_error="longer than 15 minutes")
    ids["dup"] = add(conn, "IMG_0001(1).mp4", "SKIPPED", duplicate_of=ids["xmas"])
    ids["stuck"] = add(conn, "stuck.mp4", "UPLOADING")

    upsert_playlist(conn, "Christmas", "PLX")
    upsert_playlist(conn, "Holidays", "PLH")
    record_playlist_item_added(conn, ids["xmas"], "Christmas", 0)
    record_playlist_item_added(conn, ids["both"], "Christmas", 1)
    record_playlist_item_added(conn, ids["both"], "Holidays", 0)
    add_playlist_membership(conn, ids["plain"], "Holidays", added=False, position=1)

    today = _pacific_today(lambda: FROZEN_NOW)
    upsert_quota_log(conn, today, units_used=1752, uploads_used=1)
    upsert_quota_log(conn, "2026-11-10", units_used=9999, uploads_used=6)  # yesterday: ignored
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    return ids


def read_manifest(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def test_manifest_from_seeded_db(config, seeded, tmp_path):
    out_dir = tmp_path / "out"
    conn = get_connection(config.paths.db_path)
    report, manifest, summary = write_report(conn, config, out_dir, clock=lambda: FROZEN_NOW)
    conn.close()
    assert manifest == out_dir / "manifest.csv" and summary == out_dir / "summary.txt"

    rows = read_manifest(manifest)
    with open(manifest, encoding="utf-8", newline="") as f:
        assert next(csv.reader(f)) == list(MANIFEST_COLUMNS)
    # only videos on YouTube, ordered by taken_utc
    assert [r["video_id"] for r in rows] == ["EEE", "CCC", "DDD", "AAA", "BBB", "FFF"]
    by_id = {r["video_id"]: r for r in rows}

    assert by_id["AAA"]["original_filename"] == "IMG_0001.mp4"
    assert by_id["AAA"]["url"] == "https://youtu.be/AAA"
    assert by_id["AAA"]["privacy"] == "unlisted"
    assert by_id["AAA"]["playlists"] == "Christmas"
    assert by_id["AAA"]["status"] == "PLAYLISTED"
    assert by_id["AAA"]["source_path"] == "/staging/Takeout/Google Photos/IMG_0001.mp4"
    assert by_id["AAA"]["attention"] == ""
    assert by_id["BBB"]["playlists"] == "Christmas; Holidays"
    # an add not yet done (added=0) is not listed as a playlist
    assert by_id["CCC"]["playlists"] == ""
    assert "Holidays" in by_id["CCC"]["attention"]
    assert "config requests 'unlisted'" in by_id["DDD"]["attention"]
    assert "missing on YouTube" in by_id["EEE"]["attention"]
    assert by_id["FFF"]["privacy"] == ""
    assert "not verified" in by_id["FFF"]["attention"]


def test_summary_counts_quota_and_attention(config, seeded):
    conn = get_connection(config.paths.db_path)
    report = build_report(conn, config, clock=lambda: FROZEN_NOW)
    conn.close()
    assert report.status_counts == {
        "PENDING": 1, "UPLOADING": 1, "UPLOADED": 4, "PLAYLISTED": 2, "FAILED": 1, "SKIPPED": 2,
    }
    assert report.duplicates_skipped == 1
    assert (report.units_used, report.uploads_used) == (1752, 1)
    text = format_summary(report)
    assert "units used: 1752 of 10000" in text
    assert "uploads:    1 of 6" in text
    assert "TOTAL       11" in text
    assert "On YouTube (in manifest): 6" in text
    attention = "\n".join(f"{a.filename}: {a.problem}" for a in report.attention)
    assert "broken.mp4: FAILED: HTTP 400 invalidVideo" in attention
    assert "long.mp4: SKIPPED: longer than 15 minutes" in attention
    assert "stuck.mp4: upload interrupted" in attention
    assert "gone.mp4: [reconcile" in attention
    assert "locked.mp4: privacy is 'private'" in attention
    assert "new.mp4: privacy not verified" in attention
    assert "IMG_0001(1).mp4" not in attention  # duplicate skip is fine
    assert "pending.mp4" not in attention
    assert len(report.attention) == 7


def test_quota_exhausted_shown(config, seeded):
    conn = get_connection(config.paths.db_path)
    set_quota_exhausted(conn, _pacific_today(lambda: FROZEN_NOW), "2026-11-11T19:00:00+00:00")
    text = format_summary(build_report(conn, config, clock=lambda: FROZEN_NOW))
    conn.close()
    assert "marked exhausted" in text


def test_cli_report_is_read_only_and_defaults_next_to_config(config, seeded, tmp_path, capsys):
    db = config.paths.db_path
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    package_before = sorted(p.name for p in PACKAGE_DIR.iterdir())
    assert cli.run_report_command(config) == 0
    assert (tmp_path / "manifest.csv").is_file()  # config_path is tmp_path/config.yaml
    assert (tmp_path / "summary.txt").read_text(encoding="utf-8").startswith("photos2yt report")
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert not db.with_name(db.name + "-wal").exists()
    assert sorted(p.name for p in PACKAGE_DIR.iterdir()) == package_before
    out = capsys.readouterr().out
    assert "Needs attention (7)" in out and "manifest.csv (6 video(s))" in out


def test_cli_report_out_dir(config, seeded, tmp_path):
    target = tmp_path / "reports" / "nested"
    assert cli.main(["-c", str(_write_config(tmp_path)), "report", "--out-dir", str(target)]) == 0
    assert len(read_manifest(target / "manifest.csv")) == 6


def test_cli_report_missing_db(config, tmp_path, capsys):
    assert cli.run_report_command(config, out_dir=tmp_path / "out") == 0
    assert not config.paths.db_path.exists()
    assert not (tmp_path / "out").exists()
    assert "No database" in capsys.readouterr().out


def test_empty_db_writes_header_only(config, tmp_path):
    init_db(get_connection(config.paths.db_path)).close()
    conn = get_connection(config.paths.db_path)
    report, manifest, _ = write_report(conn, config, tmp_path / "o", clock=lambda: FROZEN_NOW)
    conn.close()
    assert read_manifest(manifest) == []
    assert "Needs attention: nothing." in format_summary(report)


def _write_config(tmp_path: Path) -> Path:
    """A real config.yaml (relative db path) matching make_config's database."""
    p = tmp_path / "config.yaml"
    p.write_text(
        "paths:\n"
        "  staging_dir: .\n"
        "  db_path: data/queue.db\n"
        "  credentials: credentials.json\n"
        "  token: token.json\n"
        "timezone: America/New_York\n"
        "youtube:\n"
        "  privacy: unlisted\n"
        "  category_id: '22'\n"
        "  made_for_kids: false\n"
        "  title_template: '{date} - {title}'\n"
        "  default_title: Family Video\n"
        "quota:\n"
        "  daily_unit_limit: 10000\n"
        "  upload_cost_units: 1600\n"
        "  playlist_insert_cost: 50\n"
        "  playlist_item_insert_cost: 50\n"
        "video_extensions: ['.mp4']\n",
        encoding="utf-8",
    )
    return p
