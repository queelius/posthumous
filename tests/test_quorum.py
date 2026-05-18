"""Tests for the QuorumCoordinator and confirmation-bundle protocol (v0.7)."""

from __future__ import annotations

import asyncio
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from posthumous.config import Config, QuorumConfig
from posthumous.state import State, Status, StateManager
from posthumous.quorum import (
    QuorumCoordinator,
    Confirmation,
    sign_intent,
    verify_intent,
    sign_confirmation,
    verify_confirmation,
)

SECRET = "JBSWY3DPEHPK3PXP"


class TestSignatureHelpers:
    def test_sign_and_verify_confirmation(self):
        sig = sign_confirmation(SECRET, "abc-123", "2026-04-12T18:00:00+00:00", "https://peer1.local:8420")
        assert isinstance(sig, str) and len(sig) > 0
        assert verify_confirmation(SECRET, "abc-123", "2026-04-12T18:00:00+00:00", "https://peer1.local:8420", sig) is True

    def test_verify_rejects_tampered_peer_url(self):
        sig = sign_confirmation(SECRET, "abc-123", "2026-04-12T18:00:00+00:00", "https://peer1.local:8420")
        assert verify_confirmation(SECRET, "abc-123", "2026-04-12T18:00:00+00:00", "https://attacker.local:8420", sig) is False

    def test_verify_rejects_wrong_secret(self):
        sig = sign_confirmation(SECRET, "abc-123", "2026-04-12T18:00:00+00:00", "https://peer1.local:8420")
        # Different but valid base32 secret
        assert verify_confirmation("OTHERSECRETOTHER", "abc-123", "2026-04-12T18:00:00+00:00", "https://peer1.local:8420", sig) is False

    def test_sign_intent_is_deterministic(self):
        a = sign_intent(SECRET, "abc-123", "2026-04-12T18:00:00+00:00")
        b = sign_intent(SECRET, "abc-123", "2026-04-12T18:00:00+00:00")
        assert a == b

    def test_verify_intent_round_trip(self):
        sig = sign_intent(SECRET, "abc-123", "2026-04-12T18:00:00+00:00")
        assert verify_intent(SECRET, "abc-123", "2026-04-12T18:00:00+00:00", sig) is True


