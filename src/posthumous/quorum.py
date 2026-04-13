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

        # De-duplicate by peer_url (defensive).
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
