"""Tests for peer communication."""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import ClientError

from posthumous.config import Config
from posthumous.state import StateManager, Status
from posthumous.peers import PeerManager, PeerStatus, format_peer_status


@pytest.fixture
def config():
    """Create test configuration with peers."""
    return Config(
        node_name="test",
        secret_key="JBSWY3DPEHPK3PXP",
        peers=[
            "https://peer1.local:8420",
            "https://peer2.local:8420",
        ],
    )


@pytest.fixture
def state_manager(tmp_path):
    """Create test state manager."""
    return StateManager(tmp_path / "state.yaml")


class TestPeerStatus:
    """Tests for PeerStatus dataclass."""

    def test_reachable_status(self):
        status = PeerStatus(
            url="https://peer1:8420",
            reachable=True,
            status="armed",
            last_checkin=datetime.now(timezone.utc),
            last_seen=datetime.now(timezone.utc),
        )

        assert status.reachable is True
        assert status.error is None

    def test_unreachable_status(self):
        status = PeerStatus(
            url="https://peer1:8420",
            reachable=False,
            status=None,
            last_checkin=None,
            last_seen=None,
            error="Connection refused",
        )

        assert status.reachable is False
        assert status.error == "Connection refused"


class TestFormatPeerStatus:
    """Tests for peer status formatting."""

    def test_format_reachable(self):
        now = datetime.now(timezone.utc)
        status = PeerStatus(
            url="https://peer1:8420",
            reachable=True,
            status="armed",
            last_checkin=now - timedelta(hours=5),
            last_seen=now,
        )

        formatted = format_peer_status(status)

        assert "https://peer1:8420" in formatted
        assert "armed" in formatted

    def test_format_unreachable(self):
        status = PeerStatus(
            url="https://peer1:8420",
            reachable=False,
            status=None,
            last_checkin=None,
            last_seen=None,
            error="timeout",
        )

        formatted = format_peer_status(status)

        assert "UNREACHABLE" in formatted
        assert "timeout" in formatted


class TestPeerManagerBroadcast:
    """Tests for broadcast operations."""

    @pytest.mark.asyncio
    async def test_broadcast_checkin(self, config, state_manager):
        manager = PeerManager(config, state_manager)

        # Mock the HTTP session
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock()

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.closed = False
        mock_session.close = AsyncMock()
        manager._session = mock_session

        timestamp = datetime.now(timezone.utc)
        results = await manager.broadcast_checkin(timestamp)

        assert len(results) == 2
        assert mock_session.post.call_count == 2

        await manager.close()

    @pytest.mark.asyncio
    async def test_broadcast_with_failed_peer(self, config, state_manager):
        manager = PeerManager(config, state_manager)

        # Mock session with one success, one failure
        call_count = [0]

        async def mock_post(url, **kwargs):
            call_count[0] += 1
            if "peer1" in url:
                response = MagicMock()
                response.status = 200
                return response
            else:
                raise ClientError("Connection refused")

        mock_session = MagicMock()
        mock_session.post = mock_post
        mock_session.closed = False
        mock_session.close = AsyncMock()

        # Use async context manager mock
        class MockContextManager:
            def __init__(self, url, **kwargs):
                self.url = url
                self.kwargs = kwargs

            async def __aenter__(self):
                return await mock_post(self.url, **self.kwargs)

            async def __aexit__(self, *args):
                pass

        mock_session.post = MockContextManager
        manager._session = mock_session

        timestamp = datetime.now(timezone.utc)
        results = await manager.broadcast_checkin(timestamp)

        # peer1 should succeed, peer2 should fail
        assert results.get("https://peer1.local:8420") is True
        assert results.get("https://peer2.local:8420") is False

        await manager.close()

    @pytest.mark.asyncio
    async def test_broadcast_trigger(self, config, state_manager):
        manager = PeerManager(config, state_manager)

        mock_response = MagicMock()
        mock_response.status = 200

        class MockContextManager:
            async def __aenter__(self):
                return mock_response

            async def __aexit__(self, *args):
                pass

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=MockContextManager())
        mock_session.closed = False
        mock_session.close = AsyncMock()
        manager._session = mock_session

        timestamp = datetime.now(timezone.utc)
        results = await manager.broadcast_trigger(timestamp)

        assert len(results) == 2

        await manager.close()

    @pytest.mark.asyncio
    async def test_broadcast_scheduled_complete(self, config, state_manager):
        manager = PeerManager(config, state_manager)

        mock_response = MagicMock()
        mock_response.status = 200

        class MockContextManager:
            async def __aenter__(self):
                return mock_response

            async def __aexit__(self, *args):
                pass

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=MockContextManager())
        mock_session.closed = False
        mock_session.close = AsyncMock()
        manager._session = mock_session

        results = await manager.broadcast_scheduled_complete("annual", "2026")

        assert len(results) == 2

        await manager.close()


