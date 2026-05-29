"""Tests for the HTTP server endpoints."""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pyotp
from aiohttp.test_utils import TestClient, TestServer

from posthumous.auth import Authenticator, sign_message
from posthumous.config import Config
from posthumous.server import Server, _format_duration
from posthumous.state import StateManager, Status


SECRET = "JBSWY3DPEHPK3PXP"


@pytest.fixture
def config():
    """Create a test configuration.

    Uses listen='127.0.0.1:0' so any test that actually calls Server.start()
    binds to an OS-assigned ephemeral port. Tests that use TestClient (most of
    this module) ignore config.listen entirely. The few that bind for real
    (test_server_start_stop_lifecycle, test_server_stop_when_not_started) used
    to flake under port-8420 contention; ephemeral binding fixes that.
    """
    return Config(
        node_name="test-node",
        secret_key=SECRET,
        listen="127.0.0.1:0",
        checkin_interval=timedelta(days=7),
        warning_start=timedelta(days=8),
        grace_start=timedelta(days=12),
        trigger_at=timedelta(days=14),
    )


@pytest.fixture
def state_manager(tmp_path):
    """Create a test state manager."""
    return StateManager(tmp_path / "state.yaml")


@pytest.fixture
def authenticator():
    """Create a test authenticator."""
    return Authenticator(secret=SECRET)


@pytest.fixture
def peer_manager():
    """Create a mock peer manager."""
    mock = AsyncMock()
    mock.broadcast_checkin = AsyncMock()
    mock.broadcast_trigger = AsyncMock()
    mock.broadcast_scheduled_complete = AsyncMock()
    return mock


@pytest.fixture
def watchdog(config, state_manager):
    """Create a real Watchdog for testing."""
    from posthumous.watchdog import Watchdog
    return Watchdog(config, state_manager)


@pytest.fixture
async def client(config, state_manager, watchdog, authenticator, peer_manager):
    """Create an aiohttp test client for the server."""
    server = Server(config, state_manager, watchdog, authenticator, peer_manager)
    async with TestClient(TestServer(server.app)) as test_client:
        yield test_client


def generate_totp() -> str:
    """Generate a valid TOTP code for the test secret."""
    return pyotp.TOTP(SECRET, issuer="Posthumous").now()


async def get_csrf_token(client) -> str:
    """GET /checkin and extract the CSRF token from the cookie."""
    resp = await client.get("/checkin")
    cookies = {c.key: c for c in resp.cookies.values()}
    return cookies["csrf_token"].value


class TestHealthEndpoint:
    """Tests for GET /health."""

    @pytest.mark.asyncio
    async def test_health_returns_ok(self, client):
        resp = await client.get("/health")

        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"
        assert data["node"] == "test-node"


class TestIndexRedirect:
    """Tests for GET /."""

    @pytest.mark.asyncio
    async def test_index_redirects_to_checkin(self, client):
        resp = await client.get("/", allow_redirects=False)

        assert resp.status == 302
        assert resp.headers["Location"] == "/checkin"


class TestCheckinPage:
    """Tests for GET /checkin."""

    @pytest.mark.asyncio
    async def test_checkin_form_returns_html(self, client):
        resp = await client.get("/checkin")

        assert resp.status == 200
        assert resp.content_type == "text/html"
        text = await resp.text()
        assert "test-node" in text
        assert "Posthumous" in text

    @pytest.mark.asyncio
    async def test_checkin_form_shows_status(self, client, state_manager):
        state_manager.state.status = Status.WARNING
        resp = await client.get("/checkin")

        text = await resp.text()
        assert "WARNING" in text
        assert "warning" in text  # CSS class

