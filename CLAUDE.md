# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Test Commands

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run all tests (coverage is auto-enabled via pyproject.toml)
pytest

# Run a single test file / class / test
pytest tests/test_watchdog.py
pytest tests/test_auth.py::TestTOTP
pytest tests/test_state.py::TestState::test_transition_armed_to_warning

# Run tests matching a pattern
pytest -k "lockout"

# CLI entry points (both are equivalent)
posthumous --help
phm --help

# Daemon control (v0.6+)
posthumous run --daemon         # double-fork, write PID file
posthumous run --stop           # signal the daemonized process
posthumous service install      # write ~/.config/systemd/user/posthumous.service and enable it
posthumous service status       # systemctl --user status posthumous
posthumous service logs -f      # journalctl --user -u posthumous -f
posthumous recover              # rebuild a corrupt state.yaml from a peer
```

No linter is configured. Build system is Hatchling (`pyproject.toml`). Python 3.10+.

## Architecture

Posthumous is a federated deadman switch. Users check in periodically via TOTP; if they stop, the system progresses through ARMED, WARNING, GRACE, TRIGGERED, sending notifications and running scripts at each stage. After trigger, a scheduler runs recurring post-trigger actions forever.

### Source Layout

```
src/posthumous/     # Package source (hatch wheel target)
tests/              # One test module per source module
```

Runtime files live in `~/.posthumous/`: `config.yaml`, `state.yaml`, `scripts/`, `logs/`.

### Module Layers

**Foundation** (no internal imports):
- `config.py`: YAML config loading, duration parsing ("7 days", "12 hours"), validation. `Config` dataclass with `from_yaml()`, `to_dict()`, `validate()`.
- `state.py`: atomic YAML persistence (temp file + `os.replace`), `Status` enum (`ARMED`, `WARNING`, `GRACE`, `TRIGGERED`), `StateManager` with lazy loading. `State` dataclass tracks check-ins, failures, peer states, schedule dedup. `PeerState.alerted_at` persists the peer-down notification guard across restarts. `transition()` enforces a `valid_transitions` table; anything else returns False without raising.
- `dsl.py`: when-expression parser for scheduling. `ScheduleType` enum covers trigger-relative, recurring, anniversary, absolute-once, absolute-recurring. Key functions: `parse_when_expression()`, `get_next_occurrence()`, `should_execute()`, `get_period_key()`.
- `notifications.py`: Apprise wrapper with retry logic (3x, 5s between). Template formatting with `{days_left}`, `{hours_left}`, `{node_name}`, `{healthy_peers}`, `{total_peers}`, `{dead_peers}`. Runs Apprise via `run_in_executor` (sync library).
- `scripts.py`: async subprocess execution. `ScriptContext` exports `POSTHUMOUS_*` env vars and creates a temp JSON context file (auto-cleaned). 300s default timeout.
- `auth.py`: TOTP via pyotp, API token verification, HMAC-SHA256 message signing for peer auth (`sign_message` / `verify_signature`), brute-force lockout tracking. `Authenticator` class, `LockedOutError` / `AuthError` exceptions.
- `crypto.py`: encryption at rest for `config.yaml` and `state.yaml`. PBKDF2-HMAC-SHA256 (600k iterations, 16-byte random salt) wraps a Fernet key. Two on-disk formats: `PHM_ENC_v1` (legacy bare SHA-256 KDF) and `PHM_ENC_v2` (PBKDF2 with embedded salt). `decrypt_file()` auto-migrates v1 to v2 on read. All writes go through atomic `mkstemp` + `os.replace`.

**Integration** (compose foundation modules):
- `watchdog.py`: async timer loop (60s check interval). `_transition_through(*statuses)` fires callbacks in sequence when catching up after downtime; the path to TRIGGERED is `WARNING -> GRACE -> TRIGGERED` without intermediate consensus.
- `scheduler.py`: post-trigger action engine. Parses when-expressions via `dsl.py`, tracks execution with period-based dedup keys (e.g., "2026", "2026-W05"). Only runs when TRIGGERED.
- `peers.py`: HMAC-signed broadcasts to federated peers via `_broadcast_to_all()` (concurrent). Handles check-in sync, trigger propagation, scheduled item completion. `pick_best_peer()` chooses a healthy peer for state recovery. `merge_state_from_peers()` (v0.8) queries every peer and merges last_checkin / status / trigger_time / schedule_state on startup so a returning offline node does not false-fire from stale local state. Background `_health_check_once()` loop drives the peer-down alert path.
- `server.py`: aiohttp server: dark-themed web check-in form (CSRF double-submit cookie), JSON API, peer sync endpoints (`/sync/checkin`, `/sync/trigger`, `/sync/scheduled`, `/sync/state`).
- `systemd.py`: sd_notify protocol (`READY=1`, `WATCHDOG=1`, `STOPPING=1`) over the `NOTIFY_SOCKET` Unix datagram socket. `generate_unit_file()` produces a user-mode `posthumous.service` unit (Type=notify, WatchdogSec=30); rejects paths containing whitespace because systemd's ExecStart parser has no shell-style quoting.
- `runner.py`: `DaemonRunner` owns the async lifecycle. `attempt_state_recovery()` runs on every startup: full peer recovery if state.yaml is corrupt, otherwise `merge_state_from_peers()` to catch up on check-ins / triggers missed while offline. `build_components()` (wires state, notifications, scripts, auth, peers, watchdog, scheduler, server), `execute_actions()` (shared by the warning / grace / trigger / peer-down callbacks), and `run()` (signal handlers, sd_notify heartbeat task at 15s, ordered shutdown, PID file cleanup).

**CLI** (`cli.py`):
- Thin wrapper around `runner.DaemonRunner`. Click-based, with subcommands: `init`, `config` (`path` / `show` / `validate` / `edit`), `run` (with `--daemon` double-fork and `--stop`), `service` (`install` / `uninstall` / `status` / `logs`), `checkin`, `status`, `reset`, `recover`, `peers`, `test-notify`, `test-trigger`, `export`, `import`. `_load_config_or_exit()` centralises the config-load-or-die path.
- `checkin` (v0.8): tries `POST http://<config.listen>/checkin` first so the running daemon owns auth + state mutation + broadcast. On connection refused, falls back to direct state write plus a one-shot `PeerManager.broadcast_checkin`. Eliminates the pre-v0.8 split-brain where the CLI wrote to `state.yaml` while the daemon held a stale in-memory copy.

