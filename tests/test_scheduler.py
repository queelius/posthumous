"""Tests for post-trigger scheduler."""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from posthumous.config import Config, ScheduledItem
from posthumous.state import StateManager, Status
from posthumous.scheduler import Scheduler, ScheduledExecution
from posthumous.notifications import NotificationManager
from posthumous.scripts import ScriptRunner


@pytest.fixture
def config():
    """Create test configuration with scheduled items."""
    return Config(
        node_name="test",
        secret_key="JBSWY3DPEHPK3PXP",
        notifications={"default": ["ntfy://test"]},
        post_trigger=[
            ScheduledItem(
                name="immediate",
                when="trigger",
                notify="default",
                message="Immediate notification",
            ),
            ScheduledItem(
                name="daily",
                when="every day after trigger",
                script="scripts/daily.py",
            ),
            ScheduledItem(
                name="annual",
                when="every year on trigger",
                notify="default",
                message="Annual message",
            ),
            ScheduledItem(
                name="christmas",
                when="every December 25",
                notify="default",
                message="Merry Christmas",
            ),
        ],
    )


@pytest.fixture
def state_manager(tmp_path):
    """Create test state manager."""
    return StateManager(tmp_path / "state.yaml")


@pytest.fixture
def notification_manager():
    """Create mock notification manager."""
    manager = MagicMock(spec=NotificationManager)
    manager.send = AsyncMock(return_value=MagicMock(success=True))
    return manager


class TestSchedulerInit:
    """Tests for scheduler initialization."""

    def test_parses_all_schedules(self, config, state_manager):
        scheduler = Scheduler(config, state_manager)

        assert len(scheduler._parsed_schedules) == 4
        assert "immediate" in scheduler._parsed_schedules
        assert "daily" in scheduler._parsed_schedules

    def test_handles_invalid_schedule(self, config, state_manager):
        config.post_trigger.append(ScheduledItem(
            name="invalid",
            when="what is this",
        ))

        scheduler = Scheduler(config, state_manager)

        # Should still parse valid ones
        assert "immediate" in scheduler._parsed_schedules
        assert "invalid" not in scheduler._parsed_schedules


class TestGetNextScheduled:
    """Tests for next scheduled time calculation."""

    def test_returns_empty_when_not_triggered(self, config, state_manager):
        scheduler = Scheduler(config, state_manager)
        state_manager.state.status = Status.ARMED

        result = scheduler.get_next_scheduled()

        assert result == []

    def test_returns_sorted_items(self, config, state_manager, notification_manager):
        scheduler = Scheduler(config, state_manager, notification_manager)

        trigger_time = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        state_manager.state.status = Status.TRIGGERED
        state_manager.state.trigger_time = trigger_time

        now = datetime(2026, 1, 15, 12, 0, 1, tzinfo=timezone.utc)
        result = scheduler.get_next_scheduled(now)

        assert len(result) > 0
        # Should be sorted by time
        times = [item[1] for item in result]
        assert times == sorted(times)


class TestExecuteItem:
    """Tests for scheduled item execution."""

    @pytest.mark.asyncio
    async def test_execute_notification_item(self, config, state_manager, notification_manager):
        scheduler = Scheduler(config, state_manager, notification_manager)

        state_manager.state.status = Status.TRIGGERED
        state_manager.state.trigger_time = datetime.now(timezone.utc)

        item = config.post_trigger[0]  # immediate
        execution = await scheduler.execute_item(item, "once")

        assert execution.success is True
        assert execution.item_name == "immediate"
        notification_manager.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_updates_state(self, config, state_manager, notification_manager):
        scheduler = Scheduler(config, state_manager, notification_manager)

        state_manager.state.status = Status.TRIGGERED
        state_manager.state.trigger_time = datetime.now(timezone.utc)

        item = config.post_trigger[0]
        await scheduler.execute_item(item, "once")

        # State should be updated
        assert "immediate" in state_manager.state.schedule_state
        assert state_manager.state.schedule_state["immediate"].period == "once"

    @pytest.mark.asyncio
    async def test_calls_on_scheduled_complete(self, config, state_manager, notification_manager):
        callback = AsyncMock()
        scheduler = Scheduler(
            config, state_manager, notification_manager,
            on_scheduled_complete=callback,
        )

        state_manager.state.status = Status.TRIGGERED
        state_manager.state.trigger_time = datetime.now(timezone.utc)

        item = config.post_trigger[0]
        await scheduler.execute_item(item, "once")

        callback.assert_called_once()
        execution = callback.call_args[0][0]
        assert isinstance(execution, ScheduledExecution)


