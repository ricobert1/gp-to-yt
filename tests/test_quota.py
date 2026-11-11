"""Tests for the quota tracker (Task 5).

All tests use an in-memory SQLite database and a frozen clock — no real
network calls, no real sleeping.  Clock injection follows the pattern:

    clock=lambda: datetime(2026, 3, 8, 7, 59, 0, tzinfo=timezone.utc)

The quota day is determined by America/Los_Angeles.  UTC offset is:
  PST (UTC-8)  Nov–Mar
  PDT (UTC-7)  Mar–Nov

Spring-forward 2026: clocks jump from 1:59:59 PST → 3:00:00 PDT on
  2026-03-08 → that day is only 23 hours long.
    PST midnight 2026-03-08 = 2026-03-08 08:00 UTC
    PDT midnight 2026-03-09 = 2026-03-09 07:00 UTC
    Duration = 23 h

Fall-back 2026: clocks fall from 1:59:59 PDT → 1:00:00 PST on
  2026-11-01 → that day is 25 hours long.
    PDT midnight 2026-11-01 = 2026-11-01 07:00 UTC
    PST midnight 2026-11-02 = 2026-11-02 08:00 UTC
    Duration = 25 h
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from photos2yt.config import QuotaConfig, Config, PathsConfig, YouTubeConfig
from photos2yt.db import (
    get_connection,
    get_quota_log,
    init_db,
    upsert_quota_log,
)
from photos2yt.quota import QuotaTracker, _pacific_today, _next_pacific_midnight

_PACIFIC = ZoneInfo("America/Los_Angeles")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_config(
    daily_unit_limit: int = 10_000,
    upload_cost_units: int = 1600,
    daily_upload_limit: int | None = None,
    playlist_insert_cost: int = 50,
    playlist_item_insert_cost: int = 50,
) -> Config:
    """Return a minimal Config with the given quota settings."""
    import tempfile, pathlib
    tmp = pathlib.Path(tempfile.mkdtemp())
    return Config(
        paths=PathsConfig(
            staging_dir=tmp,
            db_path=tmp / "queue.db",
            credentials=tmp / "credentials.json",
            token=tmp / "token.json",
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
            daily_unit_limit=daily_unit_limit,
            upload_cost_units=upload_cost_units,
            daily_upload_limit=daily_upload_limit,
            playlist_insert_cost=playlist_insert_cost,
            playlist_item_insert_cost=playlist_item_insert_cost,
        ),
        video_extensions=(".mp4", ".mov"),
    )


@pytest.fixture
def db() -> sqlite3.Connection:
    """In-memory database with schema migrations applied."""
    conn = get_connection(":memory:")
    init_db(conn)
    return conn


@pytest.fixture
def cfg() -> Config:
    return _make_config()


def _tracker(
    db: sqlite3.Connection,
    cfg: Config,
    clock_dt: datetime,
) -> QuotaTracker:
    """Convenience: build a QuotaTracker with a frozen clock."""
    return QuotaTracker(db, cfg, clock=lambda: clock_dt)


# ---------------------------------------------------------------------------
# Helper: fixed Pacific / UTC datetimes
# ---------------------------------------------------------------------------

def _pacific(year, month, day, hour=12, minute=0, second=0) -> datetime:
    """Return a timezone-aware datetime in America/Los_Angeles."""
    return datetime(year, month, day, hour, minute, second, tzinfo=_PACIFIC)


def _utc(year, month, day, hour=12, minute=0, second=0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


# ===========================================================================
# _pacific_today
# ===========================================================================

class TestPacificToday:
    def test_pst_time(self):
        """UTC 2026-01-15 20:00 → PST 2026-01-15 12:00 → '2026-01-15'."""
        clock = lambda: _utc(2026, 1, 15, 20, 0, 0)
        assert _pacific_today(clock) == "2026-01-15"

    def test_pst_just_before_midnight(self):
        """UTC 2026-01-16 07:59:59 → PST 2026-01-15 23:59:59 → '2026-01-15'."""
        clock = lambda: _utc(2026, 1, 16, 7, 59, 59)
        assert _pacific_today(clock) == "2026-01-15"

    def test_pst_at_midnight(self):
        """UTC 2026-01-16 08:00:00 → PST 2026-01-16 00:00:00 → '2026-01-16'."""
        clock = lambda: _utc(2026, 1, 16, 8, 0, 0)
        assert _pacific_today(clock) == "2026-01-16"

    def test_pdt_time(self):
        """UTC 2026-07-04 19:00 → PDT 2026-07-04 12:00 → '2026-07-04'."""
        clock = lambda: _utc(2026, 7, 4, 19, 0, 0)
        assert _pacific_today(clock) == "2026-07-04"

    def test_pdt_just_before_midnight(self):
        """UTC 2026-07-05 06:59:59 → PDT 2026-07-04 23:59:59 → '2026-07-04'."""
        clock = lambda: _utc(2026, 7, 5, 6, 59, 59)
        assert _pacific_today(clock) == "2026-07-04"

    def test_pdt_at_midnight(self):
        """UTC 2026-07-05 07:00:00 → PDT 2026-07-05 00:00:00 → '2026-07-05'."""
        clock = lambda: _utc(2026, 7, 5, 7, 0, 0)
        assert _pacific_today(clock) == "2026-07-05"

    def test_naive_clock_treated_as_utc(self):
        """Naive datetime from clock is treated as UTC."""
        clock = lambda: datetime(2026, 1, 16, 8, 0, 0)  # naive, == PST midnight
        assert _pacific_today(clock) == "2026-01-16"

    def test_aware_pacific_clock(self):
        """Pacific-aware datetime passed directly works."""
        clock = lambda: _pacific(2026, 6, 15, 14, 30, 0)
        assert _pacific_today(clock) == "2026-06-15"


# ===========================================================================
# _next_pacific_midnight (returns UTC)
# ===========================================================================

class TestNextPacificMidnight:
    """_next_pacific_midnight returns UTC, so delta = midnight - now works correctly
    regardless of what tzinfo 'now' has.  Convert both to UTC for safe comparison."""

    def _delta(self, clock_dt: datetime) -> float:
        """Return seconds from clock_dt to next Pacific midnight, via UTC."""
        clock = lambda: clock_dt
        midnight_utc = _next_pacific_midnight(clock)
        # Normalise clock_dt to UTC for safe subtraction
        now_utc = clock_dt.astimezone(timezone.utc)
        return (midnight_utc - now_utc).total_seconds()

    def test_spring_forward_at_start_of_day(self):
        """Spring-forward day (2026-03-08) is 23 h long.
        From PST midnight (00:00:01) to PDT midnight next day ≈ 23 h - 1 s.
        """
        clock_dt = _pacific(2026, 3, 8, 0, 0, 1)
        delta = self._delta(clock_dt)
        # PST midnight 2026-03-08 = 08:00:01 UTC
        # PDT midnight 2026-03-09 = 07:00:00 UTC
        # Duration = 22 h 59 m 59 s = 82799 s
        assert abs(delta - 82799) < 2

    def test_fall_back_at_start_of_day(self):
        """Fall-back day (2026-11-01) is 25 h long.
        From PDT midnight (00:00:00) to PST midnight next day = 25 h.
        """
        clock_dt = _pacific(2026, 11, 1, 0, 0, 0)
        delta = self._delta(clock_dt)
        # PDT midnight 2026-11-01 = 07:00 UTC
        # PST midnight 2026-11-02 = 08:00 UTC
        # Duration = 25 h = 90000 s
        assert abs(delta - 90000) < 2

    def test_normal_pst_day(self):
        """Normal PST day: exactly 24 h from midnight to next midnight."""
        clock_dt = _pacific(2026, 1, 15, 0, 0, 0)
        delta = self._delta(clock_dt)
        assert abs(delta - 24 * 3600) < 2

    def test_normal_pdt_day(self):
        """Normal PDT day: exactly 24 h from midnight to next midnight."""
        clock_dt = _pacific(2026, 7, 4, 0, 0, 0)
        delta = self._delta(clock_dt)
        assert abs(delta - 24 * 3600) < 2


# ===========================================================================
# Day rollover
# ===========================================================================

class TestDayRollover:
    def test_quota_resets_on_new_day(self, db, cfg):
        """Units used yesterday don't count against today's limit."""
        # Record 9000 units yesterday (Nov 6 Pacific)
        yesterday_dt = _utc(2026, 11, 6, 20, 0, 0)  # 12:00 PST Nov 6
        _tracker(db, cfg, yesterday_dt).record(9000)
        assert get_quota_log(db, "2026-11-06")["units_used"] == 9000

        # Today we should have full headroom
        today_dt = _utc(2026, 11, 7, 20, 0, 0)  # 12:00 PST Nov 7
        t2 = _tracker(db, cfg, today_dt)
        assert t2.can_afford(9000)
        assert t2.remaining() == 10_000

    def test_rollover_clears_exhausted(self, db, cfg):
        """mark_exhausted() on yesterday does not block today."""
        yesterday_dt = _utc(2026, 11, 6, 20, 0, 0)
        t = _tracker(db, cfg, yesterday_dt)
        t.mark_exhausted()
        assert t.is_exhausted()

        # New day
        today_dt = _utc(2026, 11, 7, 20, 0, 0)
        t2 = _tracker(db, cfg, today_dt)
        assert not t2.is_exhausted()
        assert t2.can_afford(1)


# ===========================================================================
# Spring-forward (23-hour day) — 2026-03-08
# ===========================================================================

class TestSpringForward:
    """2026-03-08: clocks spring forward at 2 AM PST → 3 AM PDT.
    That day is only 23 hours long.
      PST midnight 2026-03-08 = 2026-03-08 08:00 UTC
      PDT midnight 2026-03-09 = 2026-03-09 07:00 UTC
    """

    def test_seconds_until_reset_at_pst_midnight(self, db, cfg):
        """seconds_until_reset at PST midnight of spring-forward day ≈ 23 h."""
        # Exactly PST midnight, 2026-03-08 00:00 PST = 08:00 UTC
        clock_dt = _utc(2026, 3, 8, 8, 0, 0)
        t = _tracker(db, cfg, clock_dt)
        secs = t.seconds_until_reset()
        # Next midnight is PDT midnight 2026-03-09 = 07:00 UTC
        # Remaining = 07:00 - 08:00 = 23 h exactly
        assert abs(secs - 23 * 3600) < 2

    def test_seconds_until_reset_spring_forward_midday(self, db, cfg):
        """seconds_until_reset at 08:30 PST on spring-forward day ≈ 14.5 h."""
        # 08:30 PST = 16:30 UTC on 2026-03-08
        clock_dt = _utc(2026, 3, 8, 16, 30, 0)
        t = _tracker(db, cfg, clock_dt)
        secs = t.seconds_until_reset()
        # Next PDT midnight 2026-03-09 = 07:00 UTC
        # Remaining = 07:00 (next day) - 16:30 = 14.5 h = 52200 s
        expected = (14 * 3600) + (30 * 60)
        assert abs(secs - expected) < 2

    def test_pacific_today_through_spring_forward(self):
        """The quota day for the spring-forward clock is correct."""
        # 1:59 PST is still Mar 8
        clock1 = lambda: _pacific(2026, 3, 8, 1, 59, 59)
        assert _pacific_today(clock1) == "2026-03-08"
        # After the gap, 3:00 PDT is still Mar 8
        clock2 = lambda: _pacific(2026, 3, 8, 3, 0, 0)
        assert _pacific_today(clock2) == "2026-03-08"


# ===========================================================================
# Fall-back (25-hour day) — 2026-11-01
# ===========================================================================

class TestFallBack:
    """2026-11-01: clocks fall back at 2 AM PDT → 1 AM PST.
    That day is 25 hours long.
      PDT midnight 2026-11-01 = 2026-11-01 07:00 UTC
      PST midnight 2026-11-02 = 2026-11-02 08:00 UTC
    """

    def test_seconds_until_reset_at_pdt_midnight(self, db, cfg):
        """seconds_until_reset at PDT midnight of fall-back day ≈ 25 h."""
        # PDT midnight 2026-11-01 = 07:00 UTC
        clock_dt = _utc(2026, 11, 1, 7, 0, 0)
        t = _tracker(db, cfg, clock_dt)
        secs = t.seconds_until_reset()
        # Next PST midnight 2026-11-02 = 08:00 UTC
        # Remaining = 25 h exactly
        assert abs(secs - 25 * 3600) < 2

    def test_seconds_until_reset_fall_back_midday(self, db, cfg):
        """seconds_until_reset at 10:00 PST on fall-back day ≈ 14 h."""
        # 10:00 PST on 2026-11-01 = 18:00 UTC (after fall-back)
        clock_dt = _utc(2026, 11, 1, 18, 0, 0)
        t = _tracker(db, cfg, clock_dt)
        secs = t.seconds_until_reset()
        # PST midnight 2026-11-02 = 08:00 UTC
        # Remaining = 08:00 (next day) - 18:00 = 14 h exactly
        expected = 14 * 3600
        assert abs(secs - expected) < 2

    def test_pacific_today_fall_back_ambiguous_hour(self):
        """Both instances of 1 AM on fall-back day map to the same quota day."""
        # First 1:30 AM (PDT, fold=0)
        dt1 = datetime(2026, 11, 1, 1, 30, 0, tzinfo=_PACIFIC, fold=0)
        clock1 = lambda: dt1
        assert _pacific_today(clock1) == "2026-11-01"

        # Second 1:30 AM (PST, fold=1)
        dt2 = datetime(2026, 11, 1, 1, 30, 0, tzinfo=_PACIFIC, fold=1)
        clock2 = lambda: dt2
        assert _pacific_today(clock2) == "2026-11-01"


# ===========================================================================
# can_afford
# ===========================================================================

class TestCanAfford:
    """Boundary cases for can_afford."""

    FIXED = _utc(2026, 11, 7, 20, 0, 0)  # a normal PST day

    def test_fresh_db_can_afford_any_reasonable_amount(self, db, cfg):
        t = _tracker(db, cfg, self.FIXED)
        assert t.can_afford(1)
        assert t.can_afford(9999)
        assert t.can_afford(10_000)

    def test_exactly_at_unit_limit_is_affordable(self, db, cfg):
        """units_used=0, requesting exactly the limit → True."""
        t = _tracker(db, cfg, self.FIXED)
        assert t.can_afford(10_000)

    def test_one_over_unit_limit_is_not_affordable(self, db, cfg):
        """units_used=0, requesting limit+1 → False."""
        t = _tracker(db, cfg, self.FIXED)
        assert not t.can_afford(10_001)

    def test_accumulated_usage_at_boundary(self, db, cfg):
        """After using 9000 units, can afford exactly 1000 more."""
        t = _tracker(db, cfg, self.FIXED)
        t.record(9000)
        assert t.can_afford(1000)
        assert not t.can_afford(1001)

    def test_zero_units_at_full_capacity(self, db, cfg):
        """can_afford(0) is True at full unit usage (0 more fits: 10000+0 <= 10000)."""
        t = _tracker(db, cfg, self.FIXED)
        t.record(10_000)
        # Over limit when requesting > 0
        assert not t.can_afford(1)
        # 0 is still at boundary
        assert t.can_afford(0)

    # --- Upload limit: null ---

    def test_upload_limit_null_ignores_upload_param(self, db):
        """When daily_upload_limit is null, the uploads param is ignored."""
        cfg = _make_config(daily_upload_limit=None)
        t = _tracker(db, cfg, self.FIXED)
        # Even if we pass a huge upload count, it should still be allowed
        assert t.can_afford(1, uploads=99999)

    # --- Upload limit: set ---

    def test_upload_limit_set_exactly_at_limit(self, db):
        """uploads_used + uploads == daily_upload_limit → True."""
        cfg = _make_config(daily_upload_limit=5)
        t = _tracker(db, cfg, self.FIXED)
        # Upload 4 times, then can afford exactly 1 more
        t.record(0, uploads=4)
        assert t.can_afford(1, uploads=1)

    def test_upload_limit_set_one_over(self, db):
        """uploads_used + uploads > daily_upload_limit → False."""
        cfg = _make_config(daily_upload_limit=5)
        t = _tracker(db, cfg, self.FIXED)
        t.record(0, uploads=5)
        assert not t.can_afford(1, uploads=1)

    def test_upload_limit_zero_uploads_param_ok(self, db):
        """When upload limit is exhausted, non-upload calls (uploads=0) still work."""
        cfg = _make_config(daily_upload_limit=5)
        t = _tracker(db, cfg, self.FIXED)
        t.record(0, uploads=5)
        # Non-upload call (uploads=0) is not blocked by upload count
        assert t.can_afford(50, uploads=0)

    # --- Exhausted flag overrides counter ---

    def test_exhausted_blocks_even_with_headroom(self, db, cfg):
        """mark_exhausted blocks regardless of units counter."""
        t = _tracker(db, cfg, self.FIXED)
        # Only used 1 unit, 9999 left — but mark exhausted
        t.record(1)
        t.mark_exhausted()
        assert not t.can_afford(1)
        assert not t.can_afford(0)


# ===========================================================================
# mark_exhausted / is_exhausted
# ===========================================================================

class TestMarkExhausted:
    FIXED = _utc(2026, 11, 7, 20, 0, 0)

    def test_not_exhausted_by_default(self, db, cfg):
        t = _tracker(db, cfg, self.FIXED)
        assert not t.is_exhausted()

    def test_mark_exhausted_persists(self, db, cfg):
        t = _tracker(db, cfg, self.FIXED)
        t.mark_exhausted()
        assert t.is_exhausted()

    def test_exhausted_at_stored_in_db(self, db, cfg):
        t = _tracker(db, cfg, self.FIXED)
        t.mark_exhausted()
        row = get_quota_log(db, "2026-11-07")
        assert row is not None
        assert row["exhausted_at"] is not None

    def test_mark_exhausted_idempotent(self, db, cfg):
        t = _tracker(db, cfg, self.FIXED)
        t.mark_exhausted()
        t.mark_exhausted()  # calling twice should not error
        assert t.is_exhausted()

    def test_mark_exhausted_without_prior_record(self, db, cfg):
        """mark_exhausted on a day with no row yet creates the row."""
        t = _tracker(db, cfg, self.FIXED)
        t.mark_exhausted()
        row = get_quota_log(db, "2026-11-07")
        assert row is not None
        assert row["exhausted_at"] is not None

    def test_exhausted_clears_after_rollover(self, db, cfg):
        """Today's exhausted_at does not block tomorrow."""
        today_dt = self.FIXED
        t = _tracker(db, cfg, today_dt)
        t.mark_exhausted()
        assert t.is_exhausted()

        tomorrow_dt = _utc(2026, 11, 8, 20, 0, 0)
        t2 = _tracker(db, cfg, tomorrow_dt)
        assert not t2.is_exhausted()
        assert t2.can_afford(1)

    def test_can_afford_false_while_exhausted(self, db, cfg):
        t = _tracker(db, cfg, self.FIXED)
        t.mark_exhausted()
        assert not t.can_afford(1)
        assert not t.can_afford(0)
        assert not t.can_afford(10_000)


