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


def _format_duration(td: timedelta) -> str:
    """Format a timedelta into a human-readable string using the most meaningful units."""
    total = int(td.total_seconds())
    if total < 0:
        return "0s"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours > 0:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    if minutes > 0:
        return f"{minutes}m {seconds}s" if seconds else f"{minutes}m"
    return f"{seconds}s"


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

        if self.status == Status.ARMED:
            if self.until_warning:
                return f"ARMED - {_format_duration(self.until_warning)} until warning"
            return "ARMED"
        elif self.status == Status.WARNING:
            if self.until_grace:
                return f"WARNING - {_format_duration(self.until_grace)} until grace period"
            return "WARNING"
        elif self.status == Status.GRACE:
            if self.until_trigger:
                return f"GRACE - {_format_duration(self.until_trigger)} until trigger"
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
        quorum_coordinator=None,  # QuorumCoordinator | None (typed loosely to avoid import cycle)
    ):
        self.config = config
        self.state_manager = state_manager
        self._on_warning = on_warning
        self._on_grace = on_grace
        self._on_trigger = on_trigger
        self._quorum_coordinator = quorum_coordinator
        self._task: asyncio.Task | None = None
        self._running = False

        # Auto-tune check interval based on timing thresholds
        if config.check_interval:
            self._check_interval = config.check_interval.total_seconds()
        else:
            min_interval = min(
                config.warning_start,
                config.grace_start - config.warning_start,
                config.trigger_at - config.grace_start,
            ).total_seconds()
            self._check_interval = max(1.0, min(60.0, min_interval / 4))

    @property
    def state(self) -> State:
        return self.state_manager.state

    def get_time_remaining(self, now: datetime | None = None) -> TimeRemaining:
        """Calculate time remaining until each transition."""
        now = now or datetime.now(timezone.utc)
        state = self.state

        if state.status == Status.TRIGGERED:
            since = (now - state.last_checkin) if state.last_checkin else None
            return TimeRemaining(
                status=Status.TRIGGERED,
                since_checkin=since,
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
                is_overdue=state.status != Status.ARMED,
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
            # Quorum path: run the protocol instead of transitioning directly.
            # The coordinator handles peer broadcast; we apply the local
            # TRIGGERED transition and fire on_trigger locally on success.
            if self.config.quorum is not None and self._quorum_coordinator is not None:
                # Catch up through WARNING and GRACE so their callbacks fire.
                await self._transition_through(Status.WARNING, Status.GRACE)
                if not self.state_manager.transition(Status.PENDING_QUORUM):
                    return None
                if await self._quorum_coordinator.attempt_trigger():
                    self.state_manager.transition(Status.TRIGGERED)
                    await self._fire_callback(Status.TRIGGERED)
                    return Status.TRIGGERED
                # Quorum failed: drop back to GRACE so the next tick retries.
                self.state_manager.transition(Status.GRACE)
                return Status.GRACE

            # No quorum configured: existing v0.6 behavior.
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
        lines.append(f"Since last check-in: {_format_duration(tr.since_checkin)}")

    if tr.until_warning:
        lines.append(f"Until warning: {_format_duration(tr.until_warning)}")

    if tr.until_grace:
        lines.append(f"Until grace: {_format_duration(tr.until_grace)}")

    if tr.until_trigger:
        lines.append(f"Until trigger: {_format_duration(tr.until_trigger)}")

    if tr.is_overdue:
        lines.append("⚠ Check-in overdue!")

    return "\n".join(lines)
