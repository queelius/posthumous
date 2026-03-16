# Resilient Federation (v0.6) Design

Date: 2026-03-16

## Goal

Make posthumous harder to kill and self-healing across federated nodes. Two capabilities: systemd-native daemon management and automatic peer state recovery.

## Architecture

Four components that build on the existing codebase with minimal new abstractions. The systemd integration is a new module (`systemd.py`); the recovery and notification work wires up existing but unused code paths. The daemon fallback uses classic Unix double-fork for non-systemd environments.

## Section 1: Systemd Service Integration

### New CLI Commands

All under a `phm service` subgroup:

- `phm service install` — Generates and installs a systemd user service unit at `~/.config/systemd/user/posthumous.service`. Enables and starts the service. The unit file is generated (not static) using the actual Python interpreter path and config path.
- `phm service uninstall` — Stops, disables, and removes the unit file.
- `phm service status` — Shows systemd service status (delegates to `systemctl --user status posthumous`).
- `phm service logs [--follow]` — Shows journal logs (delegates to `journalctl --user -u posthumous`).

### Generated Unit File

```ini
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
```

Template values resolved at install time:
- `{python_path}` — `sys.executable`
- `{config_path}` — resolved config path (default or `--config` flag)
- `{node_name}` — from loaded config

### sd_notify Integration

New module: `src/posthumous/systemd.py`

Implements the sd_notify socket protocol directly (write to `$NOTIFY_SOCKET` Unix socket). No external dependency needed. Functions:

- `notify_ready()` — sends `READY=1` after all components start
- `notify_watchdog()` — sends `WATCHDOG=1`, called every 15s from the event loop
- `notify_stopping()` — sends `STOPPING=1` during graceful shutdown
- `is_systemd_managed()` — checks if `$NOTIFY_SOCKET` is set

Integration points in `cli.py`'s `run_daemon()`:
- After `scheduler.start()`: call `notify_ready()`
- New periodic task in the event loop: ping `notify_watchdog()` every 15s (half of WatchdogSec=30)
- At start of shutdown: call `notify_stopping()`

When not running under systemd (`$NOTIFY_SOCKET` absent), all calls are no-ops.

## Section 2: Peer State Recovery

### Automatic Recovery on Startup

In `run_daemon()`, add an explicit state pre-flight check before constructing components:

1. Attempt `State.load(state_path, encryption_secret)` directly (not through `StateManager`, which silently swallows `StateCorruptError`).
2. If `StateCorruptError` is raised, attempt `peer_manager.sync_state_from_peers()`.
3. If sync succeeds, log the recovery and continue with recovered state.
4. If sync fails (no peers reachable), fall back to fresh `State()` (existing behavior).
5. Only then construct `StateManager` with the validated/recovered state.

Note: `StateManager.state` (line 361 of `state.py`) currently catches `StateCorruptError` silently and returns fresh state. The pre-flight check bypasses this by loading state directly, so `StateManager`'s silent fallback behavior is preserved for non-startup paths.

This wires up the already-written `sync_state_from_peers()` in `peers.py:267-325` — no new peer protocol needed.

### Manual Recovery Command

- `phm recover` — Fetches state from the healthiest peer and overwrites local state.
- Without `--force`: shows a field-by-field comparison of local vs peer state (status, last_checkin, trigger_time, schedule items) and prompts for confirmation before applying.
- With `--force`: skips the confirmation prompt and applies immediately.
- Uses the same `sync_state_from_peers()` code path.

### `/sync/state` Authentication

Currently `GET /sync/state` is unauthenticated. Add HMAC query parameter authentication:

- Requesting node signs a timestamp: `sig = sign_message(secret, f"state:{timestamp}")`
- Request: `GET /sync/state?ts={timestamp}&sig={signature}`
- Handler validates freshness (reuse `_verify_sync_message` pattern) + HMAC signature
- Rejects stale or unsigned requests with 401

## Section 3: Peer Down Notifications

### Wire Up the TODO

When a peer exceeds `peer_down_threshold` (default 6 hours) in `_health_check_loop`, fire notification actions. The `_alerted_peers` set already prevents repeat alerts (one notification per down episode).

