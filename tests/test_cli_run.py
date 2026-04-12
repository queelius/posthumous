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

        with patch('posthumous.runner.Server', return_value=mock_server), \
             patch('posthumous.runner.Watchdog', return_value=mock_watchdog), \
             patch('posthumous.runner.Scheduler', return_value=mock_scheduler), \
             patch('posthumous.runner.PeerManager', return_value=mock_peer_mgr), \
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

        with patch('posthumous.runner.Server', return_value=mock_server), \
             patch('posthumous.runner.Watchdog', return_value=mock_watchdog), \
             patch('posthumous.runner.Scheduler', return_value=mock_scheduler), \
             patch('posthumous.runner.PeerManager', return_value=mock_peer_mgr), \
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

        with patch('posthumous.runner.Server', return_value=mock_server), \
             patch('posthumous.runner.Watchdog', side_effect=capture_watchdog), \
             patch('posthumous.runner.Scheduler', return_value=mock_scheduler), \
             patch('posthumous.runner.PeerManager', return_value=mock_peer_mgr), \
             patch('posthumous.runner.NotificationManager', return_value=mock_notif_mgr), \
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

        with patch('posthumous.runner.Server', return_value=mock_server), \
             patch('posthumous.runner.Watchdog', side_effect=capture_watchdog), \
             patch('posthumous.runner.Scheduler', return_value=mock_scheduler), \
             patch('posthumous.runner.PeerManager', return_value=mock_peer_mgr), \
             patch('posthumous.runner.NotificationManager', return_value=mock_notif_mgr), \
             patch('posthumous.runner.ScriptRunner', return_value=mock_script_runner), \
             patch('asyncio.Event.wait', new_callable=lambda: AsyncMock(side_effect=wait_and_call_trigger)):
            result = runner.invoke(
                main, ['-c', str(config_path), 'run'],
            )

        mock_notif_mgr.send.assert_called()
        mock_script_runner.run.assert_called()

    def test_run_daemon_flag_with_systemd(self, runner, config_path):
        """--daemon should suggest systemd when service is installed."""
        with patch('posthumous.systemd.is_service_installed', return_value=True):
            result = runner.invoke(
                main, ['-c', str(config_path), 'run', '--daemon'],
            )

        assert "Systemd service is installed" in result.output
        assert result.exit_code == 0

    def test_run_daemon_flag_double_fork(self, runner, config_path):
        """--daemon should call os.fork when systemd is not installed."""
        with patch('posthumous.systemd.is_service_installed', return_value=False), \
             patch('os.fork', return_value=1) as mock_fork:
            # First fork returns >0, so parent exits
            result = runner.invoke(
                main, ['-c', str(config_path), 'run', '--daemon'],
            )

        mock_fork.assert_called_once()
        # Parent process exits with sys.exit(0)

    def test_run_keyboard_interrupt(self, runner, config_path):
        """KeyboardInterrupt during asyncio.run should be caught gracefully."""
        mock_server = _make_mock_server()
        mock_watchdog = _make_mock_watchdog()
        mock_scheduler = _make_mock_scheduler()
        mock_peer_mgr = _make_mock_peer_manager()

        with patch('posthumous.runner.Server', return_value=mock_server), \
             patch('posthumous.runner.Watchdog', return_value=mock_watchdog), \
             patch('posthumous.runner.Scheduler', return_value=mock_scheduler), \
             patch('posthumous.runner.PeerManager', return_value=mock_peer_mgr), \
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

        with patch('posthumous.runner.Server', return_value=mock_server), \
             patch('posthumous.runner.Watchdog', side_effect=capture_watchdog), \
             patch('posthumous.runner.Scheduler', return_value=mock_scheduler), \
             patch('posthumous.runner.PeerManager', return_value=mock_peer_mgr), \
             patch('posthumous.runner.NotificationManager', return_value=mock_notif_mgr), \
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

        with patch('posthumous.runner.Server', return_value=mock_server), \
             patch('posthumous.runner.Watchdog', side_effect=capture_watchdog), \
             patch('posthumous.runner.Scheduler', return_value=mock_scheduler), \
             patch('posthumous.runner.PeerManager', return_value=mock_peer_mgr), \
             patch('posthumous.runner.NotificationManager', return_value=mock_notif_mgr), \
             patch('posthumous.runner.ScriptRunner', return_value=mock_script_runner), \
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

        with patch('posthumous.runner.Server', return_value=mock_server), \
             patch('posthumous.runner.Watchdog', return_value=mock_watchdog), \
             patch('posthumous.runner.Scheduler', side_effect=capture_scheduler), \
             patch('posthumous.runner.PeerManager', return_value=mock_peer_mgr), \
             patch('asyncio.Event.wait', new_callable=lambda: AsyncMock(side_effect=wait_and_call_complete)):
            result = runner.invoke(
                main, ['-c', str(config_path), 'run'],
            )

        mock_peer_mgr.broadcast_scheduled_complete.assert_called_once_with(
            "daily", "2026-01-16"
        )