class TestCheckAndExecute:
    """Tests for periodic check and execution."""

    @pytest.mark.asyncio
    async def test_no_execution_when_not_triggered(self, config, state_manager, notification_manager):
        scheduler = Scheduler(config, state_manager, notification_manager)
        state_manager.state.status = Status.ARMED

        executions = await scheduler.check_and_execute()

        assert executions == []
        notification_manager.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_executes_due_items(self, config, state_manager, notification_manager):
        scheduler = Scheduler(config, state_manager, notification_manager)

        # Set trigger time to 10 seconds ago
        trigger_time = datetime.now(timezone.utc) - timedelta(seconds=10)
        state_manager.state.status = Status.TRIGGERED
        state_manager.state.trigger_time = trigger_time
        state_manager.save()

        executions = await scheduler.check_and_execute()

        # The "immediate" item (when: "trigger") should execute
        # It fires at trigger time, which is 10 seconds ago, so it's due
        assert len(executions) >= 1
        assert any(e.item_name == "immediate" for e in executions)

    @pytest.mark.asyncio
    async def test_does_not_duplicate_execution(self, config, state_manager, notification_manager):
        scheduler = Scheduler(config, state_manager, notification_manager)

        trigger_time = datetime.now(timezone.utc) - timedelta(seconds=10)
        state_manager.state.status = Status.TRIGGERED
        state_manager.state.trigger_time = trigger_time

        # First check
        await scheduler.check_and_execute()

        # Reset mock
        notification_manager.send.reset_mock()

        # Second check - should not execute immediate again
        executions = await scheduler.check_and_execute()

        immediate_executions = [e for e in executions if e.item_name == "immediate"]
        assert len(immediate_executions) == 0


class TestMarkCompleteFromPeer:
    """Tests for peer-broadcasted completion marking."""

    def test_marks_item_complete(self, config, state_manager):
        scheduler = Scheduler(config, state_manager)

        scheduler.mark_complete_from_peer("annual", "2026")

        assert "annual" in state_manager.state.schedule_state
        assert state_manager.state.schedule_state["annual"].period == "2026"

    @pytest.mark.asyncio
    async def test_prevents_local_execution(self, config, state_manager, notification_manager):
        scheduler = Scheduler(config, state_manager, notification_manager)

        trigger_time = datetime.now(timezone.utc)
        state_manager.state.status = Status.TRIGGERED
        state_manager.state.trigger_time = trigger_time

        # Mark as complete from peer
        scheduler.mark_complete_from_peer("immediate", "once")

        # Now check - should not execute
        executions = await scheduler.check_and_execute()

        immediate_executions = [e for e in executions if e.item_name == "immediate"]
        assert len(immediate_executions) == 0


class TestScheduleStatus:
    """Tests for schedule status reporting."""

    def test_get_schedule_status(self, config, state_manager):
        scheduler = Scheduler(config, state_manager)

        trigger_time = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        state_manager.state.status = Status.TRIGGERED
        state_manager.state.trigger_time = trigger_time

        status = scheduler.get_schedule_status()

        assert len(status) == 4
        assert all("name" in item for item in status)
        assert all("when" in item for item in status)

    def test_status_shows_last_run(self, config, state_manager):
        scheduler = Scheduler(config, state_manager)

        state_manager.state.mark_schedule_item_run("immediate", "once")
        trigger_time = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        state_manager.state.status = Status.TRIGGERED
        state_manager.state.trigger_time = trigger_time

        status = scheduler.get_schedule_status()

        immediate_status = next(s for s in status if s["name"] == "immediate")
        assert immediate_status["last_period"] == "once"


