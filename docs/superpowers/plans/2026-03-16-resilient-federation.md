# Resilient Federation (v0.6) Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make posthumous harder to kill (systemd daemon with watchdog heartbeat) and self-healing (auto-recover state from peers, alert on peer downtime).

**Architecture:** Four task groups: (1) new `systemd.py` module for sd_notify and unit file generation, (2) peer state recovery wiring in cli.py + `/sync/state` auth, (3) peer down notifications via callback in health check loop, (4) daemon mode with double-fork fallback. Each group is independent after Task 1.

**Tech Stack:** Python 3.10+, asyncio, aiohttp, click, systemd socket protocol (no new dependencies).

**Spec:** `docs/superpowers/specs/2026-03-16-resilient-federation-design.md`

---

## Chunk 1: Systemd Module + Service Commands

### Task 1: sd_notify Module

**Files:**
- Create: `src/posthumous/systemd.py`
- Create: `tests/test_systemd.py`

- [ ] **Step 1: Write failing tests for sd_notify**

```python
# tests/test_systemd.py
import os
import socket
import tempfile
import pytest
from posthumous.systemd import notify_ready, notify_watchdog, notify_stopping, is_systemd_managed


class TestSdNotify:
    def test_is_systemd_managed_true(self, monkeypatch):
        monkeypatch.setenv("NOTIFY_SOCKET", "/tmp/test.sock")
        assert is_systemd_managed() is True

    def test_is_systemd_managed_false(self, monkeypatch):
        monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
        assert is_systemd_managed() is False

    def test_notify_ready_sends_to_socket(self, tmp_path, monkeypatch):
        sock_path = str(tmp_path / "notify.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        server.bind(sock_path)
        monkeypatch.setenv("NOTIFY_SOCKET", sock_path)

        notify_ready()

        data = server.recv(256)
        assert data == b"READY=1"
        server.close()

    def test_notify_watchdog_sends_to_socket(self, tmp_path, monkeypatch):
        sock_path = str(tmp_path / "notify.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        server.bind(sock_path)
        monkeypatch.setenv("NOTIFY_SOCKET", sock_path)

        notify_watchdog()

        data = server.recv(256)
        assert data == b"WATCHDOG=1"
        server.close()

    def test_notify_stopping_sends_to_socket(self, tmp_path, monkeypatch):
        sock_path = str(tmp_path / "notify.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        server.bind(sock_path)
        monkeypatch.setenv("NOTIFY_SOCKET", sock_path)

        notify_stopping()

        data = server.recv(256)
        assert data == b"STOPPING=1"
        server.close()

    def test_notify_noop_without_socket(self, monkeypatch):
        monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
        # Should not raise
        notify_ready()
        notify_watchdog()
        notify_stopping()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_systemd.py -v`
Expected: FAIL — module does not exist

- [ ] **Step 3: Implement systemd.py**

```python
# src/posthumous/systemd.py
"""Systemd integration for Posthumous — sd_notify protocol and unit file generation."""

from __future__ import annotations

import logging
import os
import socket
from pathlib import Path

logger = logging.getLogger(__name__)


def is_systemd_managed() -> bool:
    """Check if running under systemd (NOTIFY_SOCKET is set)."""
    return "NOTIFY_SOCKET" in os.environ


def _sd_notify(message: str) -> None:
    """Send a notification to systemd via NOTIFY_SOCKET.

    No-op if NOTIFY_SOCKET is not set.
    """
    sock_path = os.environ.get("NOTIFY_SOCKET")
    if not sock_path:
        return

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.connect(sock_path)
            sock.sendall(message.encode())
        finally:
            sock.close()
    except OSError as e:
        logger.warning(f"Failed to send sd_notify({message}): {e}")


def notify_ready() -> None:
    """Tell systemd the service is ready."""
    _sd_notify("READY=1")


def notify_watchdog() -> None:
    """Ping the systemd watchdog to prevent restart."""
    _sd_notify("WATCHDOG=1")


def notify_stopping() -> None:
    """Tell systemd the service is stopping."""
    _sd_notify("STOPPING=1")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_systemd.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add src/posthumous/systemd.py tests/test_systemd.py
git commit -m "feat: add systemd sd_notify module"
```

---

### Task 2: Unit File Generation

**Files:**
- Modify: `src/posthumous/systemd.py`
- Modify: `tests/test_systemd.py`

- [ ] **Step 1: Write failing tests**

