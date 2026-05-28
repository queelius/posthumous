"""Tests for the watchdog timer and state machine."""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from posthumous.config import Config
from posthumous.state import StateManager, Status
from posthumous.watchdog import Watchdog, TimeRemaining, format_time_remaining, _format_duration


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
        # Fresh ARMED node with no checkin is not overdue — it's just new
        assert tr.is_overdue is False


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
        assert "3d" in summary  # 72h = 3d, _format_duration picks days
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
        assert "1d" in summary  # 24h = 1d, _format_duration picks days
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
        # Fresh ARMED node is not overdue
        assert "overdue" not in formatted


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


class TestTimeRemainingSummaryEdgeCases:
    """Edge-case tests for TimeRemaining.summary()."""

    def test_summary_unknown_status(self):
        """TimeRemaining with unexpected status should return status string."""
        # This covers line 55 — the fallback path for unexpected status values.
        # We simulate this by creating a TimeRemaining with a status that doesn't
        # match any of the if/elif branches (ARMED/WARNING/GRACE/TRIGGERED).
        # Since all Status enum values are handled, we test via a non-None since_checkin
        # but status that falls through. Actually, TRIGGERED and no-checkin are
        # handled first. The only way to reach line 55 is with a status not in
        # {TRIGGERED, ARMED, WARNING, GRACE} while having a since_checkin.
        # Since Status is an enum, we patch it.
        from unittest.mock import MagicMock
        fake_status = MagicMock()
        fake_status.value = "unknown_state"
        fake_status.__eq__ = lambda s, other: False  # Never matches any Status

        tr = TimeRemaining(
            status=fake_status,
            since_checkin=timedelta(days=1),
            until_warning=None,
            until_grace=None,
            until_trigger=None,
            is_overdue=False,
        )
        result = tr.summary()
        assert "UNKNOWN_STATE" in result

    @pytest.mark.asyncio
    async def test_check_no_transition_needed(self, config, state_manager):
        """When already at the correct status, check_and_transition returns None."""
        watchdog = Watchdog(config, state_manager)
        now = datetime.now(timezone.utc)
        state_manager.state.last_checkin = now - timedelta(days=5)
        state_manager.state.status = Status.ARMED

        result = await watchdog.check_and_transition()
        # Already ARMED and time says ARMED, so no transition
        assert result is None

    @pytest.mark.asyncio
    async def test_check_expected_armed_but_status_warning(self, config, state_manager):
        """When status is WARNING but time says ARMED (recent check-in), return None.

        This covers the fallthrough at line 206: expected is ARMED but current
        is different. Since we don't have a transition TO armed in the if-chain,
        the method falls through to return None.
        """
        watchdog = Watchdog(config, state_manager)
        now = datetime.now(timezone.utc)
        # Recent check-in means calculate_status returns ARMED
        state_manager.state.last_checkin = now - timedelta(days=1)
        # But status is WARNING (stale from before the check-in)
        state_manager.state.status = Status.WARNING

        result = await watchdog.check_and_transition()
        # expected=ARMED, current=WARNING → falls through to return None
        assert result is None


class TestWatchdogRunLoopEdgeCases:
    """Edge-case tests for the watchdog run loop."""

    @pytest.mark.asyncio
    async def test_run_loop_exception_handling(self, config, state_manager):
        """Exception in loop body should be logged and re-raised."""
        watchdog = Watchdog(config, state_manager)
        watchdog._check_interval = 0.01

        # Make check_and_transition raise
        original = watchdog.check_and_transition

        call_count = 0

        async def failing_check():
            nonlocal call_count
            call_count += 1
            raise RuntimeError("test error in loop")

        watchdog.check_and_transition = failing_check
        watchdog._running = True

        with pytest.raises(RuntimeError, match="test error in loop"):
            await watchdog._run_loop()

    @pytest.mark.asyncio
    async def test_start_when_already_running(self, config, state_manager):
        """start() when already running should be a no-op (early return)."""
        import asyncio

        watchdog = Watchdog(config, state_manager)
        watchdog._running = True
        watchdog._task = asyncio.ensure_future(asyncio.sleep(100))

        # start() should return without creating a new task
        watchdog.start()
        assert watchdog._running is True

        # Clean up
        watchdog._task.cancel()
        try:
            await watchdog._task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_run_loop_cancelled_error(self, config, state_manager):
        """CancelledError in run loop should be re-raised (not swallowed)."""
        import asyncio

        watchdog = Watchdog(config, state_manager)
        watchdog._check_interval = 0.01
        watchdog._running = True

        async def cancel_check():
            raise asyncio.CancelledError()

        watchdog.check_and_transition = cancel_check

        with pytest.raises(asyncio.CancelledError):
            await watchdog._run_loop()

    @pytest.mark.asyncio
    async def test_run_loop_one_full_iteration(self, config, state_manager):
        """Run loop should execute check, sleep, then stop (covers line 228)."""
        watchdog = Watchdog(config, state_manager)
        watchdog._check_interval = 0.001  # 1ms
        watchdog._running = True

        call_count = 0

        async def check_then_stop():
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                watchdog._running = False

        watchdog.check_and_transition = check_then_stop
        await watchdog._run_loop()

        # Should have completed at least 2 iterations (hitting sleep on each)
        assert call_count >= 2


