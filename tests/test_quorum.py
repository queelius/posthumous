"""Tests for the QuorumCoordinator and confirmation-bundle protocol (v0.7)."""

from __future__ import annotations

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

        assert result is False  # required=2, only self confirms
        peer_manager.broadcast_trigger.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_self_counts_as_one_confirmation(self, config, state_manager):
        config.quorum.required = 1
        peer_manager = MagicMock()
        peer_manager.broadcast_trigger_intent = AsyncMock(return_value={})
        peer_manager.broadcast_trigger = AsyncMock(return_value={})

        coord = QuorumCoordinator(config, state_manager, peer_manager)
        result = await coord.attempt_trigger()

        assert result is True
        peer_manager.broadcast_trigger.assert_awaited_once()
