# Quorum-Based Triggering (v0.7) Design

Date: 2026-04-12

## Goal

Prevent a single compromised peer from irrevocably triggering the entire federation. Require M-of-N nodes to independently agree the user is gone before any node transitions to TRIGGERED.

## Threat Model

**Today (v0.6):** any node with the shared HMAC secret can call `POST /sync/trigger` and force every peer into TRIGGERED. A single compromised host (or a stolen secret) is sufficient to fire the deadman switch.

**With v0.7:** an attacker holding the secret can broadcast an intent, but each honest peer independently checks its own state before voting "confirm". An attacker cannot forge confirmations from peers it does not control. To fire the switch, the attacker must compromise M peers (where M is the configured quorum threshold), not just one.

The trade-off is **safety over liveness**: if M peers cannot be reached, the federation does NOT trigger. Administrators handle partition recovery via `phm recover` or manual intervention.

## Architecture

Quorum is a thin layer on top of the existing v0.6 sync protocol. It introduces:

1. A new state `Status.PENDING_QUORUM` between `GRACE` and `TRIGGERED`.
2. A new endpoint `POST /sync/trigger_intent` for collecting votes.
3. A confirmation bundle attached to `POST /sync/trigger` so any peer can independently verify the trigger was authorized by M peers.
4. A new `QuorumCoordinator` class in `src/posthumous/quorum.py` that runs the protocol.

Quorum is opt-in. If `config.quorum` is absent, the system behaves identically to v0.6.

## Section 1: Configuration

New optional config block:

```yaml
quorum:
  required: 2            # M: minimum confirmations including self
  window_seconds: 30     # how long to wait for peer confirmations
```

**Field validation:**
- `required >= 1`. Setting `required: 1` is equivalent to no quorum (not an error, just a no-op).
- `required <= len(peers) + 1`. If higher, validate() returns an error: "quorum.required exceeds federation size".
- `window_seconds > 0`, default 30.

**Defaults:** if `quorum:` is absent, `config.quorum` is `None` and the watchdog skips the quorum path. This preserves v0.6 behavior for existing deployments.

**Failure mode:** fail closed. If fewer than `required` confirmations arrive within `window_seconds`, the node does NOT trigger. It stays in GRACE and the watchdog retries on its next tick.

## Section 2: Protocol

### 2.1 Intent broadcast

When a node decides to trigger (GRACE timeout or quorum retry), it generates a fresh `intent_id` (UUID4) and broadcasts:

```
POST /sync/trigger_intent
{
  "intent_id": "f4e2c5d8-...",
  "timestamp": "2026-04-12T18:42:11+00:00",
  "signature": "HMAC-SHA256(secret, 'trigger_intent:{intent_id}:{timestamp}')"
}
```

The broadcast is sent in parallel to all peers. Each peer responds within `window_seconds` or is treated as not voting.

### 2.2 Confirmation response

Each peer receiving `/sync/trigger_intent` runs this check locally:

1. Verify the intent signature (HMAC).
2. Verify the timestamp is within `SYNC_FRESHNESS_SECONDS` (5 minutes, existing constant).
3. Check own state: is it past `trigger_at` (i.e., would I also trigger right now)?

The response body contains the peer's vote:

```
HTTP 200 (confirm):
{
  "intent_id": "f4e2c5d8-...",
  "vote": "confirm",
  "peer_url": "https://peer2.local:8420",
  "signature": "HMAC-SHA256(secret, 'confirm:{intent_id}:{timestamp}:{peer_url}')"
}

HTTP 409 (reject):
{
  "intent_id": "f4e2c5d8-...",
  "vote": "reject",
  "peer_url": "https://peer2.local:8420",
  "last_checkin": "2026-04-12T17:30:00+00:00"
}
```

A reject means "I have evidence the user is alive" and includes the peer's `last_checkin` for diagnostics.

The initiator counts itself as one confirmation (it has already decided to trigger). It collects responses until either:
- It has `>= required` confirms, or
- The window expires.

### 2.3 Trigger broadcast with bundle

If quorum is reached, the initiator broadcasts the trigger with the confirmation bundle:

```
POST /sync/trigger
{
  "timestamp": "2026-04-12T18:42:41+00:00",
  "signature": "HMAC-SHA256(secret, 'trigger:{timestamp}')",
  "intent_id": "f4e2c5d8-...",
  "confirmations": [
    {"peer_url": "https://self.local:8420", "signature": "..."},
    {"peer_url": "https://peer2.local:8420", "signature": "..."}
  ]
}
```

Each peer receiving `/sync/trigger` validates:

1. The outer trigger signature (existing v0.6 check).
2. The bundle has `>= config.quorum.required` confirmations.
3. Each confirmation signature is valid for `(intent_id, timestamp, peer_url)`.
4. No duplicate `peer_url` in the bundle (one vote per peer).

If any check fails, the trigger is rejected with HTTP 401 and a logged reason.

**Backward compatibility:** if a peer receives `/sync/trigger` WITHOUT `intent_id`/`confirmations` (e.g., from a v0.6 initiator), it accepts the trigger as before. This means a single v0.6 node in the federation can still bypass quorum. Operators who require quorum should upgrade all peers and use `quorum.required >= 2`.

If a v0.6 peer receives `/sync/trigger_intent` from a v0.7 initiator, it returns HTTP 404. The initiator counts that as a non-vote.

## Section 3: State Machine

### 3.1 New status

Add `Status.PENDING_QUORUM` to the existing enum.

### 3.2 Updated valid transitions