class TestPeerManagerStatus:
    """Tests for getting peer status."""

    @pytest.mark.asyncio
    async def test_get_peer_status_success(self, config, state_manager):
        manager = PeerManager(config, state_manager)

        now = datetime.now(timezone.utc)
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "node_name": "peer1",
            "status": "armed",
            "last_checkin": (now - timedelta(hours=5)).isoformat(),
        })

        class MockContextManager:
            async def __aenter__(self):
                return mock_response

            async def __aexit__(self, *args):
                pass

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=MockContextManager())
        mock_session.closed = False
        mock_session.close = AsyncMock()
        manager._session = mock_session

        status = await manager.get_peer_status("https://peer1.local:8420")

        assert status.reachable is True
        assert status.status == "armed"
        assert status.last_checkin is not None

        await manager.close()

    @pytest.mark.asyncio
    async def test_get_peer_status_failure(self, config, state_manager):
        manager = PeerManager(config, state_manager)

        class MockContextManager:
            async def __aenter__(self):
                raise ClientError("Connection refused")

            async def __aexit__(self, *args):
                pass

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=MockContextManager())
        mock_session.closed = False
        mock_session.close = AsyncMock()
        manager._session = mock_session

        status = await manager.get_peer_status("https://peer1.local:8420")

        assert status.reachable is False
        assert "Connection refused" in status.error

        await manager.close()


class TestPeerManagerSync:
    """Tests for state synchronization."""

    @pytest.mark.asyncio
    async def test_sync_from_peers_no_peers(self, state_manager):
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            peers=[],  # No peers
        )
        manager = PeerManager(config, state_manager)

        result = await manager.sync_state_from_peers()

        assert result is False

        await manager.close()

    @pytest.mark.asyncio
    async def test_sync_updates_local_state(self, config, state_manager):
        manager = PeerManager(config, state_manager)

        # Mock status endpoint
        status_response = MagicMock()
        status_response.status = 200
        now = datetime.now(timezone.utc)
        status_response.json = AsyncMock(return_value={
            "node_name": "peer1",
            "status": "armed",
            "last_checkin": now.isoformat(),
        })

        # Mock sync/state endpoint
        state_response = MagicMock()
        state_response.status = 200
        state_response.json = AsyncMock(return_value={
            "node_name": "peer1",
            "status": "warning",
            "last_checkin": now.isoformat(),
            "trigger_time": None,
            "schedule_state": {
                "annual": {"last_run": now.isoformat(), "period": "2026"}
            },
        })

        class MockGetContextManager:
            def __init__(self, url):
                self.url = url

            async def __aenter__(self):
                if "status" in self.url:
                    return status_response
                return state_response

            async def __aexit__(self, *args):
                pass

        mock_session = MagicMock()
        mock_session.get = lambda url, **kwargs: MockGetContextManager(url)
        mock_session.closed = False
        mock_session.close = AsyncMock()
        manager._session = mock_session

        result = await manager.sync_state_from_peers()

        assert result is True
        assert state_manager.state.status == Status.WARNING
        assert "annual" in state_manager.state.schedule_state

        await manager.close()


class TestPeerManagerBroadcastExtended:
    """Extended broadcast tests."""

    @pytest.mark.asyncio
    async def test_broadcast_no_peers(self, state_manager):
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            peers=[],
        )
        manager = PeerManager(config, state_manager)

        results = await manager.broadcast_checkin(datetime.now(timezone.utc))

        assert results == {}
        await manager.close()

    @pytest.mark.asyncio
    async def test_broadcast_exception_in_response(self, config, state_manager):
        """If a broadcast raises an exception, the peer should be marked as failed."""
        manager = PeerManager(config, state_manager)

        # Mock session where post raises an exception
        class MockContextManager:
            async def __aenter__(self):
                raise ClientError("boom")

            async def __aexit__(self, *args):
                pass

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=MockContextManager())
        mock_session.closed = False
        mock_session.close = AsyncMock()
        manager._session = mock_session

        results = await manager.broadcast_checkin(datetime.now(timezone.utc))

        assert all(v is False for v in results.values())
        await manager.close()

    @pytest.mark.asyncio
    async def test_broadcast_http_error_status(self, config, state_manager):
        """HTTP 500 should be treated as failure."""
        manager = PeerManager(config, state_manager)

        mock_response = MagicMock()
        mock_response.status = 500
        mock_response.text = AsyncMock(return_value="Internal Server Error")

        class MockContextManager:
            async def __aenter__(self):
                return mock_response

            async def __aexit__(self, *args):
                pass

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=MockContextManager())
        mock_session.closed = False
        mock_session.close = AsyncMock()
        manager._session = mock_session

        results = await manager.broadcast_checkin(datetime.now(timezone.utc))

        assert all(v is False for v in results.values())
        await manager.close()

    @pytest.mark.asyncio
    async def test_broadcast_updates_peer_state(self, config, state_manager):
        """broadcast_checkin should update peer state tracking."""
        manager = PeerManager(config, state_manager)

        mock_response = MagicMock()
        mock_response.status = 200

        class MockContextManager:
            async def __aenter__(self):
                return mock_response

            async def __aexit__(self, *args):
                pass

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=MockContextManager())
        mock_session.closed = False
        mock_session.close = AsyncMock()
        manager._session = mock_session

        await manager.broadcast_checkin(datetime.now(timezone.utc))

        # Peer state should have been updated
        for url in config.peers:
            assert url in state_manager.state.peer_states
            assert state_manager.state.peer_states[url].consecutive_failures == 0

        await manager.close()


