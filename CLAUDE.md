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
```

No linter is configured. Build system is Hatchling (`pyproject.toml`). Python 3.10+.

## Architecture

Posthumous is a federated deadman switch. Users check in periodically via TOTP; if they stop, the system progresses through ARMED → WARNING → GRACE → TRIGGERED, sending notifications and running scripts at each stage. After trigger, a scheduler runs recurring post-trigger actions forever.

### Source Layout

```
src/posthumous/     # Package source (hatch wheel target)
tests/              # One test module per source module
```

Runtime files live in `~/.posthumous/`: `config.yaml`, `state.yaml`, `scripts/`, `logs/`.

### Module Layers

**Foundation** (no internal imports):
- `config.py` — YAML config loading, duration parsing ("7 days", "12 hours"), validation. `Config` dataclass with `from_yaml()`, `to_dict()`, `validate()`.
- `state.py` — Atomic YAML persistence (temp file + `os.replace`), `Status` enum, `StateManager` with lazy loading. `State` dataclass tracks check-ins, failures, peer states, schedule dedup.
- `dsl.py` — When-expression parser for scheduling. `ScheduleType` enum covers trigger-relative, recurring, anniversary, absolute-once, absolute-recurring. Key functions: `parse_when_expression()`, `get_next_occurrence()`, `should_execute()`, `get_period_key()`.
- `notifications.py` — Apprise wrapper with retry logic (3x, 5s between). Template formatting with `{days_left}`, `{hours_left}`, `{node_name}`, etc. Runs Apprise via `run_in_executor` (sync library).
- `scripts.py` — Async subprocess execution. `ScriptContext` exports `POSTHUMOUS_*` env vars and creates a temp JSON context file (auto-cleaned). 300s default timeout.
- `auth.py` — TOTP via pyotp, API token verification, HMAC-SHA256 message signing for peer auth, brute-force lockout tracking. `Authenticator` class, `LockedOutError`/`AuthError` exceptions.

**Integration** (compose foundation modules):
- `watchdog.py` — Async timer loop (60s check interval). `_transition_through(*statuses)` fires callbacks in sequence when catching up after downtime.
- `scheduler.py` — Post-trigger action engine. Parses when-expressions via `dsl.py`, tracks execution with period-based dedup keys (e.g., "2026", "2026-W05"). Only runs when TRIGGERED.
- `peers.py` — HMAC-signed broadcasts to federated peers via `_broadcast_to_all()` (concurrent). Handles check-in sync, trigger propagation, scheduled item completion. Background health check loop.
- `server.py` — aiohttp server: dark-themed web check-in form, JSON API, peer sync endpoints (`/sync/checkin`, `/sync/trigger`, `/sync/scheduled`, `/sync/state`).

**CLI** (`cli.py`):
- Wires all components together in the `run` command. Uses shared `execute_actions()` for warning/grace/trigger callbacks. Click-based with subcommands: `init`, `config`, `run`, `checkin`, `status`, `peers`, `test-notify`, `test-trigger`, `export`, `import`.

### State Machine

Check-ins reset to ARMED from any pre-trigger state. TRIGGERED is terminal — no check-in can undo it.

```
ARMED ──timeout──► WARNING ──timeout──► GRACE ──timeout──► TRIGGERED
  ▲                   │                   │                    │
  └───── check-in ────┴───── check-in ────┘                    ▼
                                                          (scheduler runs forever)
```

### Key Design Decisions

- **Atomic writes**: State uses `tempfile.mkstemp()` + `os.replace()` in the same directory for crash-safe persistence.
- **Catch-up transitions**: If a node was offline and missed WARNING, the watchdog fires all intermediate callbacks in order before reaching the current state.
- **Federation bias**: Failure mode is duplicates (annoying) not silence (catastrophic). Multiple nodes may fire the same action; dedup keys prevent repeats on the same node.
- **Async throughout**: Watchdog, scheduler, peer health, and script execution all use asyncio. Apprise (synchronous) runs via `run_in_executor`.
- **UTC everywhere**: All timestamps use `datetime.now(timezone.utc)`. Time arithmetic uses `timedelta` and `dateutil.relativedelta` for month/year offsets in the DSL.

### Test Conventions

- Async tests use `@pytest.mark.asyncio` with `asyncio_mode = "auto"` (auto-detect, no explicit marker needed)
- Watchdog/scheduler callbacks are tested with `AsyncMock`
- Peer tests mock `aiohttp.ClientSession` (must use `AsyncMock` for `.close()`)
- Time-dependent tests use `freezegun` to mock `datetime.now`
- `aioresponses` mocks HTTP calls in peer/server tests
- Shared fixtures in `tests/conftest.py`: `tmp_config_dir` (creates `~/.posthumous/` structure in `tmp_path`), `sample_config_yaml` (writes a valid config file)
