"""Tests for state persistence."""

import os
import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from posthumous.state import (
    State,
    StateManager,
    Status,
    ScheduleItemState,
    PeerState,
    FailedAttempt,
    StateCorruptError,
    parse_datetime,
)


class TestState:
    """Tests for State class."""

    def test_default_state(self):
        state = State()
        assert state.status == Status.ARMED
        assert state.last_checkin is None
        assert state.trigger_time is None

    def test_record_checkin(self):
        state = State()
        now = datetime.now(timezone.utc)

        state.record_checkin(now)

        assert state.last_checkin == now
        assert state.status == Status.ARMED

    def test_record_checkin_clears_failures(self):
        state = State()
        state.record_failed_attempt("test")
        state.record_failed_attempt("test")

        state.record_checkin()

        assert len(state.failed_attempts) == 0
        assert state.lockout_until is None

    def test_transition_armed_to_warning(self):
        state = State()
        assert state.transition_to(Status.WARNING)
        assert state.status == Status.WARNING

    def test_transition_warning_to_grace(self):
        state = State()
        state.status = Status.WARNING

        assert state.transition_to(Status.GRACE)
        assert state.status == Status.GRACE

    def test_transition_to_triggered(self):
        state = State()
        state.status = Status.GRACE

        assert state.transition_to(Status.TRIGGERED)
        assert state.status == Status.TRIGGERED
        assert state.trigger_time is not None

    def test_cannot_exit_triggered(self):
        state = State()
        state.status = Status.TRIGGERED
        state.trigger_time = datetime.now(timezone.utc)

        assert not state.transition_to(Status.ARMED)
        assert state.status == Status.TRIGGERED

    def test_checkin_resets_to_armed(self):
        state = State()
        state.status = Status.WARNING

        assert state.transition_to(Status.ARMED)
        assert state.status == Status.ARMED

    def test_no_transition_same_status(self):
        state = State()
        assert not state.transition_to(Status.ARMED)

    def test_schedule_item_tracking(self):
        state = State()

        assert state.should_run_schedule_item("annual", "2026")

        state.mark_schedule_item_run("annual", "2026")

        assert not state.should_run_schedule_item("annual", "2026")
        assert state.should_run_schedule_item("annual", "2027")

    def test_lockout(self):
        state = State()
        lockout_duration = timedelta(minutes=15)

        for _ in range(5):
            state.record_failed_attempt("test")

        state.check_and_set_lockout(5, lockout_duration)

        assert state.is_locked_out()

    def test_peer_tracking(self):
        state = State()

        state.update_peer("https://peer1:8420", success=True)
        assert state.peer_states["https://peer1:8420"].consecutive_failures == 0

        state.update_peer("https://peer1:8420", success=False, error="timeout")
        assert state.peer_states["https://peer1:8420"].consecutive_failures == 1
        assert state.peer_states["https://peer1:8420"].last_error == "timeout"


class TestStatePersistence:
    """Tests for state file operations."""

    def test_save_and_load(self, tmp_path):
        state_path = tmp_path / "state.yaml"

        # Create and save state
        state = State()
        state.record_checkin()
        state.status = Status.WARNING
        state.mark_schedule_item_run("test_item", "2026")
        state.save(state_path)

        # Load and verify
        loaded = State.load(state_path)
        assert loaded.status == Status.WARNING
        assert loaded.last_checkin is not None
        assert "test_item" in loaded.schedule_state
        assert loaded.schedule_state["test_item"].period == "2026"

    def test_atomic_write(self, tmp_path):
        state_path = tmp_path / "state.yaml"

        state = State()
        state.record_checkin()
        state.save(state_path)

        # Verify no temp files left behind
        temp_files = list(tmp_path.glob(".state_*"))
        assert len(temp_files) == 0

    def test_load_nonexistent_returns_fresh(self, tmp_path):
        state = State.load(tmp_path / "nonexistent.yaml")
        assert state.status == Status.ARMED
        assert state.last_checkin is None

    def test_load_corrupt_raises(self, tmp_path):
        state_path = tmp_path / "state.yaml"
        state_path.write_text("invalid: [yaml: content")

        with pytest.raises(StateCorruptError):
            State.load(state_path)


class TestStateManager:
    """Tests for StateManager."""

    def test_lazy_loading(self, tmp_path):
        manager = StateManager(tmp_path / "state.yaml")

        # State not loaded yet
        assert manager._state is None

        # Access triggers load
        state = manager.state
        assert state is not None
        assert manager._state is state

    def test_checkin_and_save(self, tmp_path):
        state_path = tmp_path / "state.yaml"
        manager = StateManager(state_path)

        manager.checkin()

        assert manager.state.last_checkin is not None
        assert state_path.exists()

    def test_transition_saves(self, tmp_path):
        state_path = tmp_path / "state.yaml"
        manager = StateManager(state_path)

        manager.transition(Status.WARNING)

        # Reload and verify
        loaded = State.load(state_path)
        assert loaded.status == Status.WARNING

    def test_reset(self, tmp_path):
        state_path = tmp_path / "state.yaml"
        manager = StateManager(state_path)

        manager.checkin()
        manager.transition(Status.WARNING)
        manager.reset()

        assert manager.state.status == Status.ARMED
        assert manager.state.last_checkin is None


