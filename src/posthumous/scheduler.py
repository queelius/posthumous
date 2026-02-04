"""Post-trigger scheduler for Posthumous."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Awaitable

from posthumous.config import Config, ScheduledItem
from posthumous.dsl import parse_when_expression, should_execute, get_next_occurrence, ParsedSchedule
from posthumous.notifications import NotificationManager, build_context
from posthumous.scripts import ScriptRunner, ScriptContext
from posthumous.state import StateManager, Status

logger = logging.getLogger(__name__)


@dataclass
class ScheduledExecution:
    """Record of a scheduled item execution."""
    item_name: str
    period_key: str
    executed_at: datetime
    success: bool
    error: str | None = None


class Scheduler:
    """Manages post-trigger scheduled actions.

    After trigger, runs scheduled items according to their when-expressions,
    with period-based deduplication to prevent double-execution.
    """

    def __init__(
        self,
        config: Config,
        state_manager: StateManager,
        notification_manager: NotificationManager | None = None,
        script_runner: ScriptRunner | None = None,
        on_scheduled_complete: Callable[[ScheduledExecution], Awaitable[None]] | None = None,
    ):
        self.config = config
        self.state_manager = state_manager
        self.notification_manager = notification_manager
        self.script_runner = script_runner or ScriptRunner(config.config_dir)
        self._on_scheduled_complete = on_scheduled_complete

        # Parse all when-expressions upfront
        self._parsed_schedules: dict[str, ParsedSchedule] = {}
        for item in config.post_trigger:
            try:
                self._parsed_schedules[item.name] = parse_when_expression(item.when)
            except ValueError as e:
                logger.error(f"Invalid when-expression for '{item.name}': {e}")

        self._task: asyncio.Task | None = None
        self._running = False
        self._check_interval = 60.0  # Check every minute

    @property
    def state(self):
        return self.state_manager.state

    def get_item_by_name(self, name: str) -> ScheduledItem | None:
        """Get a scheduled item by name."""
        for item in self.config.post_trigger:
            if item.name == name:
                return item
        return None

    def get_schedule(self, item_name: str) -> ParsedSchedule | None:
        """Get parsed schedule for an item."""
        return self._parsed_schedules.get(item_name)

    def get_next_scheduled(
        self,
        now: datetime | None = None,
    ) -> list[tuple[ScheduledItem, datetime, str]]:
        """Get list of items with their next scheduled time.

        Returns list of (item, next_time, period_key) tuples, sorted by time.
        """
        now = now or datetime.now(timezone.utc)

        if self.state.status != Status.TRIGGERED or not self.state.trigger_time:
            return []

        result = []
        for item in self.config.post_trigger:
            schedule = self._parsed_schedules.get(item.name)
            if not schedule:
                continue

            next_occ = get_next_occurrence(schedule, self.state.trigger_time, now)
            if next_occ:
                result.append((item, next_occ.time, next_occ.period_key))

        # Sort by time
        result.sort(key=lambda x: x[1])
        return result

    async def execute_item(self, item: ScheduledItem, period_key: str) -> ScheduledExecution:
        """Execute a scheduled item.

        Args:
            item: The scheduled item to execute
            period_key: The period key for deduplication

        Returns:
            ScheduledExecution record
        """
        logger.info(f"Executing scheduled item '{item.name}' for period {period_key}")

        errors: list[str] = []

        # Build context for scripts and notifications
        context = build_context(
            node_name=self.config.node_name,
            status=self.state.status.value,
            last_checkin=self.state.last_checkin,
            trigger_time=self.state.trigger_time,
            trigger_at=self.config.trigger_at,
        )
        context["schedule_item"] = item.name
        context["period_key"] = period_key

        # Execute notification if configured
        if item.notify and item.message and self.notification_manager:
            result = await self.notification_manager.send(
                channel=item.notify,
                message=item.message,
                title=f"Posthumous: {item.name}",
                context=context,
            )
            if not result.success:
                errors.append(f"Notification failed: {result.error}")

        # Execute script if configured
        if item.script:
            script_context = ScriptContext(
                event="scheduled",
                trigger_time=self.state.trigger_time,
                node_name=self.config.node_name,
                status=self.state.status.value,
                last_checkin=self.state.last_checkin,
                schedule_item=item.name,
                extra={"period_key": period_key},
            )

            result = await self.script_runner.run(item.script, script_context)
            if not result.success:
                errors.append(f"Script failed: {result.error}")

        # Record execution in state
        self.state_manager.state.mark_schedule_item_run(item.name, period_key)
        self.state_manager.save()

        success = len(errors) == 0
        error = "; ".join(errors) if errors else None

        execution = ScheduledExecution(
            item_name=item.name,
            period_key=period_key,
            executed_at=datetime.now(timezone.utc),
            success=success,
            error=error,
        )

        if self._on_scheduled_complete:
            try:
                await self._on_scheduled_complete(execution)
            except Exception as e:
                logger.exception(f"Error in on_scheduled_complete callback: {e}")

        if success:
            logger.info(f"Scheduled item '{item.name}' completed successfully")
        else:
            logger.error(f"Scheduled item '{item.name}' failed: {error}")

        return execution

    async def check_and_execute(self) -> list[ScheduledExecution]:
        """Check all scheduled items and execute those that are due.

        Returns list of executions performed.
        """
        if self.state.status != Status.TRIGGERED:
            return []

        if not self.state.trigger_time:
            logger.warning("Triggered but no trigger_time set")
            return []

        now = datetime.now(timezone.utc)
        executions = []

        for item in self.config.post_trigger:
            schedule = self._parsed_schedules.get(item.name)
            if not schedule:
                continue

            # Check state for last run
            item_state = self.state.schedule_state.get(item.name)
            last_period = item_state.period if item_state else None

            should_run, period_key = should_execute(
                schedule,
                self.state.trigger_time,
                last_period,
                now,
            )

            if should_run and period_key:
                execution = await self.execute_item(item, period_key)
                executions.append(execution)

        return executions

    async def _run_loop(self) -> None:
        """Main scheduler loop."""
        logger.info("Scheduler started")

        try:
            while self._running:
                if self.state.status == Status.TRIGGERED:
                    await self.check_and_execute()
                await asyncio.sleep(self._check_interval)
        except asyncio.CancelledError:
            logger.info("Scheduler loop cancelled")
            raise
        except Exception as e:
            logger.exception(f"Scheduler loop error: {e}")
            raise

    def start(self) -> None:
        """Start the scheduler."""
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Scheduler started")

    async def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Scheduler stopped")

    def is_running(self) -> bool:
        """Check if scheduler is running."""
        return self._running and self._task is not None and not self._task.done()

    def mark_complete_from_peer(self, item_name: str, period_key: str) -> None:
        """Mark a scheduled item as complete (received from peer).

        This prevents this node from running the same item.
        """
        self.state_manager.state.mark_schedule_item_run(item_name, period_key)
        self.state_manager.save()
        logger.info(f"Marked '{item_name}' as complete for period {period_key} (from peer)")

    def get_schedule_status(self) -> list[dict[str, Any]]:
        """Get status of all scheduled items.

        Returns list of dicts with item info and next run time.
        """
        now = datetime.now(timezone.utc)
        status = []

        for item in self.config.post_trigger:
            schedule = self._parsed_schedules.get(item.name)
            item_state = self.state.schedule_state.get(item.name)

            info: dict[str, Any] = {
                "name": item.name,
                "when": item.when,
                "last_run": item_state.last_run.isoformat() if item_state and item_state.last_run else None,
                "last_period": item_state.period if item_state else None,
            }

            if schedule and self.state.trigger_time:
                next_occ = get_next_occurrence(schedule, self.state.trigger_time, now)
                if next_occ:
                    info["next_run"] = next_occ.time.isoformat()
                    info["next_period"] = next_occ.period_key
                else:
                    info["next_run"] = None
                    info["next_period"] = None
            else:
                info["next_run"] = None
                info["next_period"] = None
                if not schedule:
                    info["error"] = "Invalid when-expression"

            status.append(info)

        return status
