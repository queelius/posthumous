"""Tests for the watchdog timer and state machine."""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from posthumous.config import Config
from posthumous.state import StateManager, Status
from posthumous.watchdog import Watchdog, TimeRemaining, format_time_remaining


@pytest.fixture
def config():
    """Create a test configuration."""
    return Config(
        node_name="test",
        secret_key="JBSWY3DPEHPK3PXP",
        checkin_interval=timedelta(days=7),
        warning_start=timedelta(days=8),
        grace_start=timedelta(days=12),
        trigger_at=timedelta(days=14),
    )


@pytest.fixture
def state_manager(tmp_path):
    """Create a test state manager."""
    return StateManager(tmp_path / "state.yaml")


class TestWatchdogCalculateStatus:
    """Tests for status calculation based on time."""

    def test_armed_within_warning(self, config, state_manager):
        watchdog = Watchdog(config, state_manager)

        # Check in 5 days ago
        now = datetime.now(timezone.utc)
        state_manager.state.last_checkin = now - timedelta(days=5)

        assert watchdog.calculate_status(now) == Status.ARMED

    def test_warning_past_warning_threshold(self, config, state_manager):
        watchdog = Watchdog(config, state_manager)

        now = datetime.now(timezone.utc)
        state_manager.state.last_checkin = now - timedelta(days=10)

        assert watchdog.calculate_status(now) == Status.WARNING

    def test_grace_past_grace_threshold(self, config, state_manager):
        watchdog = Watchdog(config, state_manager)

        now = datetime.now(timezone.utc)
        state_manager.state.last_checkin = now - timedelta(days=13)

        assert watchdog.calculate_status(now) == Status.GRACE

    def test_triggered_past_trigger_threshold(self, config, state_manager):
        watchdog = Watchdog(config, state_manager)

        now = datetime.now(timezone.utc)
        state_manager.state.last_checkin = now - timedelta(days=15)

        assert watchdog.calculate_status(now) == Status.TRIGGERED

    def test_no_checkin_returns_armed(self, config, state_manager):
        watchdog = Watchdog(config, state_manager)
        assert watchdog.calculate_status() == Status.ARMED

    def test_triggered_stays_triggered(self, config, state_manager):
        watchdog = Watchdog(config, state_manager)
        state_manager.state.status = Status.TRIGGERED
        state_manager.state.trigger_time = datetime.now(timezone.utc)

        assert watchdog.calculate_status() == Status.TRIGGERED


class TestWatchdogTransitions:
    """Tests for state transitions."""

    @pytest.mark.asyncio
    async def test_transition_to_warning_calls_callback(self, config, state_manager):
        on_warning = AsyncMock()
        watchdog = Watchdog(config, state_manager, on_warning=on_warning)

        now = datetime.now(timezone.utc)
        state_manager.state.last_checkin = now - timedelta(days=10)
        state_manager.state.status = Status.ARMED

        result = await watchdog.check_and_transition()

        assert result == Status.WARNING
        on_warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_transition_to_grace_calls_callback(self, config, state_manager):
        on_grace = AsyncMock()
        watchdog = Watchdog(config, state_manager, on_grace=on_grace)

        now = datetime.now(timezone.utc)
        state_manager.state.last_checkin = now - timedelta(days=13)
        state_manager.state.status = Status.WARNING

        result = await watchdog.check_and_transition()

        assert result == Status.GRACE
        on_grace.assert_called_once()

    @pytest.mark.asyncio
    async def test_transition_to_triggered_calls_callback(self, config, state_manager):
        on_trigger = AsyncMock()
        watchdog = Watchdog(config, state_manager, on_trigger=on_trigger)

        now = datetime.now(timezone.utc)
        state_manager.state.last_checkin = now - timedelta(days=15)
        state_manager.state.status = Status.GRACE

        result = await watchdog.check_and_transition()

        assert result == Status.TRIGGERED
        on_trigger.assert_called_once()

    @pytest.mark.asyncio
    async def test_skipped_transitions_fire_all_callbacks(self, config, state_manager):
        """Test that if we were offline and missed warnings, all callbacks fire."""
        on_warning = AsyncMock()
        on_grace = AsyncMock()
        on_trigger = AsyncMock()

        watchdog = Watchdog(
            config, state_manager,
            on_warning=on_warning,
            on_grace=on_grace,
            on_trigger=on_trigger,
        )

        # Node was down for a long time - jump straight from ARMED past TRIGGER
        now = datetime.now(timezone.utc)
        state_manager.state.last_checkin = now - timedelta(days=15)
        state_manager.state.status = Status.ARMED

        result = await watchdog.check_and_transition()

        assert result == Status.TRIGGERED
        on_warning.assert_called_once()
        on_grace.assert_called_once()
        on_trigger.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_transition_no_callback(self, config, state_manager):
        on_warning = AsyncMock()
        watchdog = Watchdog(config, state_manager, on_warning=on_warning)

        now = datetime.now(timezone.utc)
        state_manager.state.last_checkin = now - timedelta(days=5)
        state_manager.state.status = Status.ARMED

        result = await watchdog.check_and_transition()

        assert result is None
        on_warning.assert_not_called()


