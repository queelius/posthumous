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
- `config.py`: YAML config loading, duration parsing ("7 days", "12 hours"), validation. `Config` dataclass with `from_yaml()`, `to_dict()`, `validate()`. Nested `QuorumConfig(required, window_seconds)` is optional and only present when v0.7 quorum is enabled.
- `state.py`: atomic YAML persistence (temp file + `os.replace`), `Status` enum (`ARMED`, `WARNING`, `GRACE`, `PENDING_QUORUM`, `TRIGGERED`), `StateManager` with lazy loading. `State` dataclass tracks check-ins, failures, peer states, schedule dedup. `PeerState.alerted_at` persists the peer-down notification guard across restarts. `transition()` enforces a `valid_transitions` table; anything else raises.
- `dsl.py`: when-expression parser for scheduling. `ScheduleType` enum covers trigger-relative, recurring, anniversary, absolute-once, absolute-recurring. Key functions: `parse_when_expression()`, `get_next_occurrence()`, `should_execute()`, `get_period_key()`.
- `notifications.py`: Apprise wrapper with retry logic (3x, 5s between). Template formatting with `{days_left}`, `{hours_left}`, `{node_name}`, etc. Runs Apprise via `run_in_executor` (sync library).
- `scripts.py`: async subprocess execution. `ScriptContext` exports `POSTHUMOUS_*` env vars and creates a temp JSON context file (auto-cleaned). 300s default timeout.
- `auth.py`: TOTP via pyotp, API token verification, HMAC-SHA256 message signing for peer auth (`sign_message` / `verify_signature`), brute-force lockout tracking. `Authenticator` class, `LockedOutError` / `AuthError` exceptions.
- `crypto.py`: encryption at rest for `config.yaml` and `state.yaml`. PBKDF2-HMAC-SHA256 (600k iterations, 16-byte random salt) wraps a Fernet key. Two on-disk formats: `PHM_ENC_v1` (legacy bare SHA-256 KDF) and `PHM_ENC_v2` (PBKDF2 with embedded salt). `decrypt_file()` auto-migrates v1 to v2 on read. All writes go through atomic `mkstemp` + `os.replace`.

**Integration** (compose foundation modules):
- `watchdog.py`: async timer loop (60s check interval). `_transition_through(*statuses)` fires callbacks in sequence when catching up after downtime. Branches on `config.quorum`: when set, the path to TRIGGERED routes via `Status.PENDING_QUORUM` and delegates to a `QuorumCoordinator` injected at construction time.
- `scheduler.py`: post-trigger action engine. Parses when-expressions via `dsl.py`, tracks execution with period-based dedup keys (e.g., "2026", "2026-W05"). Only runs when TRIGGERED.
- `peers.py`: HMAC-signed broadcasts to federated peers via `_broadcast_to_all()` (concurrent). Handles check-in sync, trigger propagation (with optional v0.7 confirmation bundle), `broadcast_trigger_intent()` for vote collection, scheduled item completion. `pick_best_peer()` chooses a healthy peer for state recovery. Background `_health_check_once()` loop drives the peer-down alert path.
- `quorum.py`: v0.7 M-of-N triggering protocol (pure logic, no I/O of its own). `QuorumCoordinator.attempt_trigger()` mints an `intent_id`, broadcasts a signed intent via `peer_manager`, collects signed confirmations within `quorum.window_seconds` (enforced by `asyncio.wait_for`), and only broadcasts the actual trigger if it can assemble at least `required` confirmations. Re-checks state is still `PENDING_QUORUM` before broadcasting (handles check-ins that landed mid-vote). `verify_confirmation_bundle()` validates inbound trigger bundles: count, freshness (`max_age_seconds`), peer-URL membership against `config.peers`, dedup, signatures.
- `server.py`: aiohttp server: dark-themed web check-in form (CSRF double-submit cookie), JSON API, peer sync endpoints (`/sync/checkin`, `/sync/trigger`, `/sync/trigger_intent`, `/sync/scheduled`, `/sync/state`). `handle_sync_trigger` requires a valid confirmation bundle when quorum is configured.
- `systemd.py`: sd_notify protocol (`READY=1`, `WATCHDOG=1`, `STOPPING=1`) over the `NOTIFY_SOCKET` Unix datagram socket. `generate_unit_file()` produces a user-mode `posthumous.service` unit (Type=notify, WatchdogSec=30); rejects paths containing whitespace because systemd's ExecStart parser has no shell-style quoting.
- `runner.py`: `DaemonRunner` owns the async lifecycle. `attempt_state_recovery()` (peer recovery for corrupt state), `build_components()` (wires state, notifications, scripts, auth, peers, quorum, watchdog, scheduler, server), `execute_actions()` (shared by the warning / grace / trigger / peer-down callbacks), and `run()` (signal handlers, sd_notify heartbeat task at 15s, ordered shutdown, PID file cleanup).