class TestCheckinJSON:
    """Tests for POST /checkin with JSON content type."""

    @pytest.mark.asyncio
    async def test_valid_totp_returns_success(self, client, peer_manager):
        code = generate_totp()
        resp = await client.post(
            "/checkin",
            json={"totp": code},
        )

        assert resp.status == 200
        data = await resp.json()
        assert data["success"] is True
        assert data["status"] == "armed"
        assert "next_deadline" in data
        peer_manager.broadcast_checkin.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalid_totp_returns_401(self, client):
        resp = await client.post(
            "/checkin",
            json={"totp": "000000"},
        )

        assert resp.status == 401
        data = await resp.json()
        assert data["success"] is False
        assert data["error"] == "Invalid code"

    @pytest.mark.asyncio
    async def test_no_credentials_returns_400(self, client):
        resp = await client.post(
            "/checkin",
            json={},
        )

        assert resp.status == 400
        data = await resp.json()
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_triggered_returns_409(self, client, state_manager, authenticator):
        state_manager.state.status = Status.TRIGGERED
        state_manager.state.trigger_time = datetime.now(timezone.utc)

        code = generate_totp()
        resp = await client.post(
            "/checkin",
            json={"totp": code},
        )

        assert resp.status == 409
        data = await resp.json()
        assert data["success"] is False
        assert "triggered" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_invalid_json_returns_400(self, client):
        resp = await client.post(
            "/checkin",
            data=b"not json at all",
            headers={"Content-Type": "application/json"},
        )

        assert resp.status == 400
        data = await resp.json()
        assert data["success"] is False
        assert "Invalid JSON" in data["error"]

    @pytest.mark.asyncio
    async def test_valid_api_token(self, config, state_manager, watchdog, peer_manager):
        """Test check-in with a valid API token."""
        config.api_token = "my-secret-token"
        auth = Authenticator(secret=SECRET, api_token="my-secret-token")
        server = Server(config, state_manager, watchdog, auth, peer_manager)

        async with TestClient(TestServer(server.app)) as test_client:
            resp = await test_client.post(
                "/checkin",
                json={"token": "my-secret-token"},
            )

            assert resp.status == 200
            data = await resp.json()
            assert data["success"] is True

    @pytest.mark.asyncio
    async def test_invalid_api_token_returns_401(
        self, config, state_manager, watchdog, peer_manager
    ):
        """Test check-in with an invalid API token."""
        config.api_token = "correct-token"
        auth = Authenticator(secret=SECRET, api_token="correct-token")
        server = Server(config, state_manager, watchdog, auth, peer_manager)

        async with TestClient(TestServer(server.app)) as test_client:
            resp = await test_client.post(
                "/checkin",
                json={"token": "wrong-token"},
            )

            assert resp.status == 401
            data = await resp.json()
            assert data["success"] is False

    @pytest.mark.asyncio
    async def test_lockout_returns_429(self, client, state_manager):
        """Test that lockout after repeated failures returns 429."""
        state_manager.state.lockout_until = datetime.now(timezone.utc) + timedelta(minutes=10)

        resp = await client.post(
            "/checkin",
            json={"totp": "000000"},
        )

        assert resp.status == 429
        data = await resp.json()
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_checkin_without_peer_manager(self, config, state_manager, watchdog, authenticator):
        """Test that check-in works without a peer manager."""
        server = Server(config, state_manager, watchdog, authenticator, peer_manager=None)

        async with TestClient(TestServer(server.app)) as test_client:
            code = generate_totp()
            resp = await test_client.post(
                "/checkin",
                json={"totp": code},
            )

            assert resp.status == 200
            data = await resp.json()
            assert data["success"] is True


class TestCheckinForm:
    """Tests for POST /checkin with form data."""

    @pytest.mark.asyncio
    async def test_valid_totp_form_returns_html(self, client):
        csrf = await get_csrf_token(client)
        code = generate_totp()
        resp = await client.post(
            "/checkin",
            data={"totp": code, "csrf_token": csrf},
        )

        assert resp.status == 200
        assert resp.content_type == "text/html"
        text = await resp.text()
        assert "Check-in successful" in text

    @pytest.mark.asyncio
    async def test_invalid_totp_form_returns_html_error(self, client):
        csrf = await get_csrf_token(client)
        resp = await client.post(
            "/checkin",
            data={"totp": "000000", "csrf_token": csrf},
        )

        assert resp.status == 200
        assert resp.content_type == "text/html"
        text = await resp.text()
        assert "Invalid code" in text

    @pytest.mark.asyncio
    async def test_no_credentials_form_returns_html_error(self, client):
        csrf = await get_csrf_token(client)
        resp = await client.post(
            "/checkin",
            data={"csrf_token": csrf},
        )

        assert resp.status == 200
        assert resp.content_type == "text/html"
        text = await resp.text()
        assert "error" in text

    @pytest.mark.asyncio
    async def test_triggered_form_returns_html_error(self, client, state_manager):
        csrf = await get_csrf_token(client)
        state_manager.state.status = Status.TRIGGERED
        state_manager.state.trigger_time = datetime.now(timezone.utc)

        code = generate_totp()
        resp = await client.post(
            "/checkin",
            data={"totp": code, "csrf_token": csrf},
        )

        assert resp.status == 200
        assert resp.content_type == "text/html"
        text = await resp.text()
        assert "triggered" in text.lower()

    @pytest.mark.asyncio
    async def test_lockout_form_returns_html_error(self, client, state_manager):
        csrf = await get_csrf_token(client)
        state_manager.state.lockout_until = datetime.now(timezone.utc) + timedelta(minutes=10)

        resp = await client.post(
            "/checkin",
            data={"totp": "000000", "csrf_token": csrf},
        )

        assert resp.status == 200
        assert resp.content_type == "text/html"
        text = await resp.text()
        assert "locked out" in text.lower()


