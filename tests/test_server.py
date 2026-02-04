"""Tests for the HTTP server endpoints."""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pyotp
from aiohttp.test_utils import TestClient, TestServer

from posthumous.auth import Authenticator, sign_message
from posthumous.config import Config
from posthumous.server import Server
from posthumous.state import StateManager, Status


SECRET = "JBSWY3DPEHPK3PXP"


@pytest.fixture
def config():
    """Create a test configuration."""
    return Config(
        node_name="test-node",
        secret_key=SECRET,
        listen="127.0.0.1:8420",
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
        code = generate_totp()
        resp = await client.post(
            "/checkin",
            data={"totp": code},
        )

        assert resp.status == 200
        assert resp.content_type == "text/html"
        text = await resp.text()
        assert "Check-in successful" in text

    @pytest.mark.asyncio
    async def test_invalid_totp_form_returns_html_error(self, client):
        resp = await client.post(
            "/checkin",
            data={"totp": "000000"},
        )

        assert resp.status == 200
        assert resp.content_type == "text/html"
        text = await resp.text()
        assert "Invalid code" in text

    @pytest.mark.asyncio
    async def test_no_credentials_form_returns_html_error(self, client):
        resp = await client.post(
            "/checkin",
            data={},
        )

        assert resp.status == 200
        assert resp.content_type == "text/html"
        text = await resp.text()
        assert "error" in text

    @pytest.mark.asyncio
    async def test_triggered_form_returns_html_error(self, client, state_manager):
        state_manager.state.status = Status.TRIGGERED
        state_manager.state.trigger_time = datetime.now(timezone.utc)

        code = generate_totp()
        resp = await client.post(
            "/checkin",
            data={"totp": code},
        )

        assert resp.status == 200
        assert resp.content_type == "text/html"
        text = await resp.text()
        assert "triggered" in text.lower()

    @pytest.mark.asyncio
    async def test_lockout_form_returns_html_error(self, client, state_manager):
        state_manager.state.lockout_until = datetime.now(timezone.utc) + timedelta(minutes=10)

        resp = await client.post(
            "/checkin",
            data={"totp": "000000"},
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


class TestSyncState:
    """Tests for GET /sync/state."""

    @pytest.mark.asyncio
    async def test_sync_state_returns_json(self, client):
        resp = await client.get("/sync/state")

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

        resp = await client.get("/sync/state")

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

        resp = await client.get("/sync/state")

        data = await resp.json()
        assert data["status"] == "triggered"
        assert data["last_checkin"] == checkin_time.isoformat()
        assert data["trigger_time"] == trigger_time.isoformat()
