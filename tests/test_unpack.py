"""Tests for `photos2yt unpack`. Real zip files, all under tmp_path."""

from __future__ import annotations

import csv
import zipfile
from pathlib import Path

import pytest

from photos2yt import cli
from photos2yt.config import Config, PathsConfig, QuotaConfig, YouTubeConfig
from photos2yt.unpack import export_name, run_unpack

GB = 1_000_000_000


def make_config(tmp_path: Path, inbox: Path | None = None) -> Config:
    return Config(
        paths=PathsConfig(staging_dir=tmp_path / "staging", db_path=tmp_path / "queue.db",
                          credentials=tmp_path / "c.json", token=tmp_path / "t.json",
                          takeout_inbox=inbox),
        timezone="America/New_York",
        youtube=YouTubeConfig(privacy="unlisted", category_id="22", made_for_kids=False,
                              title_template="{title}", default_title="Family Video"),
        quota=QuotaConfig(daily_unit_limit=10_000, upload_cost_units=50,
                          playlist_insert_cost=50, playlist_item_insert_cost=50),
        video_extensions=(".mp4", ".mov"),
        config_path=tmp_path / "config.yaml",
    )


ALBUM = "Takeout/Google Photos/Takeout 2026-11/"


def make_zip(path: Path, entries: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return path


@pytest.fixture
def env(tmp_path):
    inbox = tmp_path / "gp-to-yt-inbox"
    inbox.mkdir()
    return tmp_path, inbox, make_config(tmp_path)


def run(config, **kw):
    lines: list[str] = []
    summary = run_unpack(config, out=lines.append, disk_free=lambda p: 100 * GB, **kw)
    return summary, "\n".join(lines)


def test_export_name_groups_parts():
    assert export_name("takeout-20261112T101500Z-001.zip") == "takeout-20261112T101500Z"
    assert export_name("takeout-20261112T101500Z-002.zip") == "takeout-20261112T101500Z"
    assert export_name("takeout-20261112T101500Z.zip") == "takeout-20261112T101500Z"


def test_extracts_only_videos_and_sidecars_into_staging(env):
    tmp, inbox, config = env
    make_zip(inbox / "takeout-20261112T101500Z-001.zip", {
        ALBUM + "IMG_1.MOV": b"video1",
        ALBUM + "IMG_1.MOV.supplemental-metadata.json": b"{}",
        ALBUM + "IMG_2.jpg": b"photo",
        ALBUM + "metadata.json": b"{}",
        "Takeout/archive_browser.html": b"<html>",
    })
    summary, out = run(config)
    dest = tmp / "staging" / "takeout-20261112T101500Z" / ALBUM
    assert (dest / "IMG_1.MOV").read_bytes() == b"video1"
    assert (dest / "IMG_1.MOV.supplemental-metadata.json").exists()
    assert not (dest / "IMG_2.jpg").exists()
    assert not (tmp / "staging" / "takeout-20261112T101500Z" / "Takeout/archive_browser.html").exists()
    assert summary.zips_done == 1 and summary.extracted == 3
    assert (inbox / "unpacked" / "takeout-20261112T101500Z-001.zip").exists()
    assert not (inbox / "takeout-20261112T101500Z-001.zip").exists()
    assert not list((tmp / "staging").rglob("*.unpacking"))
    with open(tmp / "unpack_log.csv", newline="") as f:
        assert [r["status"] for r in csv.DictReader(f)] == ["unpacked"]


def test_parts_of_one_export_share_a_folder_so_sidecars_match(env):
    tmp, inbox, config = env
    make_zip(inbox / "takeout-X-001.zip", {ALBUM + "clip.mp4": b"v"})
    make_zip(inbox / "takeout-X-002.zip", {ALBUM + "clip.mp4.json": b"{}"})
    run(config)
    folder = tmp / "staging" / "takeout-X" / ALBUM
    assert (folder / "clip.mp4").exists() and (folder / "clip.mp4.json").exists()


def test_nothing_to_do(env):
    _, _, config = env
    summary, out = run(config)
    assert summary.zips_done == 0 and "nothing to unpack" in out


def test_dry_run_writes_nothing(env):
    tmp, inbox, config = env
    make_zip(inbox / "takeout-A.zip", {ALBUM + "a.mp4": b"v"})
    summary, out = run(config, dry_run=True)
    assert "[DRY RUN]" in out and "1 video(s)" in out
    assert not (tmp / "staging").exists()
    assert (inbox / "takeout-A.zip").exists()
    assert not (tmp / "unpack_log.csv").exists()


def test_existing_files_are_never_overwritten(env):
    tmp, inbox, config = env
    dest = tmp / "staging" / "takeout-A" / ALBUM
    dest.mkdir(parents=True)
    (dest / "same.mp4").write_bytes(b"v")
    (dest / "diff.mp4").write_bytes(b"something else")
    make_zip(inbox / "takeout-A.zip", {ALBUM + "same.mp4": b"v", ALBUM + "diff.mp4": b"new"})
    summary, out = run(config)
    assert (dest / "diff.mp4").read_bytes() == b"something else"
    assert "1 already there, 1 conflict(s) skipped" in out


def test_unsafe_paths_are_refused(env):
    tmp, inbox, config = env
    make_zip(inbox / "takeout-evil.zip", {"../../escape.mp4": b"x"})
    summary, out = run(config)
    assert summary.zips_failed == 1 and "unsafe path" in out
    assert not (tmp / "escape.mp4").exists()
    assert (inbox / "takeout-evil.zip").exists()  # left for a human


def test_corrupt_zip_stays_in_inbox_and_is_logged(env):
    tmp, inbox, config = env
    (inbox / "takeout-broken.zip").write_bytes(b"not a zip at all")
    summary, out = run(config)
    assert summary.zips_failed == 1 and "re-download" in out
    assert (inbox / "takeout-broken.zip").exists()
    with open(tmp / "unpack_log.csv", newline="") as f:
        assert [r["status"] for r in csv.DictReader(f)] == ["failed"]


def test_not_enough_disk_space(env):
    tmp, inbox, config = env
    make_zip(inbox / "takeout-A.zip", {ALBUM + "a.mp4": b"v" * 10})
    lines: list[str] = []
    summary = run_unpack(config, out=lines.append, disk_free=lambda p: GB // 2)
    assert summary.zips_failed == 1 and "not enough disk space" in "\n".join(lines)
    assert (inbox / "takeout-A.zip").exists()
    assert not list((tmp / "staging").rglob("*.mp4"))


def test_partial_downloads_and_other_files_are_ignored(env):
    tmp, inbox, config = env
    (inbox / "takeout-A.zip.crdownload").write_bytes(b"partial")
    (inbox / "holiday.zip").write_bytes(b"x")
    summary, out = run(config)
    assert "nothing to unpack" in out


def test_config_inbox_and_cli_parser(tmp_path):
    inbox = tmp_path / "my-inbox"
    make_zip(inbox / "takeout-B.zip", {ALBUM + "b.mov": b"v"})
    config = make_config(tmp_path, inbox=inbox)
    args = cli.create_parser().parse_args(["unpack", "--dry-run"])
    assert args.subcommand == "unpack" and args.dry_run
    summary, out = run(config)
    assert (tmp_path / "staging" / "takeout-B" / ALBUM / "b.mov").exists()
