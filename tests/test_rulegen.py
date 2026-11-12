"""Tests for photos2yt.rulegen (`photos2yt make-rules`). Files only under tmp_path."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml

from photos2yt.curator import PlaylistRule, _match_rule, load_rules
from photos2yt.db import VideoRecord
from photos2yt.rulegen import (
    HOLIDAYS,
    RulesInputError,
    build_rules,
    easter,
    make_rules_file,
)

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("year,expected", [
    (2019, date(2019, 4, 21)), (2020, date(2020, 4, 12)), (2024, date(2024, 3, 31)),
    (2025, date(2025, 4, 20)), (2026, date(2026, 4, 5)), (2038, date(2038, 4, 25)),
])
def test_easter(year, expected):
    assert easter(year) == expected


@pytest.mark.parametrize("key,year,expected", [
    ("thanksgiving", 2020, date(2020, 11, 26)), ("thanksgiving", 2026, date(2026, 11, 26)),
    ("thanksgiving", 2018, date(2018, 11, 22)),
    ("mothers_day", 2026, date(2026, 5, 10)), ("fathers_day", 2026, date(2026, 6, 21)),
    ("memorial_day", 2026, date(2026, 5, 25)), ("labor_day", 2026, date(2026, 9, 7)),
    ("memorial_day", 2021, date(2021, 5, 31)),
])
def test_moving_holidays(key, year, expected):
    assert HOLIDAYS[key][1](year) == [expected]


def video(local_date: str, title: str = "clip") -> VideoRecord:
    return VideoRecord.from_row(dict(id=1, source_path="/s/a.mp4", sha256="x", size_bytes=1,
                                     status="UPLOADED", created_at="t", updated_at="t",
                                     taken_local_date=local_date, title=title))


def rules_for(spec: dict) -> dict[str, PlaylistRule]:
    out = build_rules(spec)
    return {p["name"]: PlaylistRule(p["name"], p["privacy"], p["match"]) for p in out["playlists"]}


def matches(rules, name, local_date, title="clip") -> bool:
    return _match_rule(rules[name], video(local_date, title)) is not None


BASE = {"years": [2018, 2021]}


def test_year_playlists():
    rules = rules_for(BASE)
    assert list(rules) == [f"Family Videos {y}" for y in range(2018, 2022)]
    assert matches(rules, "Family Videos 2019", "2019-06-01")
    assert not matches(rules, "Family Videos 2019", "2020-06-01")


def test_no_year_playlists_and_custom_template():
    assert build_rules({**BASE, "year_playlist": None})["playlists"] == []
    names = [p["name"] for p in build_rules({**BASE, "year_playlist": "{year} Videos"})["playlists"]]
    assert names[0] == "2018 Videos"


def test_holidays_fixed_and_moving():
    rules = rules_for({**BASE, "year_playlist": None,
                       "holidays": ["christmas", "thanksgiving", "new_years"]})
    assert matches(rules, "Christmas Through the Years", "2019-12-24")
    assert matches(rules, "Christmas Through the Years", "2020-12-25")
    assert not matches(rules, "Christmas Through the Years", "2020-12-26")
    assert matches(rules, "Thanksgiving Through the Years", "2020-11-26")
    assert not matches(rules, "Thanksgiving Through the Years", "2020-11-25")
    assert matches(rules, "Thanksgiving Through the Years", "2018-11-22")
    assert not matches(rules, "Thanksgiving Through the Years", "2022-11-24")  # outside years
    assert matches(rules, "New Year's Through the Years", "2019-12-31")
    assert matches(rules, "New Year's Through the Years", "2020-01-01")


def test_birthdays_with_window_across_month_end():
    rules = rules_for({**BASE, "year_playlist": None,
                       "birthdays": [{"name": "Emma", "month": 3, "day": 1, "window_days": 2},
                                     {"name": "Leo", "month": 7, "day": 2,
                                      "playlist": "Leo Turns Another Year"}]})
    for d in ("2019-02-27", "2019-02-28", "2019-03-01", "2019-03-02", "2019-03-03"):
        assert matches(rules, "Emma's Birthdays", d), d
    assert not matches(rules, "Emma's Birthdays", "2019-03-04")
    assert matches(rules, "Leo Turns Another Year", "2021-07-02")
    assert not matches(rules, "Leo Turns Another Year", "2021-07-03")


def test_leap_day_birthday():
    rules = rules_for({**BASE, "year_playlist": None,
                       "birthdays": [{"name": "Max", "month": 2, "day": 29}]})
    assert matches(rules, "Max's Birthdays", "2020-02-29")


def test_trips_are_inclusive_date_ranges():
    rules = rules_for({**BASE, "year_playlist": None,
                       "trips": [{"name": "Maine", "start": "2021-06-29", "end": "2021-07-02"},
                                 {"name": "Wedding", "start": date(2019, 6, 22)}]})
    assert matches(rules, "Maine", "2021-06-29") and matches(rules, "Maine", "2021-07-02")
    assert not matches(rules, "Maine", "2021-07-03")
    assert matches(rules, "Wedding", "2019-06-22") and not matches(rules, "Wedding", "2019-06-23")


def test_keywords():
    rules = rules_for({**BASE, "year_playlist": None,
                       "keywords": [{"name": "Soccer", "words": ["soccer"]}]})
    assert matches(rules, "Soccer", "2019-01-01", title="Soccer game – Saturday")
    assert not matches(rules, "Soccer", "2019-01-01", title="Beach day")


@pytest.mark.parametrize("spec,message", [
    ({}, "years must be"),
    ({"years": [2021, 2018]}, "is after last"),
    ({**BASE, "privacy": "secret"}, "privacy must be"),
    ({**BASE, "holidays": ["diwali"]}, "Unknown holiday"),
    ({**BASE, "birthdays": [{"name": "X", "month": 2, "day": 30}]}, "not a real date"),
    ({**BASE, "birthdays": [{"month": 2, "day": 3}]}, "name must be"),
    ({**BASE, "trips": [{"name": "T", "start": "2021-07-05", "end": "2021-07-01"}]}, "end is before"),
    ({**BASE, "trips": [{"name": "T", "start": "July 5"}]}, "must be a date"),
    ({**BASE, "year_playlist": "Videos"}, "must contain {year}"),
    ({**BASE, "trips": [{"name": "Family Videos 2019", "start": "2019-01-01"}]}, "same name"),
    ({**BASE, "colour": "blue"}, "Unknown key"),
])
def test_invalid_input(spec, message):
    with pytest.raises(RulesInputError, match=message):
        build_rules(spec)


def test_example_input_builds_valid_rules(tmp_path):
    out = tmp_path / "rules.yaml"
    lines: list[str] = []
    assert make_rules_file(ROOT / "rules-input.yaml.example", out, out=lines.append) == 0
    rules = load_rules(out)
    assert "Thanksgiving Through the Years" in rules and "Emma's Birthdays" in rules
    assert out.read_text().startswith("# Generated by `photos2yt make-rules`")


def test_make_rules_file_refuses_overwrite_and_dry_run_writes_nothing(tmp_path):
    src = tmp_path / "in.yaml"
    src.write_text(yaml.safe_dump(BASE))
    out = tmp_path / "rules.yaml"
    out.write_text("playlists: []\n")
    lines: list[str] = []
    assert make_rules_file(src, out, out=lines.append) == 1
    assert "--force" in lines[-1] and out.read_text() == "playlists: []\n"
    assert make_rules_file(src, out, dry_run=True, out=lines.append) == 0
    assert out.read_text() == "playlists: []\n"
    assert make_rules_file(src, out, force=True, out=lines.append) == 0
    assert len(load_rules(out)) == 4
    assert not (tmp_path / "rules.yaml.tmp").exists()


def test_make_rules_file_reports_bad_input(tmp_path):
    src = tmp_path / "in.yaml"
    src.write_text("years: [2020]\n")
    lines: list[str] = []
    assert make_rules_file(src, tmp_path / "rules.yaml", out=lines.append) == 1
    assert "years must be" in lines[-1]
    assert not (tmp_path / "rules.yaml").exists()


def test_cli_parser():
    from photos2yt import cli
    args = cli.create_parser().parse_args(["make-rules", "--dry-run", "--force"])
    assert args.subcommand == "make-rules" and args.dry_run and args.force


def test_all_playlist_matches_every_video_even_without_a_date():
    rules = rules_for({**BASE, "all_playlist": "All Family Videos"})
    assert list(rules)[0] == "All Family Videos"
    for d in ("2019-06-01", "1999-01-01", None):
        assert _match_rule(rules["All Family Videos"], video(d)) is not None
    # still in its year playlist too
    assert matches(rules, "Family Videos 2019", "2019-06-01")


def test_no_all_playlist_by_default():
    assert "All Family Videos" not in rules_for(BASE)