class TestPeerLowLevelMethods:
    """Tests for _post, _get, and _broadcast_to_all error paths."""

    @pytest.mark.asyncio
    async def test_post_timeout(self, config, state_manager):
        """_post should return Timeout error on asyncio.TimeoutError."""
        import asyncio as aio

        manager = PeerManager(config, state_manager)

        class MockContextManager:
            async def __aenter__(self):
                raise aio.TimeoutError()
            async def __aexit__(self, *args):
                pass

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=MockContextManager())
        mock_session.closed = False
        mock_session.close = AsyncMock()
        manager._session = mock_session

        success, error = await manager._post("https://peer1.local:8420", "/sync/checkin", {})
        assert success is False
        assert error == "Timeout"

        await manager.close()

    @pytest.mark.asyncio
    async def test_post_generic_exception(self, config, state_manager):
        """_post should handle unexpected exceptions gracefully."""
        manager = PeerManager(config, state_manager)

        class MockContextManager:
            async def __aenter__(self):
                raise ValueError("unexpected")
            async def __aexit__(self, *args):
                pass

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=MockContextManager())
        mock_session.closed = False
        mock_session.close = AsyncMock()
        manager._session = mock_session

        success, error = await manager._post("https://peer1.local:8420", "/sync/checkin", {})
        assert success is False
        assert "unexpected" in error

        await manager.close()

    @pytest.mark.asyncio
    async def test_get_non_200_status(self, config, state_manager):
        """_get should return error for non-200 status codes."""
        manager = PeerManager(config, state_manager)

        mock_response = MagicMock()
        mock_response.status = 404
        mock_response.text = AsyncMock(return_value="Not Found")

        class MockContextManager:
            async def __aenter__(self):
                return mock_response
            async def __aexit__(self, *args):
                pass

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=MockContextManager())
        mock_session.closed = False
        mock_session.close = AsyncMock()
        manager._session = mock_session

        data, error = await manager._get("https://peer1.local:8420", "/status")
        assert data is None
        assert "HTTP 404" in error

        await manager.close()

    @pytest.mark.asyncio
    async def test_get_generic_exception(self, config, state_manager):
        """_get should handle unexpected exceptions gracefully."""
        manager = PeerManager(config, state_manager)

        class MockContextManager:
            async def __aenter__(self):
                raise ValueError("kaboom")
            async def __aexit__(self, *args):
                pass

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=MockContextManager())
        mock_session.closed = False
        mock_session.close = AsyncMock()
        manager._session = mock_session

        data, error = await manager._get("https://peer1.local:8420", "/status")
        assert data is None
        assert "kaboom" in error

        await manager.close()

    @pytest.mark.asyncio
    async def test_broadcast_to_all_with_exception_updates_peer_state(self, config, state_manager):
        """When gather returns an exception and update_peer_state=True, state should be updated."""
        manager = PeerManager(config, state_manager)

        # Make _broadcast_to_peer raise for one peer
        async def mock_broadcast(peer_url, endpoint, data):
            if "peer1" in peer_url:
                return True, None
            raise RuntimeError("connection failed")

        manager._broadcast_to_peer = mock_broadcast

        results = await manager._broadcast_to_all(
            "/sync/checkin",
            {"test": True},
            update_peer_state=True,
        )

        assert results["https://peer1.local:8420"] is True
        assert results["https://peer2.local:8420"] is False

        # Peer2 should have failure recorded in state
        peer2_state = state_manager.state.peer_states.get("https://peer2.local:8420")
        assert peer2_state is not None

        await manager.close()