class TestFormatDuration:
    """Tests for _format_duration() helper (Issue 1)."""

    def test_zero_seconds(self):
        assert _format_duration(timedelta(seconds=0)) == "0s"

    def test_seconds_only(self):
        assert _format_duration(timedelta(seconds=45)) == "45s"

    def test_minutes_only(self):
        assert _format_duration(timedelta(minutes=5)) == "5m"

    def test_minutes_and_seconds(self):
        assert _format_duration(timedelta(minutes=2, seconds=30)) == "2m 30s"

    def test_hours_only(self):
        assert _format_duration(timedelta(hours=3)) == "3h"

    def test_hours_and_minutes(self):
        assert _format_duration(timedelta(hours=2, minutes=15)) == "2h 15m"

    def test_days_only(self):
        assert _format_duration(timedelta(days=7)) == "7d"

    def test_days_and_hours(self):
        assert _format_duration(timedelta(days=2, hours=3)) == "2d 3h"

    def test_negative_duration(self):
        assert _format_duration(timedelta(seconds=-10)) == "0s"

    def test_sub_hour_values_not_zero(self):
        """Regression: sub-hour values should not display as 0h."""
        assert _format_duration(timedelta(seconds=15)) == "15s"
        assert _format_duration(timedelta(seconds=90)) == "1m 30s"


class TestTriggeredSinceCheckin:
    """Tests for TRIGGERED state preserving since_checkin (Issue 4)."""

    def test_triggered_with_last_checkin(self, config, state_manager):
        watchdog = Watchdog(config, state_manager)
        now = datetime.now(timezone.utc)
        state_manager.state.last_checkin = now - timedelta(days=20)
        state_manager.state.status = Status.TRIGGERED
        state_manager.state.trigger_time = now - timedelta(days=6)

        tr = watchdog.get_time_remaining(now)

        assert tr.status == Status.TRIGGERED
        assert tr.since_checkin is not None
        assert tr.since_checkin.days == 20

    def test_triggered_without_last_checkin(self, config, state_manager):
        watchdog = Watchdog(config, state_manager)
        state_manager.state.status = Status.TRIGGERED
        state_manager.state.trigger_time = datetime.now(timezone.utc)

        tr = watchdog.get_time_remaining()

        assert tr.status == Status.TRIGGERED
        assert tr.since_checkin is None


class TestOverdueLogic:
    """Tests for OVERDUE logic fixes (Issue 2)."""

    def test_fresh_armed_not_overdue(self, config, state_manager):
        """Fresh ARMED node with no check-in should not be overdue."""
        watchdog = Watchdog(config, state_manager)
        tr = watchdog.get_time_remaining()
        assert tr.is_overdue is False

    def test_warning_with_no_checkin_is_overdue(self, config, state_manager):
        """Non-ARMED node with no check-in should be overdue."""
        watchdog = Watchdog(config, state_manager)
        state_manager.state.status = Status.WARNING
        tr = watchdog.get_time_remaining()
        assert tr.is_overdue is True


class TestAutoTuneInterval:
    """Tests for auto-tuned watchdog check interval (Issue 8)."""

    def test_default_config_uses_60s(self, state_manager):
        """Default config (days-scale timings) should cap at 60s."""
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            warning_start=timedelta(days=8),
            grace_start=timedelta(days=12),
            trigger_at=timedelta(days=14),
        )
        watchdog = Watchdog(config, state_manager)
        assert watchdog._check_interval == 60.0

    def test_fast_config_auto_tunes(self, state_manager):
        """Fast config should auto-tune to smaller interval."""
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            checkin_interval=timedelta(seconds=10),
            warning_start=timedelta(seconds=15),
            grace_start=timedelta(seconds=20),
            trigger_at=timedelta(seconds=40),
        )
        watchdog = Watchdog(config, state_manager)
        # min(15, 20-15=5, 40-20=20) = 5 => 5/4 = 1.25
        assert watchdog._check_interval == 1.25

    def test_explicit_check_interval(self, state_manager):
        """Explicit check_interval overrides auto-tune."""
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            check_interval=timedelta(seconds=5),
        )
        watchdog = Watchdog(config, state_manager)
        assert watchdog._check_interval == 5.0

    def test_very_small_interval_clamped_to_1s(self, state_manager):
        """Auto-tune should not go below 1 second."""
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            warning_start=timedelta(seconds=2),
            grace_start=timedelta(seconds=3),
            trigger_at=timedelta(seconds=4),
        )
        watchdog = Watchdog(config, state_manager)
        # min(2, 1, 1) = 1 => 1/4 = 0.25 => clamped to 1.0
        assert watchdog._check_interval == 1.0


