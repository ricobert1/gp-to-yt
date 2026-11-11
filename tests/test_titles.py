"""Tests for title building (caption / meaningful name / default) and `photos2yt retitle`."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from photos2yt import cli
from photos2yt.config import Config, PathsConfig, QuotaConfig, YouTubeConfig
from photos2yt.db import get_connection, get_video, init_db
from photos2yt.metadata import (
    caption_from_description,
    choose_title_subject,
    format_title,
    is_meaningless_name,
)
from photos2yt.retitle import apply_retitle, build_retitle_plan

TEMPLATE = "{title} – {weekday}, {month_name} {day_num}, {year} {time}"
DT = datetime(2020, 12, 24, 18, 15)  # local time


# ---------------------------------------------------------------------------
# Camera-name detection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "IMG_6791", "IMG_6791(1)", "IMG_6791-edited", "PXL_20201224_181500123",
    "VID-20201224-WA0001", "VID_20201224_181500", "MVI_0042", "DSCN0042", "GOPR0123",
    "DJI_0001", "C0001", "P1010001", "20201224_181500", "2020-12-24 18.15.00",
    "ScreenRecording_12-24-2020", "RPReplay_Final1608", "trim.4B2A9C1E-1234", "12345",
    "8f14e45f-ceea-467a-9f0e-1234567890ab", "", "   ", None,
])
def test_camera_names_are_meaningless(name):
    assert is_meaningless_name(name)


@pytest.mark.parametrize("name", [
    "Emma first steps", "Christmas Eve", "trimmed birthday", "Copy of party",
    "C3PO party", "Grandma 2020", "Beach day 1", "IMG of the year", "GoPro surfing",
])
def test_real_names_are_kept(name):
    assert not is_meaningless_name(name)


# ---------------------------------------------------------------------------
# Subject choice and template
# ---------------------------------------------------------------------------

def test_caption_first_line_wins():
    assert caption_from_description("\n  Christmas Eve at Grandma's \nmore text") == \
        "Christmas Eve at Grandma's"
    assert caption_from_description("   \n ") is None
    assert caption_from_description(None) is None
    assert choose_title_subject("IMG_1.MOV", "IMG_1", "Family Video",
                                caption="Opening <presents>") == "Opening presents"


def test_meaningful_name_then_default():
    assert choose_title_subject("Emma first steps.mov", "Emma first steps", "Family Video") == \
        "Emma first steps"
    assert choose_title_subject("IMG_6791.MOV", "IMG_6791", "Family Video") == "Family Video"
    assert choose_title_subject(None, "PXL_20201224_181500123", "Family Video") == "Family Video"
    assert choose_title_subject(None, None, "Family Video") == "Family Video"


def test_template_examples():
    assert format_title(TEMPLATE, "Family Video", "IMG_6791.MOV", "2020-12-24", DT, "IMG_6791") == \
        "Family Video – Thursday, December 24, 2020 6:15 PM"
    assert format_title(TEMPLATE, "Family Video", "IMG_6791.MOV", "2020-12-24", DT, "IMG_6791",
                        caption="Christmas Eve at Grandma's") == \
        "Christmas Eve at Grandma's – Thursday, December 24, 2020 6:15 PM"


def test_all_placeholders():
    t = format_title(
        "{date}|{year}|{month}|{day}|{day_num}|{month_name}|{month_short}|{weekday}|"
        "{weekday_short}|{time}|{default_title}",
        "FV", None, "2021-03-05", datetime(2021, 3, 5, 0, 7), None)
    assert t == "2021-03-05|2021|03|05|5|March|Mar|Friday|Fri|12:07 AM|FV"
    assert format_title("{time}", "FV", None, "x", datetime(2021, 3, 5, 12, 0)) == "12:00 PM"


def test_long_caption_is_shortened_but_date_kept():
    t = format_title(TEMPLATE, "Family Video", None, "2020-12-24", DT, None, caption="x" * 300)
    assert len(t) == 100
    assert t.endswith(" – Thursday, December 24, 2020 6:15 PM")


def test_unknown_placeholder_falls_back():
    assert format_title("{nope} {title}", "FV", "Party", "2020-12-24", DT) == "2020-12-24 - Party"


def test_ingest_uses_caption_and_hides_camera_names(tmp_path):
    from photos2yt.takeout import VideoProbeResult, ingest
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "IMG_0001.MOV").write_bytes(b"one")
    (staging / "IMG_0001.MOV.json").write_text(json.dumps({
        "title": "IMG_0001.MOV", "description": "Snowball fight",
        "photoTakenTime": {"timestamp": "1608851700"}}))  # 2020-12-24 23:15 UTC
    (staging / "IMG_0002.MOV").write_bytes(b"two")
    (staging / "IMG_0002.MOV.json").write_text(json.dumps({
        "title": "IMG_0002.MOV", "photoTakenTime": {"timestamp": "1608851700"}}))
    cfg = _config(tmp_path, staging=staging)
    with patch("photos2yt.takeout.check_ffprobe_available", return_value=True), \
         patch("photos2yt.takeout.probe_video", return_value=VideoProbeResult(duration_s=5.0)):
        ingest(cfg, dry_run=False, report_path=tmp_path / "r.csv")
    conn = get_connection(cfg.paths.db_path)
    titles = sorted(r[0] for r in conn.execute("SELECT title FROM videos"))
    conn.close()
    assert titles == ["Family Video – Thursday, December 24, 2020 6:15 PM",
                      "Snowball fight – Thursday, December 24, 2020 6:15 PM"]


# ---------------------------------------------------------------------------
# retitle
# ---------------------------------------------------------------------------

def _config(tmp_path: Path, *, staging: Path | None = None, template: str = TEMPLATE) -> Config:
    return Config(
        paths=PathsConfig(
            staging_dir=staging or tmp_path, db_path=tmp_path / "queue.db",
            credentials=tmp_path / "c.json", token=tmp_path / "t.json",
        ),
        timezone="America/New_York",
        youtube=YouTubeConfig(privacy="unlisted", category_id="22", made_for_kids=False,
                              title_template=template, default_title="Family Video"),
        quota=QuotaConfig(daily_unit_limit=10000, upload_cost_units=1600,
                          playlist_insert_cost=50, playlist_item_insert_cost=50),
        video_extensions=(".mov",),
    )


def _add(conn, tmp_path, name, *, status="PENDING", sidecar: dict | None = None,
         session=None, title=None, taken_utc="2020-12-24T23:15:00Z") -> int:
    sidecar_path = None
    if sidecar is not None:
        sidecar_path = tmp_path / f"{name}.json"
        sidecar_path.write_text(json.dumps(sidecar))
    cur = conn.execute(
        "INSERT INTO videos (source_path, sidecar_path, sha256, size_bytes, status, created_at, "
        "updated_at, taken_utc, taken_local_date, title, upload_session_uri) "
        "VALUES (?, ?, ?, 1, ?, 't', 't', ?, '2020-12-24', ?, ?)",
        (f"/staging/{name}", str(sidecar_path) if sidecar_path else None, name, status,
         taken_utc, title if title is not None else f"2020-12-24 - {Path(name).stem}", session),
    )
    conn.commit()
    return cur.lastrowid


@pytest.fixture
def db(tmp_path):
    conn = init_db(get_connection(tmp_path / "queue.db"))
    yield conn
    conn.close()


def test_retitle_changes_only_rows_not_yet_uploading(db, tmp_path):
    cfg = _config(tmp_path)
    pending = _add(db, tmp_path, "IMG_1.MOV", sidecar={"title": "IMG_1.MOV"})
    captioned = _add(db, tmp_path, "IMG_2.MOV",
                     sidecar={"title": "IMG_2.MOV", "description": "Snowball fight"})
    failed = _add(db, tmp_path, "Emma first steps.mov", status="FAILED")
    with_session = _add(db, tmp_path, "IMG_3.MOV", status="FAILED", session="https://x/s")
    uploading = _add(db, tmp_path, "IMG_4.MOV", status="UPLOADING")
    uploaded = _add(db, tmp_path, "IMG_5.MOV", status="UPLOADED")

    plan = build_retitle_plan(db, cfg)
    assert {v.id for v, _ in plan.changes} == {pending, captioned, failed}
    assert apply_retitle(db, plan) == 3

    t = lambda i: get_video(db, i).title  # noqa: E731
    assert t(pending) == "Family Video – Thursday, December 24, 2020 6:15 PM"
    assert t(captioned) == "Snowball fight – Thursday, December 24, 2020 6:15 PM"
    assert t(failed) == "Emma first steps – Thursday, December 24, 2020 6:15 PM"
    assert t(with_session) == "2020-12-24 - IMG_3"
    assert t(uploading) == "2020-12-24 - IMG_4"
    assert t(uploaded) == "2020-12-24 - IMG_5"

    again = build_retitle_plan(db, cfg)
    assert again.changes == [] and again.unchanged == 3


def test_retitle_keeps_title_when_sidecar_unreadable(db, tmp_path):
    cfg = _config(tmp_path)
    vid = _add(db, tmp_path, "IMG_1.MOV", sidecar={"description": "Caption"})
    Path(get_video(db, vid).sidecar_path).unlink()
    plan = build_retitle_plan(db, cfg)
    assert plan.changes == [] and len(plan.skipped) == 1
    assert "sidecar not readable" in plan.skipped[0][1]


def test_cli_retitle_dry_run_is_read_only_then_real(tmp_path, capsys):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(f"""\
paths: {{staging_dir: {tmp_path}, db_path: queue.db, credentials: c.json, token: t.json}}
timezone: America/New_York
youtube:
  privacy: unlisted
  category_id: "22"
  made_for_kids: false
  title_template: "{TEMPLATE}"
  default_title: "Family Video"
quota: {{daily_unit_limit: 10000, upload_cost_units: 1600, playlist_insert_cost: 50,
        playlist_item_insert_cost: 50}}
video_extensions: [.mov]
""", encoding="utf-8")
    conn = init_db(get_connection(tmp_path / "queue.db"))
    vid = _add(conn, tmp_path, "IMG_1.MOV")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    before = (tmp_path / "queue.db").read_bytes()

    assert cli.main(["-c", str(cfg_file), "retitle", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "'2020-12-24 - IMG_1' -> 'Family Video – Thursday, December 24, 2020 6:15 PM'" in out
    assert (tmp_path / "queue.db").read_bytes() == before

    assert cli.main(["-c", str(cfg_file), "retitle"]) == 0
    conn = get_connection(tmp_path / "queue.db")
    assert get_video(conn, vid).title == "Family Video – Thursday, December 24, 2020 6:15 PM"
    conn.close()


def test_cli_retitle_missing_db(tmp_path, capsys):
    cfg = _config(tmp_path)
    assert cli.run_retitle_command(cfg, dry_run=True) == 0
    assert cli.run_retitle_command(cfg) == 0
    assert not (tmp_path / "queue.db").exists()
