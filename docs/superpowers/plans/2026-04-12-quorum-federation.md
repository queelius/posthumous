# Quorum Federation (v0.7) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement M-of-N quorum-based triggering so a single compromised peer cannot fire the deadman switch alone. Opt-in via `quorum:` config block; absent = v0.6 behavior.

**Architecture:** Add a `Status.PENDING_QUORUM` state between GRACE and TRIGGERED. When grace times out and quorum is configured, broadcast an intent to peers, collect signed confirmation votes, and only proceed to TRIGGERED if M votes (including self) arrive within the window. Confirmation bundle travels with `/sync/trigger` so any peer can independently verify quorum was reached.

**Tech Stack:** Python 3.10+, asyncio, aiohttp, click, HMAC-SHA256, UUID4 (no new dependencies).

**Spec:** `docs/superpowers/specs/2026-04-12-quorum-federation-design.md`

**Baseline:** v0.6.0 (commit `2a9a3c0`), 704 tests passing.

---

## File Map

| File | Role | Action |
|------|------|--------|
| `src/posthumous/state.py` | Add `Status.PENDING_QUORUM`, update transition table | Modify |
| `src/posthumous/config.py` | Add `QuorumConfig` dataclass + parsing + validation | Modify |
| `src/posthumous/quorum.py` | New `QuorumCoordinator` class with `attempt_trigger()` and `verify_confirmation_bundle()` | Create |
| `src/posthumous/peers.py` | Add `broadcast_trigger_intent()` returning per-peer vote responses; update `broadcast_trigger()` to optionally include bundle | Modify |
| `src/posthumous/server.py` | Add `handle_sync_trigger_intent`; modify `handle_sync_trigger` to verify bundle when present | Modify |
| `src/posthumous/watchdog.py` | Branch on `config.quorum` in TRIGGERED transition path | Modify |
| `src/posthumous/runner.py` | Wire `QuorumCoordinator` into `DaemonRunner.build_components()`, pass to Watchdog | Modify |
| `tests/test_quorum.py` | Unit tests for `QuorumCoordinator` and bundle verification | Create |
| `tests/test_state.py` | Tests for `PENDING_QUORUM` state and transitions | Modify |
| `tests/test_config.py` | Tests for `quorum:` parsing and validation | Modify |
| `tests/test_watchdog.py` | Tests for grace-timeout branching with/without quorum | Modify |
| `tests/test_server.py` | Tests for `/sync/trigger_intent` and bundle-verified `/sync/trigger` | Modify |
| `tests/test_runner.py` | Integration test: full quorum scenario with `aioresponses` | Modify |

---

## Chunk 1: State Machine + Config

### Task 1: Add `Status.PENDING_QUORUM` and update transitions

**Files:**
- Modify: `src/posthumous/state.py:34-39` (Status enum) and `src/posthumous/state.py:272-276` (valid_transitions)
- Modify: `tests/test_state.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_state.py`:

```python
class TestPendingQuorumState:
    """Tests for the PENDING_QUORUM state added in v0.7."""

    def test_pending_quorum_status_exists(self):
        from posthumous.state import Status
        assert Status.PENDING_QUORUM.value == "pending_quorum"

    def test_grace_can_transition_to_pending_quorum(self):
        from posthumous.state import State, Status
        state = State()
        state.status = Status.GRACE
        assert state.transition_to(Status.PENDING_QUORUM) is True
        assert state.status == Status.PENDING_QUORUM

    def test_pending_quorum_can_transition_to_triggered(self):
        from posthumous.state import State, Status
        state = State()
        state.status = Status.PENDING_QUORUM
        assert state.transition_to(Status.TRIGGERED) is True
        assert state.status == Status.TRIGGERED

    def test_pending_quorum_can_transition_back_to_grace(self):
        """If quorum fails, return to GRACE for retry."""
        from posthumous.state import State, Status
        state = State()
        state.status = Status.PENDING_QUORUM
        assert state.transition_to(Status.GRACE) is True
        assert state.status == Status.GRACE

    def test_pending_quorum_can_be_reset_by_checkin(self):
        """A check-in during PENDING_QUORUM aborts the trigger attempt."""
        from posthumous.state import State, Status
        state = State()
        state.status = Status.PENDING_QUORUM
        assert state.transition_to(Status.ARMED) is True
        assert state.status == Status.ARMED

    def test_armed_cannot_skip_directly_to_pending_quorum(self):
        """PENDING_QUORUM is only reachable from GRACE."""
        from posthumous.state import State, Status
        state = State()
        state.status = Status.ARMED
        assert state.transition_to(Status.PENDING_QUORUM) is False
        assert state.status == Status.ARMED
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_state.py::TestPendingQuorumState -v`
Expected: FAIL with `AttributeError: PENDING_QUORUM` or transition rejected.

- [ ] **Step 3: Implement**

In `src/posthumous/state.py:34-39`, change the enum:

```python
class Status(Enum):
    """Current status of the deadman switch."""
    ARMED = "armed"
    WARNING = "warning"
    GRACE = "grace"
    PENDING_QUORUM = "pending_quorum"
    TRIGGERED = "triggered"
```

In `src/posthumous/state.py:272-276`, update `valid_transitions`:

```python
        valid_transitions = {
            Status.ARMED: {Status.WARNING},
            Status.WARNING: {Status.ARMED, Status.GRACE},
            Status.GRACE: {Status.ARMED, Status.PENDING_QUORUM, Status.TRIGGERED},
            Status.PENDING_QUORUM: {Status.ARMED, Status.GRACE, Status.TRIGGERED},
            Status.TRIGGERED: set(),  # No transitions out of TRIGGERED
        }
```

The check at line 282 (`if new_status == Status.ARMED and self.status != Status.TRIGGERED`) already handles check-in from any non-terminal state, so no change needed there.

- [ ] **Step 4: Verify tests pass**

Run: `pytest tests/test_state.py -v`
Expected: All pass (existing 100+ state tests + 6 new ones).

- [ ] **Step 5: Commit**

```bash
git add src/posthumous/state.py tests/test_state.py
git commit -m "feat(state): add PENDING_QUORUM status and transitions"
```

---

### Task 2: `QuorumConfig` dataclass + parsing