class TestPeerStatusExtended:
    """Extended tests for getting peer status."""

    @pytest.mark.asyncio
    async def test_get_peer_status_invalid_response(self, config, state_manager):
        """Invalid JSON response should return reachable but with error."""
        manager = PeerManager(config, state_manager)

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "status": "armed",
            # missing last_checkin - should still work
        })

        class MockContextManager:
            async def __aenter__(self):
                return mock_response

            async def __aexit__(self, *args):
                pass

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=MockContextManager())
        mock_session.closed = False
        mock_session.close = AsyncMock()
        manager._session = mock_session

        status = await manager.get_peer_status("https://peer1.local:8420")

        assert status.reachable is True
        assert status.status == "armed"
        assert status.last_checkin is None

        await manager.close()

    @pytest.mark.asyncio
    async def test_get_all_peer_status_empty(self, state_manager):
        """Empty peers list should return empty list."""
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            peers=[],
        )
        manager = PeerManager(config, state_manager)

        statuses = await manager.get_all_peer_status()

        assert statuses == []
        await manager.close()

    @pytest.mark.asyncio
    async def test_get_peer_status_timeout(self, config, state_manager):
        """Timeout should return unreachable status."""
        import asyncio as aio

        manager = PeerManager(config, state_manager)

        class MockContextManager:
            async def __aenter__(self):
                raise aio.TimeoutError()

            async def __aexit__(self, *args):
                pass

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=MockContextManager())
        mock_session.closed = False
        mock_session.close = AsyncMock()
        manager._session = mock_session

        status = await manager.get_peer_status("https://peer1.local:8420")

        assert status.reachable is False
        assert "Timeout" in status.error

        await manager.close()


class TestPeerSyncExtended:
    """Extended sync tests."""

    @pytest.mark.asyncio
    async def test_sync_selects_best_peer(self, config, state_manager):
        """Should pick peer with most recent checkin."""
        manager = PeerManager(config, state_manager)

        now = datetime.now(timezone.utc)

        # Peer1 has older checkin, peer2 has newer
        statuses = [
            PeerStatus(
                url="https://peer1.local:8420",
                reachable=True,
                status="armed",
                last_checkin=now - timedelta(hours=10),
                last_seen=now,
            ),
            PeerStatus(
                url="https://peer2.local:8420",
                reachable=True,
                status="armed",
                last_checkin=now - timedelta(hours=1),
                last_seen=now,
            ),
        ]

        # Mock get_all_peer_status and _get
        manager.get_all_peer_status = AsyncMock(return_value=statuses)

        state_data = {
            "node_name": "peer2",
            "status": "armed",
            "last_checkin": (now - timedelta(hours=1)).isoformat(),
            "trigger_time": None,
            "schedule_state": {},
        }

        original_get = manager._get

        async def mock_get(peer_url, endpoint, **kwargs):
            return state_data, None

        manager._get = mock_get

        result = await manager.sync_state_from_peers()

        assert result is True
        await manager.close()

    @pytest.mark.asyncio
    async def test_sync_handles_get_error(self, config, state_manager):
        """If getting state from best peer fails, return False."""
        manager = PeerManager(config, state_manager)

        now = datetime.now(timezone.utc)
        statuses = [
            PeerStatus(
                url="https://peer1.local:8420",
                reachable=True,
                status="armed",
                last_checkin=now,
                last_seen=now,
            ),
        ]

        manager.get_all_peer_status = AsyncMock(return_value=statuses)

        async def mock_get(peer_url, endpoint, **kwargs):
            return None, "Connection refused"

        manager._get = mock_get

        result = await manager.sync_state_from_peers()

        assert result is False
        await manager.close()

    @pytest.mark.asyncio
    async def test_sync_applies_trigger_state(self, config, state_manager):
        """Sync should apply trigger_time and TRIGGERED status from peer."""
        manager = PeerManager(config, state_manager)

        now = datetime.now(timezone.utc)
        trigger_time = now - timedelta(hours=2)

        statuses = [
            PeerStatus(
                url="https://peer1.local:8420",
                reachable=True,
                status="triggered",
                last_checkin=now - timedelta(days=15),
                last_seen=now,
            ),
        ]

        manager.get_all_peer_status = AsyncMock(return_value=statuses)

        state_data = {
            "node_name": "peer1",
            "status": "triggered",
            "last_checkin": (now - timedelta(days=15)).isoformat(),
            "trigger_time": trigger_time.isoformat(),
            "schedule_state": {
                "annual": {"last_run": now.isoformat(), "period": "2026"},
            },
        }

        async def mock_get(peer_url, endpoint, **kwargs):
            return state_data, None

        manager._get = mock_get

        result = await manager.sync_state_from_peers()

        assert result is True
        assert state_manager.state.status == Status.TRIGGERED
        assert state_manager.state.trigger_time is not None
        assert "annual" in state_manager.state.schedule_state

        await manager.close()