### Config Addition

New optional config field, same structure as `on_warning`/`on_grace`/`on_trigger`:

```yaml
actions:
  on_warning:
    - notify: default
      message: "Check in soon..."
  on_peer_down:
    - notify: default
      message: "Peer {peer_url} unreachable for {peer_downtime}"
```

Nested under `actions:` alongside `on_warning`/`on_grace`/`on_trigger`, matching the existing config structure. Parsed as `list[NotificationAction | ScriptAction]` — identical type to the existing action lists.

### New Template Variables

- `{peer_url}` — URL of the unreachable peer
- `{peer_downtime}` — human-readable duration (e.g., "6h 30m")
- `{peer_last_seen}` — ISO timestamp of last successful contact

### Implementation

The health check loop at `peers.py:350-357` already has the threshold check and `_alerted_peers` guard. The change:
- Accept an `on_peer_down` callback in `PeerManager.__init__()` (same pattern as watchdog's `on_warning`/`on_trigger` callbacks)
- In `run_daemon()`, wire it to `execute_actions()` with peer context variables
- The callback fires once per down episode (guarded by `_alerted_peers`); clears when peer comes back up

## Section 4: Daemon Mode Flag

### Replace the Stub

When `phm run -d/--daemon` is passed:

1. If systemd service is installed, print a suggestion to use `phm service start` instead and exit.
2. Otherwise, classic Unix double-fork daemonization:
   - First fork: parent exits
   - `os.setsid()` to create new session
   - Second fork: parent exits (prevents terminal reacquisition)
   - Redirect stdin to `/dev/null`, stdout/stderr to `~/.posthumous/logs/daemon.log`
   - Write PID to `~/.posthumous/posthumous.pid`
   - Run the event loop

### Stop Command

`phm run --stop`:
- Read PID from `~/.posthumous/posthumous.pid`
- Send `SIGTERM`
- Wait up to 10s for process to exit
- Clean up PID file
- If process doesn't exit, report and suggest `kill -9`

### Design Note

The systemd path (`phm service install`) is recommended for production. The `--daemon` flag is a convenience for ad-hoc use or non-systemd environments. Both paths run the same `run_daemon()` coroutine.

## Dependency Impact

No new runtime dependencies. sd_notify uses the Unix socket protocol directly (write `READY=1\n` to `$NOTIFY_SOCKET`). The double-fork daemon uses `os.fork()` + `os.setsid()` (stdlib).

## Files

### New Files
- `src/posthumous/systemd.py` — sd_notify socket protocol, unit file generation, systemd detection

### Modified Files
- `src/posthumous/cli.py` — `service` subgroup (install/uninstall/status/logs), `recover` command, daemon mode, sd_notify calls in `run_daemon()`
- `src/posthumous/peers.py` — `on_peer_down` callback in health check loop
- `src/posthumous/server.py` — HMAC auth on `GET /sync/state`
- `src/posthumous/config.py` — `on_peer_down` config field parsing
- `src/posthumous/notifications.py` — peer template variables in `build_context()`

### New Test Files
- `tests/test_systemd.py`

### Modified Test Files
- `tests/test_cli.py` — service commands, recover command, daemon mode
- `tests/test_peers.py` — peer down callback, state recovery integration
- `tests/test_server.py` — `/sync/state` authentication
- `tests/test_config.py` — `on_peer_down` parsing

## Test Plan

- **systemd.py:** Test sd_notify writes to mock socket, test unit file generation with correct paths, test no-op when `$NOTIFY_SOCKET` absent
- **service commands:** Test install generates correct unit file, test uninstall cleans up, test status/logs delegate to systemctl/journalctl
- **peer recovery:** Test `sync_state_from_peers()` called on corrupt state, test successful recovery, test fallback to fresh state when no peers
- **recover command:** Test diff display, test `--force` flag, test with unreachable peers
- **peer down notifications:** Test callback fires at threshold, test no repeat alerts, test clears on peer recovery
- **`/sync/state` auth:** Test unsigned request rejected, test stale timestamp rejected, test valid auth accepted
- **daemon mode:** Test PID file written, test `--stop` sends SIGTERM, test suggestion when systemd service exists