class TestStatusEndpoint:
    """Tests for GET /status."""

    @pytest.mark.asyncio
    async def test_status_returns_json(self, client):
        resp = await client.get("/status")

        assert resp.status == 200
        data = await resp.json()
        assert data["node_name"] == "test-node"
        assert data["status"] == "armed"
        assert data["last_checkin"] is None
        assert data["trigger_time"] is None
        assert "time_remaining" in data

    @pytest.mark.asyncio
    async def test_status_with_checkin(self, client, state_manager):
        checkin_time = datetime.now(timezone.utc) - timedelta(days=3)
        state_manager.state.last_checkin = checkin_time

        resp = await client.get("/status")

        data = await resp.json()
        assert data["status"] == "armed"
        assert data["last_checkin"] == checkin_time.isoformat()
        assert data["time_remaining"]["until_warning"] is not None

    @pytest.mark.asyncio
    async def test_status_when_triggered(self, client, state_manager):
        trigger_time = datetime.now(timezone.utc)
        state_manager.state.status = Status.TRIGGERED
        state_manager.state.trigger_time = trigger_time

        resp = await client.get("/status")

        data = await resp.json()
        assert data["status"] == "triggered"
        assert data["trigger_time"] == trigger_time.isoformat()


class TestSyncCheckin:
    """Tests for POST /sync/checkin."""

    @pytest.mark.asyncio
    async def test_valid_sync_checkin(self, client, state_manager):
        timestamp = datetime.now(timezone.utc).isoformat()
        message = f"checkin:{timestamp}"
        signature = sign_message(SECRET, message)

        resp = await client.post(
            "/sync/checkin",
            json={"timestamp": timestamp, "signature": signature},
        )

        assert resp.status == 200
        data = await resp.json()
        assert data["success"] is True
        assert state_manager.state.status == Status.ARMED

    @pytest.mark.asyncio
    async def test_invalid_signature_returns_401(self, client):
        timestamp = datetime.now(timezone.utc).isoformat()

        resp = await client.post(
            "/sync/checkin",
            json={"timestamp": timestamp, "signature": "invalid-signature"},
        )

        assert resp.status == 401
        data = await resp.json()
        assert data["error"] == "Invalid signature"

    @pytest.mark.asyncio
    async def test_missing_fields_returns_400(self, client):
        resp = await client.post(
            "/sync/checkin",
            json={"timestamp": "2026-01-01T00:00:00+00:00"},
        )

        assert resp.status == 400
        data = await resp.json()
        assert data["error"] == "Missing fields"

    @pytest.mark.asyncio
    async def test_missing_timestamp_returns_400(self, client):
        resp = await client.post(
            "/sync/checkin",
            json={"signature": "something"},
        )

        assert resp.status == 400
        data = await resp.json()
        assert data["error"] == "Missing fields"

    @pytest.mark.asyncio
    async def test_bad_json_returns_400(self, client):
        resp = await client.post(
            "/sync/checkin",
            data=b"not json",
            headers={"Content-Type": "application/json"},
        )

        assert resp.status == 400
        data = await resp.json()
        assert data["error"] == "Invalid JSON"


class TestSyncTrigger:
    """Tests for POST /sync/trigger."""

    @pytest.mark.asyncio
    async def test_valid_sync_trigger(self, client, state_manager):
        # State machine requires GRACE -> TRIGGERED transition
        # (ARMED -> TRIGGERED is not valid)
        state_manager.state.status = Status.GRACE

        timestamp = datetime.now(timezone.utc).isoformat()
        message = f"trigger:{timestamp}"
        signature = sign_message(SECRET, message)

        resp = await client.post(
            "/sync/trigger",
            json={"timestamp": timestamp, "signature": signature},
        )

        assert resp.status == 200
        data = await resp.json()
        assert data["success"] is True
        assert state_manager.state.status == Status.TRIGGERED

    @pytest.mark.asyncio
    async def test_invalid_signature_returns_401(self, client):
        timestamp = datetime.now(timezone.utc).isoformat()

        resp = await client.post(
            "/sync/trigger",
            json={"timestamp": timestamp, "signature": "bad-sig"},
        )

        assert resp.status == 401
        data = await resp.json()
        assert data["error"] == "Invalid signature"

    @pytest.mark.asyncio
    async def test_missing_fields_returns_400(self, client):
        resp = await client.post(
            "/sync/trigger",
            json={"timestamp": "2026-01-01T00:00:00+00:00"},
        )

        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_bad_json_returns_400(self, client):
        resp = await client.post(
            "/sync/trigger",
            data=b"{broken",
            headers={"Content-Type": "application/json"},
        )

        assert resp.status == 400
        data = await resp.json()
        assert data["error"] == "Invalid JSON"