**Files:**
- Modify: `src/posthumous/config.py` (add `QuorumConfig` dataclass, add to `Config`, parse in `from_dict`, serialize in `to_dict`, validate)
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_config.py`:

```python
class TestQuorumConfig:
    """Tests for the optional quorum config block (v0.7)."""

    def test_quorum_default_is_none(self):
        from posthumous.config import Config
        cfg = Config(node_name="test", secret_key="JBSWY3DPEHPK3PXP")
        assert cfg.quorum is None

    def test_parse_quorum_block(self, tmp_path):
        import yaml
        from posthumous.config import Config
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump({
            "node_name": "test",
            "secret_key": "JBSWY3DPEHPK3PXP",
            "checkin_interval": "7 days",
            "warning_start": "8 days",
            "grace_start": "12 days",
            "trigger_at": "14 days",
            "peers": ["http://peer1:8420", "http://peer2:8420"],
            "quorum": {"required": 2, "window_seconds": 45},
        }))
        cfg = Config.from_yaml(path)
        assert cfg.quorum is not None
        assert cfg.quorum.required == 2
        assert cfg.quorum.window_seconds == 45

    def test_quorum_window_default_is_30(self, tmp_path):
        import yaml
        from posthumous.config import Config
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump({
            "node_name": "test",
            "secret_key": "JBSWY3DPEHPK3PXP",
            "checkin_interval": "7 days",
            "warning_start": "8 days",
            "grace_start": "12 days",
            "trigger_at": "14 days",
            "peers": ["http://peer1:8420", "http://peer2:8420"],
            "quorum": {"required": 2},
        }))
        cfg = Config.from_yaml(path)
        assert cfg.quorum.window_seconds == 30

    def test_to_dict_round_trips_quorum(self):
        from posthumous.config import Config, QuorumConfig
        cfg = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            peers=["http://peer1:8420"],
            quorum=QuorumConfig(required=2, window_seconds=20),
        )
        data = cfg.to_dict()
        assert data["quorum"] == {"required": 2, "window_seconds": 20}

    def test_validate_quorum_required_must_be_at_least_one(self):
        from posthumous.config import Config, QuorumConfig
        cfg = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            peers=["http://peer1:8420"],
            quorum=QuorumConfig(required=0, window_seconds=30),
        )
        errors = cfg.validate()
        assert any("quorum.required" in e for e in errors)

    def test_validate_quorum_required_cannot_exceed_federation_size(self):
        from posthumous.config import Config, QuorumConfig
        cfg = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            peers=["http://peer1:8420"],  # 1 peer + self = 2
            quorum=QuorumConfig(required=5, window_seconds=30),
        )
        errors = cfg.validate()
        assert any("exceeds federation size" in e for e in errors)

    def test_validate_quorum_window_seconds_must_be_positive(self):
        from posthumous.config import Config, QuorumConfig
        cfg = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            peers=["http://peer1:8420"],
            quorum=QuorumConfig(required=2, window_seconds=0),
        )
        errors = cfg.validate()
        assert any("window_seconds" in e for e in errors)
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_config.py::TestQuorumConfig -v`
Expected: FAIL: `QuorumConfig` undefined.

- [ ] **Step 3: Implement `QuorumConfig` dataclass**

Add to `src/posthumous/config.py` after the existing `ScheduledItem` dataclass:

```python
@dataclass
class QuorumConfig:
    """Quorum requirements for triggering (v0.7).

    When set on a Config, the watchdog will not transition to TRIGGERED
    on its own. Instead it broadcasts an intent and requires `required`
    confirmations from peers (counting self) within `window_seconds`.
    """
    required: int = 1
    window_seconds: int = 30
```

- [ ] **Step 4: Add field to `Config` dataclass**

In `src/posthumous/config.py`, add to the `Config` dataclass field list (placed after the existing `peer_down_threshold` field, near the bottom of the field declarations):

```python
    # Quorum (v0.7)
    quorum: QuorumConfig | None = None
```

- [ ] **Step 5: Parse in `from_dict`**

In `src/posthumous/config.py`'s `from_dict()`, add quorum parsing before the final `return cls(...)` call:

```python
        quorum_data = data.get('quorum')
        quorum = None
        if quorum_data is not None:
            quorum = QuorumConfig(
                required=quorum_data.get('required', 1),
                window_seconds=quorum_data.get('window_seconds', 30),
            )
```

Then pass `quorum=quorum,` in the `cls(...)` constructor call.

- [ ] **Step 6: Serialize in `to_dict`**

In `src/posthumous/config.py`'s `to_dict()`, add before the `return data` statement:

```python
        if self.quorum is not None:
            data['quorum'] = {
                'required': self.quorum.required,
                'window_seconds': self.quorum.window_seconds,
            }
```

- [ ] **Step 7: Validate**

In `src/posthumous/config.py`'s `validate()` method, add at the end (before `return errors`):

```python
        if self.quorum is not None:
            if self.quorum.required < 1:
                errors.append(f"quorum.required must be >= 1, got {self.quorum.required}")
            federation_size = len(self.peers) + 1  # +1 for self
            if self.quorum.required > federation_size:
                errors.append(
                    f"quorum.required ({self.quorum.required}) exceeds federation size ({federation_size})"
                )
            if self.quorum.window_seconds <= 0:
                errors.append(f"quorum.window_seconds must be > 0, got {self.quorum.window_seconds}")
```

- [ ] **Step 8: Run tests**

Run: `pytest tests/test_config.py::TestQuorumConfig -v`
Expected: All 7 pass.

Run: `pytest tests/test_config.py -v`
Expected: All existing config tests still pass.

- [ ] **Step 9: Commit**

```bash
git add src/posthumous/config.py tests/test_config.py
git commit -m "feat(config): add optional QuorumConfig block with validation"
```

---

## Chunk 2: QuorumCoordinator (Pure Protocol Logic)

### Task 3: `QuorumCoordinator` skeleton + signature helpers

**Files:**
- Create: `src/posthumous/quorum.py`
- Create: `tests/test_quorum.py`

- [ ] **Step 1: Write failing tests for signature helpers**

Create `tests/test_quorum.py`:

```python
"""Tests for the QuorumCoordinator and confirmation-bundle protocol (v0.7)."""

from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from posthumous.config import Config, QuorumConfig
from posthumous.state import State, Status, StateManager
from posthumous.quorum import (
    QuorumCoordinator,
    sign_intent,
    sign_confirmation,
    verify_confirmation,
)


SECRET = "JBSWY3DPEHPK3PXP"


class TestSignatureHelpers:
    def test_sign_and_verify_confirmation(self):
        intent_id = "abc-123"
        timestamp = "2026-04-12T18:00:00+00:00"
        peer_url = "https://peer1.local:8420"
        sig = sign_confirmation(SECRET, intent_id, timestamp, peer_url)
        assert isinstance(sig, str) and len(sig) > 0
        assert verify_confirmation(SECRET, intent_id, timestamp, peer_url, sig) is True

    def test_verify_rejects_tampered_peer_url(self):
        intent_id = "abc-123"
        timestamp = "2026-04-12T18:00:00+00:00"
        sig = sign_confirmation(SECRET, intent_id, timestamp, "https://peer1.local:8420")
        assert verify_confirmation(SECRET, intent_id, timestamp, "https://attacker.local:8420", sig) is False

    def test_verify_rejects_wrong_secret(self):
        intent_id = "abc-123"
        timestamp = "2026-04-12T18:00:00+00:00"
        peer_url = "https://peer1.local:8420"
        sig = sign_confirmation(SECRET, intent_id, timestamp, peer_url)
        assert verify_confirmation("OTHERSECRETOTHERS", intent_id, timestamp, peer_url, sig) is False

    def test_sign_intent_is_deterministic(self):
        intent_id = "abc-123"
        timestamp = "2026-04-12T18:00:00+00:00"
        a = sign_intent(SECRET, intent_id, timestamp)
        b = sign_intent(SECRET, intent_id, timestamp)
        assert a == b
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_quorum.py::TestSignatureHelpers -v`
Expected: FAIL: `posthumous.quorum` does not exist.

- [ ] **Step 3: Implement signature helpers + skeleton**

Create `src/posthumous/quorum.py`:

```python
"""Quorum-based triggering protocol (v0.7).

A QuorumCoordinator runs the intent-broadcast / vote-collection /
trigger-with-bundle protocol described in
docs/superpowers/specs/2026-04-12-quorum-federation-design.md.

This module is pure protocol logic. All HTTP I/O happens through
the PeerManager passed in at construction time.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from posthumous.auth import sign_message, verify_signature

if TYPE_CHECKING:
    from posthumous.config import Config
    from posthumous.peers import PeerManager
    from posthumous.state import StateManager

logger = logging.getLogger(__name__)


def sign_intent(secret: str, intent_id: str, timestamp: str) -> str:
    """Sign the intent payload broadcast to peers."""
    return sign_message(secret, f"trigger_intent:{intent_id}:{timestamp}")


def verify_intent(secret: str, intent_id: str, timestamp: str, signature: str) -> bool:
    """Verify an intent signature."""
    return verify_signature(secret, f"trigger_intent:{intent_id}:{timestamp}", signature)


def sign_confirmation(secret: str, intent_id: str, timestamp: str, peer_url: str) -> str:
    """Sign a peer's confirmation vote."""
    return sign_message(secret, f"confirm:{intent_id}:{timestamp}:{peer_url}")


