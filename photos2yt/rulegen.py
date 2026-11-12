"""Build rules.yaml for `curate` from a short input file (`photos2yt make-rules`).

The rules file only knows exact dates (local_year / local_month / local_day)
and keywords. Writing year playlists, moving holidays (Thanksgiving, Easter,
...) and multi-day trips by hand is tedious, so this expands a small input:

    privacy: unlisted
    years: [2012, 2026]                       # first and last year, inclusive
    year_playlist: "Family Videos {year}"     # omit or null for no year playlists
    holiday_playlist: "{holiday} Through the Years"
    holidays: [christmas, thanksgiving, halloween, july_4th, easter]
    birthdays:
      - {name: Emma, month: 3, day: 14}               # "Emma's Birthdays"
      - {name: Dad, month: 7, day: 2, window_days: 3} # also 3 days either side
    trips:
      - {name: "Maine Trip 2021", start: 2021-07-10, end: 2021-07-17}
    keywords:                                 # needs Takeout captions
      - {name: "Soccer", words: [soccer, game]}

All dates are local dates (config.yaml timezone), the same ones curate matches.
The output is validated with the curator's own rules loader before writing.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

import yaml

from photos2yt.curator import MAX_PLAYLIST_TITLE, VALID_PRIVACY, RulesError, load_rules


class RulesInputError(ValueError):
    """The make-rules input file is invalid."""


# --- Holidays (US) ----------------------------------------------------------

def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-th (1-based) weekday (Mon=0) of a month; n=-1 means the last one."""
    if n > 0:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def easter(year: int) -> date:
    """Western (Gregorian) Easter Sunday (anonymous Gregorian algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    weekday_corr = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * weekday_corr) // 451
    month, day = divmod(h + weekday_corr - 7 * m + 114, 31)
    return date(year, month, day + 1)


# key -> (display name, fixed (month, day) pairs, or a function year -> list of dates)
HOLIDAYS: dict[str, tuple[str, Any]] = {
    "new_years": ("New Year's", [(12, 31), (1, 1)]),
    "valentines": ("Valentine's Day", [(2, 14)]),
    "st_patricks": ("St. Patrick's Day", [(3, 17)]),
    "easter": ("Easter", lambda y: [easter(y)]),
    "mothers_day": ("Mother's Day", lambda y: [_nth_weekday(y, 5, 6, 2)]),
    "memorial_day": ("Memorial Day", lambda y: [_nth_weekday(y, 5, 0, -1)]),
    "fathers_day": ("Father's Day", lambda y: [_nth_weekday(y, 6, 6, 3)]),
    "july_4th": ("Fourth of July", [(7, 4)]),
    "labor_day": ("Labor Day", lambda y: [_nth_weekday(y, 9, 0, 1)]),
    "halloween": ("Halloween", [(10, 31)]),
    "thanksgiving": ("Thanksgiving", lambda y: [_nth_weekday(y, 11, 3, 4)]),
    "christmas": ("Christmas", [(12, 24), (12, 25)]),
}


# --- Input parsing ----------------------------------------------------------

def _int(value: Any, what: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise RulesInputError(f"{what} must be a whole number from {lo} to {hi}, got {value!r}")
    return value


def _date(value: Any, what: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise RulesInputError(f"{what} must be a date like 2021-07-10, got {value!r}") from None


def _name(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RulesInputError(f"{what} must be a non-empty text")
    return value.strip()


def _md_valid(month: int, day: int) -> bool:
    try:
        date(2024, month, day)  # leap year, so Feb 29 is allowed
        return True
    except ValueError:
        return False


def _day_conditions(start: date, end: date) -> list[dict]:
    out, d = [], start
    while d <= end:
        out.append({"local_year": d.year, "local_month": d.month, "local_day": d.day})
        d += timedelta(days=1)
    return out


def _md_window(month: int, day: int, window: int) -> list[dict]:
    """month/day pairs within ``window`` days of a date, year-independent."""
    ref_year = 2024 if (month, day) == (2, 29) else 2023
    centre = date(ref_year, month, day)
    seen, out = set(), []
    for offset in range(-window, window + 1):
        d = centre + timedelta(days=offset)
        if (d.month, d.day) not in seen:
            seen.add((d.month, d.day))
            out.append({"local_month": d.month, "local_day": d.day})
    return out


def build_rules(spec: dict) -> dict:
    """Expand the input mapping into a rules.yaml mapping ({'playlists': [...]})."""
    if not isinstance(spec, dict):
        raise RulesInputError("The input file must be a mapping (key: value lines)")
    known = {"privacy", "years", "year_playlist", "holiday_playlist", "holidays",
             "birthdays", "trips", "keywords"}
    unknown = sorted(set(spec) - known)
    if unknown:
        raise RulesInputError(f"Unknown key(s) {unknown}; allowed: {sorted(known)}")

    privacy = str(spec.get("privacy", "unlisted")).strip().lower()
    if privacy not in VALID_PRIVACY:
        raise RulesInputError(f"privacy must be one of {', '.join(VALID_PRIVACY)}, got {privacy!r}")

    years = spec.get("years")
    if not (isinstance(years, list) and len(years) == 2):
        raise RulesInputError("years must be [first, last], e.g. [2012, 2026]")
    first = _int(years[0], "years[0]", 1900, 2100)
    last = _int(years[1], "years[1]", 1900, 2100)
    if first > last:
        raise RulesInputError(f"years: first ({first}) is after last ({last})")

    playlists: list[dict] = []

    def add(name: str, conditions: list[dict]) -> None:
        playlists.append({"name": name, "privacy": privacy, "match": {"any": conditions}})

    year_tpl = spec.get("year_playlist", "Family Videos {year}")
    if year_tpl:
        if "{year}" not in str(year_tpl):
            raise RulesInputError("year_playlist must contain {year}")
        for y in range(first, last + 1):
            add(str(year_tpl).replace("{year}", str(y)), [{"local_year": y}])

    holiday_tpl = str(spec.get("holiday_playlist", "{holiday} Through the Years"))
    if "{holiday}" not in holiday_tpl:
        raise RulesInputError("holiday_playlist must contain {holiday}")
    holidays = spec.get("holidays") or []
    if not isinstance(holidays, list):
        raise RulesInputError("holidays must be a list")
    for key in holidays:
        if key not in HOLIDAYS:
            raise RulesInputError(f"Unknown holiday {key!r}; choose from: {', '.join(HOLIDAYS)}")
        label, rule = HOLIDAYS[key]
        if callable(rule):
            conds = [c for y in range(first, last + 1) for d in rule(y)
                     for c in _day_conditions(d, d)]
        else:
            conds = [{"local_month": m, "local_day": d} for m, d in rule]
        add(holiday_tpl.replace("{holiday}", label), conds)

    for i, b in enumerate(spec.get("birthdays") or []):
        if not isinstance(b, dict):
            raise RulesInputError(f"birthdays[{i}] must be a mapping like {{name: Emma, month: 3, day: 14}}")
        who = _name(b.get("name"), f"birthdays[{i}].name")
        month = _int(b.get("month"), f"birthdays[{i}].month", 1, 12)
        day = _int(b.get("day"), f"birthdays[{i}].day", 1, 31)
        if not _md_valid(month, day):
            raise RulesInputError(f"birthdays[{i}]: {month}/{day} is not a real date")
        window = _int(b.get("window_days", 0), f"birthdays[{i}].window_days", 0, 14)
        title = b.get("playlist") or f"{who}'s Birthdays"
        add(_name(title, f"birthdays[{i}].playlist"), _md_window(month, day, window))

    for i, t in enumerate(spec.get("trips") or []):
        if not isinstance(t, dict):
            raise RulesInputError(f"trips[{i}] must be a mapping with name, start and end")
        name = _name(t.get("name"), f"trips[{i}].name")
        start = _date(t.get("start"), f"trips[{i}].start")
        end = _date(t.get("end", t.get("start")), f"trips[{i}].end")
        if end < start:
            raise RulesInputError(f"trips[{i}] ({name}): end is before start")
        if (end - start).days > 366:
            raise RulesInputError(f"trips[{i}] ({name}): longer than a year; use a year playlist")
        add(name, _day_conditions(start, end))

    for i, k in enumerate(spec.get("keywords") or []):
        if not isinstance(k, dict):
            raise RulesInputError(f"keywords[{i}] must be a mapping with name and words")
        name = _name(k.get("name"), f"keywords[{i}].name")
        words = k.get("words")
        if not isinstance(words, list) or not words or not all(isinstance(w, str) and w.strip() for w in words):
            raise RulesInputError(f"keywords[{i}] ({name}): words must be a list of words")
        add(name, [{"title_contains_any": [w.strip() for w in words]}])

    names = [p["name"] for p in playlists]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise RulesInputError(f"Two playlists would have the same name: {dupes}")
    too_long = [n for n in names if len(n) > MAX_PLAYLIST_TITLE or "<" in n or ">" in n]
    if too_long:
        raise RulesInputError(f"Playlist names must be at most {MAX_PLAYLIST_TITLE} characters "
                              f"without '<' or '>': {too_long}")
    return {"playlists": playlists}


HEADER = """\
# Generated by `photos2yt make-rules` from {source}.
# Edit that file and run make-rules again rather than editing this one.
# Check with: photos2yt curate --dry-run
"""


def make_rules_file(input_path: Path, output_path: Path, *, force: bool = False,
                    dry_run: bool = False, out: Callable[[str], None] = print) -> int:
    """Read the input, write the rules file, validate it. Returns an exit code."""
    try:
        with open(input_path, encoding="utf-8") as f:
            spec = yaml.safe_load(f)
    except FileNotFoundError:
        out(f"Input file not found: {input_path} (copy rules-input.yaml.example)")
        return 1
    except yaml.YAMLError as err:
        out(f"Could not read {input_path}: {err}")
        return 1
    try:
        rules = build_rules(spec)
    except RulesInputError as err:
        out(f"Problem in {input_path}: {err}")
        return 1
    if dry_run:
        out(f"[DRY RUN] Would write {output_path} with {len(rules['playlists'])} playlist(s):")
        for p in rules["playlists"]:
            out(f"  {p['name']}  ({len(p['match']['any'])} date/keyword condition(s))")
        return 0
    if output_path.exists() and not force:
        out(f"{output_path} already exists; add --force to replace it.")
        return 1
    text = HEADER.format(source=input_path.name) + yaml.safe_dump(
        rules, sort_keys=False, allow_unicode=True, width=120)
    tmp = output_path.with_name(output_path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        loaded = load_rules(tmp)  # the curator's own validation
    except RulesError as err:
        tmp.unlink()
        out(f"Internal error: generated rules are invalid: {err}")
        return 1
    tmp.replace(output_path)
    out(f"Wrote {output_path} with {len(loaded)} playlist(s):")
    for p in rules["playlists"]:
        out(f"  {p['name']}  ({len(p['match']['any'])} date/keyword condition(s))")
    out("Next: photos2yt curate --dry-run")
    return 0


def default_paths(config) -> tuple[Path, Path]:
    """(input, output) defaults: next to the config file; output = paths.rules if set."""
    base = Path(config.config_path).parent if getattr(config, "config_path", None) else Path.cwd()
    output = Path(config.paths.rules) if config.paths.rules else base / "rules.yaml"
    return base / "rules-input.yaml", output
