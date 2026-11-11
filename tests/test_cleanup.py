"""Tests for `photos2yt cleanup`. Every file lives under tmp_path."""

from __future__ import annotations

import csv
import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pytest

from photos2yt import cli
from photos2yt.cleanup import CleanupError, build_plan
from photos2yt.config import Config, PathsConfig, QuotaConfig, YouTubeConfig
from photos2yt.db import RECONCILE_NOTE_PREFIX, get_connection, init_db

NOW = datetime(2026, 11, 11, 20, 0, tzinfo=timezone.utc)
RECENT = (NOW - timedelta(days=1)).isoformat()
OLD = (NOW - timedelta(days=30)).isoformat()


def make_config(tmp_path: Path, uploaded_dir: Optional[Path] = None) -> Config:
    return Config(
        paths=PathsConfig(
            staging_dir=tmp_path / "staging",
            db_path=tmp_path / "queue.db",
            credentials=tmp_path / "credentials.json",
            token=tmp_path / "token.json",
            uploaded_dir=uploaded_dir,
        ),
        timezone="America/New_York",
        youtube=YouTubeConfig(privacy="unlisted", category_id="22", made_for_kids=False,
                              title_template="{title}", default_title="Family Video"),
        quota=QuotaConfig(daily_unit_limit=10_000, upload_cost_units=50,
                          playlist_insert_cost=50, playlist_item_insert_cost=50),
        video_extensions=(".mp4",),
        config_path=tmp_path / "config.yaml",
    )


class Env:
    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.config = make_config(tmp_path)
        self.staging = tmp_path / "staging"
        self.dest = tmp_path / "staging-uploaded"
        self.conn = init_db(get_connection(self.config.paths.db_path))
        self.n = 0

    def add(self, rel: str, status: str = "UPLOADED", *, yt: Optional[str] = "auto",
            verified: Optional[str] = RECENT, last_error: Optional[str] = None,
            duplicate_of: Optional[int] = None, sidecar: Optional[str] = "own",
            content: bytes = b"video-bytes", size: Optional[int] = None,
            create: bool = True) -> int:
        self.n += 1
        path = self.staging / rel
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        side = None
        if sidecar == "own":
            side = path.with_name(path.name + ".json")
            side.write_text("{}")
        elif sidecar:
            side = self.staging / sidecar
            side.parent.mkdir(parents=True, exist_ok=True)
            side.write_text("{}")
        cur = self.conn.execute(
            "INSERT INTO videos (source_path, sidecar_path, sha256, size_bytes, status, created_at, "
            "updated_at, youtube_video_id, verified_at, last_error, duplicate_of) "
            "VALUES (?, ?, ?, ?, ?, 't', 't', ?, ?, ?, ?)",
            (str(path), str(side) if side else None, f"sha{self.n}",
             size if size is not None else len(content), status,
             (f"YT{self.n}" if yt == "auto" else yt), verified, last_error, duplicate_of),
        )
        self.conn.commit()
        return cur.lastrowid

    def close(self):
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.conn.close()

    def run(self, **kw) -> int:
        self.close()
        return cli.run_cleanup_command(self.config, now=NOW, **kw)


@pytest.fixture
def env(tmp_path) -> Env:
    return Env(tmp_path)


def db_hash(env: Env) -> str:
    return hashlib.sha256(env.config.paths.db_path.read_bytes()).hexdigest()


