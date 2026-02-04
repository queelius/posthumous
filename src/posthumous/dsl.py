"""When-expression DSL parser for Posthumous scheduling."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import NamedTuple

from dateutil.relativedelta import relativedelta


class ScheduleType(Enum):
    """Type of schedule expression."""
    TRIGGER_RELATIVE = "trigger_relative"  # "trigger", "trigger + 3 days"
    TRIGGER_RECURRING = "trigger_recurring"  # "every day after trigger"
    TRIGGER_ANNIVERSARY = "trigger_anniversary"  # "every year on trigger"
    ABSOLUTE_ONCE = "absolute_once"  # "2030-01-01"
    ABSOLUTE_RECURRING = "absolute_recurring"  # "every December 25"


@dataclass
class ParsedSchedule:
    """Parsed schedule expression."""
    schedule_type: ScheduleType
    offset: timedelta | None = None  # Offset from trigger or base date
    interval: timedelta | relativedelta | None = None  # For recurring
    month: int | None = None  # For absolute recurring (1-12)
    day: int | None = None  # For absolute recurring (1-31)
    fixed_date: datetime | None = None  # For absolute_once
    raw_expression: str = ""

    def get_period_key(self, now: datetime, trigger_time: datetime | None = None) -> str:
        """Get the deduplication period key for the current time.

        Returns a string like "2026", "2026-W05", "2026-03-15", etc.
        """
        # One-time events always use "once" as the key
        if self.schedule_type in (ScheduleType.TRIGGER_RELATIVE, ScheduleType.ABSOLUTE_ONCE):
            return "once"

        # Annual events use the year
        if self.schedule_type in (ScheduleType.TRIGGER_ANNIVERSARY, ScheduleType.ABSOLUTE_RECURRING):
            return now.strftime("%Y")

        # For recurring schedules, determine period based on interval
        if self.schedule_type == ScheduleType.TRIGGER_RECURRING and isinstance(self.interval, timedelta):
            days = self.interval.days
            if days == 1:
                return now.strftime("%Y-%m-%d")
            if days == 7:
                return now.strftime("%Y-W%W")
            if days in (30, 31):
                return now.strftime("%Y-%m")
            # Generic interval - use period number since trigger
            if trigger_time and self.interval.total_seconds() > 0:
                elapsed = now - trigger_time
                period_num = int(elapsed.total_seconds() / self.interval.total_seconds())
                return f"period-{period_num}"

        # Default: daily granularity
        return now.strftime("%Y-%m-%d")


class NextOccurrence(NamedTuple):
    """Next scheduled occurrence."""
    time: datetime
    period_key: str


def parse_duration(text: str) -> timedelta:
    """Parse a duration like '3 days', '1 week', '12 hours'."""
    text = text.strip().lower()

    # Try "N unit" format
    match = re.match(r'(\d+)\s*(day|days|d|week|weeks|w|hour|hours|h|minute|minutes|min|m|year|years|y)', text)
    if not match:
        raise ValueError(f"Cannot parse duration: {text}")

    amount = int(match.group(1))
    unit = match.group(2)

    unit_map = {
        'day': timedelta(days=1),
        'days': timedelta(days=1),
        'd': timedelta(days=1),
        'week': timedelta(days=7),
        'weeks': timedelta(days=7),
        'w': timedelta(days=7),
        'hour': timedelta(hours=1),
        'hours': timedelta(hours=1),
        'h': timedelta(hours=1),
        'minute': timedelta(minutes=1),
        'minutes': timedelta(minutes=1),
        'min': timedelta(minutes=1),
        'm': timedelta(minutes=1),
        'year': relativedelta(years=1),
        'years': relativedelta(years=1),
        'y': relativedelta(years=1),
    }

    base = unit_map[unit]
    if isinstance(base, relativedelta):
        return relativedelta(years=amount)
    return base * amount


def parse_month(text: str) -> int:
    """Parse a month name or number to 1-12."""
    text = text.strip().lower()

    month_names = {
        'january': 1, 'jan': 1,
        'february': 2, 'feb': 2,
        'march': 3, 'mar': 3,
        'april': 4, 'apr': 4,
        'may': 5,
        'june': 6, 'jun': 6,
        'july': 7, 'jul': 7,
        'august': 8, 'aug': 8,
        'september': 9, 'sep': 9, 'sept': 9,
        'october': 10, 'oct': 10,
        'november': 11, 'nov': 11,
        'december': 12, 'dec': 12,
    }

    if text in month_names:
        return month_names[text]

    try:
        num = int(text)
        if 1 <= num <= 12:
            return num
    except ValueError:
        pass

    raise ValueError(f"Cannot parse month: {text}")


def parse_when_expression(expression: str) -> ParsedSchedule:
    """Parse a when-expression.

    Supported formats:
    - "trigger" - at trigger time
    - "trigger + 3 days" - offset from trigger
    - "trigger - 1 hour" - offset before trigger (for prep)
    - "every day after trigger" - daily from trigger
    - "every week after trigger" - weekly from trigger
    - "every year on trigger" - annual anniversary
    - "every 30 days after trigger" - custom interval
    - "2030-01-01" - specific date
    - "every December 25" - yearly on date
    - "every March 15" - yearly on date
    - "every March 15 - 7 days" - offset from yearly date
    """
    expression = expression.strip().lower()
    original = expression

    # "trigger" or "trigger + N days" or "trigger - N hours"
    if expression == "trigger":
        return ParsedSchedule(
            schedule_type=ScheduleType.TRIGGER_RELATIVE,
            offset=timedelta(0),
            raw_expression=original,
        )

    match = re.match(r'trigger\s*([+-])\s*(.+)', expression)
    if match:
        sign = match.group(1)
        duration = parse_duration(match.group(2))
        if sign == '-':
            if isinstance(duration, relativedelta):
                # relativedelta doesn't support negation easily
                raise ValueError("Negative year offsets not supported")
            duration = -duration
        return ParsedSchedule(
            schedule_type=ScheduleType.TRIGGER_RELATIVE,
            offset=duration if isinstance(duration, timedelta) else timedelta(days=365),  # Approximate year
            raw_expression=original,
        )

    # "every year on trigger"
    if expression in ("every year on trigger", "yearly on trigger", "annually on trigger"):
        return ParsedSchedule(
            schedule_type=ScheduleType.TRIGGER_ANNIVERSARY,
            interval=relativedelta(years=1),
            raw_expression=original,
        )

    # "every day/week/N days after trigger"
    match = re.match(r'every\s+(.+)\s+after\s+trigger', expression)
    if match:
        interval_str = match.group(1).strip()

        if interval_str == "day":
            interval = timedelta(days=1)
        elif interval_str == "week":
            interval = timedelta(days=7)
        elif interval_str == "month":
            interval = relativedelta(months=1)
        else:
            interval = parse_duration(interval_str)

        return ParsedSchedule(
            schedule_type=ScheduleType.TRIGGER_RECURRING,
            interval=interval,
            raw_expression=original,
        )

    # "2030-01-01" - absolute date
    match = re.match(r'(\d{4})-(\d{2})-(\d{2})', expression)
    if match and expression == match.group(0):
        year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
        return ParsedSchedule(
            schedule_type=ScheduleType.ABSOLUTE_ONCE,
            fixed_date=datetime(year, month, day, tzinfo=timezone.utc),
            raw_expression=original,
        )

    # "every December 25" or "every March 15 - 7 days"
    match = re.match(r'every\s+(\w+)\s+(\d+)(?:\s*([+-])\s*(.+))?', expression)
    if match:
        month = parse_month(match.group(1))
        day = int(match.group(2))

        offset = timedelta(0)
        if match.group(3):
            sign = match.group(3)
            offset = parse_duration(match.group(4))
            if isinstance(offset, relativedelta):
                # Convert to approximate timedelta
                offset = timedelta(days=offset.years * 365 + offset.months * 30 + offset.days)
            if sign == '-':
                offset = -offset

        return ParsedSchedule(
            schedule_type=ScheduleType.ABSOLUTE_RECURRING,
            month=month,
            day=day,
            offset=offset,
            raw_expression=original,
        )

    raise ValueError(f"Cannot parse when-expression: {expression}")


def get_next_occurrence(
    schedule: ParsedSchedule,
    trigger_time: datetime,
    now: datetime | None = None,
) -> NextOccurrence | None:
    """Calculate the next occurrence of a scheduled event.

    Args:
        schedule: Parsed schedule expression
        trigger_time: When the deadman switch triggered
        now: Current time (defaults to now)

    Returns:
        NextOccurrence with the next scheduled time, or None if no more occurrences
    """
    now = now or datetime.now(timezone.utc)

    if schedule.schedule_type == ScheduleType.TRIGGER_RELATIVE:
        # One-time event relative to trigger
        offset = schedule.offset or timedelta(0)
        event_time = trigger_time + offset
        if event_time > now:
            return NextOccurrence(event_time, "once")
        return None  # Already passed

    elif schedule.schedule_type == ScheduleType.TRIGGER_ANNIVERSARY:
        # Annual event on trigger anniversary
        years_since = now.year - trigger_time.year
        next_anniversary = trigger_time.replace(year=trigger_time.year + years_since)

        if next_anniversary <= now:
            next_anniversary = next_anniversary.replace(year=next_anniversary.year + 1)

        period_key = str(next_anniversary.year)
        return NextOccurrence(next_anniversary, period_key)

    elif schedule.schedule_type == ScheduleType.TRIGGER_RECURRING:
        # Recurring from trigger
        interval = schedule.interval

        if isinstance(interval, relativedelta):
            # Month-based interval
            current = trigger_time
            while current <= now:
                current = current + interval
            period_key = current.strftime("%Y-%m")
            return NextOccurrence(current, period_key)

        elif isinstance(interval, timedelta):
            # Calculate next occurrence
            if interval.total_seconds() <= 0:
                return None

            elapsed = (now - trigger_time).total_seconds()
            interval_seconds = interval.total_seconds()
            periods_elapsed = int(elapsed / interval_seconds)
            next_period = periods_elapsed + 1
            next_time = trigger_time + timedelta(seconds=next_period * interval_seconds)

            period_key = schedule.get_period_key(next_time, trigger_time)
            return NextOccurrence(next_time, period_key)

    elif schedule.schedule_type == ScheduleType.ABSOLUTE_ONCE:
        # One-time absolute date
        if schedule.fixed_date and schedule.fixed_date > now:
            return NextOccurrence(schedule.fixed_date, "once")
        return None

    elif schedule.schedule_type == ScheduleType.ABSOLUTE_RECURRING:
        # Yearly on a specific month/day
        if schedule.month is None or schedule.day is None:
            return None

        offset = schedule.offset or timedelta(0)

        # Try this year
        try:
            this_year = datetime(
                now.year, schedule.month, schedule.day,
                tzinfo=timezone.utc
            ) + offset
        except ValueError:
            # Invalid date (e.g., Feb 30)
            return None

        if this_year > now:
            return NextOccurrence(this_year, str(now.year))

        # Try next year
        try:
            next_year = datetime(
                now.year + 1, schedule.month, schedule.day,
                tzinfo=timezone.utc
            ) + offset
        except ValueError:
            return None

        return NextOccurrence(next_year, str(now.year + 1))

    return None


def should_execute(
    schedule: ParsedSchedule,
    trigger_time: datetime,
    last_run_period: str | None,
    now: datetime | None = None,
) -> tuple[bool, str | None]:
    """Determine if a scheduled item should execute now.

    Args:
        schedule: Parsed schedule expression
        trigger_time: When the deadman switch triggered
        last_run_period: Period key of last execution (for dedup)
        now: Current time

    Returns:
        Tuple of (should_execute, period_key)
    """
    now = now or datetime.now(timezone.utc)

    # For one-time trigger-relative events, check if we're past the scheduled time
    if schedule.schedule_type == ScheduleType.TRIGGER_RELATIVE:
        offset = schedule.offset or timedelta(0)
        event_time = trigger_time + offset

        if event_time > now:
            return False, None  # Not yet time

        if last_run_period == "once":
            return False, None  # Already executed

        return True, "once"

    # For recurring events, we need to find the most recent occurrence that's at or before now
    # and check if we've already executed for that period

    # First, get what would be the "current" period's occurrence by checking
    # slightly before now to get an event that may have just happened
    if schedule.schedule_type == ScheduleType.TRIGGER_RECURRING:
        interval = schedule.interval
        if isinstance(interval, timedelta) and interval.total_seconds() > 0:
            # Calculate which period we're in
            elapsed = (now - trigger_time).total_seconds()
            interval_seconds = interval.total_seconds()
            current_period = int(elapsed / interval_seconds)

            # The event for this period happened at:
            current_event_time = trigger_time + timedelta(seconds=current_period * interval_seconds)

            # If that event is at or before now, it's due
            if current_event_time <= now:
                period_key = schedule.get_period_key(current_event_time, trigger_time)

                # Check deduplication
                if last_run_period == period_key:
                    return False, None

                return True, period_key

    # For other recurring types (anniversary, absolute), use the standard approach
    next_occ = get_next_occurrence(schedule, trigger_time, now - timedelta(seconds=1))
    if not next_occ:
        return False, None

    # Check if we're within the execution window (event time is at or before now)
    if next_occ.time > now:
        return False, None

    # Check deduplication
    if last_run_period == next_occ.period_key:
        return False, None

    return True, next_occ.period_key