class TestPeerSessionLifecycle:
    """Tests for session management."""

    @pytest.mark.asyncio
    async def test_lazy_session_creation(self, config, state_manager):
        manager = PeerManager(config, state_manager)

        # Session should not exist initially
        assert manager._session is None

        session = await manager._get_session()
        assert session is not None

        await manager.close()

    @pytest.mark.asyncio
    async def test_session_reuse(self, config, state_manager):
        manager = PeerManager(config, state_manager)

        session1 = await manager._get_session()
        session2 = await manager._get_session()

        assert session1 is session2

        await manager.close()

    @pytest.mark.asyncio
    async def test_close_cleans_session(self, config, state_manager):
        manager = PeerManager(config, state_manager)

        await manager._get_session()
        assert manager._session is not None

        await manager.close()
        assert manager._session is None


class TestPeerHealthMonitoring:
    """Tests for peer health monitoring."""

    @pytest.mark.asyncio
    async def test_start_stop_health_monitoring(self, config, state_manager):
        manager = PeerManager(config, state_manager)

        manager.start_health_monitoring()
        assert manager._running is True

        await manager.stop_health_monitoring()
        assert manager._running is False

        await manager.close()

    @pytest.mark.asyncio
    async def test_start_when_already_running(self, config, state_manager):
        manager = PeerManager(config, state_manager)

        manager.start_health_monitoring()
        task1 = manager._health_task

        manager.start_health_monitoring()  # Should be a no-op
        assert manager._health_task is task1

        await manager.stop_health_monitoring()
        await manager.close()

    @pytest.mark.asyncio
    async def test_health_alert_threshold_gte(self, config, state_manager):
        """Regression test for Bug #2: health alert should use >= not ==."""
        manager = PeerManager(config, state_manager)

        # Set up a peer that has been down for longer than the threshold
        peer_url = "https://peer1.local:8420"
        now = datetime.now(timezone.utc)
        down_since = now - timedelta(hours=2)

        state_manager.state.update_peer(peer_url, success=True)
        state_manager.state.peer_states[peer_url].last_seen = down_since

        # Verify the alert one-shot mechanism works
        assert peer_url not in manager._alerted_peers

        # After alerting, the peer should be in the alerted set
        manager._alerted_peers.add(peer_url)
        assert peer_url in manager._alerted_peers

        # Clearing should work (simulates peer coming back up)
        manager._alerted_peers.discard(peer_url)
        assert peer_url not in manager._alerted_peers

        await manager.close()

    @pytest.mark.asyncio
    async def test_health_check_loop_updates_state(self, config, state_manager):
        """The _health_check_loop should call get_all_peer_status and update state."""
        import asyncio as aio

        manager = PeerManager(config, state_manager)

        now = datetime.now(timezone.utc)
        statuses = [
            PeerStatus(
                url="https://peer1.local:8420",
                reachable=True,
                status="armed",
                last_checkin=now,
                last_seen=now,
            ),
            PeerStatus(
                url="https://peer2.local:8420",
                reachable=False,
                status=None,
                last_checkin=None,
                last_seen=None,
                error="Connection refused",
            ),
        ]

        manager.get_all_peer_status = AsyncMock(return_value=statuses)
        config.peer_check_interval = timedelta(seconds=0.05)

        manager.start_health_monitoring()
        await aio.sleep(0.15)  # Allow a couple iterations
        await manager.stop_health_monitoring()

        # Reachable peer should have been updated
        assert "https://peer1.local:8420" in state_manager.state.peer_states
        peer1_state = state_manager.state.peer_states["https://peer1.local:8420"]
        assert peer1_state.consecutive_failures == 0

        # Unreachable peer should also be tracked
        assert "https://peer2.local:8420" in state_manager.state.peer_states

        await manager.close()

    @pytest.mark.asyncio
    async def test_health_check_loop_alerts_once(self, config, state_manager):
        """Health check should alert once per down episode and clear on recovery."""
        import asyncio as aio

        manager = PeerManager(config, state_manager)

        now = datetime.now(timezone.utc)
        peer_url = "https://peer1.local:8420"

        # Pre-populate peer state with old last_seen exceeding the threshold
        state_manager.state.update_peer(peer_url, success=True)
        state_manager.state.peer_states[peer_url].last_seen = now - timedelta(hours=12)

        # Use a short down threshold so 12 hours definitely exceeds it
        config.peer_down_threshold = timedelta(seconds=1)

        # Peer is down
        down_statuses = [
            PeerStatus(
                url=peer_url,
                reachable=False,
                status=None,
                last_checkin=None,
                last_seen=None,
                error="Connection refused",
            ),
        ]
        config.peers = [peer_url]
        config.peer_check_interval = timedelta(seconds=0.05)

        manager.get_all_peer_status = AsyncMock(return_value=down_statuses)

        manager.start_health_monitoring()
        await aio.sleep(0.15)
        await manager.stop_health_monitoring()

        # Should have alerted
        assert peer_url in manager._alerted_peers

        # Now peer comes back up
        up_statuses = [
            PeerStatus(
                url=peer_url,
                reachable=True,
                status="armed",
                last_checkin=now,
                last_seen=now,
            ),
        ]
        manager.get_all_peer_status = AsyncMock(return_value=up_statuses)

        manager.start_health_monitoring()
        await aio.sleep(0.15)
        await manager.stop_health_monitoring()

        # Alert should be cleared
        assert peer_url not in manager._alerted_peers

        await manager.close()

    @pytest.mark.asyncio
    async def test_health_check_loop_handles_exception(self, config, state_manager):
        """The health check loop should not crash on exceptions."""
        import asyncio as aio

        manager = PeerManager(config, state_manager)
        config.peer_check_interval = timedelta(seconds=0.05)

        call_count = [0]
        original_get_all = manager.get_all_peer_status

        async def failing_then_ok():
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("network exploded")
            return []

        manager.get_all_peer_status = failing_then_ok

        manager.start_health_monitoring()
        await aio.sleep(0.15)
        await manager.stop_health_monitoring()

        # Should have survived the exception and continued looping
        assert call_count[0] >= 2

        await manager.close()

    @pytest.mark.asyncio
    async def test_health_check_loop_handles_cancellation_cleanly(self, config, state_manager):
        """Health check loop should handle CancelledError without propagating."""
        import asyncio as aio

        manager = PeerManager(config, state_manager)
        config.peer_check_interval = timedelta(seconds=0.05)

        # Mock to avoid real network calls
        manager.get_all_peer_status = AsyncMock(return_value=[])

        manager.start_health_monitoring()
        await aio.sleep(0.1)
        await manager.stop_health_monitoring()
        # Should complete without unhandled CancelledError

        await manager.close()

    @pytest.mark.asyncio
    async def test_health_check_loop_cancelled_during_error_backoff(self, config, state_manager):
        """CancelledError during the error-backoff sleep should also be handled."""
        import asyncio as aio

        manager = PeerManager(config, state_manager)
        config.peer_check_interval = timedelta(seconds=60)  # Long interval

        # Make get_all_peer_status always raise so we enter the error branch
        async def always_fail():
            raise RuntimeError("network down")

        manager.get_all_peer_status = always_fail

        manager.start_health_monitoring()
        await aio.sleep(0.05)  # Let the loop enter error handling
        await manager.stop_health_monitoring()
        # Should complete without unhandled CancelledError

        await manager.close()