class TestSyncTriggerTimestamp:
    """Tests for trigger timestamp propagation through sync (I1)."""

    @pytest.mark.asyncio
    async def test_sync_trigger_uses_peer_timestamp(self, client, state_manager):
        """Sync trigger should use the peer's timestamp, not now()."""
        state_manager.state.status = Status.GRACE

        # Use a fresh timestamp (within staleness window) so the request passes validation
        peer_time = datetime.now(timezone.utc)
        timestamp = peer_time.isoformat()
        message = f"trigger:{timestamp}"
        signature = sign_message(SECRET, message)

        resp = await client.post(
            "/sync/trigger",
            json={"timestamp": timestamp, "signature": signature},
        )

        assert resp.status == 200
        data = await resp.json()
        assert data["success"] is True
        assert state_manager.state.status == Status.TRIGGERED
        # The trigger_time should be the peer's timestamp, not a locally-generated now()
        assert state_manager.state.trigger_time == peer_time


class TestSyncScheduled:
    """Tests for POST /sync/scheduled."""

    @pytest.mark.asyncio
    async def test_valid_sync_scheduled(self, client, state_manager):
        item_name = "annual-backup"
        period = "2026"
        message = f"scheduled:{item_name}:{period}"
        signature = sign_message(SECRET, message)

        resp = await client.post(
            "/sync/scheduled",
            json={"item": item_name, "period": period, "signature": signature},
        )

        assert resp.status == 200
        data = await resp.json()
        assert data["success"] is True
        assert "annual-backup" in state_manager.state.schedule_state
        item_state = state_manager.state.schedule_state["annual-backup"]
        assert item_state.period == "2026"

    @pytest.mark.asyncio
    async def test_invalid_signature_returns_401(self, client):
        resp = await client.post(
            "/sync/scheduled",
            json={"item": "task", "period": "2026", "signature": "wrong"},
        )

        assert resp.status == 401
        data = await resp.json()
        assert data["error"] == "Invalid signature"

    @pytest.mark.asyncio
    async def test_missing_item_returns_400(self, client):
        resp = await client.post(
            "/sync/scheduled",
            json={"period": "2026", "signature": "something"},
        )

        assert resp.status == 400
        data = await resp.json()
        assert data["error"] == "Missing fields"

    @pytest.mark.asyncio
    async def test_missing_period_returns_400(self, client):
        resp = await client.post(
            "/sync/scheduled",
            json={"item": "task", "signature": "something"},
        )

        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_missing_signature_returns_400(self, client):
        resp = await client.post(
            "/sync/scheduled",
            json={"item": "task", "period": "2026"},
        )

        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_bad_json_returns_400(self, client):
        resp = await client.post(
            "/sync/scheduled",
            data=b"nope",
            headers={"Content-Type": "application/json"},
        )

        assert resp.status == 400
        data = await resp.json()
        assert data["error"] == "Invalid JSON"


class TestSyncStateAuth:
    """Tests for HMAC authentication on GET /sync/state."""

    @pytest.mark.asyncio
    async def test_sync_state_rejects_unsigned_request(self, client):
        resp = await client.get("/sync/state")
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_sync_state_rejects_stale_timestamp(self, client):
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        signature = sign_message(SECRET, f"state:{old_time}")
        resp = await client.get("/sync/state", params={"ts": old_time, "sig": signature})
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_sync_state_accepts_valid_auth(self, client):
        now = datetime.now(timezone.utc).isoformat()
        signature = sign_message(SECRET, f"state:{now}")
        resp = await client.get("/sync/state", params={"ts": now, "sig": signature})
        assert resp.status == 200
        data = await resp.json()
        assert "status" in data


class TestSyncState:
    """Tests for GET /sync/state."""

    @pytest.mark.asyncio
    async def test_sync_state_returns_json(self, client):
        now = datetime.now(timezone.utc).isoformat()
        signature = sign_message(SECRET, f"state:{now}")
        resp = await client.get("/sync/state", params={"ts": now, "sig": signature})

        assert resp.status == 200
        data = await resp.json()
        assert data["node_name"] == "test-node"
        assert data["status"] == "armed"
        assert data["last_checkin"] is None
        assert data["trigger_time"] is None
        assert data["schedule_state"] == {}

    @pytest.mark.asyncio
    async def test_sync_state_with_schedule(self, client, state_manager):
        state_manager.state.mark_schedule_item_run("weekly-email", "2026-W05")
        state_manager.state.mark_schedule_item_run("annual-backup", "2026")

        now = datetime.now(timezone.utc).isoformat()
        signature = sign_message(SECRET, f"state:{now}")
        resp = await client.get("/sync/state", params={"ts": now, "sig": signature})

        data = await resp.json()
        assert "weekly-email" in data["schedule_state"]
        assert data["schedule_state"]["weekly-email"]["period"] == "2026-W05"
        assert "annual-backup" in data["schedule_state"]
        assert data["schedule_state"]["annual-backup"]["period"] == "2026"

    @pytest.mark.asyncio
    async def test_sync_state_with_checkin_and_trigger(self, client, state_manager):
        checkin_time = datetime(2026, 1, 20, 10, 0, 0, tzinfo=timezone.utc)
        trigger_time = datetime(2026, 2, 3, 10, 0, 0, tzinfo=timezone.utc)
        state_manager.state.last_checkin = checkin_time
        state_manager.state.status = Status.TRIGGERED
        state_manager.state.trigger_time = trigger_time

        now = datetime.now(timezone.utc).isoformat()
        signature = sign_message(SECRET, f"state:{now}")
        resp = await client.get("/sync/state", params={"ts": now, "sig": signature})

        data = await resp.json()
        assert data["status"] == "triggered"
        assert data["last_checkin"] == checkin_time.isoformat()
        assert data["trigger_time"] == trigger_time.isoformat()