class TestExecuteItemExtended:
    """Extended tests for scheduled item execution."""

    @pytest.mark.asyncio
    async def test_execute_script_item(self, config, state_manager, notification_manager):
        script_runner = MagicMock(spec=ScriptRunner)
        script_runner.run = AsyncMock(return_value=MagicMock(success=True, error=None))

        scheduler = Scheduler(
            config, state_manager, notification_manager,
            script_runner=script_runner,
        )

        state_manager.state.status = Status.TRIGGERED
        state_manager.state.trigger_time = datetime.now(timezone.utc)

        item = config.post_trigger[1]  # daily (has script)
        execution = await scheduler.execute_item(item, "2026-01-15")

        assert execution.success is True
        script_runner.run.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_item_both_notify_and_script(self, config, state_manager, notification_manager):
        """Item with both notification and script should run both."""
        script_runner = MagicMock(spec=ScriptRunner)
        script_runner.run = AsyncMock(return_value=MagicMock(success=True, error=None))

        scheduler = Scheduler(
            config, state_manager, notification_manager,
            script_runner=script_runner,
        )

        state_manager.state.status = Status.TRIGGERED
        state_manager.state.trigger_time = datetime.now(timezone.utc)

        # Create item with both
        item = ScheduledItem(
            name="both",
            when="trigger",
            notify="default",
            message="Test",
            script="scripts/test.py",
        )

        execution = await scheduler.execute_item(item, "once")

        assert execution.success is True
        notification_manager.send.assert_called_once()
        script_runner.run.assert_called_once()

    @pytest.mark.asyncio
    async def test_notification_failure(self, config, state_manager):
        notification_manager = MagicMock(spec=NotificationManager)
        notification_manager.send = AsyncMock(
            return_value=MagicMock(success=False, error="channel down")
        )

        scheduler = Scheduler(config, state_manager, notification_manager)

        state_manager.state.status = Status.TRIGGERED
        state_manager.state.trigger_time = datetime.now(timezone.utc)

        item = config.post_trigger[0]  # immediate (has notify)
        execution = await scheduler.execute_item(item, "once")

        assert execution.success is False
        assert "Notification failed" in execution.error

    @pytest.mark.asyncio
    async def test_script_failure(self, config, state_manager, notification_manager):
        script_runner = MagicMock(spec=ScriptRunner)
        script_runner.run = AsyncMock(
            return_value=MagicMock(success=False, error="Exit code: 1")
        )

        scheduler = Scheduler(
            config, state_manager, notification_manager,
            script_runner=script_runner,
        )

        state_manager.state.status = Status.TRIGGERED
        state_manager.state.trigger_time = datetime.now(timezone.utc)

        item = config.post_trigger[1]  # daily (has script)
        execution = await scheduler.execute_item(item, "2026-01-15")

        assert execution.success is False
        assert "Script failed" in execution.error

    @pytest.mark.asyncio
    async def test_both_fail_captures_both_errors(self, config, state_manager):
        """Regression test for Bug #1: both notification and script errors must be captured."""
        notification_manager = MagicMock(spec=NotificationManager)
        notification_manager.send = AsyncMock(
            return_value=MagicMock(success=False, error="channel down")
        )

        script_runner = MagicMock(spec=ScriptRunner)
        script_runner.run = AsyncMock(
            return_value=MagicMock(success=False, error="Exit code: 1")
        )

        scheduler = Scheduler(
            config, state_manager, notification_manager,
            script_runner=script_runner,
        )

        state_manager.state.status = Status.TRIGGERED
        state_manager.state.trigger_time = datetime.now(timezone.utc)

        item = ScheduledItem(
            name="both_fail",
            when="trigger",
            notify="default",
            message="Test",
            script="scripts/test.py",
        )

        execution = await scheduler.execute_item(item, "once")

        assert execution.success is False
        # Both errors should be present, separated by "; "
        assert "Notification failed" in execution.error
        assert "Script failed" in execution.error
        assert "; " in execution.error

    @pytest.mark.asyncio
    async def test_callback_error_does_not_crash(self, config, state_manager, notification_manager):
        """Callback exception should be logged, not propagated."""
        callback = AsyncMock(side_effect=RuntimeError("callback boom"))
        scheduler = Scheduler(
            config, state_manager, notification_manager,
            on_scheduled_complete=callback,
        )

        state_manager.state.status = Status.TRIGGERED
        state_manager.state.trigger_time = datetime.now(timezone.utc)

        item = config.post_trigger[0]
        # Should not raise
        execution = await scheduler.execute_item(item, "once")

        assert execution.success is True
        callback.assert_called_once()


class TestCheckAndExecuteExtended:
    """Extended tests for check_and_execute."""

    @pytest.mark.asyncio
    async def test_no_trigger_time_returns_empty(self, config, state_manager, notification_manager):
        scheduler = Scheduler(config, state_manager, notification_manager)

        state_manager.state.status = Status.TRIGGERED
        state_manager.state.trigger_time = None  # No trigger time

        executions = await scheduler.check_and_execute()

        assert executions == []

    @pytest.mark.asyncio
    async def test_invalid_schedule_skipped(self, config, state_manager, notification_manager):
        """Items with unparseable schedules should be silently skipped."""
        config.post_trigger.append(ScheduledItem(
            name="invalid",
            when="nonsense expression",
        ))

        scheduler = Scheduler(config, state_manager, notification_manager)

        state_manager.state.status = Status.TRIGGERED
        state_manager.state.trigger_time = datetime.now(timezone.utc) - timedelta(seconds=10)

        executions = await scheduler.check_and_execute()

        # Should execute valid items but not invalid
        invalid_execs = [e for e in executions if e.item_name == "invalid"]
        assert len(invalid_execs) == 0