def verify_confirmation(
    secret: str, intent_id: str, timestamp: str, peer_url: str, signature: str
) -> bool:
    """Verify a peer's confirmation signature."""
    return verify_signature(
        secret, f"confirm:{intent_id}:{timestamp}:{peer_url}", signature
    )


@dataclass
class Confirmation:
    """A signed vote from a peer (or self) confirming the trigger intent."""
    peer_url: str
    signature: str


class QuorumCoordinator:
    """Coordinates the M-of-N quorum protocol for triggering.

    Responsibilities:
    - Generate a fresh intent_id when a trigger is attempted.
    - Broadcast the intent to peers via PeerManager.
    - Collect signed confirmation votes and tally them.
    - Verify confirmation bundles received via /sync/trigger from other nodes.
    """

    def __init__(
        self,
        config: "Config",
        state_manager: "StateManager",
        peer_manager: "PeerManager",
    ):
        self.config = config
        self.state_manager = state_manager
        self.peer_manager = peer_manager
```

- [ ] **Step 4: Verify signature tests pass**

Run: `pytest tests/test_quorum.py::TestSignatureHelpers -v`
Expected: 4 pass.

- [ ] **Step 5: Commit**

```bash
git add src/posthumous/quorum.py tests/test_quorum.py
git commit -m "feat(quorum): add QuorumCoordinator skeleton and signature helpers"
```

---

### Task 4: `verify_confirmation_bundle` method

**Files:**
- Modify: `src/posthumous/quorum.py`
- Modify: `tests/test_quorum.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_quorum.py`:

```python
class TestVerifyConfirmationBundle:
    """Tests for QuorumCoordinator.verify_confirmation_bundle()."""

    @pytest.fixture
    def config(self):
        return Config(
            node_name="self",
            secret_key=SECRET,
            peers=["https://peer1.local:8420", "https://peer2.local:8420"],
            quorum=QuorumConfig(required=2, window_seconds=30),
        )

    @pytest.fixture
    def coordinator(self, config, tmp_path):
        state_manager = StateManager(tmp_path / "state.yaml")
        peer_manager = MagicMock()
        return QuorumCoordinator(config, state_manager, peer_manager)

    def test_accepts_valid_bundle(self, coordinator):
        intent_id = "abc-123"
        timestamp = "2026-04-12T18:00:00+00:00"
        bundle = [
            Confirmation(
                peer_url="https://self.local:8420",
                signature=sign_confirmation(SECRET, intent_id, timestamp, "https://self.local:8420"),
            ),
            Confirmation(
                peer_url="https://peer1.local:8420",
                signature=sign_confirmation(SECRET, intent_id, timestamp, "https://peer1.local:8420"),
            ),
        ]
        assert coordinator.verify_confirmation_bundle(bundle, intent_id, timestamp) is True

    def test_rejects_too_few_confirmations(self, coordinator):
        intent_id = "abc-123"
        timestamp = "2026-04-12T18:00:00+00:00"
        bundle = [
            Confirmation(
                peer_url="https://self.local:8420",
                signature=sign_confirmation(SECRET, intent_id, timestamp, "https://self.local:8420"),
            ),
        ]
        assert coordinator.verify_confirmation_bundle(bundle, intent_id, timestamp) is False

    def test_rejects_duplicate_peer_urls(self, coordinator):
        intent_id = "abc-123"
        timestamp = "2026-04-12T18:00:00+00:00"
        sig = sign_confirmation(SECRET, intent_id, timestamp, "https://peer1.local:8420")
        bundle = [
            Confirmation(peer_url="https://peer1.local:8420", signature=sig),
            Confirmation(peer_url="https://peer1.local:8420", signature=sig),
        ]
        assert coordinator.verify_confirmation_bundle(bundle, intent_id, timestamp) is False

    def test_rejects_bad_signature(self, coordinator):
        intent_id = "abc-123"
        timestamp = "2026-04-12T18:00:00+00:00"
        bundle = [
            Confirmation(
                peer_url="https://self.local:8420",
                signature=sign_confirmation(SECRET, intent_id, timestamp, "https://self.local:8420"),
            ),
            Confirmation(peer_url="https://peer1.local:8420", signature="forged-signature"),
        ]
        assert coordinator.verify_confirmation_bundle(bundle, intent_id, timestamp) is False

    def test_rejects_signature_for_different_intent_id(self, coordinator):
        wrong_id = "wrong-id"
        right_id = "abc-123"
        timestamp = "2026-04-12T18:00:00+00:00"
        bundle = [
            Confirmation(
                peer_url="https://self.local:8420",
                signature=sign_confirmation(SECRET, right_id, timestamp, "https://self.local:8420"),
            ),
            Confirmation(
                peer_url="https://peer1.local:8420",
                signature=sign_confirmation(SECRET, wrong_id, timestamp, "https://peer1.local:8420"),
            ),
        ]
        assert coordinator.verify_confirmation_bundle(bundle, right_id, timestamp) is False
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_quorum.py::TestVerifyConfirmationBundle -v`
Expected: FAIL: method does not exist.

- [ ] **Step 3: Implement**

Add to `QuorumCoordinator` in `src/posthumous/quorum.py`:

```python
    def verify_confirmation_bundle(
        self,
        bundle: list[Confirmation],
        intent_id: str,
        timestamp: str,
    ) -> bool:
        """Verify a bundle of confirmations attached to a trigger broadcast.

        Returns True if the bundle has at least `required` confirmations,
        no duplicate peer URLs, and all signatures verify against
        (intent_id, timestamp, peer_url) using the shared secret.
        """
        required = self.config.quorum.required if self.config.quorum else 1

        if len(bundle) < required:
            logger.warning(
                f"Quorum bundle has {len(bundle)} confirmations, need {required}"
            )
            return False

        seen_urls: set[str] = set()
        for conf in bundle:
            if conf.peer_url in seen_urls:
                logger.warning(f"Duplicate peer_url in bundle: {conf.peer_url}")
                return False
            seen_urls.add(conf.peer_url)

            if not verify_confirmation(
                self.config.secret_key, intent_id, timestamp, conf.peer_url, conf.signature
            ):
                logger.warning(
                    f"Invalid confirmation signature for {conf.peer_url} in bundle"
                )
                return False

        return True
```

- [ ] **Step 4: Verify tests pass**

Run: `pytest tests/test_quorum.py -v`
Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add src/posthumous/quorum.py tests/test_quorum.py
git commit -m "feat(quorum): verify_confirmation_bundle for incoming triggers"
```

---

### Task 5: `attempt_trigger` method (intent broadcast + vote collection)

**Files:**
- Modify: `src/posthumous/quorum.py`
- Modify: `tests/test_quorum.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_quorum.py`:

```python
class TestAttemptTrigger:
    """Tests for QuorumCoordinator.attempt_trigger()."""

    @pytest.fixture
    def config(self):
        return Config(
            node_name="self",
            secret_key=SECRET,
            listen="https://self.local:8420",
            peers=["https://peer1.local:8420", "https://peer2.local:8420"],
            quorum=QuorumConfig(required=2, window_seconds=5),
        )

    @pytest.fixture
    def state_manager(self, tmp_path):
        return StateManager(tmp_path / "state.yaml")

    def _make_peer_response(self, intent_id, timestamp, peer_url, vote="confirm"):
        if vote == "confirm":
            return {
                "intent_id": intent_id,
                "vote": "confirm",
                "peer_url": peer_url,
                "signature": sign_confirmation(SECRET, intent_id, timestamp, peer_url),
            }
        return {
            "intent_id": intent_id,
            "vote": "reject",
            "peer_url": peer_url,
            "last_checkin": "2026-04-12T17:30:00+00:00",
        }

    @pytest.mark.asyncio
    async def test_returns_true_when_quorum_reached(self, config, state_manager):
        peer_manager = MagicMock()

        captured_intent: dict = {}
        async def fake_broadcast_intent(payload):
            captured_intent.update(payload)
            return {
                "https://peer1.local:8420": self._make_peer_response(
                    payload["intent_id"], payload["timestamp"], "https://peer1.local:8420"
                ),
            }

        peer_manager.broadcast_trigger_intent = AsyncMock(side_effect=fake_broadcast_intent)
        peer_manager.broadcast_trigger = AsyncMock(return_value={})

        coord = QuorumCoordinator(config, state_manager, peer_manager)
        result = await coord.attempt_trigger()

        assert result is True
        peer_manager.broadcast_trigger.assert_awaited_once()
        # The trigger must include the bundle
        kwargs = peer_manager.broadcast_trigger.await_args.kwargs
        assert "confirmations" in kwargs
        assert len(kwargs["confirmations"]) >= 2  # self + 1 peer

    @pytest.mark.asyncio
    async def test_returns_false_when_no_peer_confirms(self, config, state_manager):
        peer_manager = MagicMock()

        async def fake_broadcast_intent(payload):
            return {
                "https://peer1.local:8420": self._make_peer_response(
                    payload["intent_id"], payload["timestamp"], "https://peer1.local:8420", vote="reject"
                ),
                "https://peer2.local:8420": self._make_peer_response(
                    payload["intent_id"], payload["timestamp"], "https://peer2.local:8420", vote="reject"
                ),
            }

        peer_manager.broadcast_trigger_intent = AsyncMock(side_effect=fake_broadcast_intent)
        peer_manager.broadcast_trigger = AsyncMock()

        coord = QuorumCoordinator(config, state_manager, peer_manager)
        result = await coord.attempt_trigger()

        assert result is False
        peer_manager.broadcast_trigger.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_returns_false_when_no_peers_reachable(self, config, state_manager):
        peer_manager = MagicMock()
        peer_manager.broadcast_trigger_intent = AsyncMock(return_value={})
        peer_manager.broadcast_trigger = AsyncMock()

        coord = QuorumCoordinator(config, state_manager, peer_manager)
        result = await coord.attempt_trigger()

        # required=2, only self confirms, so quorum not met
        assert result is False
        peer_manager.broadcast_trigger.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_self_counts_as_one_confirmation(self, config, state_manager):
        """When required == 1, self confirms and trigger fires even with no peer responses."""
        config.quorum.required = 1
        peer_manager = MagicMock()
        peer_manager.broadcast_trigger_intent = AsyncMock(return_value={})
        peer_manager.broadcast_trigger = AsyncMock(return_value={})

        coord = QuorumCoordinator(config, state_manager, peer_manager)
        result = await coord.attempt_trigger()

        assert result is True
        peer_manager.broadcast_trigger.assert_awaited_once()
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_quorum.py::TestAttemptTrigger -v`
Expected: FAIL: `attempt_trigger` not defined.

- [ ] **Step 3: Implement**

Add to `QuorumCoordinator` in `src/posthumous/quorum.py`:

```python
    async def attempt_trigger(self) -> bool:
        """Run one round of the quorum protocol.

        1. Generate a fresh intent_id and timestamp.
        2. Broadcast the intent to all peers in parallel.
        3. Collect signed 'confirm' votes from responses.
        4. If self + confirmed peers >= required, broadcast the trigger
           with the confirmation bundle and return True.
        5. Otherwise, return False (caller should remain in GRACE / retry).

        Returns True if the trigger was authorized and broadcast.
        """
        if self.config.quorum is None:
            logger.error("attempt_trigger called without quorum config")
            return False

        required = self.config.quorum.required
        intent_id = str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc).isoformat()
        self_url = self.config.listen if self.config.listen.startswith("http") else f"http://{self.config.listen}"

        # Self always confirms (we initiated; we believe the trigger is warranted).
        confirmations: list[Confirmation] = [
            Confirmation(
                peer_url=self_url,
                signature=sign_confirmation(self.config.secret_key, intent_id, timestamp, self_url),
            ),
        ]

        intent_payload = {
            "intent_id": intent_id,
            "timestamp": timestamp,
            "signature": sign_intent(self.config.secret_key, intent_id, timestamp),
        }

        peer_responses = await self.peer_manager.broadcast_trigger_intent(intent_payload)

        for peer_url, response in peer_responses.items():
            if not isinstance(response, dict):
                continue
            if response.get("vote") != "confirm":
                continue
            sig = response.get("signature")
            returned_url = response.get("peer_url", peer_url)
            if not sig:
                continue
            if not verify_confirmation(self.config.secret_key, intent_id, timestamp, returned_url, sig):
                logger.warning(f"Bad confirmation signature from {peer_url}, ignoring vote")
                continue
            confirmations.append(Confirmation(peer_url=returned_url, signature=sig))

        # De-duplicate by peer_url (defensive: a peer should only vote once).
        seen: set[str] = set()
        deduped: list[Confirmation] = []
        for c in confirmations:
            if c.peer_url in seen:
                continue
            seen.add(c.peer_url)
            deduped.append(c)
        confirmations = deduped

        logger.info(f"Quorum: collected {len(confirmations)} confirmations, need {required}")

        if len(confirmations) < required:
            return False

        # Broadcast the trigger with the confirmation bundle.
        trigger_time = datetime.now(timezone.utc)
        await self.peer_manager.broadcast_trigger(
            trigger_time,
            intent_id=intent_id,
            intent_timestamp=timestamp,
            confirmations=confirmations,
        )
        return True
```

- [ ] **Step 4: Verify tests pass**

Run: `pytest tests/test_quorum.py -v`
Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add src/posthumous/quorum.py tests/test_quorum.py
git commit -m "feat(quorum): attempt_trigger broadcasts intent and tallies votes"
```

---

## Chunk 3: Peer Manager + Server Endpoints

### Task 6: `PeerManager.broadcast_trigger_intent` + bundle in `broadcast_trigger`

**Files:**
- Modify: `src/posthumous/peers.py:194-207` (`broadcast_trigger`)
- Modify: `src/posthumous/peers.py` (add `broadcast_trigger_intent`)
- Modify: `tests/test_peers.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_peers.py`:

```python
class TestBroadcastTriggerIntent:
    """Tests for PeerManager.broadcast_trigger_intent (v0.7)."""

    @pytest.fixture
    def config(self):
        return Config(
            node_name="self",
            secret_key="JBSWY3DPEHPK3PXP",
            peers=["https://peer1.local:8420"],
        )

    @pytest.fixture
    def state_manager(self, tmp_path):
        return StateManager(tmp_path / "state.yaml")

    @pytest.mark.asyncio
    async def test_broadcast_trigger_intent_returns_per_peer_responses(self, config, state_manager):
        from posthumous.peers import PeerManager
        from aioresponses import aioresponses

        manager = PeerManager(config, state_manager)
        intent_payload = {
            "intent_id": "abc-123",
            "timestamp": "2026-04-12T18:00:00+00:00",
            "signature": "sig",
        }

        with aioresponses() as m:
            m.post("https://peer1.local:8420/sync/trigger_intent", payload={
                "intent_id": "abc-123",
                "vote": "confirm",
                "peer_url": "https://peer1.local:8420",
                "signature": "peersig",
            })
            responses = await manager.broadcast_trigger_intent(intent_payload)

        assert "https://peer1.local:8420" in responses
        assert responses["https://peer1.local:8420"]["vote"] == "confirm"
        await manager.close()