```
ARMED        → WARNING
WARNING      → ARMED, GRACE
GRACE        → ARMED, PENDING_QUORUM (if quorum configured), TRIGGERED (no quorum)
PENDING_QUORUM → ARMED (check-in), TRIGGERED (quorum reached), GRACE (quorum failed)
TRIGGERED    → terminal
```

### 3.3 Watchdog behavior

In `Watchdog._on_grace_timeout`:

```python
if self.config.quorum is None:
    self.state_manager.transition(Status.TRIGGERED)
    await self.on_trigger()
else:
    self.state_manager.transition(Status.PENDING_QUORUM)
    success = await self.quorum_coordinator.attempt_trigger()
    if success:
        # quorum_coordinator already called transition(TRIGGERED) and broadcast
        await self.on_trigger()
    else:
        # fail closed, stay reachable for next tick
        self.state_manager.transition(Status.GRACE)
        logger.warning("Quorum not reached, will retry on next tick")
```

A check-in during `PENDING_QUORUM` resets to `ARMED` and aborts any in-flight quorum attempt (the in-flight attempt's confirmations become moot because no one will broadcast `/sync/trigger`).

### 3.4 Visibility

`PENDING_QUORUM` is exposed in:
- `GET /status` JSON response
- The web dashboard (`pending_quorum` CSS class, amber color)
- `phm status` CLI output

Users can see "waiting for peer confirmation (3 of 5 confirms received)" rather than silent inaction.

## Section 4: Files

### New files
- `src/posthumous/quorum.py` (~150 lines): `QuorumCoordinator` class with `attempt_trigger()` and `verify_confirmation_bundle()` methods. Pure protocol logic, no aiohttp directly.
- `tests/test_quorum.py` (~200 lines): unit tests for `QuorumCoordinator` and bundle verification.

### Modified files
- `src/posthumous/state.py`: add `Status.PENDING_QUORUM`, update `transition_to` valid_transitions table.
- `src/posthumous/config.py`: add `QuorumConfig` dataclass, parse `quorum:` block, validate constraints.
- `src/posthumous/watchdog.py`: branch on `config.quorum` in grace-timeout handling, hand off to `QuorumCoordinator` when configured.
- `src/posthumous/server.py`: new `handle_sync_trigger_intent` endpoint; modify `handle_sync_trigger` to verify confirmation bundle when `intent_id` is present.
- `src/posthumous/peers.py`: new `broadcast_trigger_intent(intent_payload)` method returning `list[(peer_url, vote, signature)]`. Reuse `_broadcast_to_all` plumbing.
- `src/posthumous/runner.py`: wire `QuorumCoordinator` in `DaemonRunner.build_components()`, pass to `Watchdog`.
- `src/posthumous/auth.py`: no API changes; reuse existing `sign_message`/`verify_signature`.

### Modified test files
- `tests/test_state.py`: `PENDING_QUORUM` transitions.
- `tests/test_config.py`: `quorum:` parsing and validation.
- `tests/test_watchdog.py`: grace-timeout branches with and without quorum config.
- `tests/test_server.py`: `/sync/trigger_intent` endpoint, `/sync/trigger` with bundle.
- `tests/test_runner.py`: end-to-end quorum scenario with `aioresponses`.

## Section 5: Test Plan

**Unit tests (`test_quorum.py`):**
- `attempt_trigger` returns True when `>= M` confirmations received.
- `attempt_trigger` returns False when window expires with `< M` confirmations.
- `attempt_trigger` returns False when peers reject (vote=reject).
- `verify_confirmation_bundle` accepts valid bundles.
- Rejects bundles with `< M` confirmations, duplicate peer URLs, mismatched signatures, mismatched intent_ids, expired timestamps.

**Integration tests (`test_runner.py` or new file):**
- Full quorum success: 3 nodes, M=2, all reachable, trigger fires after one round of intent broadcast.
- Quorum failure (partition): 3 nodes, M=2, only 1 reachable, trigger does NOT fire, state returns to GRACE, retries succeed when partition heals.
- Quorum failure (rejection): 3 nodes, M=2, peers' local timers haven't expired (e.g., stale `last_checkin` data on initiator), peers reject, trigger does NOT fire.
- Check-in during PENDING_QUORUM aborts the trigger attempt.
- Backward compat: v0.7 initiator broadcasting to a peer that returns 404 on `/sync/trigger_intent` correctly counts as a non-vote.

**Failure-mode tests:**
- Malformed intent payload → 400.
- Stale intent timestamp → 401.
- Forged confirmation signatures → trigger rejected with logged reason.
- Initiator crashes mid-vote → next watchdog tick starts fresh attempt with new intent_id.

## Section 6: Out of Scope (Deliberate)

- **Leader election**: this is not Raft. Each node makes its own decision; no coordinator role exists.
- **Vote persistence**: intent_ids and confirmations are ephemeral. Crashes lose in-flight state but the next tick retries cleanly.
- **Quorum reconfiguration protocol**: changing `required` requires manual config edits on every node. We do not handle "rolling quorum changes" or "online membership changes."
- **Byzantine fault tolerance beyond M**: an attacker who compromises M peers can still force a trigger. Quorum raises the bar from 1 to M; it does not eliminate the threat.
- **Asynchronous voting / vote forwarding**: peers vote synchronously in the response to `/sync/trigger_intent`. There's no separate vote endpoint.

## Dependency Impact

No new runtime dependencies. UUID4 is in `uuid` (stdlib). All HMAC and HTTP plumbing already exists in v0.6.

## Migration

- Existing deployments: no action required. Without `quorum:` in config, behavior is identical to v0.6.
- Operators enabling quorum: upgrade all peers to v0.7, then add the `quorum:` block to each node's config and restart. A mixed federation (some v0.6, some v0.7) is operable but the v0.6 nodes can still trigger unilaterally.