class TestVerifyConfirmationBundle:
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
            Confirmation(peer_url="https://self.local:8420",
                         signature=sign_confirmation(SECRET, intent_id, timestamp, "https://self.local:8420")),
            Confirmation(peer_url="https://peer1.local:8420",
                         signature=sign_confirmation(SECRET, intent_id, timestamp, "https://peer1.local:8420")),
        ]
        assert coordinator.verify_confirmation_bundle(bundle, intent_id, timestamp) is True

    def test_rejects_too_few_confirmations(self, coordinator):
        intent_id = "abc-123"
        timestamp = "2026-04-12T18:00:00+00:00"
        bundle = [
            Confirmation(peer_url="https://self.local:8420",
                         signature=sign_confirmation(SECRET, intent_id, timestamp, "https://self.local:8420")),
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
            Confirmation(peer_url="https://self.local:8420",
                         signature=sign_confirmation(SECRET, intent_id, timestamp, "https://self.local:8420")),
            Confirmation(peer_url="https://peer1.local:8420", signature="forged-signature"),
        ]
        assert coordinator.verify_confirmation_bundle(bundle, intent_id, timestamp) is False

    def test_rejects_signature_for_different_intent_id(self, coordinator):
        bundle = [
            Confirmation(peer_url="https://self.local:8420",
                         signature=sign_confirmation(SECRET, "abc-123", "2026-04-12T18:00:00+00:00", "https://self.local:8420")),
            Confirmation(peer_url="https://peer1.local:8420",
                         signature=sign_confirmation(SECRET, "wrong-id", "2026-04-12T18:00:00+00:00", "https://peer1.local:8420")),
        ]
        assert coordinator.verify_confirmation_bundle(bundle, "abc-123", "2026-04-12T18:00:00+00:00") is False

    def test_rejects_bundle_with_unknown_peer_url(self, coordinator):
        """An attacker with the secret cannot invent peer URLs."""
        intent_id = "abc-123"
        timestamp = "2026-04-12T18:00:00+00:00"
        bundle = [
            Confirmation(peer_url="https://self.local:8420",
                         signature=sign_confirmation(SECRET, intent_id, timestamp, "https://self.local:8420")),
            Confirmation(peer_url="https://attacker.invented:8420",  # not in config.peers
                         signature=sign_confirmation(SECRET, intent_id, timestamp, "https://attacker.invented:8420")),
        ]
        allowed = {"https://self.local:8420", "https://peer1.local:8420", "https://peer2.local:8420"}
        assert coordinator.verify_confirmation_bundle(
            bundle, intent_id, timestamp, allowed_peer_urls=allowed
        ) is False

    def test_accepts_bundle_when_all_urls_in_allowed_set(self, coordinator):
        intent_id = "abc-123"
        timestamp = "2026-04-12T18:00:00+00:00"
        bundle = [
            Confirmation(peer_url="https://self.local:8420",
                         signature=sign_confirmation(SECRET, intent_id, timestamp, "https://self.local:8420")),
            Confirmation(peer_url="https://peer1.local:8420",
                         signature=sign_confirmation(SECRET, intent_id, timestamp, "https://peer1.local:8420")),
        ]
        allowed = {"https://self.local:8420", "https://peer1.local:8420", "https://peer2.local:8420"}
        assert coordinator.verify_confirmation_bundle(
            bundle, intent_id, timestamp, allowed_peer_urls=allowed
        ) is True

    def test_rejects_stale_intent_timestamp(self, coordinator):
        intent_id = "abc-123"
        # Craft a bundle with an hour-old intent_timestamp
        stale_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        bundle = [
            Confirmation(peer_url="https://self.local:8420",
                         signature=sign_confirmation(SECRET, intent_id, stale_ts, "https://self.local:8420")),
            Confirmation(peer_url="https://peer1.local:8420",
                         signature=sign_confirmation(SECRET, intent_id, stale_ts, "https://peer1.local:8420")),
        ]
        assert coordinator.verify_confirmation_bundle(
            bundle, intent_id, stale_ts, max_age_seconds=300
        ) is False

    def test_accepts_fresh_intent_timestamp(self, coordinator):
        intent_id = "abc-123"
        fresh_ts = datetime.now(timezone.utc).isoformat()
        bundle = [
            Confirmation(peer_url="https://self.local:8420",
                         signature=sign_confirmation(SECRET, intent_id, fresh_ts, "https://self.local:8420")),
            Confirmation(peer_url="https://peer1.local:8420",
                         signature=sign_confirmation(SECRET, intent_id, fresh_ts, "https://peer1.local:8420")),
        ]
        assert coordinator.verify_confirmation_bundle(
            bundle, intent_id, fresh_ts, max_age_seconds=300
        ) is True