class TestBroadcastTriggerWithBundle:
    """Tests for PeerManager.broadcast_trigger including the v0.7 bundle."""

    @pytest.mark.asyncio
    async def test_broadcast_trigger_includes_bundle_when_provided(self, tmp_path):
        from posthumous.peers import PeerManager
        from posthumous.quorum import Confirmation
        from aioresponses import aioresponses

        config = Config(
            node_name="self",
            secret_key="JBSWY3DPEHPK3PXP",
            peers=["https://peer1.local:8420"],
        )
        state_manager = StateManager(tmp_path / "state.yaml")
        manager = PeerManager(config, state_manager)

        bundle = [
            Confirmation(peer_url="https://self.local:8420", signature="s1"),
            Confirmation(peer_url="https://peer1.local:8420", signature="s2"),
        ]
        captured = {}

        with aioresponses() as m:
            m.post(
                "https://peer1.local:8420/sync/trigger",
                callback=lambda url, **kwargs: captured.update(kwargs.get("json", {})) or aioresponses.CallbackResult(payload={"success": True})
            )
            await manager.broadcast_trigger(
                datetime.now(timezone.utc),
                intent_id="abc-123",
                intent_timestamp="2026-04-12T18:00:00+00:00",
                confirmations=bundle,
            )

        assert captured.get("intent_id") == "abc-123"
        assert "confirmations" in captured
        assert len(captured["confirmations"]) == 2
        await manager.close()

    @pytest.mark.asyncio
    async def test_broadcast_trigger_omits_bundle_when_not_provided(self, tmp_path):
        """Backward compat: v0.6 callers don't pass bundle args."""
        from posthumous.peers import PeerManager
        from aioresponses import aioresponses

        config = Config(
            node_name="self",
            secret_key="JBSWY3DPEHPK3PXP",
            peers=["https://peer1.local:8420"],
        )
        state_manager = StateManager(tmp_path / "state.yaml")
        manager = PeerManager(config, state_manager)
        captured = {}

        with aioresponses() as m:
            m.post(
                "https://peer1.local:8420/sync/trigger",
                callback=lambda url, **kwargs: captured.update(kwargs.get("json", {})) or aioresponses.CallbackResult(payload={"success": True})
            )
            await manager.broadcast_trigger(datetime.now(timezone.utc))

        assert "intent_id" not in captured
        assert "confirmations" not in captured
        await manager.close()
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_peers.py::TestBroadcastTriggerIntent tests/test_peers.py::TestBroadcastTriggerWithBundle -v`
Expected: FAIL: `broadcast_trigger_intent` undefined and bundle args not accepted.

- [ ] **Step 3: Implement `broadcast_trigger_intent` and update `broadcast_trigger`**

In `src/posthumous/peers.py`, replace the existing `broadcast_trigger` method (around lines 194-207) with:

```python
    async def broadcast_trigger(
        self,
        timestamp: datetime,
        intent_id: str | None = None,
        intent_timestamp: str | None = None,
        confirmations: list | None = None,  # list[Confirmation] when v0.7 quorum is in use
    ) -> dict[str, bool]:
        """Broadcast trigger event to all peers.

        v0.6 mode: pass only `timestamp`. The trigger is accepted by peers
        based on the outer HMAC signature.

        v0.7 mode: pass `intent_id`, `intent_timestamp`, and `confirmations`.
        The bundle is attached so each peer can independently verify quorum.
        """
        timestamp_str = timestamp.isoformat()
        signature = sign_message(self.config.secret_key, f"trigger:{timestamp_str}")

        payload: dict = {
            "event": "triggered",
            "timestamp": timestamp_str,
            "signature": signature,
            "node": self.config.node_name,
        }

        if intent_id is not None and confirmations is not None:
            payload["intent_id"] = intent_id
            payload["intent_timestamp"] = intent_timestamp
            payload["confirmations"] = [
                {"peer_url": c.peer_url, "signature": c.signature} for c in confirmations
            ]

        return await self._broadcast_to_all("sync/trigger", payload)

    async def broadcast_trigger_intent(self, intent_payload: dict) -> dict[str, dict]:
        """Broadcast an intent to all peers and collect their vote responses.

        Unlike other broadcasts, we want the response body (the vote), not
        just success/failure. Returns a dict mapping peer_url -> response dict.
        Peers that returned non-200 or errored are absent from the result.
        """
        if not self.config.peers:
            return {}

        async def post_and_parse(peer_url: str) -> tuple[str, dict | None]:
            url = f"{peer_url.rstrip('/')}/sync/trigger_intent"
            try:
                session = await self._get_session()
                async with session.post(url, json=intent_payload) as response:
                    if response.status in (200, 409):
                        return peer_url, await response.json()
                    return peer_url, None
            except Exception as e:
                logger.debug(f"intent broadcast to {peer_url} failed: {e}")
                return peer_url, None

        tasks = [post_and_parse(url) for url in self.config.peers]
        results = await asyncio.gather(*tasks)
        return {url: resp for url, resp in results if resp is not None}
```

- [ ] **Step 4: Verify tests pass**

Run: `pytest tests/test_peers.py -v`
Expected: All pass (existing + new).

- [ ] **Step 5: Commit**

```bash
git add src/posthumous/peers.py tests/test_peers.py
git commit -m "feat(peers): broadcast_trigger_intent + bundle on broadcast_trigger"
```

---

### Task 7: `/sync/trigger_intent` server endpoint

**Files:**
- Modify: `src/posthumous/server.py` (add `handle_sync_trigger_intent`, register route)
- Modify: `tests/test_server.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_server.py`:

```python
class TestSyncTriggerIntent:
    """Tests for POST /sync/trigger_intent (v0.7)."""

    @pytest.mark.asyncio
    async def test_confirms_when_state_is_overdue(self, client, state_manager):
        from posthumous.quorum import sign_intent, verify_confirmation
        # State: last_checkin was 20 days ago, trigger_at is 14 days, so overdue
        state_manager.state.last_checkin = datetime.now(timezone.utc) - timedelta(days=20)
        intent_id = "abc-123"
        timestamp = datetime.now(timezone.utc).isoformat()
        payload = {
            "intent_id": intent_id,
            "timestamp": timestamp,
            "signature": sign_intent(SECRET, intent_id, timestamp),
        }
        resp = await client.post("/sync/trigger_intent", json=payload)
        assert resp.status == 200
        data = await resp.json()
        assert data["vote"] == "confirm"
        # Returned signature must verify
        assert verify_confirmation(SECRET, intent_id, timestamp, data["peer_url"], data["signature"])

    @pytest.mark.asyncio
    async def test_rejects_when_state_is_recent(self, client, state_manager):
        from posthumous.quorum import sign_intent
        # Recent check-in: peer should NOT confirm a trigger
        state_manager.state.last_checkin = datetime.now(timezone.utc) - timedelta(minutes=1)
        intent_id = "abc-123"
        timestamp = datetime.now(timezone.utc).isoformat()
        payload = {
            "intent_id": intent_id,
            "timestamp": timestamp,
            "signature": sign_intent(SECRET, intent_id, timestamp),
        }
        resp = await client.post("/sync/trigger_intent", json=payload)
        assert resp.status == 409
        data = await resp.json()
        assert data["vote"] == "reject"
        assert "last_checkin" in data

    @pytest.mark.asyncio
    async def test_rejects_invalid_signature(self, client):
        intent_id = "abc-123"
        timestamp = datetime.now(timezone.utc).isoformat()
        payload = {
            "intent_id": intent_id,
            "timestamp": timestamp,
            "signature": "forged",
        }
        resp = await client.post("/sync/trigger_intent", json=payload)
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_rejects_stale_timestamp(self, client):
        from posthumous.quorum import sign_intent
        intent_id = "abc-123"
        timestamp = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        payload = {
            "intent_id": intent_id,
            "timestamp": timestamp,
            "signature": sign_intent(SECRET, intent_id, timestamp),
        }
        resp = await client.post("/sync/trigger_intent", json=payload)
        assert resp.status == 401
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_server.py::TestSyncTriggerIntent -v`
Expected: FAIL: endpoint does not exist.

- [ ] **Step 3: Register route + implement handler**

In `src/posthumous/server.py`, find the `_setup_routes` method and add (alongside the existing `/sync/trigger` route registration):

```python
        self.app.router.add_post('/sync/trigger_intent', self.handle_sync_trigger_intent)