class TestDashboard:
    """Tests for GET /dashboard."""

    @pytest.mark.asyncio
    async def test_dashboard_returns_html(self, client):
        resp = await client.get("/dashboard")

        assert resp.status == 200
        assert resp.content_type == "text/html"
        text = await resp.text()
        assert "Dashboard" in text
        assert "test-node" in text
        assert "ARMED" in text

    @pytest.mark.asyncio
    async def test_dashboard_shows_no_checkins(self, client):
        resp = await client.get("/dashboard")
        text = await resp.text()
        assert "Never" in text
        assert "No check-ins recorded" in text

    @pytest.mark.asyncio
    async def test_dashboard_with_recent_checkin(self, client, state_manager):
        state_manager.state.last_checkin = datetime.now(timezone.utc) - timedelta(days=2, hours=3)
        resp = await client.get("/dashboard")
        text = await resp.text()
        assert "Since check-in" in text
        assert "2d 3h" in text

    @pytest.mark.asyncio
    async def test_dashboard_warning_status(self, client, state_manager):
        state_manager.state.status = Status.WARNING
        state_manager.state.last_checkin = datetime.now(timezone.utc) - timedelta(days=9)
        resp = await client.get("/dashboard")
        text = await resp.text()
        assert "WARNING" in text
        assert "warning" in text  # CSS class

    @pytest.mark.asyncio
    async def test_dashboard_triggered_shows_trigger_time(self, client, state_manager):
        trigger_time = datetime(2026, 2, 10, 14, 30, 0, tzinfo=timezone.utc)
        state_manager.state.status = Status.TRIGGERED
        state_manager.state.trigger_time = trigger_time
        state_manager.state.last_checkin = datetime.now(timezone.utc) - timedelta(days=30)
        resp = await client.get("/dashboard")
        text = await resp.text()
        assert "TRIGGERED" in text
        assert "2026-02-10 14:30 UTC" in text

    @pytest.mark.asyncio
    async def test_dashboard_no_peers(self, client):
        resp = await client.get("/dashboard")
        text = await resp.text()
        assert "No peers configured" in text

    @pytest.mark.asyncio
    async def test_dashboard_with_peers(self, config, state_manager, watchdog, authenticator, peer_manager):
        config.peers = ["http://peer1:8420", "http://peer2:8420"]
        from posthumous.state import PeerState
        state_manager.state.peer_states["http://peer1:8420"] = PeerState(
            url="http://peer1:8420",
            last_seen=datetime.now(timezone.utc) - timedelta(minutes=5),
            consecutive_failures=0,
        )
        state_manager.state.peer_states["http://peer2:8420"] = PeerState(
            url="http://peer2:8420",
            last_error="Connection refused",
        )
        server = Server(config, state_manager, watchdog, authenticator, peer_manager)
        async with TestClient(TestServer(server.app)) as test_client:
            resp = await test_client.get("/dashboard")
            text = await resp.text()
            assert "peer1" in text
            assert "peer2" in text
            assert "Connection refused" in text

    @pytest.mark.asyncio
    async def test_dashboard_no_schedule(self, client):
        resp = await client.get("/dashboard")
        text = await resp.text()
        assert "No post-trigger items configured" in text

    @pytest.mark.asyncio
    async def test_dashboard_with_schedule(self, config, state_manager, watchdog, authenticator, peer_manager):
        from posthumous.config import ScheduledItem
        config.post_trigger = [
            ScheduledItem(name="annual-email", when="every year on Jan 1"),
            ScheduledItem(name="weekly-check", when="every week"),
        ]
        state_manager.state.mark_schedule_item_run("annual-email", "2026")
        server = Server(config, state_manager, watchdog, authenticator, peer_manager)
        async with TestClient(TestServer(server.app)) as test_client:
            resp = await test_client.get("/dashboard")
            text = await resp.text()
            assert "annual-email" in text
            assert "weekly-check" in text
            assert "never" in text  # weekly-check hasn't run

    @pytest.mark.asyncio
    async def test_dashboard_has_nav_links(self, client):
        resp = await client.get("/dashboard")
        text = await resp.text()
        assert "/checkin" in text
        assert "/dashboard" in text
        assert "/status" in text

    @pytest.mark.asyncio
    async def test_dashboard_overdue(self, client, state_manager):
        """Dashboard shows OVERDUE when past trigger deadline."""
        state_manager.state.last_checkin = datetime.now(timezone.utc) - timedelta(days=30)
        state_manager.state.status = Status.GRACE
        resp = await client.get("/dashboard")
        text = await resp.text()
        assert "OVERDUE" in text


