"""Peer communication for federated Posthumous nodes."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import aiohttp

from posthumous.auth import sign_message

if TYPE_CHECKING:
    from posthumous.config import Config
    from posthumous.state import StateManager

logger = logging.getLogger(__name__)


@dataclass
class PeerStatus:
    """Status of a peer node."""
    url: str
    reachable: bool
    status: str | None  # armed, warning, grace, triggered
    last_checkin: datetime | None
    last_seen: datetime | None
    error: str | None = None


class PeerManager:
    """Manages communication with peer nodes."""

    def __init__(
        self,
        config: "Config",
        state_manager: "StateManager",
        timeout: float = 10.0,
        on_peer_down: Callable | None = None,
    ):
        self.config = config
        self.state_manager = state_manager
        self.timeout = timeout
        self._on_peer_down = on_peer_down
        self._session: aiohttp.ClientSession | None = None
        self._health_task: asyncio.Task | None = None
        self._running = False
        self._alerted_peers: set[str] = set()

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create HTTP session."""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self) -> None:
        """Close HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _post(
        self,
        peer_url: str,
        endpoint: str,
        data: dict,
    ) -> tuple[bool, str | None]:
        """POST to a peer endpoint.

        Returns (success, error_message).
        """
        url = f"{peer_url.rstrip('/')}/{endpoint.lstrip('/')}"

        try:
            session = await self._get_session()
            async with session.post(url, json=data) as response:
                if response.status == 200:
                    return True, None
                else:
                    text = await response.text()
                    return False, f"HTTP {response.status}: {text}"
        except asyncio.TimeoutError:
            return False, "Timeout"
        except aiohttp.ClientError as e:
            return False, str(e)
        except Exception as e:
            logger.exception(f"Error posting to {url}: {e}")
            return False, str(e)

    async def _get(
        self,
        peer_url: str,
        endpoint: str,
        params: dict[str, str] | None = None,
    ) -> tuple[dict | None, str | None]:
        """GET from a peer endpoint.

        Returns (data, error_message).
        """
        url = f"{peer_url.rstrip('/')}/{endpoint.lstrip('/')}"

        try:
            session = await self._get_session()
            async with session.get(url, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    return data, None
                else:
                    text = await response.text()
                    return None, f"HTTP {response.status}: {text}"
        except asyncio.TimeoutError:
            return None, "Timeout"
        except aiohttp.ClientError as e:
            return None, str(e)
        except Exception as e:
            logger.exception(f"Error getting from {url}: {e}")
            return None, str(e)

    async def _broadcast_to_peer(
        self,
        peer_url: str,
        endpoint: str,
        data: dict,
    ) -> tuple[bool, str | None]:
        """Broadcast to a single peer."""
        success, error = await self._post(peer_url, endpoint, data)

        if success:
            logger.debug(f"Broadcast to {peer_url} successful")
        else:
            logger.warning(f"Broadcast to {peer_url} failed: {error}")

        return success, error

    async def _broadcast_to_all(
        self,
        endpoint: str,
        data: dict,
        update_peer_state: bool = False,
    ) -> dict[str, bool]:
        """Broadcast data to all peers.

        Args:
            endpoint: API endpoint to POST to
            data: Data to send
            update_peer_state: If True, update peer tracking state

        Returns:
            Dict mapping peer URL to success status
        """
        if not self.config.peers:
            return {}

        tasks = [self._broadcast_to_peer(url, endpoint, data) for url in self.config.peers]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

        results = {}
        for peer_url, response in zip(self.config.peers, responses):
            if isinstance(response, Exception):
                results[peer_url] = False
                if update_peer_state:
                    self.state_manager.state.update_peer(peer_url, success=False, error=str(response))
            else:
                success, error = response
                results[peer_url] = success
                if update_peer_state:
                    self.state_manager.state.update_peer(peer_url, success=success, error=error)

        if update_peer_state:
            self.state_manager.save()

        return results

    async def broadcast_checkin(self, timestamp: datetime) -> dict[str, bool]:
        """Broadcast check-in to all peers."""
        timestamp_str = timestamp.isoformat()
        signature = sign_message(self.config.secret_key, f"checkin:{timestamp_str}")

        return await self._broadcast_to_all(
            "sync/checkin",
            {
                "event": "checkin",
                "timestamp": timestamp_str,
                "signature": signature,
                "node": self.config.node_name,
            },
            update_peer_state=True,
        )

    async def broadcast_trigger(self, timestamp: datetime) -> dict[str, bool]:
        """Broadcast a trigger event to all peers. Outer HMAC over the timestamp."""
        timestamp_str = timestamp.isoformat()
        signature = sign_message(self.config.secret_key, f"trigger:{timestamp_str}")

        return await self._broadcast_to_all(
            "sync/trigger",
            {
                "event": "triggered",
                "timestamp": timestamp_str,
                "signature": signature,
                "node": self.config.node_name,
            },
        )

    async def broadcast_scheduled_complete(
        self,
        item_name: str,
        period: str,
    ) -> dict[str, bool]:
        """Broadcast scheduled item completion to all peers."""
        signature = sign_message(self.config.secret_key, f"scheduled:{item_name}:{period}")

        return await self._broadcast_to_all(
            "sync/scheduled",
            {
                "event": "scheduled_complete",
                "item": item_name,
                "period": period,
                "signature": signature,
                "node": self.config.node_name,
            },
        )

    async def get_peer_status(self, peer_url: str) -> PeerStatus:
        """Get status from a single peer."""
        data, error = await self._get(peer_url, "status")

        if error:
            return PeerStatus(
                url=peer_url,
                reachable=False,
                status=None,
                last_checkin=None,
                last_seen=None,
                error=error,
            )

        try:
            last_checkin = None
            if data and data.get("last_checkin"):
                last_checkin = datetime.fromisoformat(
                    data["last_checkin"].replace('Z', '+00:00')
                )

            return PeerStatus(
                url=peer_url,
                reachable=True,
                status=data.get("status") if data else None,
                last_checkin=last_checkin,
                last_seen=datetime.now(timezone.utc),
                error=None,
            )
        except (KeyError, ValueError) as e:
            return PeerStatus(
                url=peer_url,
                reachable=True,
                status=None,
                last_checkin=None,
                last_seen=datetime.now(timezone.utc),
                error=f"Invalid response: {e}",
            )

    async def get_all_peer_status(self) -> list[PeerStatus]:
        """Get status from all peers."""
        if not self.config.peers:
            return []

        tasks = [self.get_peer_status(url) for url in self.config.peers]
        return await asyncio.gather(*tasks)

    async def merge_state_from_peers(self) -> dict[str, str]:
        """Pull state from every peer and merge into local state.

        Used on every startup (not just corrupt-state recovery) to close the
        stale-state false-alarm window: a node returning from being offline
        adopts peer state before the watchdog fires anything.

        Merge rules:
        - last_checkin: take max(local, all peers).
        - status / trigger_time: if any peer is TRIGGERED, adopt TRIGGERED
          with that peer's trigger_time (federation bias: don't drop triggers).
        - schedule_state: union of completed period_keys across peers.

        Returns a dict describing what changed (empty if nothing changed).
        Never raises: peer-side errors are logged and skipped.
        """
        if not self.config.peers:
            return {}

        from posthumous.state import Status

        def parse_ts(value: object) -> datetime | None:
            """Parse a peer-supplied ISO timestamp, or None if absent/malformed.

            Returns None rather than raising so one bad peer payload cannot
            abort the merge (suppression-resistance: we must never silently
            drop a trigger because of a parse error elsewhere).
            """
            if not isinstance(value, str):
                return None
            try:
                return datetime.fromisoformat(value.replace('Z', '+00:00'))
            except ValueError:
                return None

        # Pull full state from every configured peer directly. We do NOT
        # pre-probe /status first: fetch() already returns None for an
        # unreachable peer, so the extra round-trip only doubled startup
        # latency and the chance of a clock-skew rejection on the real call.
        async def fetch(peer_url: str) -> dict | None:
            ts = datetime.now(timezone.utc).isoformat()
            sig = sign_message(self.config.secret_key, f"state:{ts}")
            data, error = await self._get(
                peer_url, "sync/state", params={"ts": ts, "sig": sig}
            )
            if error:
                # Surface the skipped peer. A silent drop here (e.g. a
                # clock-skew 401 from /sync/state) would mean a returning
                # offline node fails to adopt a peer's TRIGGERED status,
                # which is exactly the suppression this merge exists to stop.
                logger.warning(f"State merge: skipping peer {peer_url}: {error}")
                return None
            return data if isinstance(data, dict) else None

        peer_states = await asyncio.gather(
            *(fetch(url) for url in self.config.peers), return_exceptions=True
        )
        peer_states = [p for p in peer_states if isinstance(p, dict)]

        changes: dict[str, str] = {}
        state = self.state_manager.state

        # 1. Newest last_checkin wins.
        checkins = [parse_ts(p.get("last_checkin")) for p in peer_states]
        candidates = [c for c in checkins if c is not None]
        if state.last_checkin is not None:
            candidates.append(state.last_checkin)
        best_checkin = max(candidates) if candidates else None
        if best_checkin is not None and best_checkin != state.last_checkin:
            state.last_checkin = best_checkin
            changes["last_checkin"] = best_checkin.isoformat()

        # 2. If any peer is TRIGGERED and we are not, adopt TRIGGERED.
        #    Adopt the earliest peer trigger_time (closest to the actual event).
        if state.status != Status.TRIGGERED:
            triggered_peers = [
                p for p in peer_states if p.get("status") == Status.TRIGGERED.value
            ]
            if triggered_peers:
                trigger_times = [parse_ts(p.get("trigger_time")) for p in triggered_peers]
                trigger_times = [t for t in trigger_times if t is not None]
                adopted = min(trigger_times) if trigger_times else datetime.now(timezone.utc)
                # The state machine requires going through GRACE before TRIGGERED.
                # Force the status field directly here: this is recovery, not a
                # live transition, and we already know peers fired.
                state.status = Status.TRIGGERED
                state.trigger_time = adopted
                changes["status"] = "triggered"
                changes["trigger_time"] = adopted.isoformat()

        # 3. Schedule state: union of completed period_keys.
        for data in peer_states:
            for name, item_data in (data.get("schedule_state") or {}).items():
                period = item_data.get("period") if isinstance(item_data, dict) else None
                if not period:
                    continue
                local_item = state.schedule_state.get(name)
                if local_item is None or local_item.period != period:
                    state.mark_schedule_item_run(name, period)
                    changes.setdefault("schedule_state", "merged")

        if changes:
            self.state_manager.save()
            logger.info(f"Merged state from peers: {changes}")

        return changes

    async def sync_state_from_peers(
        self, statuses: list[PeerStatus] | None = None
    ) -> bool:
        """Attempt to sync state from peers.

        Used for recovery when local state is corrupt or missing.
        If ``statuses`` is provided, those are used instead of querying peers
        again (avoids a duplicate round-trip when the caller already has them).
        Returns True if sync was successful.
        """
        if statuses is None:
            statuses = await self.get_all_peer_status()

        best_peer = pick_best_peer(statuses)
        if best_peer is None:
            logger.warning("No peers available for state sync")
            return False

        # Get full state from best peer (signed request)
        ts = datetime.now(timezone.utc).isoformat()
        sig = sign_message(self.config.secret_key, f"state:{ts}")
        data, error = await self._get(
            best_peer.url, "sync/state", params={"ts": ts, "sig": sig}
        )
        if error:
            logger.error(f"Failed to get state from {best_peer.url}: {error}")
            return False

        # Apply state
        from posthumous.state import State, Status

        try:
            state = self.state_manager.state

            if data.get("last_checkin"):
                state.last_checkin = datetime.fromisoformat(
                    data["last_checkin"].replace('Z', '+00:00')
                )

            if data.get("status"):
                state.status = Status(data["status"])

            if data.get("trigger_time"):
                state.trigger_time = datetime.fromisoformat(
                    data["trigger_time"].replace('Z', '+00:00')
                )

            # Sync schedule state
            for name, item_data in data.get("schedule_state", {}).items():
                if item_data.get("period"):
                    state.mark_schedule_item_run(name, item_data["period"])

            self.state_manager.save()
            logger.info(f"State synced from {best_peer.url}")
            return True

        except Exception as e:
            logger.exception(f"Error applying synced state: {e}")
            return False

    async def _health_check_once(self) -> None:
        """Run a single health check iteration.

        Checks all peers, updates state, and fires the on_peer_down callback
        when a peer exceeds the configured down threshold (once per episode).
        """
        statuses = await self.get_all_peer_status()
        down_threshold = self.config.peer_down_threshold

        now = datetime.now(timezone.utc)
        for status in statuses:
            peer_state = self.state_manager.state.peer_states.get(status.url)

            if status.reachable:
                self.state_manager.state.update_peer(
                    status.url, success=True
                )
                # Clear the alert guard now that the peer is healthy again.
                self._alerted_peers.discard(status.url)
                refreshed = self.state_manager.state.peer_states.get(status.url)
                if refreshed and refreshed.alerted_at is not None:
                    refreshed.alerted_at = None
            else:
                self.state_manager.state.update_peer(
                    status.url, success=False, error=status.error
                )

                # Check if we should alert (once per down episode). The guard
                # is persisted via PeerState.alerted_at so it survives daemon
                # restarts; the in-memory set is kept for intra-run speed.
                if peer_state and peer_state.last_seen:
                    down_duration = now - peer_state.last_seen
                    already_alerted = (
                        status.url in self._alerted_peers
                        or peer_state.alerted_at is not None
                    )
                    if down_duration >= down_threshold and not already_alerted:
                        logger.warning(
                            f"Peer {status.url} has been unreachable for {down_duration}"
                        )
                        self._alerted_peers.add(status.url)
                        peer_state.alerted_at = now
                        if self._on_peer_down:
                            await self._on_peer_down(
                                status.url,
                                down_duration,
                                peer_state.last_seen,
                            )

        self.state_manager.save()

    async def _health_check_loop(self) -> None:
        """Periodically check peer health."""
        check_interval = self.config.peer_check_interval.total_seconds()

        while self._running:
            try:
                await self._health_check_once()
                await asyncio.sleep(check_interval)
            except asyncio.CancelledError:
                logger.debug("Health check loop cancelled")
                break
            except Exception as e:
                logger.exception(f"Error in health check: {e}")
                try:
                    await asyncio.sleep(check_interval)
                except asyncio.CancelledError:
                    logger.debug("Health check loop cancelled during error backoff")
                    break

    def start_health_monitoring(self) -> None:
        """Start background health monitoring of peers."""
        if self._running:
            return

        self._running = True
        self._health_task = asyncio.create_task(self._health_check_loop())
        logger.info("Peer health monitoring started")

    async def stop_health_monitoring(self) -> None:
        """Stop health monitoring."""
        self._running = False
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
            self._health_task = None
        logger.info("Peer health monitoring stopped")


def pick_best_peer(statuses: list[PeerStatus]) -> PeerStatus | None:
    """Return the peer with the most recent last_checkin, or None.

    Used both for state recovery (corrupt local state) and for CLI preview
    in the ``recover`` command.
    """
    candidates = [s for s in statuses if s.reachable and s.last_checkin]
    if not candidates:
        return None
    return max(candidates, key=lambda s: s.last_checkin)


def format_peer_status(status: PeerStatus) -> str:
    """Format peer status for display."""
    if not status.reachable:
        return f"{status.url}: UNREACHABLE ({status.error})"

    parts = [status.url, status.status or "unknown"]

    if status.last_checkin:
        age = datetime.now(timezone.utc) - status.last_checkin
        days = age.days
        hours = int(age.total_seconds() // 3600) % 24
        parts.append(f"last checkin: {days}d {hours}h ago")

    return " | ".join(parts)