class TestSchedulerHelpers:
    """Tests for get_item_by_name and get_schedule."""

    def test_get_item_by_name_found(self, config, state_manager):
        scheduler = Scheduler(config, state_manager)
        item = scheduler.get_item_by_name("immediate")
        assert item is not None
        assert item.name == "immediate"

    def test_get_item_by_name_not_found(self, config, state_manager):
        scheduler = Scheduler(config, state_manager)
        assert scheduler.get_item_by_name("nonexistent") is None

    def test_get_schedule_found(self, config, state_manager):
        scheduler = Scheduler(config, state_manager)
        schedule = scheduler.get_schedule("immediate")
        assert schedule is not None

    def test_get_schedule_not_found(self, config, state_manager):
        scheduler = Scheduler(config, state_manager)
        assert scheduler.get_schedule("nonexistent") is None


class TestSchedulerLifecycle:
    """Tests for scheduler start/stop."""

    @pytest.mark.asyncio
    async def test_start_stop(self, config, state_manager):
        scheduler = Scheduler(config, state_manager)
        scheduler._check_interval = 0.1

        scheduler.start()
        assert scheduler.is_running()

        await scheduler.stop()
        assert not scheduler.is_running()

    @pytest.mark.asyncio
    async def test_start_when_already_running(self, config, state_manager):
        scheduler = Scheduler(config, state_manager)
        scheduler._check_interval = 0.1

        scheduler.start()
        task1 = scheduler._task

        scheduler.start()  # Should be a no-op
        assert scheduler._task is task1

        await scheduler.stop()

    @pytest.mark.asyncio
    async def test_run_loop_executes_when_triggered(self, config, state_manager, notification_manager):
        """The _run_loop should call check_and_execute when TRIGGERED."""
        import asyncio

        scheduler = Scheduler(config, state_manager, notification_manager)
        scheduler._check_interval = 0.05

        trigger_time = datetime.now(timezone.utc) - timedelta(seconds=10)
        state_manager.state.status = Status.TRIGGERED
        state_manager.state.trigger_time = trigger_time

        scheduler.start()
        await asyncio.sleep(0.15)  # Allow a couple loop iterations
        await scheduler.stop()

        # The "immediate" item should have been executed
        assert "immediate" in state_manager.state.schedule_state

    @pytest.mark.asyncio
    async def test_run_loop_skips_when_not_triggered(self, config, state_manager, notification_manager):
        """The _run_loop should not execute anything when ARMED."""
        import asyncio

        scheduler = Scheduler(config, state_manager, notification_manager)
        scheduler._check_interval = 0.05

        state_manager.state.status = Status.ARMED

        scheduler.start()
        await asyncio.sleep(0.15)
        await scheduler.stop()

        notification_manager.send.assert_not_called()


class TestScheduleStatusExtended:
    """Extended tests for schedule status reporting."""

    def test_status_no_trigger_time(self, config, state_manager):
        """get_schedule_status with no trigger_time returns null next_run."""
        scheduler = Scheduler(config, state_manager)

        state_manager.state.status = Status.TRIGGERED
        state_manager.state.trigger_time = None

        status = scheduler.get_schedule_status()

        for item in status:
            assert item["next_run"] is None

    def test_status_invalid_schedule_shows_error(self, config, state_manager):
        """Invalid schedule items should show error in status."""
        config.post_trigger.append(ScheduledItem(
            name="broken",
            when="nonsense",
        ))

        scheduler = Scheduler(config, state_manager)

        state_manager.state.status = Status.TRIGGERED
        state_manager.state.trigger_time = None

        status = scheduler.get_schedule_status()

        broken_status = next(s for s in status if s["name"] == "broken")
        assert broken_status["error"] == "Invalid when-expression"

    def test_status_with_next_occurrence(self, config, state_manager):
        """get_schedule_status when triggered should compute next_run."""
        scheduler = Scheduler(config, state_manager)

        trigger_time = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        state_manager.state.status = Status.TRIGGERED
        state_manager.state.trigger_time = trigger_time

        now = datetime(2026, 1, 16, 12, 0, 0, tzinfo=timezone.utc)
        with patch("posthumous.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            # get_schedule_status uses datetime.now internally
            status = scheduler.get_schedule_status()

        daily_status = next(s for s in status if s["name"] == "daily")
        assert daily_status["next_run"] is not None