class TestServerTimeInfoEdgeCases:
    """Edge cases for _get_time_info and Server lifecycle."""

    @pytest.fixture
    def server(self, config, state_manager, authenticator, peer_manager):
        from posthumous.watchdog import Watchdog
        watchdog = Watchdog(config, state_manager)
        return Server(config, state_manager, watchdog, authenticator, peer_manager)

    def test_time_info_no_trigger_no_deadline(self, config, state_manager, server):
        """_get_time_info when checkin exists but trigger is not approaching."""
        # Set a recent checkin so until_trigger is still positive
        state_manager.state.last_checkin = datetime.now(timezone.utc) - timedelta(days=1)
        info = server._get_time_info()
        assert "Last check-in:" in info
        assert "Trigger in:" in info

    def test_time_info_past_trigger_deadline(self, config, state_manager, server):
        """_get_time_info when past the trigger deadline (until_trigger is None)."""
        # Set checkin very far in the past
        state_manager.state.last_checkin = datetime.now(timezone.utc) - timedelta(days=30)
        info = server._get_time_info()
        # until_trigger is None because we're past trigger threshold
        assert "Last check-in:" in info
        assert "Trigger in:" not in info

    @pytest.mark.asyncio
    async def test_server_start_stop_lifecycle(self, config, state_manager, authenticator, peer_manager):
        """Server.start() then .stop() should work cleanly."""
        from posthumous.watchdog import Watchdog
        watchdog = Watchdog(config, state_manager)
        server = Server(config, state_manager, watchdog, authenticator, peer_manager)

        await server.start()
        assert server._runner is not None

        await server.stop()
        assert server._runner is None

    @pytest.mark.asyncio
    async def test_server_stop_when_not_started(self, config, state_manager, authenticator, peer_manager):
        """stop() with no runner should be a no-op."""
        from posthumous.watchdog import Watchdog
        watchdog = Watchdog(config, state_manager)
        server = Server(config, state_manager, watchdog, authenticator, peer_manager)

        assert server._runner is None
        await server.stop()  # Should not raise
        assert server._runner is None


class TestServerPeerDisplay:
    """Tests for peer status display in the web UI."""

    @pytest.mark.asyncio
    async def test_peer_unknown_status_in_checkin_page(self, tmp_path):
        """Peer with no last_seen and no last_error shows 'unknown'."""
        from posthumous.state import PeerState
        from posthumous.peers import PeerManager
        from posthumous.watchdog import Watchdog

        config = Config(
            node_name="test",
            secret_key=SECRET,
            peers=["http://peer1:8420"],
        )
        state_manager = StateManager(tmp_path / "state.yaml")
        # Create a peer state with no last_seen and no last_error
        state_manager.state.peer_states["http://peer1:8420"] = PeerState(
            url="http://peer1:8420",
            last_seen=None,
            last_error=None,
            consecutive_failures=0,
        )

        authenticator = Authenticator(config)
        peer_manager = PeerManager(config, state_manager, authenticator)
        watchdog = Watchdog(config, state_manager)
        server = Server(config, state_manager, watchdog, authenticator, peer_manager)

        async with TestClient(TestServer(server.app)) as test_client:
            resp = await test_client.get("/dashboard")
            text = await resp.text()
            assert "unknown" in text
            assert "peer1" in text


class TestFormatDurationServer:
    """Tests for _format_duration() in server module (Issue 1)."""

    def test_sub_hour_shows_minutes(self):
        assert _format_duration(timedelta(minutes=45)) == "45m"

    def test_sub_minute_shows_seconds(self):
        assert _format_duration(timedelta(seconds=30)) == "30s"


class TestDashboardTimeDisplay:
    """Tests for dashboard time display improvements (Issue 1)."""

    @pytest.mark.asyncio
    async def test_dashboard_sub_hour_checkin(self, client, state_manager):
        """Sub-hour check-in should show minutes, not '0h'."""
        state_manager.state.last_checkin = datetime.now(timezone.utc) - timedelta(minutes=30)
        resp = await client.get("/dashboard")
        text = await resp.text()
        assert "30m" in text
        assert "0h" not in text

    @pytest.mark.asyncio
    async def test_checkin_page_sub_hour(self, client, state_manager):
        """Check-in page should show minutes for sub-hour times."""
        state_manager.state.last_checkin = datetime.now(timezone.utc) - timedelta(seconds=45)
        resp = await client.get("/checkin")
        text = await resp.text()
        assert "45s" in text