class TestStateSerializationEdgeCases:
    """Tests for state serialization edge cases."""

    def test_datetime_with_timezone(self, tmp_path):
        state_path = tmp_path / "state.yaml"

        state = State()
        state.last_checkin = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        state.save(state_path)

        loaded = State.load(state_path)
        assert loaded.last_checkin.tzinfo is not None
        assert loaded.last_checkin.year == 2026

    def test_preserves_failed_attempts(self, tmp_path):
        state_path = tmp_path / "state.yaml"

        state = State()
        state.record_failed_attempt("192.168.1.1")
        state.record_failed_attempt("192.168.1.2")
        state.save(state_path)

        loaded = State.load(state_path)
        assert len(loaded.failed_attempts) == 2
        assert loaded.failed_attempts[0].source == "192.168.1.1"


class TestStateExtended:
    """Additional state edge case tests."""

    def test_checkin_from_grace(self):
        """Check-in from GRACE state should reset to ARMED."""
        state = State()
        state.status = Status.GRACE

        assert state.transition_to(Status.ARMED)
        assert state.status == Status.ARMED

    def test_invalid_status_defaults_to_armed(self):
        """Loading state with unknown status string defaults to ARMED."""
        state = State.from_dict({"status": "unknown_status"})
        assert state.status == Status.ARMED

    def test_from_dict_with_peer_states(self):
        now = datetime.now(timezone.utc)
        data = {
            "status": "armed",
            "peer_states": {
                "https://peer1:8420": {
                    "last_seen": now.isoformat(),
                    "last_error": None,
                    "consecutive_failures": 0,
                },
            },
        }
        state = State.from_dict(data)
        assert "https://peer1:8420" in state.peer_states
        assert state.peer_states["https://peer1:8420"].consecutive_failures == 0

    def test_from_dict_with_schedule_states(self):
        now = datetime.now(timezone.utc)
        data = {
            "status": "triggered",
            "schedule_state": {
                "annual": {
                    "last_run": now.isoformat(),
                    "period": "2026",
                },
            },
        }
        state = State.from_dict(data)
        assert "annual" in state.schedule_state
        assert state.schedule_state["annual"].period == "2026"

    def test_naive_datetime_handling(self):
        """parse_datetime should add UTC to naive datetimes."""
        naive_dt = datetime(2026, 1, 15, 12, 0, 0)
        result = parse_datetime(naive_dt)
        assert result.tzinfo == timezone.utc

    def test_parse_datetime_none(self):
        assert parse_datetime(None) is None

    def test_parse_datetime_string(self):
        result = parse_datetime("2026-01-15T12:00:00+00:00")
        assert result.year == 2026
        assert result.tzinfo is not None

    def test_parse_datetime_z_suffix(self):
        result = parse_datetime("2026-01-15T12:00:00Z")
        assert result.year == 2026


class TestStatePersistenceExtended:
    """Additional persistence edge cases."""

    def test_save_error_cleanup(self, tmp_path):
        """If save fails, temp file should be cleaned up."""
        state_path = tmp_path / "state.yaml"
        state = State()
        state.record_checkin()

        # Make the directory read-only to force a rename failure
        # This is hard to test portably, so we mock os.replace
        with patch("os.replace", side_effect=OSError("mock error")):
            with pytest.raises(OSError):
                state.save(state_path)

        # Temp files should be cleaned up
        temp_files = list(tmp_path.glob(".state_*"))
        assert len(temp_files) == 0

    def test_concurrent_rapid_saves(self, tmp_path):
        """Multiple rapid saves should not corrupt."""
        state_path = tmp_path / "state.yaml"
        state = State()

        for i in range(20):
            state.record_checkin(datetime(2026, 1, 1, 12, i, 0, tzinfo=timezone.utc))
            state.save(state_path)

        loaded = State.load(state_path)
        assert loaded.last_checkin.minute == 19  # Last save

        # No temp files left
        temp_files = list(tmp_path.glob(".state_*"))
        assert len(temp_files) == 0


class TestStateManagerExtended:
    """Additional StateManager tests."""

    def test_corrupt_file_returns_fresh_state(self, tmp_path):
        """StateManager should recover from corrupt state files."""
        state_path = tmp_path / "state.yaml"
        state_path.write_text("invalid: [yaml: content")

        manager = StateManager(state_path)
        state = manager.state

        # Should return fresh state, not raise
        assert state.status == Status.ARMED

    def test_save_when_no_state_loaded(self, tmp_path):
        """Save when _state is None should be a no-op."""
        state_path = tmp_path / "state.yaml"
        manager = StateManager(state_path)

        # Don't access .state, so _state is None
        manager.save()

        # File should not be created
        assert not state_path.exists()


class TestLockoutExtended:
    """Additional lockout mechanism tests."""

    def test_lockout_expiry(self):
        """After lockout expires, should no longer be locked out."""
        state = State()
        state.lockout_until = datetime.now(timezone.utc) - timedelta(minutes=1)
        assert not state.is_locked_out()

    def test_max_attempts_zero(self):
        """With max_attempts=0, any attempt should trigger lockout."""
        state = State()
        state.record_failed_attempt("test")
        result = state.check_and_set_lockout(0, timedelta(minutes=15))
        # 1 attempt >= 0, so lockout should trigger
        assert result is True
        assert state.is_locked_out()

    def test_sliding_window_lockout(self):
        """Failed attempts outside the lockout window shouldn't count."""
        state = State()
        lockout_duration = timedelta(minutes=15)

        # Add old attempts that are outside the window
        old_time = datetime.now(timezone.utc) - timedelta(minutes=20)
        for _ in range(5):
            state.failed_attempts.append(FailedAttempt(
                timestamp=old_time,
                source="test",
            ))

        # These are outside the 15-minute window
        result = state.check_and_set_lockout(5, lockout_duration)
        assert result is False