```python
class TestUnitFileGeneration:
    def test_generate_unit_file(self):
        from posthumous.systemd import generate_unit_file
        content = generate_unit_file(
            python_path="/usr/bin/python3",
            config_path="/home/user/.posthumous/config.yaml",
            node_name="my-node",
        )
        assert "[Unit]" in content
        assert "Type=notify" in content
        assert "WatchdogSec=30" in content
        assert "Restart=always" in content
        assert "/usr/bin/python3 -m posthumous run" in content
        assert "/home/user/.posthumous/config.yaml" in content
        assert "my-node" in content

    def test_get_unit_path(self):
        from posthumous.systemd import get_unit_path
        path = get_unit_path()
        assert path.name == "posthumous.service"
        assert ".config/systemd/user" in str(path)

    def test_is_service_installed(self, tmp_path, monkeypatch):
        from posthumous.systemd import is_service_installed, get_unit_path
        # Not installed
        monkeypatch.setattr("posthumous.systemd.get_unit_path", lambda: tmp_path / "nope.service")
        assert is_service_installed() is False
        # Installed
        unit = tmp_path / "exists.service"
        unit.write_text("[Unit]")
        monkeypatch.setattr("posthumous.systemd.get_unit_path", lambda: unit)
        assert is_service_installed() is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_systemd.py::TestUnitFileGeneration -v`
Expected: FAIL

- [ ] **Step 3: Implement**

Add to `src/posthumous/systemd.py`:

```python
UNIT_TEMPLATE = """\
[Unit]
Description=Posthumous Deadman Switch ({node_name})
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
ExecStart={python_path} -m posthumous run --config {config_path}
WatchdogSec=30
Restart=always
RestartSec=5
Environment=POSTHUMOUS_CONFIG={config_path}

[Install]
WantedBy=default.target
"""


def get_unit_path() -> Path:
    """Get the path for the systemd user service unit file."""
    return Path.home() / ".config" / "systemd" / "user" / "posthumous.service"


def is_service_installed() -> bool:
    """Check if the systemd service unit file exists."""
    return get_unit_path().exists()


def generate_unit_file(
    python_path: str,
    config_path: str,
    node_name: str,
) -> str:
    """Generate a systemd unit file with the given parameters."""
    return UNIT_TEMPLATE.format(
        python_path=python_path,
        config_path=config_path,
        node_name=node_name,
    )
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_systemd.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add src/posthumous/systemd.py tests/test_systemd.py
git commit -m "feat: add systemd unit file generation"
```

---

### Task 3: Service CLI Commands

**Files:**
- Modify: `src/posthumous/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write failing tests**

```python
# Add to tests/test_cli.py
from unittest.mock import patch, MagicMock