class TestDashboardStatusRows:
    """Tests for status-aware dashboard rows (Issue 3)."""

    @pytest.mark.asyncio
    async def test_warning_shows_active_row(self, client, state_manager):
        """WARNING state should show 'Warning: Active' row."""
        state_manager.state.status = Status.WARNING
        state_manager.state.last_checkin = datetime.now(timezone.utc) - timedelta(days=9)
        resp = await client.get("/dashboard")
        text = await resp.text()
        assert "Active" in text

    @pytest.mark.asyncio
    async def test_grace_shows_active_rows(self, client, state_manager):
        """GRACE state should show active rows for warning and grace."""
        state_manager.state.status = Status.GRACE
        state_manager.state.last_checkin = datetime.now(timezone.utc) - timedelta(days=13)
        resp = await client.get("/dashboard")
        text = await resp.text()
        assert "Active" in text

    @pytest.mark.asyncio
    async def test_triggered_shows_activated(self, client, state_manager):
        """TRIGGERED state should show 'Activated' for trigger row."""
        state_manager.state.status = Status.TRIGGERED
        state_manager.state.trigger_time = datetime.now(timezone.utc)
        state_manager.state.last_checkin = datetime.now(timezone.utc) - timedelta(days=20)
        resp = await client.get("/dashboard")
        text = await resp.text()
        assert "Activated" in text


class TestDashboardTriggeredCheckin:
    """Tests for TRIGGERED since_checkin display (Issue 4)."""

    @pytest.mark.asyncio
    async def test_triggered_shows_last_checkin(self, client, state_manager):
        """TRIGGERED state should show since_checkin, not 'Never'."""
        state_manager.state.last_checkin = datetime.now(timezone.utc) - timedelta(days=20)
        state_manager.state.status = Status.TRIGGERED
        state_manager.state.trigger_time = datetime.now(timezone.utc) - timedelta(days=6)
        resp = await client.get("/dashboard")
        text = await resp.text()
        assert "20d" in text
        assert "Never" not in text.split("Since check-in")[1].split("</td>")[0]


class TestDashboardOverdue:
    """Tests for OVERDUE display logic (Issue 2)."""

    @pytest.mark.asyncio
    async def test_armed_not_overdue(self, client, state_manager):
        """ARMED state should not show OVERDUE even with no checkin."""
        resp = await client.get("/dashboard")
        text = await resp.text()
        assert "OVERDUE" not in text


class TestFormHiding:
    """Tests for form hiding in TRIGGERED and lockout states (Issues 5+6)."""

    @pytest.mark.asyncio
    async def test_triggered_hides_form(self, client, state_manager):
        """TRIGGERED state should hide the check-in form."""
        state_manager.state.status = Status.TRIGGERED
        state_manager.state.trigger_time = datetime.now(timezone.utc)
        resp = await client.get("/checkin")
        text = await resp.text()
        assert '<form' not in text
        assert "no longer possible" in text.lower()

    @pytest.mark.asyncio
    async def test_lockout_hides_form(self, client, state_manager):
        """Lockout should hide the check-in form."""
        state_manager.state.lockout_until = datetime.now(timezone.utc) + timedelta(minutes=10)
        resp = await client.get("/checkin")
        text = await resp.text()
        assert '<form' not in text
        assert "locked" in text.lower()

    @pytest.mark.asyncio
    async def test_armed_shows_form(self, client, state_manager):
        """ARMED state should show the check-in form."""
        resp = await client.get("/checkin")
        text = await resp.text()
        assert '<form' in text

    @pytest.mark.asyncio
    async def test_lockout_on_checkin_post(self, client, state_manager):
        """Form POST with lockout should hide form in response."""
        csrf = await get_csrf_token(client)
        state_manager.state.lockout_until = datetime.now(timezone.utc) + timedelta(minutes=10)
        resp = await client.post("/checkin", data={"totp": "000000", "csrf_token": csrf})
        text = await resp.text()
        assert '<form' not in text
        assert "locked" in text.lower()

    @pytest.mark.asyncio
    async def test_triggered_checkin_post_hides_form(self, client, state_manager):
        """POST to triggered node should hide form in response."""
        csrf = await get_csrf_token(client)
        state_manager.state.status = Status.TRIGGERED
        state_manager.state.trigger_time = datetime.now(timezone.utc)
        code = generate_totp()
        resp = await client.post("/checkin", data={"totp": code, "csrf_token": csrf})
        text = await resp.text()
        assert '<form' not in text


