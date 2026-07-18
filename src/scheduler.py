"""Decision-loop cadence + daily-report/nightly-maintenance timing.

A simple sleep-based scheduler (no external cron) so the kill switch is honored
within one cycle. Sleeps are broken into short slices so a kill/stop is noticed
promptly.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, interval_min: int, daily_report_hour: int, tz=None):
        self.interval_s = max(1, int(interval_min * 60))
        self.daily_report_hour = daily_report_hour
        self.tz = tz
        self._last_report_date = None

    def sleep_interval(self, should_stop=None, slice_s: float = 5.0) -> None:
        """Sleep one decision interval, waking early if should_stop() is True."""
        waited = 0.0
        while waited < self.interval_s:
            if should_stop and should_stop():
                return
            chunk = min(slice_s, self.interval_s - waited)
            time.sleep(chunk)
            waited += chunk

    def due_for_daily(self, now: datetime | None = None) -> bool:
        """True at most once per calendar day, at/after the report hour (local)."""
        now = now or datetime.now(self.tz)
        if now.hour < self.daily_report_hour:
            return False
        today = now.date()
        if self._last_report_date == today:
            return False
        self._last_report_date = today
        return True