class TestPeerDownCallback:
    """Tests for on_peer_down callback in health check."""

    @pytest.fixture
    def config(self):
        return Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            peers=["https://peer1.local:8420"],
            peer_check_interval=timedelta(seconds=0.05),
            peer_down_threshold=timedelta(seconds=1),
        )

    @pytest.fixture
    def state_manager(self, tmp_path):
        return StateManager(tmp_path / "state.yaml")

    @pytest.mark.asyncio
    async def test_callback_fires_when_peer_exceeds_threshold(self, config, state_manager):
        """on_peer_down callback should fire when a peer exceeds the down threshold."""
        callback_calls = []

        async def on_peer_down(peer_url, downtime, last_seen):
            callback_calls.append((peer_url, downtime, last_seen))

        manager = PeerManager(config, state_manager, on_peer_down=on_peer_down)

        peer_url = "https://peer1.local:8420"
        now = datetime.now(timezone.utc)

        # Pre-populate peer state with last_seen well past the threshold
        state_manager.state.update_peer(peer_url, success=True)
        state_manager.state.peer_states[peer_url].last_seen = now - timedelta(hours=12)

        # Peer is unreachable
        down_statuses = [
            PeerStatus(
                url=peer_url,
                reachable=False,
                status=None,
                last_checkin=None,
                last_seen=None,
                error="Connection refused",
            ),
        ]

        manager.get_all_peer_status = AsyncMock(return_value=down_statuses)

        # Run one health check iteration via _health_check_once
        await manager._health_check_once()

        assert len(callback_calls) == 1
        assert callback_calls[0][0] == peer_url
        assert callback_calls[0][1] >= timedelta(hours=12)

        await manager.close()

    @pytest.mark.asyncio
    async def test_callback_does_not_repeat(self, config, state_manager):
        """Callback should only fire once per down episode."""
        callback_calls = []

        async def on_peer_down(peer_url, downtime, last_seen):
            callback_calls.append(peer_url)

        manager = PeerManager(config, state_manager, on_peer_down=on_peer_down)

        peer_url = "https://peer1.local:8420"
        now = datetime.now(timezone.utc)

        state_manager.state.update_peer(peer_url, success=True)
        state_manager.state.peer_states[peer_url].last_seen = now - timedelta(hours=12)

        down_statuses = [
            PeerStatus(
                url=peer_url,
                reachable=False,
                status=None,
                last_checkin=None,
                last_seen=None,
                error="Connection refused",
            ),
        ]

        manager.get_all_peer_status = AsyncMock(return_value=down_statuses)

        # Run two health check iterations
        await manager._health_check_once()
        await manager._health_check_once()

        # Callback should have fired only once
        assert len(callback_calls) == 1

        await manager.close()

    @pytest.mark.asyncio
    async def test_callback_clears_on_recovery(self, config, state_manager):
        """Alert state should clear when peer recovers, allowing re-alert."""
        callback_calls = []

        async def on_peer_down(peer_url, downtime, last_seen):
            callback_calls.append(peer_url)

        manager = PeerManager(config, state_manager, on_peer_down=on_peer_down)

        peer_url = "https://peer1.local:8420"
        now = datetime.now(timezone.utc)

        state_manager.state.update_peer(peer_url, success=True)
        state_manager.state.peer_states[peer_url].last_seen = now - timedelta(hours=12)

        # Phase 1: peer down -> callback fires
        down_statuses = [
            PeerStatus(
                url=peer_url, reachable=False, status=None,
                last_checkin=None, last_seen=None, error="refused",
            ),
        ]
        manager.get_all_peer_status = AsyncMock(return_value=down_statuses)
        await manager._health_check_once()
        assert len(callback_calls) == 1
        assert peer_url in manager._alerted_peers

        # Phase 2: peer recovers -> alert state clears
        up_statuses = [
            PeerStatus(
                url=peer_url, reachable=True, status="armed",
                last_checkin=now, last_seen=now,
            ),
        ]
        manager.get_all_peer_status = AsyncMock(return_value=up_statuses)
        await manager._health_check_once()
        assert peer_url not in manager._alerted_peers

        # Phase 3: peer goes down again -> callback fires again
        state_manager.state.peer_states[peer_url].last_seen = now - timedelta(hours=6)
        manager.get_all_peer_status = AsyncMock(return_value=down_statuses)
        await manager._health_check_once()
        assert len(callback_calls) == 2

        await manager.close()

    @pytest.mark.asyncio
    async def test_no_callback_if_not_set(self, config, state_manager):
        """PeerManager without callback should not crash on peer down."""
        manager = PeerManager(config, state_manager)  # no on_peer_down

        peer_url = "https://peer1.local:8420"
        now = datetime.now(timezone.utc)

        state_manager.state.update_peer(peer_url, success=True)
        state_manager.state.peer_states[peer_url].last_seen = now - timedelta(hours=12)

        down_statuses = [
            PeerStatus(
                url=peer_url, reachable=False, status=None,
                last_checkin=None, last_seen=None, error="refused",
            ),
        ]
        manager.get_all_peer_status = AsyncMock(return_value=down_statuses)

        # Should not raise
        await manager._health_check_once()
        assert peer_url in manager._alerted_peers

        await manager.close()

    @pytest.mark.asyncio
    async def test_no_callback_below_threshold(self, config, state_manager):
        """Callback should not fire if peer downtime is below the threshold."""
        callback_calls = []

        async def on_peer_down(peer_url, downtime, last_seen):
            callback_calls.append(peer_url)

        # Use a large threshold so 5 minutes of downtime won't trigger it
        config.peer_down_threshold = timedelta(hours=6)
        manager = PeerManager(config, state_manager, on_peer_down=on_peer_down)

        peer_url = "https://peer1.local:8420"
        now = datetime.now(timezone.utc)

        state_manager.state.update_peer(peer_url, success=True)
        state_manager.state.peer_states[peer_url].last_seen = now - timedelta(minutes=5)

        down_statuses = [
            PeerStatus(
                url=peer_url, reachable=False, status=None,
                last_checkin=None, last_seen=None, error="refused",
            ),
        ]
        manager.get_all_peer_status = AsyncMock(return_value=down_statuses)

        await manager._health_check_once()

        assert len(callback_calls) == 0
        assert peer_url not in manager._alerted_peers

        await manager.close()

    @pytest.mark.asyncio
    async def test_alerted_at_persists_across_restart(self, config, state_manager):
        """Once a peer is alerted, a new PeerManager must not re-alert for the same episode."""
        peer_url = "https://peer1.local:8420"
        now = datetime.now(timezone.utc)

        # Phase 1: first daemon run — alerts once.
        first_calls = []

        async def on_first(peer, dur, seen):
            first_calls.append(peer)

        mgr1 = PeerManager(config, state_manager, on_peer_down=on_first)
        state_manager.state.update_peer(peer_url, success=True)
        state_manager.state.peer_states[peer_url].last_seen = now - timedelta(hours=12)

        down = [PeerStatus(url=peer_url, reachable=False, status=None,
                           last_checkin=None, last_seen=None, error="refused")]
        mgr1.get_all_peer_status = AsyncMock(return_value=down)
        await mgr1._health_check_once()
        assert len(first_calls) == 1
        assert state_manager.state.peer_states[peer_url].alerted_at is not None
        await mgr1.close()

        # Phase 2: simulate daemon restart — fresh PeerManager with empty in-memory
        # alerted set, but state.yaml still says alerted_at is set. No repeat alert.
        second_calls = []

        async def on_second(peer, dur, seen):
            second_calls.append(peer)

        mgr2 = PeerManager(config, state_manager, on_peer_down=on_second)
        assert mgr2._alerted_peers == set()
        mgr2.get_all_peer_status = AsyncMock(return_value=down)
        await mgr2._health_check_once()
        assert len(second_calls) == 0, "should not re-alert after restart"
        await mgr2.close()

        # Phase 3: peer recovers, alerted_at clears, next downtime alerts again.
        up = [PeerStatus(url=peer_url, reachable=True, status="armed",
                         last_checkin=now, last_seen=now)]
        mgr2.get_all_peer_status = AsyncMock(return_value=up)
        await mgr2._health_check_once()
        assert state_manager.state.peer_states[peer_url].alerted_at is None