class TestServiceCommands:
    def test_service_install(self, runner, tmp_path, monkeypatch):
        """service install generates unit file and enables service."""
        # Setup: create a valid config
        config_dir = tmp_path / ".posthumous"
        config_dir.mkdir()
        (config_dir / "scripts").mkdir()
        config_path = config_dir / "config.yaml"
        import yaml
        config_data = {
            'node_name': 'test-node',
            'secret_key': 'JBSWY3DPEHPK3PXP',
            'listen': '127.0.0.1:8420',
            'checkin_interval': '7 days',
            'warning_start': '8 days',
            'grace_start': '12 days',
            'trigger_at': '14 days',
        }
        with open(config_path, 'w') as f:
            yaml.dump(config_data, f)

        unit_path = tmp_path / "posthumous.service"
        monkeypatch.setattr("posthumous.systemd.get_unit_path", lambda: unit_path)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = runner.invoke(main, ['--config', str(config_path), 'service', 'install'])

        assert result.exit_code == 0
        assert unit_path.exists()
        content = unit_path.read_text()
        assert "Type=notify" in content

    def test_service_uninstall(self, runner, tmp_path, monkeypatch):
        """service uninstall stops, disables, and removes unit file."""
        unit_path = tmp_path / "posthumous.service"
        unit_path.write_text("[Unit]")
        monkeypatch.setattr("posthumous.systemd.get_unit_path", lambda: unit_path)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = runner.invoke(main, ['service', 'uninstall'])

        assert result.exit_code == 0
        assert not unit_path.exists()

    def test_service_status(self, runner):
        """service status delegates to systemctl."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="active")
            result = runner.invoke(main, ['service', 'status'])
        assert result.exit_code == 0
        mock_run.assert_called_once()
        assert "systemctl" in str(mock_run.call_args)
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_cli.py::TestServiceCommands -v`
Expected: FAIL

- [ ] **Step 3: Implement service subgroup in cli.py**

Add to `src/posthumous/cli.py` after the `config` subgroup:

```python
@main.group()
def service():
    """Manage the systemd service."""
    pass


@service.command()
@click.pass_context
def install(ctx: click.Context) -> None:
    """Install and start the systemd user service."""
    from posthumous.config import Config
    from posthumous.systemd import generate_unit_file, get_unit_path

    config_path = _resolve_config_path(ctx)
    if not config_path.exists():
        click.echo(f"Config not found at {config_path}", err=True)
        sys.exit(1)

    config = Config.from_yaml(config_path)
    unit_content = generate_unit_file(
        python_path=sys.executable,
        config_path=str(config_path.resolve()),
        node_name=config.node_name,
    )

    unit_path = get_unit_path()
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(unit_content)
    click.echo(f"Unit file written to {unit_path}")

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "posthumous"], check=True)
    subprocess.run(["systemctl", "--user", "start", "posthumous"], check=True)
    click.echo("Service installed, enabled, and started.")


@service.command()
def uninstall() -> None:
    """Stop, disable, and remove the systemd user service."""
    from posthumous.systemd import get_unit_path

    unit_path = get_unit_path()
    if not unit_path.exists():
        click.echo("Service is not installed.", err=True)
        sys.exit(1)

    subprocess.run(["systemctl", "--user", "stop", "posthumous"], check=False)
    subprocess.run(["systemctl", "--user", "disable", "posthumous"], check=False)
    unit_path.unlink()
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    click.echo("Service stopped, disabled, and removed.")


@service.command()
def status() -> None:
    """Show systemd service status."""
    subprocess.run(["systemctl", "--user", "status", "posthumous"])


@service.command()
@click.option('--follow', '-f', is_flag=True, help='Follow log output')
def logs(follow: bool) -> None:
    """Show service journal logs."""
    cmd = ["journalctl", "--user", "-u", "posthumous", "--no-pager"]
    if follow:
        cmd.append("-f")
    subprocess.run(cmd)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_cli.py::TestServiceCommands -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add src/posthumous/cli.py tests/test_cli.py
git commit -m "feat: add phm service install/uninstall/status/logs commands"
```

---

### Task 4: sd_notify Integration in run_daemon

**Files:**
- Modify: `src/posthumous/cli.py:371-413` (run_daemon function)
- Modify: `tests/test_cli_run.py`

- [ ] **Step 1: Write failing test**

```python
# Add to tests/test_cli_run.py
from unittest.mock import patch, AsyncMock, MagicMock
import asyncio

class TestSdNotifyIntegration:
    def test_run_calls_notify_ready(self, runner, valid_config_dir):
        """run command should call notify_ready after starting components."""
        config_path = valid_config_dir / "config.yaml"

        with patch("posthumous.systemd.notify_ready") as mock_ready, \
             patch("posthumous.systemd.notify_watchdog"), \
             patch("posthumous.systemd.notify_stopping"), \
             patch("posthumous.server.Server.start", new_callable=AsyncMock), \
             patch("posthumous.server.Server.stop", new_callable=AsyncMock), \
             patch("posthumous.peers.PeerManager.start_health_monitoring"), \
             patch("posthumous.peers.PeerManager.stop_health_monitoring", new_callable=AsyncMock), \
             patch("posthumous.peers.PeerManager.close", new_callable=AsyncMock), \
             patch("posthumous.watchdog.Watchdog.start"), \
             patch("posthumous.watchdog.Watchdog.stop", new_callable=AsyncMock), \
             patch("posthumous.scheduler.Scheduler.start"), \
             patch("posthumous.scheduler.Scheduler.stop", new_callable=AsyncMock), \
             patch("asyncio.Event.wait", new_callable=AsyncMock) as mock_wait:
            # Simulate immediate shutdown
            mock_wait.return_value = None
            result = runner.invoke(main, ['--config', str(config_path), 'run'])
            mock_ready.assert_called_once()
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_cli_run.py::TestSdNotifyIntegration -v`
Expected: FAIL — `notify_ready` not called (hasn't been added yet)

- [ ] **Step 3: Implement sd_notify calls in run_daemon**

In `src/posthumous/cli.py`, modify `run_daemon()`:

After `scheduler.start()` (line 388):
```python
        from posthumous.systemd import notify_ready, notify_watchdog, notify_stopping

        notify_ready()
        logger.info("sd_notify: READY=1")
```

Add a watchdog ping task before `await stop_event.wait()`:
```python
        async def watchdog_ping():
            while not stop_event.is_set():
                notify_watchdog()
                await asyncio.sleep(15)

        watchdog_task = asyncio.create_task(watchdog_ping())
```

At start of shutdown (before cleanup):
```python
        notify_stopping()
        watchdog_task.cancel()
        try:
            await watchdog_task
        except asyncio.CancelledError:
            pass
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_cli_run.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add src/posthumous/cli.py tests/test_cli_run.py
git commit -m "feat: integrate sd_notify heartbeat into daemon loop"
```

---

## Chunk 2: Peer State Recovery

### Task 5: /sync/state Authentication

**Files:**
- Modify: `src/posthumous/server.py:683-697`
- Modify: `tests/test_server.py`

- [ ] **Step 1: Write failing tests**

```python
class TestSyncStateAuth:
    async def test_sync_state_rejects_unsigned_request(self, client):
        resp = await client.get("/sync/state")
        assert resp.status == 401

    async def test_sync_state_rejects_stale_timestamp(self, client):
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        signature = sign_message(SECRET, f"state:{old_time}")
        resp = await client.get(f"/sync/state?ts={old_time}&sig={signature}")
        assert resp.status == 401

    async def test_sync_state_accepts_valid_auth(self, client):
        now = datetime.now(timezone.utc).isoformat()
        signature = sign_message(SECRET, f"state:{now}")
        resp = await client.get(f"/sync/state?ts={now}&sig={signature}")
        assert resp.status == 200
        data = await resp.json()
        assert "status" in data
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_server.py::TestSyncStateAuth -v`
Expected: FAIL — currently returns 200 without auth

- [ ] **Step 3: Implement auth on /sync/state**

In `src/posthumous/server.py`, modify `handle_sync_state`:

```python
    async def handle_sync_state(self, request: web.Request) -> web.Response:
        """Return current state for peer sync (authenticated)."""
        ts = request.query.get('ts')
        sig = request.query.get('sig')

        if not ts or not sig:
            return web.json_response({'error': 'Authentication required'}, status=401)

        error = self._verify_sync_message(ts, sig, "state")
        if error:
            logger.warning(f"Sync state request rejected: {error}")
            return web.json_response({'error': error}, status=401)

        state = self.state_manager.state
        # ... rest of existing response code unchanged
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_server.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add src/posthumous/server.py tests/test_server.py
git commit -m "feat: add HMAC authentication to /sync/state endpoint"
```

---

### Task 6: Update PeerManager to Sign /sync/state Requests

**Files:**
- Modify: `src/posthumous/peers.py:267-325`
- Modify: `tests/test_peers.py`

- [ ] **Step 1: Write failing test**

```python
class TestSyncStateSigning:
    @pytest.mark.asyncio
    async def test_sync_state_sends_signed_request(self):
        """sync_state_from_peers should send ts and sig query params."""
        from aioresponses import aioresponses
        from posthumous.peers import PeerManager
        from posthumous.config import Config
        from posthumous.state import StateManager

        config = Config(node_name="test", secret_key="JBSWY3DPEHPK3PXP",
                        peers=["http://peer1:8420"])
        state_manager = MagicMock(spec=StateManager)
        state_manager.state = State()
        state_manager.save = MagicMock()

        peer_manager = PeerManager(config, state_manager)

        with aioresponses() as m:
            # Mock health check (for get_peer_status)
            m.get("http://peer1:8420/status", payload={
                "status": "armed", "last_checkin": datetime.now(timezone.utc).isoformat(),
            })
            # Mock state endpoint — match any URL starting with /sync/state
            m.get(re.compile(r"http://peer1:8420/sync/state\?.*"), payload={
                "status": "armed", "last_checkin": None,
            })

            await peer_manager.sync_state_from_peers()

            # Verify the state request had ts= and sig= params
            calls = [str(call[0][1]) for call in m.requests.get(("GET",), [])]
            state_calls = [c for c in calls if "sync/state" in str(c)]
            assert len(state_calls) >= 1
            assert "ts=" in str(state_calls[0])
            assert "sig=" in str(state_calls[0])

        await peer_manager.close()
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_peers.py::TestSyncStateAuth -v`

- [ ] **Step 3: Implement**

In `peers.py`, modify the `_get` method call inside `sync_state_from_peers()` (line 290) to append auth params:

```python
        from posthumous.auth import sign_message
        ts = datetime.now(timezone.utc).isoformat()
        sig = sign_message(self.config.secret_key, f"state:{ts}")
        data, error = await self._get(best_peer.url, f"sync/state?ts={ts}&sig={sig}")
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_peers.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add src/posthumous/peers.py tests/test_peers.py
git commit -m "feat: sign /sync/state requests with HMAC"
```

---

### Task 7: Automatic State Recovery on Startup

**Files:**
- Modify: `src/posthumous/cli.py:278-290`
- Modify: `tests/test_cli_run.py`

- [ ] **Step 1: Write failing test**

```python
from unittest.mock import patch, AsyncMock, MagicMock
from posthumous.state import State, StateCorruptError

class TestStateRecovery:
    def test_corrupt_state_triggers_peer_recovery(self, runner, valid_config_dir):
        """When state file is corrupt, daemon should attempt peer recovery."""
        state_path = valid_config_dir / "state.yaml"
        state_path.write_text("corrupt: {{{invalid yaml")
        config_path = valid_config_dir / "config.yaml"

        with patch.object(PeerManager, 'sync_state_from_peers', new_callable=AsyncMock) as mock_sync, \
             patch.object(PeerManager, 'close', new_callable=AsyncMock), \
             patch("posthumous.server.Server.start", new_callable=AsyncMock), \
             patch("posthumous.server.Server.stop", new_callable=AsyncMock), \
             patch("posthumous.peers.PeerManager.start_health_monitoring"), \
             patch("posthumous.peers.PeerManager.stop_health_monitoring", new_callable=AsyncMock), \
             patch("posthumous.watchdog.Watchdog.start"), \
             patch("posthumous.watchdog.Watchdog.stop", new_callable=AsyncMock), \
             patch("posthumous.scheduler.Scheduler.start"), \
             patch("posthumous.scheduler.Scheduler.stop", new_callable=AsyncMock), \
             patch("asyncio.Event.wait", new_callable=AsyncMock):
            mock_sync.return_value = True
            result = runner.invoke(main, ['--config', str(config_path), 'run'])
            mock_sync.assert_called_once()
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_cli_run.py::TestStateRecovery -v`

- [ ] **Step 3: Implement state pre-flight in run_daemon**

In `src/posthumous/cli.py`, before `state_manager = StateManager(...)` (line 279), add:

```python
    # Pre-flight: check state file integrity before constructing StateManager
    from posthumous.state import State, StateCorruptError
    state_path = config.config_dir / "state.yaml"
    encryption_secret = config.get_encryption_secret()

    if state_path.exists():
        try:
            State.load(state_path, encryption_secret)
        except StateCorruptError:
            logger.warning("State file is corrupt, attempting peer recovery...")
            temp_manager = StateManager(state_path, encryption_secret)
            temp_peer = PeerManager(config, temp_manager)

            async def attempt_recovery():
                try:
                    recovered = await temp_peer.sync_state_from_peers()
                    if recovered:
                        logger.info("State recovered from peer")
                    else:
                        logger.warning("No peers available, starting with fresh state")
                except Exception as e:
                    logger.warning(f"Peer recovery failed: {e}, starting with fresh state")
                finally:
                    await temp_peer.close()

            asyncio.run(attempt_recovery())
```

Note: The `asyncio.run` here runs before the daemon event loop starts. The existing `StateManager` construction on line 279 follows and will either load the recovered state or create fresh state.

- [ ] **Step 4: Run tests**

Run: `pytest -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add src/posthumous/cli.py tests/test_cli_run.py
git commit -m "feat: auto-recover state from peers on corrupt/missing state"
```

---

### Task 8: Manual Recovery Command

**Files:**
- Modify: `src/posthumous/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write failing test**

```python
class TestRecoverCommand:
    def test_recover_with_force(self, runner, tmp_config_dir):
        """phm recover --force should sync from peers without prompting."""
        with patch.object(PeerManager, 'sync_state_from_peers', new_callable=AsyncMock) as mock_sync:
            mock_sync.return_value = True
            result = runner.invoke(main, ['recover', '--force'])
        assert result.exit_code == 0
        assert "recovered" in result.output.lower() or "synced" in result.output.lower()

    def test_recover_without_force_prompts(self, runner, tmp_config_dir):
        """phm recover without --force should prompt for confirmation."""
        result = runner.invoke(main, ['recover'], input='n\n')
        assert result.exit_code == 0
        # Should show state comparison

    def test_recover_no_peers(self, runner, tmp_config_dir):
        """phm recover should fail gracefully with no peers."""
        with patch.object(PeerManager, 'sync_state_from_peers', new_callable=AsyncMock) as mock_sync:
            mock_sync.return_value = False
            result = runner.invoke(main, ['recover', '--force'])
        assert result.exit_code != 0 or "no peers" in result.output.lower()
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_cli.py::TestRecoverCommand -v`

- [ ] **Step 3: Implement recover command**

Add to `src/posthumous/cli.py`:

```python
@main.command()
@click.option('--force', is_flag=True, help='Skip confirmation prompt')
@click.pass_context
def recover(ctx: click.Context, force: bool) -> None:
    """Recover state from the healthiest peer."""
    from posthumous.config import Config
    from posthumous.state import StateManager
    from posthumous.peers import PeerManager

    config_path = _resolve_config_path(ctx)
    if not config_path.exists():
        click.echo(f"Config not found at {config_path}", err=True)
        sys.exit(1)

    config = Config.from_yaml(config_path)
    state_manager = StateManager(config.config_dir / "state.yaml", config.get_encryption_secret())

    async def do_recover():
        peer_manager = PeerManager(config, state_manager)
        try:
            if not force:
                # Show local state
                local = state_manager.state
                click.echo("Local state:")
                click.echo(f"  status:       {local.status.value}")
                click.echo(f"  last_checkin: {local.last_checkin or 'never'}")
                click.echo(f"  trigger_time: {local.trigger_time or 'none'}")
                click.echo(f"  schedule:     {len(local.schedule_state)} items")

                # Fetch peer state for comparison
                statuses = await peer_manager.get_all_peer_status()
                best = max(
                    (s for s in statuses if s.reachable and s.last_checkin),
                    key=lambda s: s.last_checkin, default=None,
                )
                if best:
                    click.echo(f"\nPeer state ({best.url}):")
                    click.echo(f"  status:       {best.status or 'unknown'}")
                    click.echo(f"  last_checkin: {best.last_checkin or 'never'}")
                else:
                    click.echo("\nNo reachable peers with state to compare.")
                    return

                click.echo()
                if not click.confirm("Overwrite local state from peer?"):
                    click.echo("Aborted.")
                    return

            success = await peer_manager.sync_state_from_peers()
            if success:
                click.echo("State recovered from peer successfully.")
            else:
                click.echo("No peers available for recovery.", err=True)
                sys.exit(1)
        finally:
            await peer_manager.close()

    asyncio.run(do_recover())
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_cli.py::TestRecoverCommand -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add src/posthumous/cli.py tests/test_cli.py
git commit -m "feat: add phm recover command for manual peer state recovery"
```

---

## Chunk 3: Peer Down Notifications + Daemon Mode

### Task 9: on_peer_down Config Field

**Files:**
- Modify: `src/posthumous/config.py:80-107, 218-221, 291-299`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing test**

```python
class TestOnPeerDownConfig:
    def test_parse_on_peer_down(self, tmp_path):
        import yaml
        config_data = {
            'node_name': 'test', 'secret_key': 'JBSWY3DPEHPK3PXP',
            'checkin_interval': '7 days', 'warning_start': '8 days',
            'grace_start': '12 days', 'trigger_at': '14 days',
            'actions': {
                'on_peer_down': [
                    {'notify': 'default', 'message': 'Peer {peer_url} down'}
                ]
            },
            'notifications': {'default': ['ntfy://test']},
        }
        path = tmp_path / "config.yaml"
        with open(path, 'w') as f:
            yaml.dump(config_data, f)
        from posthumous.config import Config
        config = Config.from_yaml(path)
        assert len(config.on_peer_down) == 1
        assert config.on_peer_down[0].message == "Peer {peer_url} down"

    def test_on_peer_down_defaults_empty(self):
        from posthumous.config import Config
        config = Config(node_name="test", secret_key="JBSWY3DPEHPK3PXP")
        assert config.on_peer_down == []

    def test_to_dict_includes_on_peer_down(self):
        from posthumous.config import Config, NotificationAction
        config = Config(
            node_name="test", secret_key="JBSWY3DPEHPK3PXP",
            on_peer_down=[NotificationAction(channel="default", message="down")],
        )
        d = config.to_dict()
        assert 'on_peer_down' in d.get('actions', {})
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_config.py::TestOnPeerDownConfig -v`

- [ ] **Step 3: Implement**

In `src/posthumous/config.py`:

Add field to `Config` dataclass (after `on_trigger`):
```python
    on_peer_down: list[NotificationAction | ScriptAction] = field(default_factory=list)
```

In `from_dict()` (around line 219-221), add:
```python
            on_peer_down=parse_actions(actions_config.get('on_peer_down')),
```

In `to_dict()` (around line 291-299), add inside the `actions` block:
```python
        if self.on_peer_down:
            actions['on_peer_down'] = serialize_actions(self.on_peer_down)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_config.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add src/posthumous/config.py tests/test_config.py
git commit -m "feat: add on_peer_down config field"
```

---

### Task 10: Peer Down Callback in Health Check Loop

**Files:**
- Modify: `src/posthumous/peers.py:33-48, 350-358`
- Modify: `src/posthumous/cli.py:290, 330-345` (wire up in run_daemon)
- Modify: `src/posthumous/notifications.py:203-232` (peer template vars)
- Modify: `tests/test_peers.py`

- [ ] **Step 1: Write failing tests**

```python
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta, timezone
from posthumous.peers import PeerManager, PeerStatus
from posthumous.config import Config
from posthumous.state import StateManager, State, PeerState

class TestPeerDownCallback:
    def _make_peer_manager(self, callback, peer_url="http://peer1:8420"):
        config = Config(
            node_name="test", secret_key="JBSWY3DPEHPK3PXP",
            peers=[peer_url],
            peer_check_interval=timedelta(seconds=1),
            peer_down_threshold=timedelta(hours=6),
        )
        state_manager = MagicMock(spec=StateManager)
        state = State()
        # Set peer as seen 7 hours ago (past threshold)
        state.peer_states[peer_url] = PeerState(
            url=peer_url,
            last_seen=datetime.now(timezone.utc) - timedelta(hours=7),
        )
        state_manager.state = state
        state_manager.save = MagicMock()
        return PeerManager(config, state_manager, on_peer_down=callback)

    @pytest.mark.asyncio
    async def test_callback_fires_at_threshold(self):
        callback = AsyncMock()
        pm = self._make_peer_manager(callback)
        with patch.object(pm, 'get_all_peer_status', new_callable=AsyncMock) as mock_status:
            mock_status.return_value = [PeerStatus(
                url="http://peer1:8420", reachable=False,
                status=None, last_checkin=None, last_seen=None, error="timeout",
            )]
            # Run one iteration manually
            await pm._health_check_loop_once()
        callback.assert_called_once()
        await pm.close()

    @pytest.mark.asyncio
    async def test_callback_not_repeated(self):
        callback = AsyncMock()
        pm = self._make_peer_manager(callback)
        with patch.object(pm, 'get_all_peer_status', new_callable=AsyncMock) as mock_status:
            mock_status.return_value = [PeerStatus(
                url="http://peer1:8420", reachable=False,
                status=None, last_checkin=None, last_seen=None, error="timeout",
            )]
            await pm._health_check_loop_once()
            await pm._health_check_loop_once()
        assert callback.call_count == 1
        await pm.close()
```

Note: The tests reference `_health_check_loop_once()` — a new method that extracts one iteration of the health check loop body. This is simpler than running the full async loop with timing. The implementer should extract the loop body into `_health_check_loop_once()` and have `_health_check_loop` call it in a while loop.

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_peers.py::TestPeerDownCallback -v`

- [ ] **Step 3: Implement**

In `src/posthumous/peers.py`, add `on_peer_down` callback to `PeerManager.__init__()`:

```python
    def __init__(
        self,
        config: "Config",
        state_manager: "StateManager",
        timeout: float = 10.0,
        on_peer_down: Callable | None = None,
    ):
        # ... existing init ...
        self._on_peer_down = on_peer_down
```

At the TODO on line 358, replace the comment:
```python
                                self._alerted_peers.add(status.url)
                                if self._on_peer_down:
                                    await self._on_peer_down(
                                        status.url,
                                        down_duration,
                                        peer_state.last_seen,
                                    )
```

Add `from typing import Callable` to the existing TYPE_CHECKING imports, or use `collections.abc.Callable`.

First, modify `execute_actions` in `cli.py` to accept optional extra context:

```python
    async def execute_actions(
        event: str,
        status: str,
        title: str,
        actions: list,
        trigger_time: datetime | None = None,
        extra_context: dict[str, str] | None = None,
    ) -> None:
        """Execute a list of notification and script actions."""
        logger.info(f"{title} - executing actions")

        context = build_context(
            node_name=config.node_name,
            status=status,
            last_checkin=state_manager.state.last_checkin,
            trigger_time=trigger_time,
            trigger_at=config.trigger_at,
            base_url=config.get_base_url(),
        )
        if extra_context:
            context.update(extra_context)
        # ... rest unchanged
```

Then in `run_daemon()`, wire the callback (after the existing `on_trigger` callback):

```python
    async def on_peer_down(peer_url: str, downtime: timedelta, last_seen: datetime):
        from posthumous.server import _format_duration
        await execute_actions(
            "peer_down", state_manager.state.status.value,
            "Posthumous - Peer Down", config.on_peer_down,
            extra_context={
                'peer_url': peer_url,
                'peer_downtime': _format_duration(downtime),
                'peer_last_seen': last_seen.isoformat() if last_seen else 'unknown',
            },
        )
```

Update `PeerManager` construction:
```python
    peer_manager = PeerManager(config, state_manager, on_peer_down=on_peer_down)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_peers.py tests/test_cli_run.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add src/posthumous/peers.py src/posthumous/cli.py tests/test_peers.py
git commit -m "feat: fire on_peer_down callback when peer exceeds downtime threshold"
```

---

### Task 11: Daemon Mode (Double-Fork)

**Files:**
- Modify: `src/posthumous/cli.py:242-413`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write failing tests**

```python
class TestDaemonMode:
    def test_daemon_writes_pid_file(self, tmp_config_dir, monkeypatch):
        """--daemon should write PID file."""
        pid_path = tmp_config_dir / "posthumous.pid"
        # Mock os.fork to simulate daemonization without actually forking
        # Verify PID file is written

    def test_stop_sends_sigterm(self, runner, tmp_config_dir):
        """--stop should read PID and send SIGTERM."""
        pid_path = tmp_config_dir / "posthumous.pid"
        pid_path.write_text("12345")
        with patch("os.kill") as mock_kill, \
             patch("os.kill", side_effect=[None, ProcessLookupError]):
            result = runner.invoke(main, ['run', '--stop'])
        mock_kill.assert_any_call(12345, signal.SIGTERM)

    def test_daemon_suggests_systemd_when_installed(self, runner, monkeypatch):
        """When systemd service exists, --daemon should suggest using it."""
        monkeypatch.setattr("posthumous.systemd.is_service_installed", lambda: True)
        result = runner.invoke(main, ['run', '--daemon'])
        assert "phm service start" in result.output
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_cli.py::TestDaemonMode -v`

- [ ] **Step 3: Implement daemon mode**

In `src/posthumous/cli.py`, add `--stop` flag and replace the daemon stub:

```python
@main.command()
@click.option('--daemon', '-d', is_flag=True, help='Run in background (daemon mode)')
@click.option('--stop', is_flag=True, help='Stop a running daemon')
@click.pass_context
def run(ctx: click.Context, daemon: bool, stop: bool) -> None:
```

Add stop logic at top of `run()`:
```python
    if stop:
        pid_path = config.config_dir / "posthumous.pid"
        if not pid_path.exists():
            click.echo("No PID file found. Is the daemon running?", err=True)
            sys.exit(1)
        pid = int(pid_path.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        click.echo(f"Sent SIGTERM to PID {pid}")
        # Wait up to 10s
        import time
        for _ in range(20):
            try:
                os.kill(pid, 0)  # Check if still running
                time.sleep(0.5)
            except ProcessLookupError:
                pid_path.unlink(missing_ok=True)
                click.echo("Daemon stopped.")
                return
        click.echo(f"Process {pid} still running. Try: kill -9 {pid}", err=True)
        sys.exit(1)
```

Replace daemon stub (line 406-407):
```python
    if daemon:
        from posthumous.systemd import is_service_installed
        if is_service_installed():
            click.echo("Systemd service is installed. Use 'phm service start' instead.")
            sys.exit(0)

        # Double-fork daemonization
        pid = os.fork()
        if pid > 0:
            sys.exit(0)  # Parent exits
        os.setsid()
        pid = os.fork()
        if pid > 0:
            sys.exit(0)  # Second parent exits

        # Redirect stdio
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        log_path = config.config_dir / "logs" / "daemon.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.dup2(log_fd, 1)
        os.dup2(log_fd, 2)
        os.close(devnull)
        os.close(log_fd)

        # Write PID file
        pid_path = config.config_dir / "posthumous.pid"
        pid_path.write_text(str(os.getpid()))
```

- [ ] **Step 4: Run tests**

Run: `pytest -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add src/posthumous/cli.py tests/test_cli.py
git commit -m "feat: implement daemon mode with double-fork and --stop flag"
```

---

### Task 12: Final Integration Test Run

- [ ] **Step 1: Run full test suite**

Run: `pytest --tb=long -v`
Expected: All tests pass

- [ ] **Step 2: Verify coverage**

Run: `pytest`
Expected: Coverage >= 99%

- [ ] **Step 3: Commit any fixups**

```bash
git add -u
git commit -m "fix: address integration test fixups for v0.6"
```