# ===========================================================================
# record
# ===========================================================================

class TestRecord:
    FIXED = _utc(2026, 11, 7, 20, 0, 0)

    def test_record_accumulates(self, db, cfg):
        """Multiple record() calls accumulate correctly."""
        t = _tracker(db, cfg, self.FIXED)
        t.record(100)
        t.record(200)
        t.record(50, uploads=1)
        row = get_quota_log(db, "2026-11-07")
        assert row["units_used"] == 350
        assert row["uploads_used"] == 1

    def test_record_different_days(self, db, cfg):
        """Recording on different days keeps separate rows."""
        dt1 = _utc(2026, 11, 6, 20, 0, 0)
        dt2 = _utc(2026, 11, 7, 20, 0, 0)
        _tracker(db, cfg, dt1).record(500)
        _tracker(db, cfg, dt2).record(300)

        row1 = get_quota_log(db, "2026-11-06")
        row2 = get_quota_log(db, "2026-11-07")
        assert row1["units_used"] == 500
        assert row2["units_used"] == 300

    def test_record_zero_is_harmless(self, db, cfg):
        t = _tracker(db, cfg, self.FIXED)
        t.record(0)
        row = get_quota_log(db, "2026-11-07")
        assert row is not None
        assert row["units_used"] == 0

    def test_record_affects_can_afford(self, db, cfg):
        t = _tracker(db, cfg, self.FIXED)
        t.record(9500)
        assert t.can_afford(500)
        assert not t.can_afford(501)