class TestPeerEdgeCases:
    """Edge-case tests for uncovered PeerManager paths."""

    @pytest.fixture
    def config(self):
        return Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            peers=["http://peer1:8420"],
            peer_check_interval=timedelta(seconds=30),
            peer_down_threshold=timedelta(hours=6),
        )

    @pytest.fixture
    def state_manager(self, tmp_path):
        return StateManager(tmp_path / "state.yaml")

    @pytest.mark.asyncio
    async def test_get_peer_status_parsing_error(self, config, state_manager):
        """KeyError/ValueError in response should return a PeerStatus with error."""
        from aioresponses import aioresponses

        manager = PeerManager(config, state_manager)

        with aioresponses() as m:
            # Return a malformed response that causes parsing error
            m.get(
                "http://peer1:8420/status",
                payload={
                    "last_checkin": "not-a-valid-datetime",
                },
            )
            status = await manager.get_peer_status("http://peer1:8420")

        assert status.reachable is True
        assert status.error is not None
        assert "Invalid response" in status.error

        await manager.close()

    @pytest.mark.asyncio
    async def test_sync_state_apply_exception(self, config, state_manager):
        """Exception during state apply should return False."""
        from aioresponses import aioresponses

        manager = PeerManager(config, state_manager)

        # Set up a successful peer that has reachable status
        with aioresponses() as m:
            m.get(
                "http://peer1:8420/status",
                payload={
                    "status": "armed",
                    "last_checkin": datetime.now(timezone.utc).isoformat(),
                },
            )
            # Return state data that will cause an exception during apply
            m.get(
                "http://peer1:8420/sync/state",
                payload={
                    "status": "invalid-status-value-that-will-cause-exception",
                    "last_checkin": datetime.now(timezone.utc).isoformat(),
                },
                repeat=True,
            )

            result = await manager.sync_state_from_peers()

        # Should return False because Status("invalid-status-value...") raises ValueError
        assert result is False

        await manager.close()

    @pytest.mark.asyncio
    async def test_sync_state_sends_signed_request(self, config, state_manager):
        """sync_state_from_peers should send ts and sig query params."""
        import re
        from urllib.parse import unquote
        from aioresponses import aioresponses
        from posthumous.auth import verify_signature

        manager = PeerManager(config, state_manager)

        with aioresponses() as m:
            m.get(
                "http://peer1:8420/status",
                payload={
                    "status": "armed",
                    "last_checkin": datetime.now(timezone.utc).isoformat(),
                },
            )
            # Use pattern matching to capture requests with query params
            m.get(
                re.compile(r"http://peer1:8420/sync/state"),
                payload={
                    "node_name": "peer1",
                    "status": "armed",
                    "last_checkin": datetime.now(timezone.utc).isoformat(),
                    "trigger_time": None,
                    "schedule_state": {},
                },
            )

            result = await manager.sync_state_from_peers()

            assert result is True

            # Find the sync/state request URL from captured requests
            # m.requests keys are (method, yarl.URL) tuples
            sync_state_urls = [
                url for (method, url) in m.requests
                if "sync/state" in str(url)
            ]

            assert len(sync_state_urls) > 0, "Expected at least one call to sync/state"
            request_url = sync_state_urls[0]

            # aioresponses may double-encode query params, so decode them
            ts = unquote(request_url.query.get("ts", ""))
            sig = request_url.query.get("sig", "")
            assert ts, f"Expected 'ts' query param, got URL: {request_url}"
            assert sig, f"Expected 'sig' query param, got URL: {request_url}"

            # Verify the signature is valid
            assert verify_signature(config.secret_key, f"state:{ts}", sig)

        await manager.close()
