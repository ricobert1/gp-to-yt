"""YouTube API daily quota tracking module.

The YouTube Data API quota resets at midnight America/Los_Angeles (Pacific Time).
This module tracks usage locally because there is no API endpoint for remaining quota.
The authoritative stop signal for exhaustion is a 403 quotaExceeded response from
the API; call mark_exhausted() when you receive one.

All limits and costs are read from config (quota.daily_unit_limit,
upload_cost_units, daily_upload_limit, etc.). Nothing is hardcoded.

Clock injection
---------------
Every method that needs "now" accepts an optional ``clock`` keyword argument,
which defaults to ``datetime.now``. Pass a zero-argument callable that returns
a timezone-aware or naive datetime for testing:

    tracker = QuotaTracker(conn, cfg, clock=lambda: fixed_dt)

The quota day is always computed in America/Los_Angeles regardless of the
``config.timezone`` setting (which is used for recording dates, not quota).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from photos2yt.config import Config
from photos2yt.db import (
    get_quota_log,
    set_quota_exhausted,
    upsert_quota_log,
)

# The quota always resets at Pacific midnight, independent of config.timezone.
_PACIFIC = ZoneInfo("America/Los_Angeles")


def _pacific_today(clock: Optional[Callable[[], datetime]] = None) -> str:
    """Return the current Pacific-time date as 'YYYY-MM-DD'.

    Args:
        clock: Zero-argument callable returning the current time.  If the
               returned datetime is timezone-aware it is converted to Pacific;
               if naive it is assumed to already be UTC.  Defaults to
               ``datetime.now(timezone.utc)``.
    """
    if clock is None:
        now = datetime.now(timezone.utc)
    else:
        now = clock()
        if now.tzinfo is None:
            # Treat naive clocks as UTC (common in tests using datetime.utcnow style)
            now = now.replace(tzinfo=timezone.utc)

    pacific_now = now.astimezone(_PACIFIC)
    return pacific_now.strftime("%Y-%m-%d")


def _next_pacific_midnight(clock: Optional[Callable[[], datetime]] = None) -> datetime:
    """Return the next Pacific midnight as a UTC-aware datetime.

    Returns UTC so that callers can subtract any tz-aware datetime from it
    without hitting the zoneinfo same-zone wall-clock subtraction quirk.

    On the spring-forward day (23 h), the returned UTC value will be ~23 h
    after the start of the PST day.  On the fall-back day (25 h), ~25 h.
    """
    if clock is None:
        now = datetime.now(timezone.utc)
    else:
        now = clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

    pacific_now = now.astimezone(_PACIFIC)

    # Build midnight for the *next* day in Pacific time then convert to UTC.
    # replace(tzinfo=_PACIFIC) correctly picks up the DST offset for that date
    # (e.g. PDT -7 for dates after spring-forward, PST -8 for dates after fall-back).
    # We then convert to UTC so that subtraction works across any tzinfo.
    from datetime import timedelta
    tomorrow = pacific_now.date() + timedelta(days=1)
    midnight_naive = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 0, 0, 0)
    midnight_pacific = midnight_naive.replace(tzinfo=_PACIFIC)
    return midnight_pacific.astimezone(timezone.utc)


class QuotaTracker:
    """Stateless facade over the quota_log table for checking and recording API usage.

    Parameters
    ----------
    conn:
        An open SQLite connection (must have been initialised with init_db so
        that quota_log and schema_migrations exist).
    config:
        Loaded Config object.  Quota limits are read from config.quota.
    clock:
        Optional callable returning the current time (timezone-aware or naive UTC).
        Defaults to ``datetime.now(timezone.utc)``.  Inject a frozen clock in tests.
    """

    def __init__(
        self,
        conn,
        config: Config,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._conn = conn
        self._cfg = config
        self._clock = clock  # None → use real time

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _today(self) -> str:
        """Pacific-time date string for the current quota day."""
        return _pacific_today(self._clock)

    def _row(self, date_pacific: Optional[str] = None):
        """Return the quota_log row for today (or given date), or None."""
        date = date_pacific or self._today()
        return get_quota_log(self._conn, date)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def can_afford(self, units: int, uploads: int = 0) -> bool:
        """Return True if today's remaining quota can cover *units* and *uploads*.

        Checks:
        1. If today is exhausted (exhausted_at is set), returns False immediately.
        2. units_used + units <= daily_unit_limit.
        3. If quota.daily_upload_limit is not null:
               uploads_used + uploads <= daily_upload_limit.

        Args:
            units:   API unit cost of the intended operation.
            uploads: Number of upload slots the operation would consume
                     (only matters when daily_upload_limit is not null).
        """
        if self.is_exhausted():
            return False

        row = self._row()
        units_used = row["units_used"] if row else 0
        uploads_used = row["uploads_used"] if row else 0

        if units_used + units > self._cfg.quota.daily_unit_limit:
            return False

        if self._cfg.quota.daily_upload_limit is not None:
            if uploads_used + uploads > self._cfg.quota.daily_upload_limit:
                return False

        return True

    def record(self, units: int, uploads: int = 0) -> None:
        """Atomically increment today's quota usage counters.

        Uses the existing upsert helper which does an INSERT … ON CONFLICT
        DO UPDATE so this is safe to call concurrently or repeatedly.

        Args:
            units:   Units consumed by this API call.
            uploads: Upload slots consumed (1 for a videos.insert, 0 otherwise).
        """
        upsert_quota_log(
            self._conn,
            self._today(),
            units_used=units,
            uploads_used=uploads,
        )

    def mark_exhausted(self) -> None:
        """Record that quota is exhausted for today (called on 403 quotaExceeded).

        Persists an exhausted_at timestamp so that is_exhausted() returns True
        and can_afford() returns False for every call until the quota day rolls
        over, regardless of the unit counter.
        """
        now_utc = (
            self._clock() if self._clock is not None else datetime.now(timezone.utc)
        )
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)
        exhausted_at = now_utc.astimezone(timezone.utc).isoformat()
        set_quota_exhausted(self._conn, self._today(), exhausted_at)

    def is_exhausted(self) -> bool:
        """Return True if today's quota has been marked exhausted.

        Exhaustion clears automatically when the quota day rolls over: a row
        for the *previous* day having exhausted_at set does not affect today.
        """
        row = self._row()
        if row is None:
            return False
        exhausted_at = row["exhausted_at"]
        return exhausted_at is not None

    def seconds_until_reset(self) -> float:
        """Return seconds until the next Pacific midnight (quota reset).

        Correct on 23-hour days (spring-forward) and 25-hour days (fall-back)
        because it works in wall-clock Pacific time, not fixed-length hours.

        Returns:
            Non-negative float.  Returns 0.0 if, due to clock skew, midnight
            appears to be in the past.
        """
        if self._clock is None:
            now = datetime.now(timezone.utc)
        else:
            now = self._clock()
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)

        midnight = _next_pacific_midnight(self._clock)
        delta = (midnight - now).total_seconds()
        return max(0.0, delta)

    def remaining(self) -> int:
        """Return units remaining today (daily_unit_limit − units_used).

        Does not account for exhausted_at; callers should check is_exhausted()
        if they need the full picture.
        """
        row = self._row()
        units_used = row["units_used"] if row else 0
        return max(0, self._cfg.quota.daily_unit_limit - units_used)


# ---------------------------------------------------------------------------
# Module-level convenience functions used by auth.py and other callers that
# only have a Config and want a quick record() without constructing the full
# tracker object.  They open no extra connections; callers pass the conn.
# ---------------------------------------------------------------------------

def record(conn, config: Config, units: int = 0, uploads: int = 0) -> None:
    """Record quota usage without constructing a QuotaTracker explicitly.

    Thin wrapper for callers (e.g. auth.py check_authorization) that have a
    connection and config but don't need the full tracker.

    Args:
        conn:    Open SQLite connection (init_db must have been run).
        config:  Loaded Config.
        units:   Units consumed.
        uploads: Upload slots consumed.
    """
    tracker = QuotaTracker(conn, config)
    tracker.record(units, uploads)