# ===========================================================================
# seconds_until_reset
# ===========================================================================

class TestSecondsUntilReset:
    def test_pst_midday(self, db, cfg):
        """PST noon: 12 hours until midnight."""
        clock_dt = _pacific(2026, 1, 15, 12, 0, 0)
        t = _tracker(db, cfg, clock_dt)
        secs = t.seconds_until_reset()
        assert abs(secs - 12 * 3600) < 2

    def test_pdt_midday(self, db, cfg):
        """PDT noon: 12 hours until midnight."""
        clock_dt = _pacific(2026, 7, 4, 12, 0, 0)
        t = _tracker(db, cfg, clock_dt)
        secs = t.seconds_until_reset()
        assert abs(secs - 12 * 3600) < 2

    def test_just_after_pst_midnight(self, db, cfg):
        """1 second after PST midnight: ~86399 seconds until next PST midnight."""
        # 2026-11-07 00:00:01 PST = 08:00:01 UTC
        clock_dt = _utc(2026, 11, 7, 8, 0, 1)
        t = _tracker(db, cfg, clock_dt)
        secs = t.seconds_until_reset()
        # Normal PST day: next PST midnight is 2026-11-08 08:00 UTC
        # Remaining = 86400 - 1 = 86399 s
        assert abs(secs - (24 * 3600 - 1)) < 2

    def test_just_before_midnight(self, db, cfg):
        """1 second before midnight: ~1 second until reset."""
        clock_dt = _pacific(2026, 11, 7, 23, 59, 59)
        t = _tracker(db, cfg, clock_dt)
        secs = t.seconds_until_reset()
        assert abs(secs - 1) < 2

    def test_non_negative(self, db, cfg):
        """seconds_until_reset is always >= 0."""
        clock_dt = _pacific(2026, 6, 1, 23, 59, 59)
        t = _tracker(db, cfg, clock_dt)
        assert t.seconds_until_reset() >= 0

    def test_spring_forward_23h_day_at_midnight(self, db, cfg):
        """seconds_until_reset at PST midnight on spring-forward day ≈ 23 h."""
        # 2026-03-08 00:00 PST = 08:00 UTC
        clock_dt = _utc(2026, 3, 8, 8, 0, 0)
        t = _tracker(db, cfg, clock_dt)
        secs = t.seconds_until_reset()
        # Next midnight is PDT 2026-03-09 00:00 = 07:00 UTC → 23 h
        assert abs(secs - 23 * 3600) < 2

    def test_fall_back_25h_day_at_midnight(self, db, cfg):
        """seconds_until_reset at PDT midnight on fall-back day ≈ 25 h."""
        # 2026-11-01 00:00 PDT = 07:00 UTC
        clock_dt = _utc(2026, 11, 1, 7, 0, 0)
        t = _tracker(db, cfg, clock_dt)
        secs = t.seconds_until_reset()
        # Next midnight is PST 2026-11-02 00:00 = 08:00 UTC → 25 h
        assert abs(secs - 25 * 3600) < 2