```

Then add the handler method to `Server`:

```python
    async def handle_sync_trigger_intent(self, request: web.Request) -> web.Response:
        """Receive a trigger intent broadcast and respond with a vote.

        Confirms if our local state agrees the trigger is warranted (we
        are also overdue). Rejects with HTTP 409 if our last_checkin is
        recent enough that we believe the user is alive.
        """
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        intent_id = data.get("intent_id")
        timestamp = data.get("timestamp")
        signature = data.get("signature")
        if not intent_id or not timestamp or not signature:
            return web.json_response({"error": "Missing fields"}, status=400)

        # Reuse the same freshness + HMAC validation as other sync endpoints.
        # Use a custom verifier because the message format differs (we sign trigger_intent:id:ts).
        from posthumous.quorum import verify_intent, sign_confirmation
        try:
            msg_time = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            return web.json_response({"error": "Malformed timestamp"}, status=401)
        if abs((datetime.now(timezone.utc) - msg_time).total_seconds()) > self.SYNC_FRESHNESS_SECONDS:
            return web.json_response({"error": "Stale timestamp"}, status=401)
        if not verify_intent(self.config.secret_key, intent_id, timestamp, signature):
            return web.json_response({"error": "Invalid signature"}, status=401)

        # Decide our vote based on our own state. If our calculated status
        # would be TRIGGERED right now, we confirm.
        calculated = self.watchdog.calculate_status()
        peer_url = self._self_url()

        if calculated == Status.TRIGGERED:
            sig = sign_confirmation(self.config.secret_key, intent_id, timestamp, peer_url)
            return web.json_response(
                {
                    "intent_id": intent_id,
                    "vote": "confirm",
                    "peer_url": peer_url,
                    "signature": sig,
                },
                status=200,
            )

        last_checkin = self.state_manager.state.last_checkin
        return web.json_response(
            {
                "intent_id": intent_id,
                "vote": "reject",
                "peer_url": peer_url,
                "last_checkin": last_checkin.isoformat() if last_checkin else None,
            },
            status=409,
        )

    def _self_url(self) -> str:
        """Return the canonical URL used to identify this node in confirmations."""
        listen = self.config.listen
        if listen.startswith("http"):
            return listen
        return f"http://{listen}"
```

- [ ] **Step 4: Verify tests pass**

Run: `pytest tests/test_server.py::TestSyncTriggerIntent -v`
Expected: All pass.

Run: `pytest tests/test_server.py -v`
Expected: All existing server tests still pass.

- [ ] **Step 5: Commit**

```bash
git add src/posthumous/server.py tests/test_server.py
git commit -m "feat(server): /sync/trigger_intent endpoint with vote response"
```

---

### Task 8: `handle_sync_trigger` verifies confirmation bundle

**Files:**
- Modify: `src/posthumous/server.py` (`handle_sync_trigger` method)
- Modify: `tests/test_server.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_server.py`:

```python
class TestSyncTriggerWithBundle:
    """Tests for /sync/trigger when an intent_id + bundle is present (v0.7)."""

    @pytest.mark.asyncio
    async def test_accepts_trigger_with_valid_bundle(self, client_with_quorum, state_manager_with_quorum):
        """A trigger carrying a valid confirmation bundle is accepted even with quorum=2."""
        from posthumous.quorum import sign_confirmation
        intent_id = "abc-123"
        intent_timestamp = "2026-04-12T18:00:00+00:00"
        timestamp = datetime.now(timezone.utc).isoformat()
        signature = sign_message(SECRET, f"trigger:{timestamp}")

        bundle = [
            {
                "peer_url": "https://self.local:8420",
                "signature": sign_confirmation(SECRET, intent_id, intent_timestamp, "https://self.local:8420"),
            },
            {
                "peer_url": "https://peer1.local:8420",
                "signature": sign_confirmation(SECRET, intent_id, intent_timestamp, "https://peer1.local:8420"),
            },
        ]

        payload = {
            "event": "triggered",
            "timestamp": timestamp,
            "signature": signature,
            "intent_id": intent_id,
            "intent_timestamp": intent_timestamp,
            "confirmations": bundle,
        }
        resp = await client_with_quorum.post("/sync/trigger", json=payload)
        assert resp.status == 200
        assert state_manager_with_quorum.state.status == Status.TRIGGERED

    @pytest.mark.asyncio
    async def test_rejects_trigger_with_insufficient_bundle(self, client_with_quorum):
        """If quorum=2 but bundle has only 1 confirmation, reject."""
        from posthumous.quorum import sign_confirmation
        intent_id = "abc-123"
        intent_timestamp = "2026-04-12T18:00:00+00:00"
        timestamp = datetime.now(timezone.utc).isoformat()
        signature = sign_message(SECRET, f"trigger:{timestamp}")

        bundle = [
            {
                "peer_url": "https://self.local:8420",
                "signature": sign_confirmation(SECRET, intent_id, intent_timestamp, "https://self.local:8420"),
            },
        ]
        payload = {
            "event": "triggered",
            "timestamp": timestamp,
            "signature": signature,
            "intent_id": intent_id,
            "intent_timestamp": intent_timestamp,
            "confirmations": bundle,
        }
        resp = await client_with_quorum.post("/sync/trigger", json=payload)
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_v06_compat_trigger_without_bundle_still_accepted_when_quorum_unset(self, client, state_manager):
        """When quorum is NOT configured, a v0.6-style trigger (no bundle) is accepted."""
        timestamp = datetime.now(timezone.utc).isoformat()
        signature = sign_message(SECRET, f"trigger:{timestamp}")
        payload = {"event": "triggered", "timestamp": timestamp, "signature": signature}
        resp = await client.post("/sync/trigger", json=payload)
        assert resp.status == 200
        assert state_manager.state.status == Status.TRIGGERED

    @pytest.mark.asyncio
    async def test_quorum_required_but_no_bundle_rejected(self, client_with_quorum):
        """When quorum is configured but the trigger has no bundle, reject."""
        timestamp = datetime.now(timezone.utc).isoformat()
        signature = sign_message(SECRET, f"trigger:{timestamp}")
        payload = {"event": "triggered", "timestamp": timestamp, "signature": signature}
        resp = await client_with_quorum.post("/sync/trigger", json=payload)
        assert resp.status == 401
```

You'll need fixtures `client_with_quorum` and `state_manager_with_quorum` that build a Server whose Config has `quorum=QuorumConfig(required=2, window_seconds=30)` and at least one peer. Mirror the existing `client` and `state_manager` fixtures in `tests/test_server.py`.

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_server.py::TestSyncTriggerWithBundle -v`
Expected: FAIL: handler does not yet check the bundle.

- [ ] **Step 3: Implement bundle verification**

