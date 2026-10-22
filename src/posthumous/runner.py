"""Daemon lifecycle orchestration for Posthumous.

Owns the async event loop that wires together server, watchdog, scheduler,
and peer manager, plus the callbacks that react to state transitions and
peer events.

The cli.py ``run`` command is a thin wrapper around :class:`DaemonRunner`:
cli.py handles CLI-level concerns (argument parsing, daemonization fork,
--stop signal sending), while this module owns the async lifecycle.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timedelta, timezone

from posthumous.auth import Authenticator
from posthumous.config import Config, NotificationAction, ScriptAction, action_keys
from posthumous.notifications import NotificationManager, build_context
from posthumous.peers import PeerManager
from posthumous.scheduler import Scheduler, ScheduledExecution
from posthumous.scripts import ScriptContext, ScriptRunner
from posthumous.server import Server
from posthumous.state import State, StateCorruptError, StateManager
from posthumous.systemd import notify_ready, notify_stopping, notify_watchdog
from posthumous.watchdog import Watchdog

logger = logging.getLogger(__name__)


class DaemonRunner:
    """Coordinates the async lifecycle of a Posthumous daemon.

    Wires config -> state -> components -> callbacks, runs the event loop,
    and handles graceful shutdown including sd_notify heartbeats and PID
    file cleanup.

    Typical usage::

        runner = DaemonRunner(config)
        await runner.attempt_state_recovery()
        runner.build_components()
        await runner.run()
    """

    def __init__(self, config: Config):
        self.config = config
        self.state_manager: StateManager | None = None
        self.notification_manager: NotificationManager | None = None
        self.script_runner: ScriptRunner | None = None
        self.authenticator: Authenticator | None = None
        self.peer_manager: PeerManager | None = None
        self.watchdog: Watchdog | None = None
        self.scheduler: Scheduler | None = None
        self.server: Server | None = None
        self._stop_event: asyncio.Event | None = None

    async def attempt_state_recovery(self) -> None:
        """Reconcile local state with peers before components are built.

        Two paths:

        1. State file is corrupt: full recovery from the healthiest peer
           (overwrite semantics).
        2. State file is valid: merge peer state into local state, adopting
           the newest last_checkin, any peer's TRIGGERED status, and the
           union of completed schedule periods. Closes the stale-state
           false-alarm window for a node returning from being offline.

        Idempotent. Never raises: peer-side errors are logged and skipped.
        """
        state_path = State.get_default_state_path(self.config.config_dir)
        encryption_secret = self.config.get_encryption_secret()

        if not state_path.exists():
            return

        try:
            State.load(state_path, encryption_secret)
            state_is_corrupt = False
        except StateCorruptError:
            state_is_corrupt = True

        if not self.config.peers:
            # Nothing to sync against. A corrupt state with no peers means we
            # start fresh anyway; a valid state with no peers means trust disk.
            if state_is_corrupt:
                logger.warning("State corrupt and no peers configured; starting fresh")
            return

        temp_manager = StateManager.for_config(self.config)
        temp_peer = PeerManager(self.config, temp_manager)
        try:
            if state_is_corrupt:
                logger.warning("State file is corrupt, attempting peer recovery...")
                recovered = await temp_peer.sync_state_from_peers()
                if recovered:
                    logger.info("State recovered from peer")
                else:
                    logger.warning("Peer recovery failed; starting with fresh state")
            else:
                # Valid state: merge peer state in to catch up on any
                # check-ins / triggers we missed while offline.
                await temp_peer.merge_state_from_peers()
        except Exception as e:
            logger.warning(f"Peer state reconciliation failed: {e}")
        finally:
            await temp_peer.close()

    def build_components(self) -> None:
        """Instantiate all components. Called after optional state recovery."""
        config = self.config

        # Foundation: state + notifications + scripts + auth
        self.state_manager = StateManager.for_config(config)
        self.notification_manager = NotificationManager(config.notifications)
        self.script_runner = ScriptRunner(config.config_dir)
        self.authenticator = Authenticator(
            secret=config.secret_key,
            api_token=config.api_token,
            max_attempts=config.max_failed_attempts,
            lockout_duration=config.lockout_duration,
        )

        # Peer manager gets the on_peer_down callback bound to this instance
        self.peer_manager = PeerManager(
            config,
            self.state_manager,
            on_peer_down=self.on_peer_down,
        )

        # Watchdog fires on_warning / on_grace / on_trigger as state transitions,
        # and keeps settling owed trigger actions on every tick while TRIGGERED.
        self.watchdog = Watchdog(
            config,
            self.state_manager,
            on_warning=self.on_warning,
            on_grace=self.on_grace,
            on_trigger=self.on_trigger,
            while_triggered=self.settle_trigger_actions,
        )

        # Scheduler fires on_scheduled_complete after each scheduled action
        self.scheduler = Scheduler(
            config,
            self.state_manager,
            notification_manager=self.notification_manager,
            script_runner=self.script_runner,
            on_scheduled_complete=self.on_scheduled_complete,
        )

        # Server ties them all together via HTTP
        self.server = Server(
            config,
            self.state_manager,
            self.watchdog,
            self.authenticator,
            self.peer_manager,
        )

    def _action_context(
        self,
        status: str,
        trigger_time: datetime | None = None,
        extra_context: dict[str, str] | None = None,
    ) -> dict:
        """Template variables available to notification messages."""
        assert self.state_manager is not None, "build_components() must be called first"
        config = self.config
        context = build_context(
            node_name=config.node_name,
            status=status,
            last_checkin=self.state_manager.state.last_checkin,
            trigger_time=trigger_time,
            trigger_at=config.trigger_at,
            base_url=config.get_base_url(),
            peer_urls=config.peers,
            peer_states=self.state_manager.state.peer_states,
            peer_down_threshold=config.peer_down_threshold,
        )
        if extra_context:
            context.update(extra_context)
        return context

    async def _run_action(
        self,
        action: NotificationAction | ScriptAction,
        event: str,
        status: str,
        title: str,
        context: dict,
        trigger_time: datetime | None = None,
    ) -> bool:
        """Run one notification or script action. Returns True on success."""
        assert self.state_manager is not None
        assert self.notification_manager is not None
        assert self.script_runner is not None

        if isinstance(action, NotificationAction):
            result = await self.notification_manager.send(
                action.channel,
                action.message,
                title=title,
                context=context,
            )
            return result.success
        script_context = ScriptContext(
            event=event,
            trigger_time=trigger_time,
            node_name=self.config.node_name,
            status=status,
            last_checkin=self.state_manager.state.last_checkin,
        )
        result = await self.script_runner.run(action.script, script_context)
        return result.success

    async def execute_actions(
        self,
        event: str,
        status: str,
        title: str,
        actions: list,
        trigger_time: datetime | None = None,
        extra_context: dict[str, str] | None = None,
    ) -> None:
        """Execute a list of notification and script actions."""
        logger.info(f"{title} - executing actions")
        context = self._action_context(status, trigger_time, extra_context)
        for action in actions:
            await self._run_action(action, event, status, title, context, trigger_time)

    async def settle_trigger_actions(self) -> None:
        """Run every on_trigger action this node owes and is due, recording each attempt.

        A TRIGGERED node owes each action until it succeeds once in the current
        trigger episode, however it became TRIGGERED. A failed action stays owed
        and is retried with exponential backoff (a duplicate attempt, never
        silence); every attempt is persisted immediately so a success is not
        repeated after a restart. Actions are identified by content
        (config.action_keys), so editing on_trigger after the trigger runs the
        new or changed actions without repeating ones that already succeeded.
        """
        assert self.state_manager is not None
        state = self.state_manager.state
        actions = self.config.on_trigger
        keys = action_keys(actions)
        due = set(state.owed_trigger_actions(keys, datetime.now(timezone.utc)))
        if not due:
            return

        title = "Posthumous Activated"
        logger.info(f"{title} - executing {len(due)} owed trigger action(s)")
        context = self._action_context("triggered", state.trigger_time)
        for action, key in zip(actions, keys):
            if key not in due:
                continue
            succeeded = await self._run_action(
                action, "trigger", "triggered", title, context, state.trigger_time,
            )
            state.record_trigger_action(key, succeeded, datetime.now(timezone.utc))
            self.state_manager.save()
            if not succeeded:
                logger.warning(
                    f"on_trigger action {action} failed; "
                    f"retrying at {state.trigger_actions[key].retry_at}"
                )

    # ------------------------------------------------------------------
    # State-transition callbacks (passed to Watchdog / PeerManager / Scheduler)
    # ------------------------------------------------------------------

    async def on_warning(self) -> None:
        """Fired when watchdog transitions into WARNING."""
        await self.execute_actions(
            "warning", "warning", "Posthumous Warning", self.config.on_warning,
        )

    async def on_grace(self) -> None:
        """Fired when watchdog transitions into GRACE."""
        await self.execute_actions(
            "grace", "grace", "Posthumous - URGENT", self.config.on_grace,
        )

    async def on_trigger(self) -> None:
        """Fired when watchdog transitions into TRIGGERED.

        Broadcasts to peers first so they mark TRIGGERED before any local
        side-effects (matching the original cli.py ordering).
        """
        assert self.state_manager is not None
        assert self.peer_manager is not None

        if self.state_manager.state.trigger_time:
            await self.peer_manager.broadcast_trigger(
                self.state_manager.state.trigger_time,
            )

        await self.settle_trigger_actions()

    async def on_peer_down(
        self,
        peer_url: str,
        downtime: timedelta,
        last_seen: datetime,
    ) -> None:
        """Fired by PeerManager when a peer exceeds the down threshold.

        Formats the downtime inline to avoid reaching into server.py's or
        watchdog.py's private duration helpers.
        """
        assert self.state_manager is not None

        hours = int(downtime.total_seconds() // 3600)
        minutes = int((downtime.total_seconds() % 3600) // 60)
        downtime_str = f"{hours}h {minutes}m" if hours else f"{minutes}m"

        await self.execute_actions(
            "peer_down",
            self.state_manager.state.status.value,
            "Posthumous - Peer Down",
            self.config.on_peer_down,
            extra_context={
                "peer_url": peer_url,
                "peer_downtime": downtime_str,
                "peer_last_seen": (
                    last_seen.isoformat() if last_seen else "unknown"
                ),
            },
        )

    async def on_scheduled_complete(self, execution: ScheduledExecution) -> None:
        """Fired by Scheduler after each scheduled item runs.

        Broadcasts completion to peers so they can dedupe.
        """
        assert self.peer_manager is not None
        await self.peer_manager.broadcast_scheduled_complete(
            execution.item_name,
            execution.period_key,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Run the full daemon lifecycle. Blocks until shutdown."""
        assert self.state_manager is not None, "build_components() must be called first"
        assert self.peer_manager is not None
        assert self.watchdog is not None
        assert self.scheduler is not None
        assert self.server is not None

        config = self.config

        # Signal handlers
        loop = asyncio.get_event_loop()
        self._stop_event = asyncio.Event()
        stop_event = self._stop_event

        def handle_signal() -> None:
            logger.info("Shutdown signal received")
            stop_event.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, handle_signal)

        # Start components. Order matters: server must be ready before
        # watchdog, which may fire callbacks that broadcast to peers via
        # peer_manager.
        await self.server.start()
        self.peer_manager.start_health_monitoring()
        self.watchdog.start()
        self.scheduler.start()

        notify_ready()
        logger.info("sd_notify: READY=1")

        logger.info(f"Posthumous node '{config.node_name}' started")
        logger.info(f"Status: {self.state_manager.state.status.value}")
        logger.info(f"Web UI: http://{config.listen}/")

        async def watchdog_ping() -> None:
            while not stop_event.is_set():
                notify_watchdog()
                await asyncio.sleep(15)

        watchdog_task = asyncio.create_task(watchdog_ping())

        # Wait for shutdown
        await stop_event.wait()

        # Cleanup
        notify_stopping()
        watchdog_task.cancel()
        try:
            await watchdog_task
        except asyncio.CancelledError:
            pass

        logger.info("Shutting down...")
        await self.peer_manager.stop_health_monitoring()
        await self.scheduler.stop()
        await self.watchdog.stop()
        await self.server.stop()
        await self.peer_manager.close()

        # Clean up PID file if it exists (daemon mode)
        pid_path = config.config_dir / "posthumous.pid"
        pid_path.unlink(missing_ok=True)

        logger.info("Shutdown complete")