**CLI** (`cli.py`):
- Thin wrapper around `runner.DaemonRunner`. Click-based, with subcommands: `init`, `config` (`path` / `show` / `validate` / `edit`), `run` (with `--daemon` double-fork and `--stop`), `service` (`install` / `uninstall` / `status` / `logs`), `checkin`, `status`, `reset`, `recover`, `peers`, `test-notify`, `test-trigger`, `export`, `import`. `_load_config_or_exit()` centralises the config-load-or-die path.

### State Machine

Check-ins reset to ARMED from any pre-trigger state. TRIGGERED is terminal: no check-in can undo it.

Without quorum (default):

```
ARMED ──timeout──► WARNING ──timeout──► GRACE ──timeout──► TRIGGERED
  ▲                   │                   │                    │
  └───── check-in ────┴───── check-in ────┘                    ▼
                                                          (scheduler runs forever)
```

With quorum (`config.quorum` set, v0.7+):

```
ARMED ─► WARNING ─► GRACE ─► PENDING_QUORUM ─► TRIGGERED
  ▲         │          │          │   │            │
  └─checkin─┴────checkin──────────┘   └─quorum─────┘
                                       fails / aborts back to GRACE or ARMED
```

`PENDING_QUORUM` is entered only after GRACE times out and only when `config.quorum` is set. The coordinator stays there until it can assemble >= `required` confirmations within `window_seconds`, then transitions to TRIGGERED and broadcasts the bundle to peers. A check-in landing mid-vote causes `attempt_trigger()` to abort before broadcast.

### Key Design Decisions

- **Atomic writes**: state uses `tempfile.mkstemp()` + `os.replace()` in the same directory for crash-safe persistence. The same pattern is used for encrypted files in `crypto.py`.
- **Catch-up transitions**: if a node was offline and missed WARNING, the watchdog fires all intermediate callbacks in order before reaching the current state.
- **Federation bias**: failure mode is duplicates (annoying) not silence (catastrophic). Multiple nodes may fire the same action; dedup keys prevent repeats on the same node. Quorum (v0.7) opts in to "many false negatives, no false positives" instead.
- **Async throughout**: watchdog, scheduler, peer health, and script execution all use asyncio. Apprise (synchronous) runs via `run_in_executor`.
- **UTC everywhere**: all timestamps use `datetime.now(timezone.utc)`. Time arithmetic uses `timedelta` and `dateutil.relativedelta` for month / year offsets in the DSL.
- **Lifecycle ownership**: `runner.DaemonRunner` owns the event loop, components, and shutdown order. `cli.py` only deals with CLI-shaped concerns (argv, daemonization fork, stop signal). When adding a new background task or callback, wire it in `runner.py`, not `cli.py`.
- **Encryption at rest**: opt-in via `encryption_secret` (or `secret_key` fallback). On-disk format is self-identifying via the `PHM_ENC_v*` magic header. Reading a v1 file rewrites it as v2 with a fresh salt, so a "read" is also a quiet write; mind file watchers in tests.
- **Quorum security caveat (v0.7)**: all federation peers share a single `secret_key`, so the M-of-N quorum claim degrades to "any single secret holder can forge a bundle." This is documented in the README under "Threat Model and Known Limitations" and is the target of v0.8 (per-peer identity keys).

### Test Conventions

- Async tests use `@pytest.mark.asyncio` with `asyncio_mode = "auto"` (auto-detect, no explicit marker needed)
- Watchdog / scheduler / quorum callbacks are tested with `AsyncMock`
- Peer tests mock `aiohttp.ClientSession` (must use `AsyncMock` for `.close()`)
- Time-dependent tests use `freezegun` to mock `datetime.now`
- `aioresponses` mocks HTTP calls in peer / server tests
- State-transition tests must respect `valid_transitions`: to test a TRIGGERED-state behavior, pre-set the state to GRACE (or PENDING_QUORUM, with quorum) first, then transition forward; direct ARMED to TRIGGERED is rejected by the state machine.
- A few server / peer tests bind to a fixed port (8420) and can flake when run in parallel or under port contention. Prefer ephemeral ports (`listen="127.0.0.1:0"`) for new tests.
- Shared fixtures in `tests/conftest.py`: `tmp_config_dir` (creates `~/.posthumous/` structure in `tmp_path`), `sample_config_yaml` (writes a valid config file).