In `src/posthumous/server.py`'s `handle_sync_trigger`, after the existing signature verification but before applying the trigger, add:

```python
        # v0.7: if quorum is configured locally, require a valid bundle.
        # Even if quorum is not configured, a bundle in the payload is verified
        # if present (defense in depth).
        intent_id = data.get("intent_id")
        intent_timestamp = data.get("intent_timestamp")
        bundle_raw = data.get("confirmations")

        quorum_required = self.config.quorum is not None

        if quorum_required and bundle_raw is None:
            logger.warning("Quorum configured but trigger arrived without bundle")
            return web.json_response({"error": "Quorum bundle required"}, status=401)

        if bundle_raw is not None:
            from posthumous.quorum import Confirmation, QuorumCoordinator
            bundle = [
                Confirmation(peer_url=b["peer_url"], signature=b["signature"])
                for b in bundle_raw
            ]
            # Construct a transient coordinator just for verification.
            coordinator = QuorumCoordinator(self.config, self.state_manager, self.peer_manager)
            if not coordinator.verify_confirmation_bundle(bundle, intent_id, intent_timestamp):
                logger.warning("Quorum bundle verification failed")
                return web.json_response({"error": "Invalid quorum bundle"}, status=401)
```

- [ ] **Step 4: Verify tests pass**

Run: `pytest tests/test_server.py -v`
Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add src/posthumous/server.py tests/test_server.py
git commit -m "feat(server): verify quorum bundle on /sync/trigger"
```

---

## Chunk 4: Watchdog + Runner Wiring

### Task 9: Watchdog branches on `config.quorum` for grace-timeout

**Files:**
- Modify: `src/posthumous/watchdog.py` (`__init__`, `check_and_transition`, `_transition_through`)
- Modify: `tests/test_watchdog.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_watchdog.py`:

```python
class TestQuorumPath:
    """Tests for the watchdog branching when quorum is configured (v0.7)."""

    @pytest.mark.asyncio
    async def test_grace_timeout_with_quorum_calls_coordinator(self, tmp_path):
        from posthumous.config import Config, QuorumConfig
        from posthumous.state import StateManager, Status
        from posthumous.watchdog import Watchdog
        from unittest.mock import AsyncMock

        config = Config(
            node_name="test", secret_key="JBSWY3DPEHPK3PXP",
            checkin_interval=timedelta(seconds=1),
            warning_start=timedelta(seconds=2),
            grace_start=timedelta(seconds=3),
            trigger_at=timedelta(seconds=4),
            peers=["https://peer1:8420"],
            quorum=QuorumConfig(required=2, window_seconds=10),
        )
        sm = StateManager(tmp_path / "state.yaml")
        sm.state.last_checkin = datetime.now(timezone.utc) - timedelta(seconds=10)
        sm.state.status = Status.GRACE  # already in grace

        coordinator = AsyncMock()
        coordinator.attempt_trigger = AsyncMock(return_value=True)

        wd = Watchdog(config, sm, quorum_coordinator=coordinator)
        await wd.check_and_transition()

        coordinator.attempt_trigger.assert_awaited_once()
        # On success, coordinator's attempt_trigger broadcast the trigger
        # and also transitioned local state to TRIGGERED via state_manager.
        # The watchdog should have observed TRIGGERED.

    @pytest.mark.asyncio
    async def test_grace_timeout_without_quorum_transitions_directly(self, tmp_path):
        """When quorum is None, the watchdog goes straight to TRIGGERED (v0.6 behavior)."""
        from posthumous.config import Config
        from posthumous.state import StateManager, Status
        from posthumous.watchdog import Watchdog

        config = Config(
            node_name="test", secret_key="JBSWY3DPEHPK3PXP",
            checkin_interval=timedelta(seconds=1),
            warning_start=timedelta(seconds=2),
            grace_start=timedelta(seconds=3),
            trigger_at=timedelta(seconds=4),
        )
        sm = StateManager(tmp_path / "state.yaml")
        sm.state.last_checkin = datetime.now(timezone.utc) - timedelta(seconds=10)
        sm.state.status = Status.GRACE

        wd = Watchdog(config, sm)
        await wd.check_and_transition()

        assert sm.state.status == Status.TRIGGERED

    @pytest.mark.asyncio
    async def test_grace_timeout_quorum_failure_returns_to_grace(self, tmp_path):
        """If coordinator returns False, state returns to GRACE for retry."""
        from posthumous.config import Config, QuorumConfig
        from posthumous.state import StateManager, Status
        from posthumous.watchdog import Watchdog
        from unittest.mock import AsyncMock

        config = Config(
            node_name="test", secret_key="JBSWY3DPEHPK3PXP",
            checkin_interval=timedelta(seconds=1),
            warning_start=timedelta(seconds=2),
            grace_start=timedelta(seconds=3),
            trigger_at=timedelta(seconds=4),
            peers=["https://peer1:8420"],
            quorum=QuorumConfig(required=2, window_seconds=10),
        )
        sm = StateManager(tmp_path / "state.yaml")
        sm.state.last_checkin = datetime.now(timezone.utc) - timedelta(seconds=10)
        sm.state.status = Status.GRACE

        coordinator = AsyncMock()
        coordinator.attempt_trigger = AsyncMock(return_value=False)

        wd = Watchdog(config, sm, quorum_coordinator=coordinator)
        await wd.check_and_transition()

        assert sm.state.status == Status.GRACE  # back to grace, not TRIGGERED
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_watchdog.py::TestQuorumPath -v`
Expected: FAIL: `quorum_coordinator` not accepted by Watchdog constructor.

- [ ] **Step 3: Update Watchdog `__init__`**

In `src/posthumous/watchdog.py:79-104`, add a `quorum_coordinator` parameter:

```python
    def __init__(
        self,
        config: Config,
        state_manager: StateManager,
        on_warning: Callable[[], Awaitable[None]] | None = None,
        on_grace: Callable[[], Awaitable[None]] | None = None,
        on_trigger: Callable[[], Awaitable[None]] | None = None,
        quorum_coordinator=None,  # QuorumCoordinator | None (typed loosely to avoid import cycle)
    ):
        self.config = config
        self.state_manager = state_manager
        self._on_warning = on_warning
        self._on_grace = on_grace
        self._on_trigger = on_trigger
        self._quorum_coordinator = quorum_coordinator
        self._task: asyncio.Task | None = None
        self._running = False
        # ... existing check_interval auto-tune ...
```

- [ ] **Step 4: Update `check_and_transition` to branch on quorum**

In `src/posthumous/watchdog.py`'s `check_and_transition` (around lines 202-227), modify the TRIGGERED branch:

```python
        if expected_status == Status.TRIGGERED:
            # If quorum is configured AND we have a coordinator, run the protocol
            # instead of transitioning directly. The coordinator owns the
            # PENDING_QUORUM transition + final TRIGGERED transition.
            if self.config.quorum is not None and self._quorum_coordinator is not None:
                # Catch up through WARNING and GRACE first to fire callbacks.
                await self._transition_through(Status.WARNING, Status.GRACE)
                # Now attempt quorum.
                if not self.state_manager.transition(Status.PENDING_QUORUM):
                    return None
                success = await self._quorum_coordinator.attempt_trigger()
                if success:
                    self.state_manager.transition(Status.TRIGGERED)
                    await self._fire_callback(Status.TRIGGERED)
                    return Status.TRIGGERED
                # Failed. return to GRACE and let the next tick retry.
                self.state_manager.transition(Status.GRACE)
                return Status.GRACE

            # No quorum: existing v0.6 behavior.
            return await self._transition_through(Status.WARNING, Status.GRACE, Status.TRIGGERED)
