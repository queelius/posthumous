"""Quorum-based triggering protocol (v0.7).

A QuorumCoordinator runs the intent-broadcast / vote-collection /
trigger-with-bundle protocol described in
docs/superpowers/specs/2026-04-12-quorum-federation-design.md.

This module is pure protocol logic. All HTTP I/O happens through
the PeerManager passed in at construction time.
"""

from __future__ import annotations

import asyncio
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
    """Coordinates the M-of-N quorum protocol for triggering."""

    def __init__(
        self,
        config: "Config",
        state_manager: "StateManager",
        peer_manager: "PeerManager",
    ):
        self.config = config
        self.state_manager = state_manager
        self.peer_manager = peer_manager

    def verify_confirmation_bundle(
        self,
        bundle: list[Confirmation],
        intent_id: str,
        timestamp: str,
        allowed_peer_urls: set[str] | None = None,
        max_age_seconds: int | None = None,
    ) -> bool:
        """Verify a bundle of confirmations attached to a trigger broadcast.

        Checks:
        - Bundle has >= required confirmations.
        - If max_age_seconds is set, intent_timestamp is within that window of now.
        - If allowed_peer_urls is set, every peer_url in the bundle is in the set
          (i.e., the voter is a known federation member, not an invented URL).
        - No duplicate peer_url.
        - Every signature verifies against (intent_id, timestamp, peer_url).
        """
        required = self.config.quorum.required if self.config.quorum else 1

        if max_age_seconds is not None:
            try:
                ts = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            except (ValueError, AttributeError, TypeError):
                logger.warning("Bundle rejected: malformed intent_timestamp")
                return False
            age = abs((datetime.now(timezone.utc) - ts).total_seconds())
            if age > max_age_seconds:
                logger.warning(f"Bundle rejected: timestamp too old ({age:.0f}s > {max_age_seconds}s)")
                return False

        if len(bundle) < required:
            logger.warning(f"Bundle rejected: {len(bundle)} confirmations < {required} required")
            return False

        seen_urls: set[str] = set()
        for conf in bundle:
            if conf.peer_url in seen_urls:
                logger.warning(f"Bundle rejected: duplicate peer_url {conf.peer_url}")
                return False
            if allowed_peer_urls is not None and conf.peer_url not in allowed_peer_urls:
                logger.warning(f"Bundle rejected: unknown voter {conf.peer_url}")
                return False
            if not verify_confirmation(
                self.config.secret_key, intent_id, timestamp, conf.peer_url, conf.signature
            ):
                logger.warning(f"Bundle rejected: bad signature from {conf.peer_url}")
                return False
            seen_urls.add(conf.peer_url)

        return True

    async def attempt_trigger(self) -> bool:
        """Run one round of the quorum protocol.

        1. Generate a fresh intent_id and timestamp.
        2. Broadcast the intent to all peers in parallel.
        3. Collect signed 'confirm' votes from responses.
        4. If self + confirmed peers >= required, broadcast the trigger
           with the confirmation bundle and return True.
        5. Otherwise, return False (caller should remain in GRACE / retry).
        """
        if self.config.quorum is None:
            logger.error("attempt_trigger called without quorum config")
            return False

        required = self.config.quorum.required
        intent_id = str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc).isoformat()
        self_url = self.config.self_url()

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

        try:
            peer_responses = await asyncio.wait_for(
                self.peer_manager.broadcast_trigger_intent(intent_payload),
                timeout=self.config.quorum.window_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"Quorum window ({self.config.quorum.window_seconds}s) expired "
                f"before collecting all votes"
            )
            peer_responses = {}

        for peer_url, response in peer_responses.items():
            if not isinstance(response, dict) or response.get("vote") != "confirm":
                continue
            sig = response.get("signature")
            returned_url = response.get("peer_url", peer_url)
            if not sig:
                continue
            if not verify_confirmation(self.config.secret_key, intent_id, timestamp, returned_url, sig):
                logger.warning(f"Bad confirmation signature from {peer_url}, ignoring vote")
                continue
            confirmations.append(Confirmation(peer_url=returned_url, signature=sig))

        # De-duplicate by peer_url (defensive: a malicious peer could echo self_url).
        by_url: dict[str, Confirmation] = {}
        for c in confirmations:
            by_url.setdefault(c.peer_url, c)
        confirmations = list(by_url.values())

        logger.info(f"Quorum: collected {len(confirmations)} confirmations, need {required}")

        if len(confirmations) < required:
            return False

        # A check-in during vote collection may have transitioned us out of
        # PENDING_QUORUM (e.g. back to ARMED). Abort rather than fire a trigger
        # the user cancelled.
        from posthumous.state import Status
        current = self.state_manager.state.status
        if current != Status.PENDING_QUORUM:
            logger.info(f"Quorum reached but state is {current.value}; aborting broadcast")
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
