"""Integration tests for the DaemonRunner class.

These tests instantiate a real ``DaemonRunner`` against real components
on ``tmp_path`` and exercise callbacks end-to-end. External peer HTTP
calls are mocked via ``aioresponses``; Apprise notifications are mocked
at the ``apprise.Apprise.notify`` boundary so the rest of the chain
(NotificationManager -> template formatting -> retry loop) runs for real.

These complement the heavy-mocked tests in ``test_cli_run.py`` that
verify wiring order; the tests here verify the callbacks actually work.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from posthumous.config import (
    Config,
    NotificationAction,
    ScheduledItem,
    ScriptAction,
)
from posthumous.peers import PeerStatus
from posthumous.runner import DaemonRunner
from posthumous.scheduler import ScheduledExecution
from posthumous.state import Status


SECRET = "JBSWY3DPEHPK3PXP"


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """Create a config directory structure."""
    d = tmp_path / ".posthumous"
    d.mkdir()
    (d / "scripts").mkdir()
    (d / "logs").mkdir()
    return d


def _make_config(
    config_dir: Path,
    *,
    peers: list[str] | None = None,
    on_peer_down: list | None = None,
    on_warning: list | None = None,
    on_grace: list | None = None,
    on_trigger: list | None = None,
    post_trigger: list | None = None,
) -> Config:
    """Build a Config with realistic timing and the supplied actions."""
    return Config(
        node_name="integration-node",
        secret_key=SECRET,
        listen="127.0.0.1:18421",
        checkin_interval=timedelta(days=7),
        warning_start=timedelta(days=8),
        grace_start=timedelta(days=12),
        trigger_at=timedelta(days=14),
        peers=peers or [],
        notifications={"default": ["ntfy://test-topic"]},
        on_warning=on_warning or [],
        on_grace=on_grace or [],
        on_trigger=on_trigger or [],
        on_peer_down=on_peer_down or [],
        post_trigger=post_trigger or [],
        peer_check_interval=timedelta(seconds=30),
        peer_down_threshold=timedelta(seconds=1),
        config_dir=config_dir,
    )


class TestDaemonRunnerConstruction:
    """Verify DaemonRunner wires up real components correctly."""

    def test_init_defers_component_construction(self, config_dir):
        """Fresh runner has no components until build_components() is called."""
        config = _make_config(config_dir)
        runner = DaemonRunner(config)

        assert runner.state_manager is None
        assert runner.notification_manager is None
        assert runner.script_runner is None
        assert runner.authenticator is None
        assert runner.peer_manager is None
        assert runner.watchdog is None
        assert runner.scheduler is None
        assert runner.server is None

    def test_build_components_populates_all_attrs(self, config_dir):
        """After build_components(), every component attribute is set."""
        config = _make_config(config_dir)
        runner = DaemonRunner(config)
        runner.build_components()

        assert runner.state_manager is not None
        assert runner.notification_manager is not None
        assert runner.script_runner is not None
        assert runner.authenticator is not None
        assert runner.peer_manager is not None
        assert runner.watchdog is not None
        assert runner.scheduler is not None
        assert runner.server is not None

        # Peer manager has the on_peer_down callback bound to this runner
        assert runner.peer_manager._on_peer_down == runner.on_peer_down

        # Watchdog has the transition callbacks bound to this runner
        assert runner.watchdog._on_warning == runner.on_warning
        assert runner.watchdog._on_grace == runner.on_grace
        assert runner.watchdog._on_trigger == runner.on_trigger

        # Scheduler has the scheduled-complete callback bound to this runner
        assert runner.scheduler._on_scheduled_complete == runner.on_scheduled_complete

    def test_build_components_shares_state_manager(self, config_dir):
        """All components read/write the same StateManager instance."""
        config = _make_config(config_dir)
        runner = DaemonRunner(config)
        runner.build_components()

        sm = runner.state_manager
        assert runner.peer_manager.state_manager is sm
        assert runner.watchdog.state_manager is sm
        assert runner.scheduler.state_manager is sm
        assert runner.server.state_manager is sm


class TestPeerDownCallbackEndToEnd:
    """Real integration test: on_peer_down callback fires through
    the real PeerManager._health_check_once -> runner.on_peer_down ->
    runner.execute_actions -> real NotificationManager -> Apprise.

    Only external I/O is mocked (Apprise.notify, peer HTTP).
    """

    @pytest.mark.asyncio
    async def test_peer_down_callback_wires_through(self, config_dir):
        peer_url = "http://peer1.local:18422"
        config = _make_config(
            config_dir,
            peers=[peer_url],
            on_peer_down=[
                NotificationAction(
                    channel="default",
                    message=(
                        "Peer {peer_url} on {node_name} has been down "
                        "for {peer_downtime}"
                    ),
                ),
            ],
        )

        runner = DaemonRunner(config)
        runner.build_components()

        # Pre-populate peer state with last_seen well past the down threshold
        now = datetime.now(timezone.utc)
        runner.state_manager.state.update_peer(peer_url, success=True)
        runner.state_manager.state.peer_states[peer_url].last_seen = (
            now - timedelta(hours=3)
        )

        # Stub out peer HTTP calls: have the peer appear unreachable
        # (we bypass aiohttp entirely here by patching get_all_peer_status,
        # which is cleaner than mocking the network for a unit test).
        down_statuses = [
            PeerStatus(
                url=peer_url,
                reachable=False,
                status=None,
                last_checkin=None,
                last_seen=None,
                error="Connection refused",
            ),
        ]
        runner.peer_manager.get_all_peer_status = AsyncMock(
            return_value=down_statuses,
        )

        # Capture Apprise.notify calls at the library boundary. The rest of
        # NotificationManager (template formatting, retry logic) runs for real.
        notify_calls: list[dict] = []

        def capture_notify(title, body):
            notify_calls.append({"title": title, "body": body})
            return True

        with patch("apprise.Apprise.notify", side_effect=capture_notify):
            # Drive one real health-check iteration. This will:
            #   1. Notice peer is unreachable
            #   2. See last_seen > threshold and no prior alert
            #   3. Invoke runner.on_peer_down(...)
            #   4. Which calls runner.execute_actions(...)
            #   5. Which calls notification_manager.send(...)
            #   6. Which calls apprise.Apprise.notify(...) via executor
            await runner.peer_manager._health_check_once()

        # The chain fired end-to-end: Apprise.notify was invoked with the
        # formatted message, with template variables substituted correctly.
        assert len(notify_calls) == 1
        call = notify_calls[0]
        assert call["title"] == "Posthumous - Peer Down"
        assert peer_url in call["body"]
        assert config.node_name in call["body"]
        # The downtime format is "Nh Mm" or "Mm". For 3 hours it should
        # include "3h".
        assert "3h" in call["body"]

        # The peer is now in the alerted set (guard against repeat alerts)
        assert peer_url in runner.peer_manager._alerted_peers

        await runner.peer_manager.close()


class TestExecuteActionsRealCallbacks:
    """Exercise on_warning/on_grace/on_trigger through a real runner."""

    @pytest.mark.asyncio
    async def test_on_warning_fires_notification(self, config_dir):
        config = _make_config(
            config_dir,
            on_warning=[
                NotificationAction(
                    channel="default",
                    message="Warning for {node_name}",
                ),
            ],
        )
        runner = DaemonRunner(config)
        runner.build_components()

        notify_calls: list[dict] = []

        def capture_notify(title, body):
            notify_calls.append({"title": title, "body": body})
            return True

        with patch("apprise.Apprise.notify", side_effect=capture_notify):
            await runner.on_warning()

        assert len(notify_calls) == 1
        assert notify_calls[0]["title"] == "Posthumous Warning"
        assert notify_calls[0]["body"] == f"Warning for {config.node_name}"

    @pytest.mark.asyncio
    async def test_on_grace_fires_notification(self, config_dir):
        config = _make_config(
            config_dir,
            on_grace=[
                NotificationAction(
                    channel="default",
                    message="Grace period for {node_name}",
                ),
            ],
        )
        runner = DaemonRunner(config)
        runner.build_components()

        notify_calls: list[dict] = []

        def capture_notify(title, body):
            notify_calls.append({"title": title, "body": body})
            return True

        with patch("apprise.Apprise.notify", side_effect=capture_notify):
            await runner.on_grace()

        assert len(notify_calls) == 1
        assert notify_calls[0]["title"] == "Posthumous - URGENT"
        assert config.node_name in notify_calls[0]["body"]

    @pytest.mark.asyncio
    async def test_on_trigger_broadcasts_before_notifying(self, config_dir):
        """on_trigger must broadcast to peers before local actions run.

        This preserves the original cli.py ordering: peers learn about
        the trigger event before we potentially fire scripts that take
        arbitrary time or fail.
        """
        peer_url = "http://peer1.local:18423"
        config = _make_config(
            config_dir,
            peers=[peer_url],
            on_trigger=[
                NotificationAction(
                    channel="default",
                    message="Triggered",
                ),
            ],
        )
        runner = DaemonRunner(config)
        runner.build_components()

        # Set up a trigger_time on the state
        trigger_time = datetime.now(timezone.utc)
        runner.state_manager.state.status = Status.TRIGGERED
        runner.state_manager.state.trigger_time = trigger_time

        call_order: list[str] = []

        original_broadcast = runner.peer_manager.broadcast_trigger

        async def track_broadcast(ts):
            call_order.append("broadcast")
            # Don't actually hit the network; just return a success dict
            return {peer_url: True}

        def capture_notify(title, body):
            call_order.append("notify")
            return True

        with patch.object(runner.peer_manager, "broadcast_trigger",
                          side_effect=track_broadcast), \
             patch("apprise.Apprise.notify", side_effect=capture_notify):
            await runner.on_trigger()

        assert call_order == ["broadcast", "notify"]

    @pytest.mark.asyncio
    async def test_on_scheduled_complete_broadcasts(self, config_dir):
        """on_scheduled_complete should broadcast completion to peers."""
        peer_url = "http://peer1.local:18424"
        config = _make_config(config_dir, peers=[peer_url])
        runner = DaemonRunner(config)
        runner.build_components()

        with patch.object(
            runner.peer_manager, "broadcast_scheduled_complete",
            new_callable=AsyncMock,
        ) as mock_broadcast:
            execution = ScheduledExecution(
                item_name="weekly-reminder",
                period_key="2026-W15",
                executed_at=datetime.now(timezone.utc),
                success=True,
            )
            await runner.on_scheduled_complete(execution)

        mock_broadcast.assert_called_once_with("weekly-reminder", "2026-W15")

    @pytest.mark.asyncio
    async def test_execute_actions_runs_script(self, config_dir, tmp_path):
        """execute_actions should run ScriptAction via ScriptRunner."""
        # Create a trivial script the runner can exec. It just has to exist;
        # we'll patch ScriptRunner.run so we don't actually subprocess it.
        script_rel = "scripts/noop.sh"
        (config_dir / "scripts" / "noop.sh").write_text(
            "#!/bin/sh\nexit 0\n"
        )

        config = _make_config(
            config_dir,
            on_warning=[
                NotificationAction(channel="default", message="warn"),
                ScriptAction(script=script_rel),
            ],
        )
        runner = DaemonRunner(config)
        runner.build_components()

        with patch.object(
            runner.script_runner, "run",
            new_callable=AsyncMock,
        ) as mock_run, \
             patch("apprise.Apprise.notify", return_value=True):
            await runner.on_warning()

        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert args[0] == script_rel
        # The second arg is a ScriptContext
        script_context = args[1]
        assert script_context.event == "warning"
        assert script_context.node_name == config.node_name


class TestStateRecovery:
    """Tests for attempt_state_recovery."""

    @pytest.mark.asyncio
    async def test_no_state_file_is_noop(self, config_dir):
        """No state file -> nothing to recover, no-op."""
        config = _make_config(config_dir)
        runner = DaemonRunner(config)

        # Should not raise, even without components built
        await runner.attempt_state_recovery()

    @pytest.mark.asyncio
    async def test_valid_state_file_is_noop(self, config_dir):
        """Valid state file -> nothing to recover, no-op."""
        from posthumous.state import State

        config = _make_config(config_dir)
        state = State()
        state.save(config_dir / "state.yaml")

        runner = DaemonRunner(config)
        await runner.attempt_state_recovery()

    @pytest.mark.asyncio
    async def test_corrupt_state_attempts_recovery(self, config_dir):
        """Corrupt state file -> sync_state_from_peers is attempted."""
        peer_url = "http://peer1.local:18425"
        config = _make_config(config_dir, peers=[peer_url])

        # Write a corrupt state file
        (config_dir / "state.yaml").write_text("{{{{not valid yaml: [[[")

        runner = DaemonRunner(config)

        with patch(
            "posthumous.peers.PeerManager.sync_state_from_peers",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_sync, \
             patch(
                 "posthumous.peers.PeerManager.close",
                 new_callable=AsyncMock,
             ):
            await runner.attempt_state_recovery()

        mock_sync.assert_called_once()

    @pytest.mark.asyncio
    async def test_corrupt_state_recovery_exception_is_swallowed(self, config_dir):
        """Exception during recovery -> logged but not re-raised."""
        config = _make_config(config_dir, peers=["http://peer1.local:18426"])

        (config_dir / "state.yaml").write_text("{{{{not valid yaml: [[[")

        runner = DaemonRunner(config)

        with patch(
            "posthumous.peers.PeerManager.sync_state_from_peers",
            new_callable=AsyncMock,
            side_effect=RuntimeError("network died"),
        ), \
             patch(
                 "posthumous.peers.PeerManager.close",
                 new_callable=AsyncMock,
             ):
            # Should not raise
            await runner.attempt_state_recovery()


class TestQuorumWiring:
    """DaemonRunner constructs QuorumCoordinator when quorum is configured (v0.7)."""

    def test_no_quorum_means_no_coordinator(self, tmp_path):
        from posthumous.runner import DaemonRunner
        from posthumous.config import Config

        config = Config(
            node_name="test", secret_key="JBSWY3DPEHPK3PXP",
            config_dir=tmp_path,
        )
        runner = DaemonRunner(config)
        runner.build_components()
        assert runner.quorum_coordinator is None
        assert runner.watchdog._quorum_coordinator is None

    def test_quorum_creates_coordinator_and_passes_to_watchdog(self, tmp_path):
        from posthumous.runner import DaemonRunner
        from posthumous.config import Config, QuorumConfig

        config = Config(
            node_name="test", secret_key="JBSWY3DPEHPK3PXP",
            peers=["https://peer1:8420"],
            quorum=QuorumConfig(required=2, window_seconds=30),
            config_dir=tmp_path,
        )
        runner = DaemonRunner(config)
        runner.build_components()
        assert runner.quorum_coordinator is not None
        assert runner.watchdog._quorum_coordinator is runner.quorum_coordinator


class TestQuorumEndToEnd:
    """Integration test for the full quorum protocol against mocked peer HTTP."""

    @pytest.mark.asyncio
    async def test_quorum_success_triggers_with_bundle(self, tmp_path):
        """3 nodes, M=2: self decides to trigger, peer1 confirms, trigger fires with bundle."""
        from aioresponses import aioresponses, CallbackResult
        from posthumous.runner import DaemonRunner
        from posthumous.config import Config, QuorumConfig
        from posthumous.state import Status
        from posthumous.quorum import sign_confirmation

        config = Config(
            node_name="self",
            secret_key="JBSWY3DPEHPK3PXP",
            listen="https://self.local:8420",
            checkin_interval=timedelta(seconds=1),
            warning_start=timedelta(seconds=2),
            grace_start=timedelta(seconds=3),
            trigger_at=timedelta(seconds=4),
            peers=["https://peer1.local:8420", "https://peer2.local:8420"],
            quorum=QuorumConfig(required=2, window_seconds=5),
            config_dir=tmp_path,
        )

        runner = DaemonRunner(config)
        runner.build_components()
        # Pre-position the state in GRACE with a stale check-in to trigger on next tick.
        runner.state_manager.state.last_checkin = datetime.now(timezone.utc) - timedelta(seconds=10)
        runner.state_manager.state.status = Status.GRACE

        captured_trigger: dict = {}

        def confirm_callback(url, **kwargs):
            payload = kwargs.get("json", {})
            intent_id = payload["intent_id"]
            timestamp = payload["timestamp"]
            return CallbackResult(payload={
                "intent_id": intent_id,
                "vote": "confirm",
                "peer_url": "https://peer1.local:8420",
                "signature": sign_confirmation(
                    "JBSWY3DPEHPK3PXP", intent_id, timestamp, "https://peer1.local:8420"
                ),
            }, status=200)

        def reject_callback(url, **kwargs):
            payload = kwargs.get("json", {})
            return CallbackResult(payload={
                "intent_id": payload["intent_id"],
                "vote": "reject",
                "peer_url": "https://peer2.local:8420",
                "last_checkin": datetime.now(timezone.utc).isoformat(),
            }, status=409)

        def trigger_callback(url, **kwargs):
            captured_trigger.update(kwargs.get("json", {}))
            return CallbackResult(payload={"success": True}, status=200)

        with aioresponses() as m:
            m.post("https://peer1.local:8420/sync/trigger_intent", callback=confirm_callback)
            m.post("https://peer2.local:8420/sync/trigger_intent", callback=reject_callback)
            m.post("https://peer1.local:8420/sync/trigger", callback=trigger_callback)
            m.post("https://peer2.local:8420/sync/trigger", callback=trigger_callback)

            await runner.watchdog.check_and_transition()

        assert runner.state_manager.state.status == Status.TRIGGERED
        # The trigger broadcast should include the bundle.
        assert captured_trigger.get("intent_id") is not None
        assert "confirmations" in captured_trigger
        assert len(captured_trigger["confirmations"]) >= 2

        await runner.peer_manager.close()

    @pytest.mark.asyncio
    async def test_quorum_failure_keeps_state_in_grace(self, tmp_path):
        """When peers reject, no trigger fires and state returns to GRACE."""
        from aioresponses import aioresponses, CallbackResult
        from posthumous.runner import DaemonRunner
        from posthumous.config import Config, QuorumConfig
        from posthumous.state import Status

        config = Config(
            node_name="self",
            secret_key="JBSWY3DPEHPK3PXP",
            listen="https://self.local:8420",
            checkin_interval=timedelta(seconds=1),
            warning_start=timedelta(seconds=2),
            grace_start=timedelta(seconds=3),
            trigger_at=timedelta(seconds=4),
            peers=["https://peer1.local:8420", "https://peer2.local:8420"],
            quorum=QuorumConfig(required=2, window_seconds=5),
            config_dir=tmp_path,
        )

        runner = DaemonRunner(config)
        runner.build_components()
        runner.state_manager.state.last_checkin = datetime.now(timezone.utc) - timedelta(seconds=10)
        runner.state_manager.state.status = Status.GRACE

        def reject_callback(url, **kwargs):
            payload = kwargs.get("json", {})
            return CallbackResult(payload={
                "intent_id": payload["intent_id"],
                "vote": "reject",
                "peer_url": str(url),
                "last_checkin": datetime.now(timezone.utc).isoformat(),
            }, status=409)

        broadcast_count = {"n": 0}

        def trigger_callback(url, **kwargs):
            broadcast_count["n"] += 1
            return CallbackResult(payload={"success": True}, status=200)

        with aioresponses() as m:
            m.post("https://peer1.local:8420/sync/trigger_intent", callback=reject_callback)
            m.post("https://peer2.local:8420/sync/trigger_intent", callback=reject_callback)
            m.post("https://peer1.local:8420/sync/trigger", callback=trigger_callback)
            m.post("https://peer2.local:8420/sync/trigger", callback=trigger_callback)

            await runner.watchdog.check_and_transition()

        assert runner.state_manager.state.status == Status.GRACE
        assert broadcast_count["n"] == 0  # trigger was NOT broadcast

        await runner.peer_manager.close()