```

- [ ] **Step 5: Verify tests pass**

Run: `pytest tests/test_watchdog.py -v`
Expected: All pass (existing + new TestQuorumPath).

- [ ] **Step 6: Commit**

```bash
git add src/posthumous/watchdog.py tests/test_watchdog.py
git commit -m "feat(watchdog): branch on quorum for trigger transition"
```

---

### Task 10: Wire `QuorumCoordinator` in `DaemonRunner`

**Files:**
- Modify: `src/posthumous/runner.py` (`build_components`)
- Modify: `tests/test_runner.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_runner.py`:

```python
class TestQuorumWiring:
    """Tests that DaemonRunner constructs QuorumCoordinator when quorum is configured."""

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
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_runner.py::TestQuorumWiring -v`
Expected: FAIL: `quorum_coordinator` attribute does not exist on DaemonRunner.

- [ ] **Step 3: Wire it up**

In `src/posthumous/runner.py`, add `quorum_coordinator` to the `__init__` attribute list (initialized to None):

```python
        self.quorum_coordinator: QuorumCoordinator | None = None
```

Add the import at the top of `runner.py`:

```python
from posthumous.quorum import QuorumCoordinator
```

In `build_components()`, after `self.peer_manager` is constructed and before `self.watchdog` is constructed, add:

```python
        if self.config.quorum is not None:
            self.quorum_coordinator = QuorumCoordinator(
                self.config, self.state_manager, self.peer_manager
            )
```

Then update the Watchdog construction to pass it:

```python
        self.watchdog = Watchdog(
            self.config, self.state_manager,
            on_warning=self.on_warning,
            on_grace=self.on_grace,
            on_trigger=self.on_trigger,
            quorum_coordinator=self.quorum_coordinator,
        )
```

- [ ] **Step 4: Verify tests pass**

Run: `pytest tests/test_runner.py -v`
Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add src/posthumous/runner.py tests/test_runner.py
git commit -m "feat(runner): wire QuorumCoordinator into DaemonRunner"
```

---

## Chunk 5: End-to-End Integration

### Task 11: Full quorum scenario integration test

**Files:**
- Modify: `tests/test_runner.py` (or create `tests/test_quorum_integration.py`)

- [ ] **Step 1: Write integration test**

Add to `tests/test_runner.py`:

```python
class TestQuorumEndToEnd:
    """Integration test for the full quorum protocol against mocked peer HTTP."""

    @pytest.mark.asyncio
    async def test_quorum_success_triggers_with_bundle(self, tmp_path):
        """3 nodes, M=2: self decides to trigger, peer1 confirms, trigger fires with bundle."""
        from aioresponses import aioresponses
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
            from aioresponses import CallbackResult
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
            from aioresponses import CallbackResult
            payload = kwargs.get("json", {})
            return CallbackResult(payload={
                "intent_id": payload["intent_id"],
                "vote": "reject",
                "peer_url": "https://peer2.local:8420",
                "last_checkin": datetime.now(timezone.utc).isoformat(),
            }, status=409)

        def trigger_callback(url, **kwargs):
            from aioresponses import CallbackResult
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
        from aioresponses import aioresponses
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
            from aioresponses import CallbackResult
            payload = kwargs.get("json", {})
            return CallbackResult(payload={
                "intent_id": payload["intent_id"],
                "vote": "reject",
                "peer_url": url.host or "peer",
                "last_checkin": datetime.now(timezone.utc).isoformat(),
            }, status=409)

        broadcast_count = {"n": 0}

        def trigger_callback(url, **kwargs):
            broadcast_count["n"] += 1
            from aioresponses import CallbackResult
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
```

- [ ] **Step 2: Run to verify failure or pass**

Run: `pytest tests/test_runner.py::TestQuorumEndToEnd -v`
Expected: PASS (all pieces from prior tasks are now in place).

If FAIL, debug the integration: most likely culprits are:
- `Watchdog._self_url` mismatch with what the coordinator generates (compare `config.listen` formatting in both places).
- `aioresponses` URL pattern mismatch (paths, query params).

- [ ] **Step 3: Commit**

```bash
git add tests/test_runner.py
git commit -m "test(runner): end-to-end quorum success and failure scenarios"
```

---

## Chunk 6: Final Integration

### Task 12: Run full suite + coverage check

- [ ] **Step 1: Run full suite**

Run: `pytest --tb=long -v`
Expected: All tests pass except the known environment-specific `test_server_start_stop_lifecycle` port conflict.

- [ ] **Step 2: Verify coverage**

Run: `pytest`
Expected: Overall coverage >= 97% (same or better than v0.6). New `quorum.py` should be at >=95%.

- [ ] **Step 3: Sanity check the dashboard / status output**

Run: `pytest tests/test_server.py -k "dashboard or status" -v`
Expected: Pass. If you want PENDING_QUORUM to render distinctly, add a CSS class in `DASHBOARD_HTML` (this is optional polish, not required by spec).

- [ ] **Step 4: Commit any fix-ups**

```bash
git add -u
git commit -m "fix: address integration test fix-ups for v0.7"
```

- [ ] **Step 5: Bump version + update README**

In `src/posthumous/__init__.py`:
```python
__version__ = "0.7.0"
```

In `pyproject.toml`:
```toml
version = "0.7.0"
```

Add a feature bullet to README.md (under the existing feature list, no em-dashes per the soul-voice convention):
```markdown
- **Quorum-based triggering**: Optional M-of-N consensus before triggering. A single compromised peer cannot fire the deadman switch alone.
```

```bash
git add -u
git commit -m "Release v0.7.0"
git tag -a v0.7.0 -m "v0.7.0: Quorum-based triggering"
```

---

## Self-Review

**Spec coverage:**
- ✅ Section 1 (Configuration): Task 2: `QuorumConfig` + parsing + validation.
- ✅ Section 2 (Protocol): Tasks 3-7: signature helpers, `attempt_trigger`, `broadcast_trigger_intent`, `/sync/trigger_intent`.
- ✅ Section 3 (State Machine): Tasks 1, 9: `PENDING_QUORUM` state, watchdog branching with retry-to-GRACE.
- ✅ Section 4 (Files): Task list aligns one-to-one with the spec's modified-files list.
- ✅ Section 5 (Test Plan): Tasks 4 (bundle verification), 5 (vote tallying), 11 (integration end-to-end), Tasks 7-9 (failure modes).
- ✅ Section 6 (Out of Scope): No tasks for leader election, vote persistence, or reconfig: correctly excluded.

**Type consistency check:**
- `Confirmation` dataclass defined in Task 3, used in Tasks 4-6 ✓
- `QuorumConfig.required` (int, default 1), `QuorumConfig.window_seconds` (int, default 30): consistent across all tasks ✓
- `attempt_trigger() -> bool` , same signature in Task 5, consumed in Task 9 ✓
- `verify_confirmation_bundle(bundle, intent_id, timestamp) -> bool` , same signature in Task 4, consumed in Task 8 ✓
- `broadcast_trigger_intent(intent_payload: dict) -> dict[str, dict]` , Task 6, consumed in Task 5 ✓
- `broadcast_trigger(timestamp, intent_id=None, intent_timestamp=None, confirmations=None)`, Task 6, consumed in Task 5 ✓

**Placeholder scan:** No `TBD`, `TODO`, "implement later", or "similar to" patterns. All code shown inline.

**Open questions resolved during planning:**
- The `_self_url` helper exists on both `Server` (Task 7) and is computed inline in `QuorumCoordinator.attempt_trigger` (Task 5). Both must produce identical URLs for self to verify its own confirmation in incoming bundles. The format `http://<listen>` if `listen` doesn't start with `http`, else `listen` as-is. Both implementations follow this rule.