# ===========================================================================
# remaining()
# ===========================================================================

class TestRemaining:
    FIXED = _utc(2026, 11, 7, 20, 0, 0)

    def test_remaining_full_on_fresh_db(self, db, cfg):
        t = _tracker(db, cfg, self.FIXED)
        assert t.remaining() == 10_000

    def test_remaining_decreases_with_record(self, db, cfg):
        t = _tracker(db, cfg, self.FIXED)
        t.record(3000)
        assert t.remaining() == 7000

    def test_remaining_floors_at_zero(self, db, cfg):
        """If units_used > limit (guard against edge cases), floor at 0."""
        # Manually insert a row with more units than the limit
        upsert_quota_log(db, "2026-11-07", units_used=10_001)
        t = _tracker(db, cfg, self.FIXED)
        assert t.remaining() == 0


# ===========================================================================
# Migration 3: exhausted_at column
# ===========================================================================

class TestMigration3:
    def test_migration_adds_exhausted_at_column(self):
        """After init_db, quota_log has an exhausted_at column."""
        conn = get_connection(":memory:")
        init_db(conn)
        # Insert a row and verify exhausted_at starts as NULL
        conn.execute(
            "INSERT INTO quota_log (date_pacific, units_used, uploads_used) VALUES ('2026-01-01', 0, 0)"
        )
        row = conn.execute("SELECT * FROM quota_log WHERE date_pacific = '2026-01-01'").fetchone()
        assert "exhausted_at" in row.keys()
        assert row["exhausted_at"] is None

    def test_migration_idempotent(self):
        """init_db can be called twice on the same connection without error."""
        conn = get_connection(":memory:")
        init_db(conn)
        # Insert a row to prove data survives
        conn.execute(
            "INSERT INTO quota_log (date_pacific, units_used, uploads_used) VALUES ('2026-06-01', 42, 2)"
        )
        # Second call must not fail or wipe data
        init_db(conn)
        row = conn.execute("SELECT * FROM quota_log WHERE date_pacific = '2026-06-01'").fetchone()
        assert row["units_used"] == 42

    def test_migration_preserves_existing_rows(self, tmp_path):
        """Migration 3 adds the column without destroying pre-existing rows."""
        db_path = tmp_path / "mig_test.db"

        # Simulate a database that has only migrations 1 and 2 applied
        conn = get_connection(db_path)
        from photos2yt.db import MIGRATIONS, _iso_now
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);"
        )
        for v in [1, 2]:
            with conn:
                for stmt in MIGRATIONS[v]:
                    conn.execute(stmt)
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (v, _iso_now()),
                )

        # Insert a quota_log row (before migration 3 adds exhausted_at)
        with conn:
            conn.execute(
                "INSERT INTO quota_log (date_pacific, units_used, uploads_used) "
                "VALUES ('2025-12-25', 1600, 1)"
            )
        conn.close()

        # Now run full init_db (applies migration 3)
        conn2 = get_connection(db_path)
        init_db(conn2)

        row = conn2.execute(
            "SELECT * FROM quota_log WHERE date_pacific = '2025-12-25'"
        ).fetchone()
        assert row is not None
        assert row["units_used"] == 1600
        assert row["uploads_used"] == 1
        assert row["exhausted_at"] is None  # new column, default null

        conn2.close()

    def test_schema_version_includes_3(self):
        """Migration 3 exists and SCHEMA_VERSION is at least 3 (later migrations add more)."""
        from photos2yt.db import MIGRATIONS, SCHEMA_VERSION
        assert 3 in MIGRATIONS and SCHEMA_VERSION >= 3 and SCHEMA_VERSION == max(MIGRATIONS)