class TestSyncReplayProtection:
    """Tests for replay protection on /sync/* endpoints."""

    @pytest.mark.asyncio
    async def test_sync_checkin_rejects_stale_timestamp(self, client):
        """A checkin with a 10-minute-old timestamp should be rejected."""
        stale_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        timestamp = stale_time.isoformat()
        message = f"checkin:{timestamp}"
        signature = sign_message(SECRET, message)

        resp = await client.post(
            "/sync/checkin",
            json={"timestamp": timestamp, "signature": signature},
        )

        assert resp.status == 401
        data = await resp.json()
        assert "stale" in data["error"].lower() or "expired" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_sync_checkin_accepts_fresh_timestamp(self, client, state_manager):
        """A checkin with a current timestamp should be accepted."""
        timestamp = datetime.now(timezone.utc).isoformat()
        message = f"checkin:{timestamp}"
        signature = sign_message(SECRET, message)

        resp = await client.post(
            "/sync/checkin",
            json={"timestamp": timestamp, "signature": signature},
        )

        assert resp.status == 200
        data = await resp.json()
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_sync_trigger_rejects_stale_timestamp(self, client, state_manager):
        """A trigger with a 10-minute-old timestamp should be rejected."""
        state_manager.state.status = Status.GRACE

        stale_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        timestamp = stale_time.isoformat()
        message = f"trigger:{timestamp}"
        signature = sign_message(SECRET, message)

        resp = await client.post(
            "/sync/trigger",
            json={"timestamp": timestamp, "signature": signature},
        )

        assert resp.status == 401
        data = await resp.json()
        assert "stale" in data["error"].lower() or "expired" in data["error"].lower()
        # Verify trigger was NOT processed
        assert state_manager.state.status == Status.GRACE

    @pytest.mark.asyncio
    async def test_sync_scheduled_rejects_stale_timestamp(self, client, state_manager):
        """A scheduled completion with a 10-minute-old timestamp should be rejected."""
        stale_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        timestamp = stale_time.isoformat()
        item_name = "annual-backup"
        period = "2026"
        message = f"scheduled:{item_name}:{period}:{timestamp}"
        signature = sign_message(SECRET, message)

        resp = await client.post(
            "/sync/scheduled",
            json={
                "item": item_name,
                "period": period,
                "timestamp": timestamp,
                "signature": signature,
            },
        )

        assert resp.status == 401
        data = await resp.json()
        assert "stale" in data["error"].lower() or "expired" in data["error"].lower()
        # Verify item was NOT marked complete
        assert item_name not in state_manager.state.schedule_state

    @pytest.mark.asyncio
    async def test_sync_checkin_rejects_future_timestamp(self, client):
        """A checkin with a timestamp 10 minutes in the future should be rejected."""
        future_time = datetime.now(timezone.utc) + timedelta(minutes=10)
        timestamp = future_time.isoformat()
        message = f"checkin:{timestamp}"
        signature = sign_message(SECRET, message)

        resp = await client.post(
            "/sync/checkin",
            json={"timestamp": timestamp, "signature": signature},
        )

        assert resp.status == 401
        data = await resp.json()
        assert "stale" in data["error"].lower() or "expired" in data["error"].lower()


class TestFavicon:
    """Tests for /favicon.ico endpoint (Issue 7)."""

    @pytest.mark.asyncio
    async def test_favicon_returns_svg(self, client):
        resp = await client.get("/favicon.ico")
        assert resp.status == 200
        assert resp.content_type == "image/svg+xml"
        text = await resp.text()
        assert "<svg" in text

    @pytest.mark.asyncio
    async def test_favicon_cache_header(self, client):
        resp = await client.get("/favicon.ico")
        assert "max-age=86400" in resp.headers.get("Cache-Control", "")


class TestCSRFProtection:
    """Tests for CSRF double-submit cookie on POST /checkin."""

    @pytest.mark.asyncio
    async def test_get_checkin_sets_csrf_cookie(self, client):
        """GET /checkin should set a csrf_token cookie."""
        resp = await client.get("/checkin")

        assert resp.status == 200
        cookies = {c.key: c for c in resp.cookies.values()}
        assert "csrf_token" in cookies
        assert len(cookies["csrf_token"].value) == 64  # hex(32 bytes)

    @pytest.mark.asyncio
    async def test_post_checkin_without_csrf_rejected(self, client):
        """POST /checkin with form data but no CSRF token should return 403."""
        resp = await client.post(
            "/checkin",
            data={"totp": "123456"},
        )

        assert resp.status == 403
        text = await resp.text()
        assert "CSRF" in text

    @pytest.mark.asyncio
    async def test_post_checkin_with_valid_csrf_accepted(self, client):
        """POST /checkin with matching CSRF cookie and field should not return 403."""
        # GET to obtain the CSRF token
        get_resp = await client.get("/checkin")
        cookies = {c.key: c for c in get_resp.cookies.values()}
        csrf_token = cookies["csrf_token"].value

        # POST with matching cookie (auto-sent by client) and form field
        resp = await client.post(
            "/checkin",
            data={"totp": "123456", "csrf_token": csrf_token},
        )

        # Should NOT be 403 (may be 401 for bad TOTP, that's fine)
        assert resp.status != 403

    @pytest.mark.asyncio
    async def test_json_api_bypasses_csrf(self, client):
        """POST /checkin with JSON content-type should bypass CSRF check."""
        resp = await client.post(
            "/checkin",
            json={"totp": "123456"},
        )

        # Should NOT be 403 (may be 401 for invalid TOTP)
        assert resp.status != 403