### State Machine

Check-ins reset to ARMED from any pre-trigger state. TRIGGERED is terminal: no check-in can undo it.

```
ARMED ──timeout──► WARNING ──timeout──► GRACE ──timeout──► TRIGGERED
  ▲                   │                   │                    │
  └───── check-in ────┴───── check-in ────┘                    ▼
                                                          (scheduler runs forever)
```

`valid_transitions` (in `state.py`):

```
ARMED     -> {WARNING}
WARNING   -> {ARMED, GRACE}
GRACE     -> {ARMED, TRIGGERED}
TRIGGERED -> {}        # terminal
```

Check-in is a separate reset path (ARMED from any non-TRIGGERED state). Direct ARMED -> TRIGGERED is rejected; tests must pre-position via GRACE.

### Key Design Decisions

- **Atomic writes**: state uses `tempfile.mkstemp()` + `os.replace()` in the same directory for crash-safe persistence. The same pattern is used for encrypted files in `crypto.py`.
- **Catch-up transitions**: if a node was offline and missed WARNING, the watchdog fires all intermediate callbacks in order before reaching the current state.
- **Federation bias is suppression-resistance**: failure mode is duplicates (annoying) not silence (catastrophic). Any single node can fire the trigger independently; dedup keys prevent repeats on the same node. v0.8 deliberately removed the v0.7 quorum protocol because its fail-closed semantics worked against this property.
- **Startup peer-state pull**: every daemon startup queries peers and merges newer `last_checkin`, TRIGGERED status, and union of completed schedule periods. Closes the stale-state false-alarm window for a returning offline node.
- **CLI prefers the daemon**: `phm checkin` POSTs to the running daemon's HTTP endpoint to keep state mutation and peer broadcast on a single code path. The direct fallback (when daemon is down) is intentional and preserves suppression-resistance.
- **Async throughout**: watchdog, scheduler, peer health, and script execution all use asyncio. Apprise (synchronous) runs via `run_in_executor`. urllib (synchronous) is used for the CLI -> daemon HTTP call because the CLI is not async.
- **UTC everywhere**: all timestamps use `datetime.now(timezone.utc)`. Time arithmetic uses `timedelta` and `dateutil.relativedelta` for month / year offsets in the DSL.
- **Lifecycle ownership**: `runner.DaemonRunner` owns the event loop, components, and shutdown order. `cli.py` only deals with CLI-shaped concerns (argv, daemonization fork, stop signal, the daemon-or-direct routing decision). When adding a new background task or callback, wire it in `runner.py`, not `cli.py`.
- **Encryption at rest**: opt-in via `encryption_secret` (or `secret_key` fallback). On-disk format is self-identifying via the `PHM_ENC_v*` magic header. Reading a v1 file rewrites it as v2 with a fresh salt, so a "read" is also a quiet write; mind file watchers in tests.

### Test Conventions

- Async tests use `@pytest.mark.asyncio` with `asyncio_mode = "auto"` (auto-detect, no explicit marker needed)
- Watchdog / scheduler callbacks are tested with `AsyncMock`
- Peer tests mock `aiohttp.ClientSession` (must use `AsyncMock` for `.close()`)
- Time-dependent tests use `freezegun` to mock `datetime.now`
- `aioresponses` mocks HTTP calls in peer / server tests. Mocks for endpoints called with query params (`/sync/state?ts=...&sig=...`) need `re.compile(...)` rather than a plain URL.
- State-transition tests must respect `valid_transitions`: to test a TRIGGERED-state behavior, pre-set the state to GRACE first, then transition forward; direct ARMED to TRIGGERED is rejected by the state machine.
- A few server / peer tests bind to a fixed port (8420) and can flake when run in parallel or under port contention. Prefer ephemeral ports (`listen="127.0.0.1:0"`) for new tests.
- CLI checkin tests should mock `posthumous.cli._try_daemon_checkin` to control the daemon-up vs daemon-down branch deterministically.
- Shared fixtures in `tests/conftest.py`: `tmp_config_dir` (creates `~/.posthumous/` structure in `tmp_path`), `sample_config_yaml` (writes a valid config file).