def test_moves_confirmed_video_and_sidecar_keeping_folders(env, capsys):
    env.add("2020/a.mp4")
    env.close()
    before = db_hash(env)
    assert cli.run_cleanup_command(env.config, now=NOW) == 0
    assert not (env.staging / "2020/a.mp4").exists()
    assert (env.dest / "2020/a.mp4").read_bytes() == b"video-bytes"
    assert (env.dest / "2020/a.mp4.json").exists()
    assert db_hash(env) == before  # database never changed
    with open(env.tmp / "cleanup_log.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    assert [r["action"] for r in rows] == ["moved", "moved"]
    assert rows[0]["youtube_video_id"] == "YT1"
    assert "Cleanup done: 2 file(s) moved" in capsys.readouterr().out


@pytest.mark.parametrize("kwargs,reason", [
    (dict(verified=None), "not confirmed by reconcile"),
    (dict(verified=OLD), "30 day(s) ago"),
    (dict(last_error=f"{RECONCILE_NOTE_PREFIX} 2026-11-11] missing on YouTube"), "reconcile problem"),
    (dict(yt=None), "no YouTube video id"),
])
def test_unconfirmed_uploads_are_left_alone(env, capsys, kwargs, reason):
    env.add("a.mp4", **kwargs)
    assert env.run() == 0
    assert (env.staging / "a.mp4").exists()
    out = capsys.readouterr().out
    assert reason in out and "0 video file(s)" in out


@pytest.mark.parametrize("status", ["PENDING", "UPLOADING", "FAILED", "SKIPPED"])
def test_rows_not_on_youtube_are_never_touched(env, status):
    env.add("a.mp4", status=status)
    env.run()
    assert (env.staging / "a.mp4").exists()


def test_duplicate_goes_with_its_original(env):
    orig = env.add("a.mp4")
    env.add("copy/a(1).mp4", status="SKIPPED", yt=None, verified=None, duplicate_of=orig)
    env.run()
    assert (env.dest / "copy/a(1).mp4").exists()


def test_duplicate_of_unconfirmed_original_stays(env):
    orig = env.add("a.mp4", verified=None)
    env.add("a(1).mp4", status="SKIPPED", yt=None, verified=None, duplicate_of=orig)
    env.run()
    assert (env.staging / "a(1).mp4").exists()


def test_shared_sidecar_stays_while_another_row_needs_it(env):
    env.add("a.mp4", sidecar="shared.json")
    env.add("b.mp4", status="PENDING", sidecar="shared.json")
    env.run()
    assert (env.dest / "a.mp4").exists()
    assert (env.staging / "shared.json").exists()


def test_changed_file_is_left_alone(env, capsys):
    env.add("a.mp4", size=999)
    assert env.run() == 0
    assert (env.staging / "a.mp4").exists()
    assert "file size changed" in capsys.readouterr().out


def test_file_outside_staging_is_left_alone(env, tmp_path, capsys):
    rid = env.add("a.mp4")
    outside = tmp_path / "elsewhere.mp4"
    outside.write_bytes(b"video-bytes")
    env.conn.execute("UPDATE videos SET source_path = ? WHERE id = ?", (str(outside), rid))
    env.conn.commit()
    env.run()
    assert outside.exists()
    assert "outside the staging folder" in capsys.readouterr().out


def test_existing_destination_is_not_overwritten(env, capsys):
    env.add("a.mp4")
    (env.dest).mkdir(parents=True)
    (env.dest / "a.mp4").write_bytes(b"other")
    env.run()
    assert (env.staging / "a.mp4").exists()
    assert (env.dest / "a.mp4").read_bytes() == b"other"
    assert "destination already exists" in capsys.readouterr().out


def test_already_moved_files_are_counted_not_errors(env, capsys):
    env.add("a.mp4", create=False, sidecar=None)
    assert env.run() == 0
    assert "1 eligible file(s) already gone" in capsys.readouterr().out


def test_dry_run_changes_nothing(env, capsys):
    env.add("a.mp4")
    assert env.run(dry_run=True) == 0
    assert (env.staging / "a.mp4").exists()
    assert not env.dest.exists()
    assert not (env.tmp / "cleanup_log.csv").exists()
    assert "[DRY RUN] Would move" in capsys.readouterr().out


def test_delete_needs_yes(env):
    env.add("a.mp4")
    assert env.run(delete=True) == 2
    assert (env.staging / "a.mp4").exists()


def test_delete_with_yes_removes_files(env):
    env.add("a.mp4")
    assert env.run(delete=True, yes=True) == 0
    assert not (env.staging / "a.mp4").exists()
    assert not env.dest.exists()
    with open(env.tmp / "cleanup_log.csv", newline="") as f:
        assert {r["action"] for r in csv.DictReader(f)} == {"deleted"}


def test_custom_destination_and_config_uploaded_dir(env, tmp_path):
    env.add("a.mp4")
    env.config = make_config(tmp_path, uploaded_dir=tmp_path / "done")
    env.run()
    assert (tmp_path / "done" / "a.mp4").exists()


def test_destination_inside_staging_is_refused(env, capsys):
    env.add("a.mp4")
    assert env.run(dest=str(env.staging / "done")) == 1
    assert (env.staging / "a.mp4").exists()
    assert "must not be inside the staging folder" in capsys.readouterr().err


def test_old_database_without_verified_at_asks_for_reconcile(tmp_path, capsys):
    config = make_config(tmp_path)
    conn = sqlite3.connect(config.paths.db_path)
    conn.execute("CREATE TABLE videos (id INTEGER PRIMARY KEY, source_path TEXT)")
    conn.commit()
    conn.close()
    assert cli.run_cleanup_command(config, now=NOW) == 1
    assert "Run `photos2yt reconcile` once" in capsys.readouterr().err


def test_missing_database(tmp_path, capsys):
    assert cli.run_cleanup_command(make_config(tmp_path), now=NOW) == 0
    assert not (tmp_path / "queue.db").exists()


def test_max_age_days_option(env):
    env.add("a.mp4", verified=(NOW - timedelta(days=3)).isoformat())
    env.run(max_age_days=2)
    assert (env.staging / "a.mp4").exists()
    env.conn = get_connection(env.config.paths.db_path)
    env.run(max_age_days=5)
    assert (env.dest / "a.mp4").exists()


def test_cli_parser():
    args = cli.create_parser().parse_args(["cleanup", "--dry-run", "--max-age-days", "3"])
    assert args.subcommand == "cleanup" and args.dry_run and args.max_age_days == 3