class TestSdNotifyIntegration:
    """Tests for sd_notify integration in run_daemon."""

    def test_notify_ready_called_on_start(self, runner, config_path):
        """notify_ready should be called after all components start."""
        mock_server = _make_mock_server()
        mock_watchdog = _make_mock_watchdog()
        mock_scheduler = _make_mock_scheduler()
        mock_peer_mgr = _make_mock_peer_manager()

        with patch('posthumous.runner.Server', return_value=mock_server), \
             patch('posthumous.runner.Watchdog', return_value=mock_watchdog), \
             patch('posthumous.runner.Scheduler', return_value=mock_scheduler), \
             patch('posthumous.runner.PeerManager', return_value=mock_peer_mgr), \
             patch('posthumous.runner.notify_ready') as mock_ready, \
             patch('posthumous.runner.notify_watchdog') as mock_wd, \
             patch('posthumous.runner.notify_stopping') as mock_stopping, \
             patch('asyncio.Event.wait', new_callable=AsyncMock):
            result = runner.invoke(
                main, ['-c', str(config_path), 'run'],
            )

        mock_ready.assert_called_once()

    def test_notify_stopping_called_on_shutdown(self, runner, config_path):
        """notify_stopping should be called during shutdown."""
        mock_server = _make_mock_server()
        mock_watchdog = _make_mock_watchdog()
        mock_scheduler = _make_mock_scheduler()
        mock_peer_mgr = _make_mock_peer_manager()

        with patch('posthumous.runner.Server', return_value=mock_server), \
             patch('posthumous.runner.Watchdog', return_value=mock_watchdog), \
             patch('posthumous.runner.Scheduler', return_value=mock_scheduler), \
             patch('posthumous.runner.PeerManager', return_value=mock_peer_mgr), \
             patch('posthumous.runner.notify_ready') as mock_ready, \
             patch('posthumous.runner.notify_watchdog') as mock_wd, \
             patch('posthumous.runner.notify_stopping') as mock_stopping, \
             patch('asyncio.Event.wait', new_callable=AsyncMock):
            result = runner.invoke(
                main, ['-c', str(config_path), 'run'],
            )

        mock_stopping.assert_called_once()

    def test_watchdog_ping_runs_before_shutdown(self, runner, config_path):
        """notify_watchdog should be called at least once during the loop."""
        mock_server = _make_mock_server()
        mock_watchdog = _make_mock_watchdog()
        mock_scheduler = _make_mock_scheduler()
        mock_peer_mgr = _make_mock_peer_manager()

        async def slow_wait(*args, **kwargs):
            """Let the watchdog_ping task run at least one iteration."""
            # The watchdog_ping loop sleeps 15s between pings, but we mock
            # asyncio.sleep below so it resolves instantly. We just need to
            # yield control so the task gets a chance to run.
            await asyncio.sleep(0.05)

        original_sleep = asyncio.sleep

        async def fast_sleep(seconds):
            """Replace 15s sleeps with instant yields, keep short sleeps real."""
            if seconds >= 1:
                await original_sleep(0)
            else:
                await original_sleep(seconds)

        with patch('posthumous.runner.Server', return_value=mock_server), \
             patch('posthumous.runner.Watchdog', return_value=mock_watchdog), \
             patch('posthumous.runner.Scheduler', return_value=mock_scheduler), \
             patch('posthumous.runner.PeerManager', return_value=mock_peer_mgr), \
             patch('posthumous.runner.notify_ready'), \
             patch('posthumous.runner.notify_watchdog') as mock_wd, \
             patch('posthumous.runner.notify_stopping'), \
             patch('asyncio.Event.wait', new_callable=lambda: AsyncMock(side_effect=slow_wait)), \
             patch('asyncio.sleep', side_effect=fast_sleep):
            result = runner.invoke(
                main, ['-c', str(config_path), 'run'],
            )

        # The watchdog_ping task should have called notify_watchdog at least once
        assert mock_wd.call_count >= 1


class TestPeerDownWiring:
    """Test that on_peer_down callback is wired to PeerManager in the run command."""

    def test_peer_manager_receives_callback(self, runner, config_path):
        """PeerManager should be constructed with an on_peer_down callback."""
        mock_server = _make_mock_server()
        mock_watchdog = _make_mock_watchdog()
        mock_scheduler = _make_mock_scheduler()
        mock_peer_mgr = _make_mock_peer_manager()

        captured_kwargs = {}

        def capture_peer_manager(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return mock_peer_mgr

        with patch('posthumous.runner.Server', return_value=mock_server), \
             patch('posthumous.runner.Watchdog', return_value=mock_watchdog), \
             patch('posthumous.runner.Scheduler', return_value=mock_scheduler), \
             patch('posthumous.runner.PeerManager', side_effect=capture_peer_manager), \
             patch('asyncio.Event.wait', new_callable=AsyncMock):
            result = runner.invoke(
                main, ['-c', str(config_path), 'run'],
            )

        assert 'on_peer_down' in captured_kwargs
        assert callable(captured_kwargs['on_peer_down'])


class TestDaemonStop:
    """Tests for the --stop flag."""

    def test_stop_reads_pid_and_sends_sigterm(self, runner, config_path):
        """--stop should read PID file and send SIGTERM."""
        pid_path = config_path.parent / "posthumous.pid"
        pid_path.write_text("12345")

        with patch('os.kill') as mock_kill:
            # First call is SIGTERM, second call (os.kill(pid, 0)) raises ProcessLookupError
            mock_kill.side_effect = [None, ProcessLookupError]
            result = runner.invoke(
                main, ['-c', str(config_path), 'run', '--stop'],
            )

        assert "Sent SIGTERM to PID 12345" in result.output
        assert "Daemon stopped" in result.output
        mock_kill.assert_any_call(12345, 15)  # signal.SIGTERM = 15

    def test_stop_no_pid_file(self, runner, config_path):
        """--stop with no PID file should show error."""
        result = runner.invoke(
            main, ['-c', str(config_path), 'run', '--stop'],
        )

        assert "No PID file found" in result.output
        assert result.exit_code == 1

    def test_stop_process_wont_die(self, runner, config_path):
        """--stop should warn if process doesn't terminate."""
        pid_path = config_path.parent / "posthumous.pid"
        pid_path.write_text("99999")

        with patch('os.kill', return_value=None), \
             patch('time.sleep'):
            result = runner.invoke(
                main, ['-c', str(config_path), 'run', '--stop'],
            )

        assert "still running" in result.output
        assert result.exit_code == 1