class TestAttemptTrigger:
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

    def _make_confirm_response(self, intent_id, timestamp, peer_url):
        return {
            "intent_id": intent_id,
            "vote": "confirm",
            "peer_url": peer_url,
            "signature": sign_confirmation(SECRET, intent_id, timestamp, peer_url),
        }

    def _make_reject_response(self, intent_id, peer_url):
        return {
            "intent_id": intent_id,
            "vote": "reject",
            "peer_url": peer_url,
            "last_checkin": "2026-04-12T17:30:00+00:00",
        }

    @pytest.mark.asyncio
    async def test_returns_true_when_quorum_reached(self, config, state_manager):
        peer_manager = MagicMock()

        async def fake_broadcast_intent(payload):
            return {
                "https://peer1.local:8420": self._make_confirm_response(
                    payload["intent_id"], payload["timestamp"], "https://peer1.local:8420"
                ),
            }

        peer_manager.broadcast_trigger_intent = AsyncMock(side_effect=fake_broadcast_intent)
        peer_manager.broadcast_trigger = AsyncMock(return_value={})

        # Pre-set to PENDING_QUORUM to match how watchdog calls this
        state_manager.state.status = Status.PENDING_QUORUM

        coord = QuorumCoordinator(config, state_manager, peer_manager)
        result = await coord.attempt_trigger()

        assert result is True
        peer_manager.broadcast_trigger.assert_awaited_once()
        kwargs = peer_manager.broadcast_trigger.await_args.kwargs
        assert "confirmations" in kwargs
        assert len(kwargs["confirmations"]) >= 2  # self + 1 peer

    @pytest.mark.asyncio
    async def test_returns_false_when_no_peer_confirms(self, config, state_manager):
        peer_manager = MagicMock()

        async def fake_broadcast_intent(payload):
            return {
                "https://peer1.local:8420": self._make_reject_response(payload["intent_id"], "https://peer1.local:8420"),
                "https://peer2.local:8420": self._make_reject_response(payload["intent_id"], "https://peer2.local:8420"),
            }

        peer_manager.broadcast_trigger_intent = AsyncMock(side_effect=fake_broadcast_intent)
        peer_manager.broadcast_trigger = AsyncMock()

        state_manager.state.status = Status.PENDING_QUORUM

        coord = QuorumCoordinator(config, state_manager, peer_manager)
        result = await coord.attempt_trigger()

        assert result is False
        peer_manager.broadcast_trigger.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_returns_false_when_no_peers_reachable(self, config, state_manager):
        peer_manager = MagicMock()
        peer_manager.broadcast_trigger_intent = AsyncMock(return_value={})
        peer_manager.broadcast_trigger = AsyncMock()

        state_manager.state.status = Status.PENDING_QUORUM

        coord = QuorumCoordinator(config, state_manager, peer_manager)
        result = await coord.attempt_trigger()

        assert result is False  # required=2, only self confirms
        peer_manager.broadcast_trigger.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_self_counts_as_one_confirmation(self, config, state_manager):
        config.quorum.required = 1
        peer_manager = MagicMock()
        peer_manager.broadcast_trigger_intent = AsyncMock(return_value={})
        peer_manager.broadcast_trigger = AsyncMock(return_value={})

        state_manager.state.status = Status.PENDING_QUORUM

        coord = QuorumCoordinator(config, state_manager, peer_manager)
        result = await coord.attempt_trigger()

        assert result is True
        peer_manager.broadcast_trigger.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_aborts_if_state_leaves_pending_quorum(self, config, state_manager):
        """A check-in during vote collection must abort the broadcast."""
        peer_manager = MagicMock()

        async def fake_broadcast_intent(payload):
            # Simulate check-in arriving while we're collecting votes
            state_manager.state.status = Status.ARMED
            return {
                "https://peer1.local:8420": self._make_confirm_response(
                    payload["intent_id"], payload["timestamp"], "https://peer1.local:8420"
                ),
            }

        peer_manager.broadcast_trigger_intent = AsyncMock(side_effect=fake_broadcast_intent)
        peer_manager.broadcast_trigger = AsyncMock()

        # Pre-set to PENDING_QUORUM to match how watchdog calls this
        state_manager.state.status = Status.PENDING_QUORUM

        coord = QuorumCoordinator(config, state_manager, peer_manager)
        result = await coord.attempt_trigger()

        assert result is False
        peer_manager.broadcast_trigger.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_window_timeout_enforced(self, state_manager):
        """Slow peer responses past window_seconds are treated as non-votes."""
        config = Config(
            node_name="self",
            secret_key=SECRET,
            listen="https://self.local:8420",
            peers=["https://peer1.local:8420"],
            quorum=QuorumConfig(required=2, window_seconds=0.1),  # 100ms
        )
        peer_manager = MagicMock()

        async def slow_broadcast_intent(payload):
            await asyncio.sleep(1.0)  # way past the 0.1s window
            return {"https://peer1.local:8420": self._make_confirm_response(
                payload["intent_id"], payload["timestamp"], "https://peer1.local:8420"
            )}

        peer_manager.broadcast_trigger_intent = AsyncMock(side_effect=slow_broadcast_intent)
        peer_manager.broadcast_trigger = AsyncMock()

        state_manager.state.status = Status.PENDING_QUORUM

        coord = QuorumCoordinator(config, state_manager, peer_manager)
        import time
        start = time.monotonic()
        result = await coord.attempt_trigger()
        elapsed = time.monotonic() - start

        assert result is False
        assert elapsed < 0.5, f"wait_for should have cut off at ~100ms, took {elapsed}s"
        peer_manager.broadcast_trigger.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_returns_false_when_quorum_not_configured(self, state_manager):
        """attempt_trigger is a no-op (returns False) if config.quorum is None."""
        config = Config(
            node_name="self",
            secret_key=SECRET,
            listen="https://self.local:8420",
            quorum=None,
        )
        peer_manager = MagicMock()
        peer_manager.broadcast_trigger_intent = AsyncMock()
        peer_manager.broadcast_trigger = AsyncMock()

        coord = QuorumCoordinator(config, state_manager, peer_manager)
        result = await coord.attempt_trigger()

        assert result is False
        peer_manager.broadcast_trigger_intent.assert_not_awaited()
        peer_manager.broadcast_trigger.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ignores_non_dict_peer_response(self, config, state_manager):
        """A peer that returns something other than a dict is silently ignored."""
        config.quorum.required = 2
        peer_manager = MagicMock()

        async def fake_broadcast_intent(payload):
            return {
                "https://peer1.local:8420": "not a dict",
                "https://peer2.local:8420": self._make_confirm_response(
                    payload["intent_id"], payload["timestamp"], "https://peer2.local:8420"
                ),
            }

        peer_manager.broadcast_trigger_intent = AsyncMock(side_effect=fake_broadcast_intent)
        peer_manager.broadcast_trigger = AsyncMock(return_value={})
        state_manager.state.status = Status.PENDING_QUORUM

        coord = QuorumCoordinator(config, state_manager, peer_manager)
        result = await coord.attempt_trigger()

        assert result is True  # self + peer2 = 2, peer1's garbage was dropped
        kwargs = peer_manager.broadcast_trigger.await_args.kwargs
        urls = {c.peer_url for c in kwargs["confirmations"]}
        assert "https://peer1.local:8420" not in urls
        assert "https://peer2.local:8420" in urls

    @pytest.mark.asyncio
    async def test_ignores_confirm_vote_missing_signature(self, config, state_manager):
        """A confirm vote without a signature field is dropped silently."""
        config.quorum.required = 2
        peer_manager = MagicMock()

        async def fake_broadcast_intent(payload):
            return {
                # peer1: confirm vote with no signature
                "https://peer1.local:8420": {
                    "intent_id": payload["intent_id"],
                    "vote": "confirm",
                    "peer_url": "https://peer1.local:8420",
                },
                "https://peer2.local:8420": self._make_confirm_response(
                    payload["intent_id"], payload["timestamp"], "https://peer2.local:8420"
                ),
            }

        peer_manager.broadcast_trigger_intent = AsyncMock(side_effect=fake_broadcast_intent)
        peer_manager.broadcast_trigger = AsyncMock(return_value={})
        state_manager.state.status = Status.PENDING_QUORUM

        coord = QuorumCoordinator(config, state_manager, peer_manager)
        result = await coord.attempt_trigger()

        assert result is True  # self + peer2 = 2; peer1's malformed vote dropped
        kwargs = peer_manager.broadcast_trigger.await_args.kwargs
        urls = {c.peer_url for c in kwargs["confirmations"]}
        assert "https://peer1.local:8420" not in urls