# ===========================================================================
# auth.py integration: check_authorization records 1 unit
# ===========================================================================

class TestAuthQuotaIntegration:
    """check_authorization wires quota.record(1) via the conn parameter."""

    def test_check_authorization_records_1_unit(self, db):
        """check_authorization with a mocked API records 1 unit."""
        from unittest.mock import MagicMock, patch
        from photos2yt.auth import check_authorization

        cfg = _make_config()

        # Build a mock YouTube service that returns a channel
        mock_channel = {
            "items": [{"id": "UC_test", "snippet": {"title": "Test Channel"}}]
        }
        mock_request = MagicMock()
        mock_request.execute.return_value = mock_channel
        mock_service = MagicMock()
        mock_service.channels.return_value.list.return_value = mock_request

        mock_creds = MagicMock()
        mock_creds.valid = True

        with (
            patch("photos2yt.auth.build", return_value=mock_service),
            patch("photos2yt.auth.get_credentials", return_value=mock_creds),
        ):
            result = check_authorization(config=cfg, conn=db)

        assert result["id"] == "UC_test"
        assert result["title"] == "Test Channel"

        # Verify 1 unit was recorded in quota_log for today (Pacific)
        today = _pacific_today()
        row = get_quota_log(db, today)
        assert row is not None
        assert row["units_used"] == 1
