"""Integration tests for the CLI 'run' command (cli.py:247-411)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from posthumous.cli import main

SECRET = "JBSWY3DPEHPK3PXP"


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def valid_config_dir(tmp_path):
    """Create a config directory with a valid config."""
    config_dir = tmp_path / ".posthumous"
    config_dir.mkdir()
    (config_dir / "scripts").mkdir()
    (config_dir / "logs").mkdir()
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        "node_name: test-node\n"
        "secret_key: JBSWY3DPEHPK3PXP\n"
        "listen: '127.0.0.1:18420'\n"
        "checkin_interval: 7 days\n"
        "warning_start: 8 days\n"
        "grace_start: 12 days\n"
        "trigger_at: 14 days\n"
        "notifications:\n"
        "  default:\n"
        "    - 'ntfy://test-topic'\n"
        "actions:\n"
        "  on_warning:\n"
        "    - notify: default\n"
        "      message: 'Warning'\n"
        "  on_grace:\n"
        "    - notify: default\n"
        "      message: 'Grace'\n"
        "  on_trigger:\n"
        "    - notify: default\n"
        "      message: 'Triggered!'\n"
        "    - script: scripts/on_trigger.py\n"
    )
    return config_dir


@pytest.fixture
def config_path(valid_config_dir):
    return valid_config_dir / "config.yaml"


def _make_mock_server():
    server = MagicMock()
    server.start = AsyncMock()
    server.stop = AsyncMock()
    return server


def _make_mock_watchdog():
    watchdog = MagicMock()
    watchdog.start = MagicMock()
    watchdog.stop = AsyncMock()
    return watchdog


def _make_mock_scheduler():
    scheduler = MagicMock()
    scheduler.start = MagicMock()
    scheduler.stop = AsyncMock()
    return scheduler


def _make_mock_peer_manager():
    pm = MagicMock()
    pm.start_health_monitoring = MagicMock()
    pm.stop_health_monitoring = AsyncMock()
    pm.close = AsyncMock()
    pm.broadcast_trigger = AsyncMock()
    return pm


class TestRunNoConfig:
    """Test 'run' with missing or invalid config."""

    def test_run_no_config_exits(self, runner, tmp_path):
        """Missing config file -> exit code 1."""
        config_path = tmp_path / "missing.yaml"
        result = runner.invoke(
            main, ['-c', str(config_path), 'run'],
        )
        assert result.exit_code == 1
        assert "Config not found" in result.output

    def test_run_invalid_config_exits(self, runner, tmp_path):
        """Config with invalid secret_key -> exit code 1 with validation errors."""
        config_dir = tmp_path / ".posthumous"
        config_dir.mkdir()
        (config_dir / "logs").mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            "node_name: test-node\n"
            "secret_key: NOT-VALID-BASE32!!!\n"
            "listen: '127.0.0.1:8420'\n"
            "checkin_interval: 7 days\n"
            "warning_start: 8 days\n"
            "grace_start: 12 days\n"
            "trigger_at: 14 days\n"
        )
        result = runner.invoke(
            main, ['-c', str(config_path), 'run'],
        )
        assert result.exit_code == 1
        assert "Configuration errors" in result.output or "secret_key" in result.output


class TestRunComponents:
    """Test 'run' component orchestration using mocked subsystems.

    Strategy: Patch asyncio.Event.wait to return immediately so the run_daemon
    coroutine completes its start/stop lifecycle without actual signal handling.
    """

    def test_run_starts_all_components(self, runner, config_path):
        """All components should have start() called."""
        mock_server = _make_mock_server()
        mock_watchdog = _make_mock_watchdog()
        mock_scheduler = _make_mock_scheduler()
        mock_peer_mgr = _make_mock_peer_manager()

        with patch('posthumous.server.Server', return_value=mock_server), \
             patch('posthumous.watchdog.Watchdog', return_value=mock_watchdog), \
             patch('posthumous.scheduler.Scheduler', return_value=mock_scheduler), \
             patch('posthumous.peers.PeerManager', return_value=mock_peer_mgr), \
             patch('asyncio.Event.wait', new_callable=AsyncMock):
            result = runner.invoke(
                main, ['-c', str(config_path), 'run'],
            )

        mock_server.start.assert_called_once()
        mock_watchdog.start.assert_called_once()
        mock_scheduler.start.assert_called_once()
        mock_peer_mgr.start_health_monitoring.assert_called_once()

    def test_run_graceful_shutdown(self, runner, config_path):
        """Components should be stopped in correct order on shutdown."""
        mock_server = _make_mock_server()
        mock_watchdog = _make_mock_watchdog()
        mock_scheduler = _make_mock_scheduler()
        mock_peer_mgr = _make_mock_peer_manager()

        shutdown_order = []

        async def track_peer_stop():
            shutdown_order.append("peer_stop_health")

        async def track_scheduler_stop():
            shutdown_order.append("scheduler_stop")

        async def track_watchdog_stop():
            shutdown_order.append("watchdog_stop")

        async def track_server_stop():
            shutdown_order.append("server_stop")

        async def track_peer_close():
            shutdown_order.append("peer_close")

        mock_peer_mgr.stop_health_monitoring = AsyncMock(side_effect=track_peer_stop)
        mock_scheduler.stop = AsyncMock(side_effect=track_scheduler_stop)
        mock_watchdog.stop = AsyncMock(side_effect=track_watchdog_stop)
        mock_server.stop = AsyncMock(side_effect=track_server_stop)
        mock_peer_mgr.close = AsyncMock(side_effect=track_peer_close)

        with patch('posthumous.server.Server', return_value=mock_server), \
             patch('posthumous.watchdog.Watchdog', return_value=mock_watchdog), \
             patch('posthumous.scheduler.Scheduler', return_value=mock_scheduler), \
             patch('posthumous.peers.PeerManager', return_value=mock_peer_mgr), \
             patch('asyncio.Event.wait', new_callable=AsyncMock):
            result = runner.invoke(
                main, ['-c', str(config_path), 'run'],
            )

        # Verify shutdown order: peer_stop -> scheduler -> watchdog -> server -> peer_close
        assert shutdown_order == [
            "peer_stop_health",
            "scheduler_stop",
            "watchdog_stop",
            "server_stop",
            "peer_close",
        ]

    def test_run_execute_actions_notification(self, runner, config_path):
        """on_warning callback should call notification_manager.send."""
        mock_server = _make_mock_server()
        mock_watchdog = _make_mock_watchdog()
        mock_scheduler = _make_mock_scheduler()
        mock_peer_mgr = _make_mock_peer_manager()
        mock_notif_mgr = MagicMock()
        mock_notif_mgr.send = AsyncMock()

        captured_on_warning = None

        def capture_watchdog(*args, **kwargs):
            nonlocal captured_on_warning
            captured_on_warning = kwargs.get('on_warning')
            return mock_watchdog

        async def wait_and_call_warning(*args, **kwargs):
            if captured_on_warning:
                await captured_on_warning()

        with patch('posthumous.server.Server', return_value=mock_server), \
             patch('posthumous.watchdog.Watchdog', side_effect=capture_watchdog), \
             patch('posthumous.scheduler.Scheduler', return_value=mock_scheduler), \
             patch('posthumous.peers.PeerManager', return_value=mock_peer_mgr), \
             patch('posthumous.notifications.NotificationManager', return_value=mock_notif_mgr), \
             patch('asyncio.Event.wait', new_callable=lambda: AsyncMock(side_effect=wait_and_call_warning)):
            result = runner.invoke(
                main, ['-c', str(config_path), 'run'],
            )

        mock_notif_mgr.send.assert_called()

    def test_run_execute_actions_script(self, runner, config_path):
        """on_trigger callback should call script_runner.run."""
        mock_server = _make_mock_server()
        mock_watchdog = _make_mock_watchdog()
        mock_scheduler = _make_mock_scheduler()
        mock_peer_mgr = _make_mock_peer_manager()
        mock_notif_mgr = MagicMock()
        mock_notif_mgr.send = AsyncMock()
        mock_script_runner = MagicMock()
        mock_script_runner.run = AsyncMock()

        captured_on_trigger = None

        def capture_watchdog(*args, **kwargs):
            nonlocal captured_on_trigger
            captured_on_trigger = kwargs.get('on_trigger')
            return mock_watchdog

        async def wait_and_call_trigger(*args, **kwargs):
            if captured_on_trigger:
                await captured_on_trigger()

        with patch('posthumous.server.Server', return_value=mock_server), \
             patch('posthumous.watchdog.Watchdog', side_effect=capture_watchdog), \
             patch('posthumous.scheduler.Scheduler', return_value=mock_scheduler), \
             patch('posthumous.peers.PeerManager', return_value=mock_peer_mgr), \
             patch('posthumous.notifications.NotificationManager', return_value=mock_notif_mgr), \
             patch('posthumous.scripts.ScriptRunner', return_value=mock_script_runner), \
             patch('asyncio.Event.wait', new_callable=lambda: AsyncMock(side_effect=wait_and_call_trigger)):
            result = runner.invoke(
                main, ['-c', str(config_path), 'run'],
            )

        mock_notif_mgr.send.assert_called()
        mock_script_runner.run.assert_called()

    def test_run_daemon_flag(self, runner, config_path):
        """--daemon flag should print 'not yet implemented' message."""
        mock_server = _make_mock_server()
        mock_watchdog = _make_mock_watchdog()
        mock_scheduler = _make_mock_scheduler()
        mock_peer_mgr = _make_mock_peer_manager()

        with patch('posthumous.server.Server', return_value=mock_server), \
             patch('posthumous.watchdog.Watchdog', return_value=mock_watchdog), \
             patch('posthumous.scheduler.Scheduler', return_value=mock_scheduler), \
             patch('posthumous.peers.PeerManager', return_value=mock_peer_mgr), \
             patch('asyncio.Event.wait', new_callable=AsyncMock):
            result = runner.invoke(
                main, ['-c', str(config_path), 'run', '--daemon'],
            )

        assert "Daemon mode not yet implemented" in result.output

    def test_run_keyboard_interrupt(self, runner, config_path):
        """KeyboardInterrupt during asyncio.run should be caught gracefully."""
        mock_server = _make_mock_server()
        mock_watchdog = _make_mock_watchdog()
        mock_scheduler = _make_mock_scheduler()
        mock_peer_mgr = _make_mock_peer_manager()

        with patch('posthumous.server.Server', return_value=mock_server), \
             patch('posthumous.watchdog.Watchdog', return_value=mock_watchdog), \
             patch('posthumous.scheduler.Scheduler', return_value=mock_scheduler), \
             patch('posthumous.peers.PeerManager', return_value=mock_peer_mgr), \
             patch('asyncio.Event.wait', new_callable=AsyncMock, side_effect=KeyboardInterrupt):
            result = runner.invoke(
                main, ['-c', str(config_path), 'run'],
            )

        # Should not crash — the except KeyboardInterrupt: pass handles it
        assert result.exit_code == 0 or result.exit_code is None

    def test_run_execute_actions_grace(self, runner, config_path):
        """on_grace callback should call execute_actions with 'grace' event."""
        mock_server = _make_mock_server()
        mock_watchdog = _make_mock_watchdog()
        mock_scheduler = _make_mock_scheduler()
        mock_peer_mgr = _make_mock_peer_manager()
        mock_notif_mgr = MagicMock()
        mock_notif_mgr.send = AsyncMock()

        captured_on_grace = None

        def capture_watchdog(*args, **kwargs):
            nonlocal captured_on_grace
            captured_on_grace = kwargs.get('on_grace')
            return mock_watchdog

        async def wait_and_call_grace(*args, **kwargs):
            if captured_on_grace:
                await captured_on_grace()

        with patch('posthumous.server.Server', return_value=mock_server), \
             patch('posthumous.watchdog.Watchdog', side_effect=capture_watchdog), \
             patch('posthumous.scheduler.Scheduler', return_value=mock_scheduler), \
             patch('posthumous.peers.PeerManager', return_value=mock_peer_mgr), \
             patch('posthumous.notifications.NotificationManager', return_value=mock_notif_mgr), \
             patch('asyncio.Event.wait', new_callable=lambda: AsyncMock(side_effect=wait_and_call_grace)):
            result = runner.invoke(
                main, ['-c', str(config_path), 'run'],
            )

        # on_grace calls execute_actions which sends notifications
        mock_notif_mgr.send.assert_called()

    def test_run_on_trigger_broadcasts(self, runner, config_path):
        """on_trigger should broadcast to peers when trigger_time is set."""
        mock_server = _make_mock_server()
        mock_watchdog = _make_mock_watchdog()
        mock_scheduler = _make_mock_scheduler()
        mock_peer_mgr = _make_mock_peer_manager()
        mock_notif_mgr = MagicMock()
        mock_notif_mgr.send = AsyncMock()
        mock_script_runner = MagicMock()
        mock_script_runner.run = AsyncMock()

        captured_callbacks = {}

        def capture_watchdog(*args, **kwargs):
            captured_callbacks.update(kwargs)
            return mock_watchdog

        trigger_time = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

        async def wait_and_call_trigger(*args, **kwargs):
            on_trigger = captured_callbacks.get('on_trigger')
            if on_trigger:
                await on_trigger()

        # Pre-write a state file with trigger_time set
        import yaml
        state_path = config_path.parent / "state.yaml"
        state_path.write_text(yaml.dump({
            "status": "triggered",
            "trigger_time": trigger_time.isoformat(),
            "last_checkin": datetime(2026, 1, 10, 8, 0, 0, tzinfo=timezone.utc).isoformat(),
        }))

        with patch('posthumous.server.Server', return_value=mock_server), \
             patch('posthumous.watchdog.Watchdog', side_effect=capture_watchdog), \
             patch('posthumous.scheduler.Scheduler', return_value=mock_scheduler), \
             patch('posthumous.peers.PeerManager', return_value=mock_peer_mgr), \
             patch('posthumous.notifications.NotificationManager', return_value=mock_notif_mgr), \
             patch('posthumous.scripts.ScriptRunner', return_value=mock_script_runner), \
             patch('asyncio.Event.wait', new_callable=lambda: AsyncMock(side_effect=wait_and_call_trigger)):
            result = runner.invoke(
                main, ['-c', str(config_path), 'run'],
            )

        # on_trigger should have called peer_manager.broadcast_trigger
        mock_peer_mgr.broadcast_trigger.assert_called_once()

    def test_run_on_scheduled_complete(self, runner, config_path):
        """on_scheduled_complete should broadcast to peers."""
        mock_server = _make_mock_server()
        mock_watchdog = _make_mock_watchdog()
        mock_scheduler = _make_mock_scheduler()
        mock_peer_mgr = _make_mock_peer_manager()
        mock_peer_mgr.broadcast_scheduled_complete = AsyncMock()

        captured_on_scheduled_complete = None

        def capture_scheduler(*args, **kwargs):
            nonlocal captured_on_scheduled_complete
            captured_on_scheduled_complete = kwargs.get('on_scheduled_complete')
            return mock_scheduler

        async def wait_and_call_complete(*args, **kwargs):
            if captured_on_scheduled_complete:
                from posthumous.scheduler import ScheduledExecution
                execution = ScheduledExecution(
                    item_name="daily",
                    period_key="2026-01-16",
                    executed_at=datetime(2026, 1, 16, 12, 0, 0, tzinfo=timezone.utc),
                    success=True,
                )
                await captured_on_scheduled_complete(execution)

        with patch('posthumous.server.Server', return_value=mock_server), \
             patch('posthumous.watchdog.Watchdog', return_value=mock_watchdog), \
             patch('posthumous.scheduler.Scheduler', side_effect=capture_scheduler), \
             patch('posthumous.peers.PeerManager', return_value=mock_peer_mgr), \
             patch('asyncio.Event.wait', new_callable=lambda: AsyncMock(side_effect=wait_and_call_complete)):
            result = runner.invoke(
                main, ['-c', str(config_path), 'run'],
            )

        mock_peer_mgr.broadcast_scheduled_complete.assert_called_once_with(
            "daily", "2026-01-16"
        )