class TestWatchdogCheckin:
    """Tests for check-in processing."""

    def test_checkin_resets_timer(self, config, state_manager):
        watchdog = Watchdog(config, state_manager)

        # Set up warning state
        now = datetime.now(timezone.utc)
        state_manager.state.last_checkin = now - timedelta(days=10)
        state_manager.state.status = Status.WARNING

        result = watchdog.checkin()

        assert result is True
        assert state_manager.state.status == Status.ARMED
        # Last checkin should be very recent
        assert (datetime.now(timezone.utc) - state_manager.state.last_checkin).seconds < 2

    def test_checkin_with_timestamp(self, config, state_manager):
        watchdog = Watchdog(config, state_manager)

        timestamp = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        watchdog.checkin(timestamp)

        assert state_manager.state.last_checkin == timestamp

    def test_checkin_rejected_when_triggered(self, config, state_manager):
        watchdog = Watchdog(config, state_manager)

        state_manager.state.status = Status.TRIGGERED
        state_manager.state.trigger_time = datetime.now(timezone.utc)

        result = watchdog.checkin()

        assert result is False
        assert state_manager.state.status == Status.TRIGGERED


class TestTimeRemaining:
    """Tests for time remaining calculations."""

    def test_time_remaining_armed(self, config, state_manager):
        watchdog = Watchdog(config, state_manager)

        now = datetime.now(timezone.utc)
        state_manager.state.last_checkin = now - timedelta(days=5)
        state_manager.state.status = Status.ARMED

        tr = watchdog.get_time_remaining(now)

        assert tr.status == Status.ARMED
        assert tr.since_checkin == timedelta(days=5)
        assert tr.until_warning is not None
        assert tr.until_warning.days == 3  # 8 days - 5 days
        assert not tr.is_overdue

    def test_time_remaining_overdue(self, config, state_manager):
        watchdog = Watchdog(config, state_manager)

        now = datetime.now(timezone.utc)
        state_manager.state.last_checkin = now - timedelta(days=9)

        tr = watchdog.get_time_remaining(now)

        assert tr.is_overdue  # Past 7 day checkin interval

    def test_time_remaining_triggered(self, config, state_manager):
        watchdog = Watchdog(config, state_manager)

        state_manager.state.status = Status.TRIGGERED
        state_manager.state.trigger_time = datetime.now(timezone.utc)

        tr = watchdog.get_time_remaining()

        assert tr.status == Status.TRIGGERED
        assert tr.until_trigger is None

    def test_time_remaining_no_checkin(self, config, state_manager):
        watchdog = Watchdog(config, state_manager)

        tr = watchdog.get_time_remaining()

        assert tr.since_checkin is None
        assert tr.is_overdue is True


class TestWatchdogLifecycle:
    """Tests for watchdog start/stop."""

    @pytest.mark.asyncio
    async def test_start_stop(self, config, state_manager):
        watchdog = Watchdog(config, state_manager)
        watchdog._check_interval = 0.1  # Speed up for test

        watchdog.start()
        assert watchdog.is_running()

        await watchdog.stop()
        assert not watchdog.is_running()

    @pytest.mark.asyncio
    async def test_force_check(self, config, state_manager):
        on_warning = AsyncMock()
        watchdog = Watchdog(config, state_manager, on_warning=on_warning)

        now = datetime.now(timezone.utc)
        state_manager.state.last_checkin = now - timedelta(days=10)

        result = await watchdog.force_check()

        assert result == Status.WARNING
        on_warning.assert_called_once()


