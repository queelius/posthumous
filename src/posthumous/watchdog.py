"""Watchdog timer and state machine for Posthumous."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Awaitable

from posthumous.config import Config
from posthumous.state import State, StateManager, Status

logger = logging.getLogger(__name__)


@dataclass
class TimeRemaining:
    """Information about time remaining until next transition."""
    status: Status
    since_checkin: timedelta | None
    until_warning: timedelta | None
    until_grace: timedelta | None
    until_trigger: timedelta | None
    is_overdue: bool

    def summary(self) -> str:
        """Human-readable summary of time remaining."""
        if self.status == Status.TRIGGERED:
            return "TRIGGERED"

        if self.since_checkin is None:
            return "Never checked in"

        days = self.since_checkin.days
        hours = int(self.since_checkin.total_seconds() // 3600) % 24

        if self.status == Status.ARMED:
            if self.until_warning:
                warn_days = self.until_warning.days
                warn_hours = int(self.until_warning.total_seconds() // 3600) % 24
                return f"ARMED - {warn_days}d {warn_hours}h until warning"
            return "ARMED"
        elif self.status == Status.WARNING:
            if self.until_grace:
                grace_hours = int(self.until_grace.total_seconds() // 3600)
                return f"WARNING - {grace_hours}h until grace period"
            return "WARNING"
        elif self.status == Status.GRACE:
            if self.until_trigger:
                trigger_hours = int(self.until_trigger.total_seconds() // 3600)
                return f"GRACE - {trigger_hours}h until trigger"
            return "GRACE"

        return str(self.status.value).upper()


class Watchdog:
    """Manages the countdown timer and state transitions.

    State machine:
    ARMED ──timeout──► WARNING ──timeout──► GRACE ──timeout──► TRIGGERED
      ▲                   │                   │                    │
      └───── check-in ────┴───── check-in ────┘                    │
                                                                   ▼
                                                              (runs forever)
    """

    def __init__(
        self,
        config: Config,
        state_manager: StateManager,
        on_warning: Callable[[], Awaitable[None]] | None = None,
        on_grace: Callable[[], Awaitable[None]] | None = None,
        on_trigger: Callable[[], Awaitable[None]] | None = None,
    ):
        self.config = config
        self.state_manager = state_manager
        self._on_warning = on_warning
        self._on_grace = on_grace
        self._on_trigger = on_trigger
        self._task: asyncio.Task | None = None
        self._running = False
        self._check_interval = 60.0  # Check every minute

    @property
    def state(self) -> State:
        return self.state_manager.state

    def get_time_remaining(self, now: datetime | None = None) -> TimeRemaining:
        """Calculate time remaining until each transition."""
        now = now or datetime.now(timezone.utc)
        state = self.state

        if state.status == Status.TRIGGERED:
            return TimeRemaining(
                status=Status.TRIGGERED,
                since_checkin=None,
                until_warning=None,
                until_grace=None,
                until_trigger=None,
                is_overdue=False,
            )

        if state.last_checkin is None:
            return TimeRemaining(
                status=state.status,
                since_checkin=None,
                until_warning=None,
                until_grace=None,
                until_trigger=None,
                is_overdue=True,
            )

        since = now - state.last_checkin

        # Calculate time until each threshold
        until_warning = self.config.warning_start - since
        until_grace = self.config.grace_start - since
        until_trigger = self.config.trigger_at - since

        return TimeRemaining(
            status=state.status,
            since_checkin=since,
            until_warning=until_warning if until_warning.total_seconds() > 0 else None,
            until_grace=until_grace if until_grace.total_seconds() > 0 else None,
            until_trigger=until_trigger if until_trigger.total_seconds() > 0 else None,
            is_overdue=since > self.config.checkin_interval,
        )

    def calculate_status(self, now: datetime | None = None) -> Status:
        """Calculate what the current status should be based on timing."""
        now = now or datetime.now(timezone.utc)
        state = self.state

        # Once triggered, stay triggered
        if state.status == Status.TRIGGERED:
            return Status.TRIGGERED

        # No check-in yet - depends on how we want to handle fresh start
        if state.last_checkin is None:
            return Status.ARMED

        elapsed = now - state.last_checkin

        if elapsed >= self.config.trigger_at:
            return Status.TRIGGERED
        elif elapsed >= self.config.grace_start:
            return Status.GRACE
        elif elapsed >= self.config.warning_start:
            return Status.WARNING
        else:
            return Status.ARMED

    async def _fire_callback(self, status: Status) -> None:
        """Fire the callback for a status transition, logging any errors."""
        callbacks = {
            Status.WARNING: self._on_warning,
            Status.GRACE: self._on_grace,
            Status.TRIGGERED: self._on_trigger,
        }
        callback = callbacks.get(status)
        if callback:
            try:
                await callback()
            except Exception as e:
                logger.exception(f"Error in {status.value} callback: {e}")

    async def _transition_through(self, *statuses: Status) -> Status | None:
        """Transition through a sequence of statuses, firing callbacks.

        Returns the final status reached, or None if no transitions occurred.
        """
        final_status = None
        for status in statuses:
            if self.state_manager.transition(status):
                await self._fire_callback(status)
                final_status = status
        return final_status

    async def check_and_transition(self) -> Status | None:
        """Check current time and transition if needed.

        Returns the new status if a transition occurred, None otherwise.
        """
        current_status = self.state.status
        expected_status = self.calculate_status()

        if current_status == expected_status:
            return None

        logger.info(f"Transitioning from {current_status.value} to {expected_status.value}")

        # Determine the transition path based on current and expected status
        if expected_status == Status.WARNING:
            return await self._transition_through(Status.WARNING)

        if expected_status == Status.GRACE:
            # May skip WARNING if we were down - transition through it first
            return await self._transition_through(Status.WARNING, Status.GRACE)

        if expected_status == Status.TRIGGERED:
            # May need to fire missed callbacks for WARNING and GRACE
            return await self._transition_through(Status.WARNING, Status.GRACE, Status.TRIGGERED)

        return None

    def checkin(self, timestamp: datetime | None = None) -> bool:
        """Process a check-in.

        Returns True if check-in was accepted, False if node is triggered.
        """
        if self.state.status == Status.TRIGGERED:
            logger.warning("Check-in rejected: node is already triggered")
            return False

        self.state_manager.checkin(timestamp)
        logger.info("Check-in accepted, timer reset")
        return True

    async def _run_loop(self) -> None:
        """Main watchdog loop."""
        logger.info("Watchdog started")

        try:
            while self._running:
                await self.check_and_transition()
                await asyncio.sleep(self._check_interval)
        except asyncio.CancelledError:
            logger.info("Watchdog loop cancelled")
            raise
        except Exception as e:
            logger.exception(f"Watchdog loop error: {e}")
            raise

    def start(self) -> None:
        """Start the watchdog timer."""
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Watchdog started")

    async def stop(self) -> None:
        """Stop the watchdog timer."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Watchdog stopped")

    def is_running(self) -> bool:
        """Check if watchdog is running."""
        return self._running and self._task is not None and not self._task.done()

    async def force_check(self) -> Status | None:
        """Force an immediate status check and transition if needed."""
        return await self.check_and_transition()


def format_time_remaining(tr: TimeRemaining) -> str:
    """Format time remaining for display."""
    lines = [f"Status: {tr.status.value.upper()}"]

    if tr.since_checkin:
        days = tr.since_checkin.days
        hours = int(tr.since_checkin.total_seconds() // 3600) % 24
        minutes = int(tr.since_checkin.total_seconds() // 60) % 60
        lines.append(f"Since last check-in: {days}d {hours}h {minutes}m")

    if tr.until_warning:
        days = tr.until_warning.days
        hours = int(tr.until_warning.total_seconds() // 3600) % 24
        lines.append(f"Until warning: {days}d {hours}h")

    if tr.until_grace:
        hours = int(tr.until_grace.total_seconds() // 3600)
        minutes = int(tr.until_grace.total_seconds() // 60) % 60
        lines.append(f"Until grace: {hours}h {minutes}m")

    if tr.until_trigger:
        hours = int(tr.until_trigger.total_seconds() // 3600)
        minutes = int(tr.until_trigger.total_seconds() // 60) % 60
        lines.append(f"Until trigger: {hours}h {minutes}m")

    if tr.is_overdue:
        lines.append("⚠ Check-in overdue!")

    return "\n".join(lines)