class TestTimeRemainingSummary:
    """Tests for TimeRemaining.summary() method."""

    def test_summary_triggered(self):
        tr = TimeRemaining(
            status=Status.TRIGGERED,
            since_checkin=None,
            until_warning=None,
            until_grace=None,
            until_trigger=None,
            is_overdue=False,
        )
        assert tr.summary() == "TRIGGERED"

    def test_summary_no_checkin(self):
        tr = TimeRemaining(
            status=Status.ARMED,
            since_checkin=None,
            until_warning=None,
            until_grace=None,
            until_trigger=None,
            is_overdue=True,
        )
        assert tr.summary() == "Never checked in"

    def test_summary_armed_with_warning_countdown(self):
        tr = TimeRemaining(
            status=Status.ARMED,
            since_checkin=timedelta(days=5),
            until_warning=timedelta(days=3, hours=5),
            until_grace=timedelta(days=7, hours=5),
            until_trigger=timedelta(days=9, hours=5),
            is_overdue=False,
        )
        summary = tr.summary()
        assert "ARMED" in summary
        assert "3d" in summary
        assert "until warning" in summary

    def test_summary_armed_without_warning(self):
        tr = TimeRemaining(
            status=Status.ARMED,
            since_checkin=timedelta(days=1),
            until_warning=None,
            until_grace=None,
            until_trigger=None,
            is_overdue=False,
        )
        assert tr.summary() == "ARMED"

    def test_summary_warning_with_grace_countdown(self):
        tr = TimeRemaining(
            status=Status.WARNING,
            since_checkin=timedelta(days=9),
            until_warning=None,
            until_grace=timedelta(hours=72),
            until_trigger=timedelta(hours=120),
            is_overdue=True,
        )
        summary = tr.summary()
        assert "WARNING" in summary
        assert "72h" in summary
        assert "until grace" in summary

    def test_summary_warning_without_grace(self):
        tr = TimeRemaining(
            status=Status.WARNING,
            since_checkin=timedelta(days=9),
            until_warning=None,
            until_grace=None,
            until_trigger=None,
            is_overdue=True,
        )
        assert tr.summary() == "WARNING"

    def test_summary_grace_with_trigger_countdown(self):
        tr = TimeRemaining(
            status=Status.GRACE,
            since_checkin=timedelta(days=13),
            until_warning=None,
            until_grace=None,
            until_trigger=timedelta(hours=24),
            is_overdue=True,
        )
        summary = tr.summary()
        assert "GRACE" in summary
        assert "24h" in summary
        assert "until trigger" in summary

    def test_summary_grace_without_trigger(self):
        tr = TimeRemaining(
            status=Status.GRACE,
            since_checkin=timedelta(days=13),
            until_warning=None,
            until_grace=None,
            until_trigger=None,
            is_overdue=True,
        )
        assert tr.summary() == "GRACE"


class TestFormatTimeRemaining:
    """Tests for format_time_remaining() display function."""

    def test_format_armed_with_all_times(self, config, state_manager):
        watchdog = Watchdog(config, state_manager)

        now = datetime.now(timezone.utc)
        state_manager.state.last_checkin = now - timedelta(days=5)
        state_manager.state.status = Status.ARMED

        tr = watchdog.get_time_remaining(now)
        formatted = format_time_remaining(tr)

        assert "ARMED" in formatted
        assert "Since last check-in:" in formatted
        assert "Until warning:" in formatted

    def test_format_triggered(self, config, state_manager):
        watchdog = Watchdog(config, state_manager)
        state_manager.state.status = Status.TRIGGERED
        state_manager.state.trigger_time = datetime.now(timezone.utc)

        tr = watchdog.get_time_remaining()
        formatted = format_time_remaining(tr)

        assert "TRIGGERED" in formatted

    def test_format_overdue_shows_warning(self, config, state_manager):
        watchdog = Watchdog(config, state_manager)

        now = datetime.now(timezone.utc)
        state_manager.state.last_checkin = now - timedelta(days=9)

        tr = watchdog.get_time_remaining(now)
        formatted = format_time_remaining(tr)

        assert "overdue" in formatted

    def test_format_no_checkin(self, config, state_manager):
        watchdog = Watchdog(config, state_manager)

        tr = watchdog.get_time_remaining()
        formatted = format_time_remaining(tr)

        assert "ARMED" in formatted
        assert "overdue" in formatted


class TestWatchdogCallbackExceptions:
    """Tests for error handling in callbacks."""

    @pytest.mark.asyncio
    async def test_callback_exception_does_not_stop_transition(self, config, state_manager):
        """If a callback raises, the watchdog should log it and continue."""
        on_warning = AsyncMock(side_effect=RuntimeError("callback boom"))
        on_grace = AsyncMock()

        watchdog = Watchdog(
            config, state_manager,
            on_warning=on_warning,
            on_grace=on_grace,
        )

        now = datetime.now(timezone.utc)
        state_manager.state.last_checkin = now - timedelta(days=13)
        state_manager.state.status = Status.ARMED

        result = await watchdog.check_and_transition()

        # Despite warning callback failure, grace transition should still happen
        assert result == Status.GRACE
        on_warning.assert_called_once()
        on_grace.assert_called_once()

    @pytest.mark.asyncio
    async def test_partial_catchup_from_warning_to_grace(self, config, state_manager):
        """Already in WARNING, time passes to GRACE — only grace callback fires."""
        on_warning = AsyncMock()
        on_grace = AsyncMock()

        watchdog = Watchdog(
            config, state_manager,
            on_warning=on_warning,
            on_grace=on_grace,
        )

        now = datetime.now(timezone.utc)
        state_manager.state.last_checkin = now - timedelta(days=13)
        state_manager.state.status = Status.WARNING

        result = await watchdog.check_and_transition()

        assert result == Status.GRACE
        # WARNING callback should NOT fire (already in WARNING)
        on_warning.assert_not_called()
        on_grace.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_callbacks_configured(self, config, state_manager):
        """Watchdog should work fine even with no callbacks."""
        watchdog = Watchdog(config, state_manager)

        now = datetime.now(timezone.utc)
        state_manager.state.last_checkin = now - timedelta(days=15)
        state_manager.state.status = Status.ARMED

        result = await watchdog.check_and_transition()
        assert result == Status.TRIGGERED
