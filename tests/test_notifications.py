"""Tests for notification system."""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

from posthumous.notifications import (
    NotificationManager,
    NotificationResult,
    build_context,
    send_warning_notification,
    send_grace_notification,
    send_trigger_notification,
)


class TestNotificationManager:
    """Tests for NotificationManager."""

    def test_init_with_channels(self):
        channels = {
            "default": ["ntfy://test"],
            "urgent": ["mailto://test@test.com", "ntfy://urgent"],
        }
        manager = NotificationManager(channels)

        assert "default" in manager._apprise_instances
        assert "urgent" in manager._apprise_instances

    def test_list_channels(self):
        channels = {"default": ["ntfy://test"], "urgent": ["ntfy://urgent"]}
        manager = NotificationManager(channels)

        assert set(manager.list_channels()) == {"default", "urgent"}

    def test_get_channel_urls(self):
        channels = {"default": ["ntfy://test1", "ntfy://test2"]}
        manager = NotificationManager(channels)

        urls = manager.get_channel_urls("default")
        assert urls == ["ntfy://test1", "ntfy://test2"]

    def test_get_unknown_channel_urls(self):
        manager = NotificationManager({})
        assert manager.get_channel_urls("unknown") == []


class TestMessageFormatting:
    """Tests for message template formatting."""

    def test_format_simple_message(self):
        manager = NotificationManager({})

        result = manager.format_message("Hello {name}!", {"name": "World"})
        assert result == "Hello World!"

    def test_format_missing_key(self):
        manager = NotificationManager({})

        # Should not raise, just return template
        result = manager.format_message("Hello {name}!", {})
        assert result == "Hello N/A!"

    def test_format_message_missing_var_shows_na(self):
        manager = NotificationManager({})
        result = manager.format_message(
            "Hello {node_name}, {days_left} days left",
            {"node_name": "test-node"}
        )
        assert result == "Hello test-node, N/A days left"

    def test_format_with_context(self):
        manager = NotificationManager({})

        context = {
            "days_left": 5,
            "hours_left": 120,
            "node_name": "test-node",
        }

        result = manager.format_message(
            "Check-in needed. {days_left} days remaining.",
            context
        )
        assert result == "Check-in needed. 5 days remaining."


class TestBuildContext:
    """Tests for context building."""

    def test_basic_context(self):
        context = build_context(
            node_name="test-node",
            status="armed",
        )

        assert context["node_name"] == "test-node"
        assert context["status"] == "armed"
        assert "now" in context

    def test_context_with_trigger(self):
        trigger_time = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        context = build_context(
            node_name="test",
            status="triggered",
            trigger_time=trigger_time,
        )

        assert "trigger_time" in context
        assert "2026-01-15" in context["trigger_time"]

    def test_context_with_time_remaining(self):
        now = datetime.now(timezone.utc)
        last_checkin = now - timedelta(days=5)
        trigger_at = timedelta(days=14)

        context = build_context(
            node_name="test",
            status="armed",
            last_checkin=last_checkin,
            trigger_at=trigger_at,
        )

        assert "days_left" in context
        # days_left calculation: (checkin + trigger_at - now).days
        # With timedelta arithmetic, this should be 8 or 9 depending on exact timing
        assert context["days_left"] in (8, 9)  # 14 - 5 = 9, but .days may round down
        assert "hours_left" in context

    def test_context_with_past_deadline(self):
        now = datetime.now(timezone.utc)
        last_checkin = now - timedelta(days=20)
        trigger_at = timedelta(days=14)

        context = build_context(
            node_name="test",
            status="triggered",
            last_checkin=last_checkin,
            trigger_at=trigger_at,
        )

        # Should show 0 when past deadline
        assert context["days_left"] == 0

    def test_context_with_base_url(self):
        context = build_context(
            node_name="test",
            status="armed",
            base_url="https://example.com:8420",
        )

        assert context["base_url"] == "https://example.com:8420"
        assert context["checkin_url"] == "https://example.com:8420/checkin"
        assert context["dashboard_url"] == "https://example.com:8420/dashboard"

    def test_context_with_base_url_trailing_slash(self):
        context = build_context(
            node_name="test",
            status="armed",
            base_url="https://example.com:8420/",
        )

        assert context["base_url"] == "https://example.com:8420"
        assert context["checkin_url"] == "https://example.com:8420/checkin"

    def test_context_without_base_url(self):
        context = build_context(
            node_name="test",
            status="armed",
        )

        assert "base_url" not in context
        assert "checkin_url" not in context
        assert "dashboard_url" not in context

    def test_context_with_base_url_none(self):
        context = build_context(
            node_name="test",
            status="armed",
            base_url=None,
        )

        assert "checkin_url" not in context

    def test_url_variables_render_in_message(self):
        manager = NotificationManager({})
        context = build_context(
            node_name="test",
            status="warning",
            base_url="https://phm.example.com",
        )
        result = manager.format_message(
            "Check in: {checkin_url}", context
        )
        assert result == "Check in: https://phm.example.com/checkin"

    def test_url_variables_in_full_message(self):
        manager = NotificationManager({})
        now = datetime.now(timezone.utc)
        context = build_context(
            node_name="mynode",
            status="warning",
            last_checkin=now - timedelta(days=5),
            trigger_at=timedelta(days=14),
            base_url="http://localhost:8420",
        )
        result = manager.format_message(
            "Check-in needed. {days_left} days left.\n\nCheck in: {checkin_url}",
            context,
        )
        assert "http://localhost:8420/checkin" in result
        assert "days left" in result


class TestFederationHealthContext:
    """Tests for the {healthy_peers}/{total_peers}/{dead_peers} context vars.

    The "dead" definition must match the on_peer_down alert: a peer counts as
    dead only once its last successful contact is older than peer_down_threshold.
    """

    def _peer_state(self, last_seen, consecutive_failures=0):
        from posthumous.state import PeerState
        return PeerState(
            url="http://p:8420",
            last_seen=last_seen,
            consecutive_failures=consecutive_failures,
        )

    def test_no_peer_urls_omits_vars(self):
        context = build_context(node_name="n", status="armed")
        assert "total_peers" not in context
        assert "healthy_peers" not in context
        assert "dead_peers" not in context

    def test_all_peers_healthy(self):
        now = datetime.now(timezone.utc)
        peers = ["http://p1:8420", "http://p2:8420"]
        states = {
            "http://p1:8420": self._peer_state(now - timedelta(minutes=5)),
            "http://p2:8420": self._peer_state(now - timedelta(minutes=1)),
        }
        ctx = build_context(
            node_name="n", status="armed",
            peer_urls=peers, peer_states=states,
            peer_down_threshold=timedelta(hours=6),
        )
        assert ctx["total_peers"] == 2
        assert ctx["healthy_peers"] == 2
        assert ctx["dead_peers"] == 0

    def test_peer_past_threshold_is_dead(self):
        now = datetime.now(timezone.utc)
        peers = ["http://p1:8420"]
        states = {"http://p1:8420": self._peer_state(now - timedelta(hours=7))}
        ctx = build_context(
            node_name="n", status="armed",
            peer_urls=peers, peer_states=states,
            peer_down_threshold=timedelta(hours=6),
        )
        assert ctx["healthy_peers"] == 0
        assert ctx["dead_peers"] == 1

    def test_transient_failure_with_recent_last_seen_is_healthy(self):
        """The #3 fix: one failed probe (consecutive_failures>0) but a recent
        last_seen must NOT count as dead, matching on_peer_down semantics."""
        now = datetime.now(timezone.utc)
        peers = ["http://p1:8420"]
        states = {
            "http://p1:8420": self._peer_state(
                now - timedelta(minutes=2), consecutive_failures=3
            ),
        }
        ctx = build_context(
            node_name="n", status="armed",
            peer_urls=peers, peer_states=states,
            peer_down_threshold=timedelta(hours=6),
        )
        assert ctx["healthy_peers"] == 1
        assert ctx["dead_peers"] == 0

    def test_never_contacted_peer_is_dead(self):
        peers = ["http://p1:8420"]
        states = {"http://p1:8420": self._peer_state(None)}
        ctx = build_context(
            node_name="n", status="armed",
            peer_urls=peers, peer_states=states,
            peer_down_threshold=timedelta(hours=6),
        )
        assert ctx["healthy_peers"] == 0
        assert ctx["dead_peers"] == 1

    def test_no_peer_states_means_all_dead(self):
        peers = ["http://p1:8420", "http://p2:8420"]
        ctx = build_context(
            node_name="n", status="armed",
            peer_urls=peers, peer_states=None,
        )
        assert ctx["total_peers"] == 2
        assert ctx["healthy_peers"] == 0
        assert ctx["dead_peers"] == 2

    def test_default_threshold_when_unset(self):
        now = datetime.now(timezone.utc)
        peers = ["http://p1:8420"]
        # 5h old, default threshold is 6h -> still healthy
        states = {"http://p1:8420": self._peer_state(now - timedelta(hours=5))}
        ctx = build_context(
            node_name="n", status="armed",
            peer_urls=peers, peer_states=states,
        )
        assert ctx["healthy_peers"] == 1


class TestNotificationSending:
    """Tests for actual notification sending."""

    @pytest.mark.asyncio
    async def test_send_unknown_channel(self):
        manager = NotificationManager({})

        result = await manager.send(
            channel="unknown",
            message="Test message",
        )

        assert result.success is False
        assert "Unknown" in result.error

    @pytest.mark.asyncio
    async def test_send_with_mock_apprise(self):
        manager = NotificationManager({"default": ["ntfy://test"]})

        # Mock the Apprise instance
        mock_apprise = MagicMock()
        mock_apprise.notify.return_value = True
        mock_apprise.__len__ = lambda self: 1
        manager._apprise_instances["default"] = mock_apprise

        result = await manager.send(
            channel="default",
            message="Test message",
        )

        assert result.success is True
        mock_apprise.notify.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_with_retry_on_failure(self):
        manager = NotificationManager(
            {"default": ["ntfy://test"]},
            retry_count=3,
            retry_delay=0.01,
        )

        mock_apprise = MagicMock()
        mock_apprise.notify.return_value = False
        mock_apprise.__len__ = lambda self: 1
        manager._apprise_instances["default"] = mock_apprise

        result = await manager.send(
            channel="default",
            message="Test message",
        )

        assert result.success is False
        assert mock_apprise.notify.call_count == 3  # 3 retries

    @pytest.mark.asyncio
    async def test_test_channel(self):
        manager = NotificationManager({"default": ["ntfy://test"]})

        mock_apprise = MagicMock()
        mock_apprise.notify.return_value = True
        mock_apprise.__len__ = lambda self: 1
        manager._apprise_instances["default"] = mock_apprise

        result = await manager.test_channel("default")

        assert result.success is True
        # Verify test message was sent
        call_args = mock_apprise.notify.call_args
        assert "test" in call_args.kwargs.get("body", "").lower() or \
               "test" in call_args.kwargs.get("title", "").lower()

    @pytest.mark.asyncio
    async def test_send_to_multiple_channels(self):
        manager = NotificationManager({
            "channel1": ["ntfy://test1"],
            "channel2": ["ntfy://test2"],
        })

        for name in ["channel1", "channel2"]:
            mock_apprise = MagicMock()
            mock_apprise.notify.return_value = True
            mock_apprise.__len__ = lambda self: 1
            manager._apprise_instances[name] = mock_apprise

        results = await manager.send_to_multiple(
            channels=["channel1", "channel2"],
            message="Test message",
        )

        assert len(results) == 2
        assert all(r.success for r in results)

    @pytest.mark.asyncio
    async def test_send_exception_retries(self):
        """Exception during Apprise notify should retry."""
        manager = NotificationManager(
            {"default": ["ntfy://test"]},
            retry_count=3,
            retry_delay=0.01,
        )

        mock_apprise = MagicMock()
        mock_apprise.notify.side_effect = RuntimeError("connection error")
        mock_apprise.__len__ = lambda self: 1
        manager._apprise_instances["default"] = mock_apprise

        result = await manager.send(channel="default", message="Test")

        assert result.success is False
        assert mock_apprise.notify.call_count == 3
        assert "connection error" in result.error

    @pytest.mark.asyncio
    async def test_send_eventual_success_after_retries(self):
        """Should succeed if retry eventually works."""
        manager = NotificationManager(
            {"default": ["ntfy://test"]},
            retry_count=3,
            retry_delay=0.01,
        )

        mock_apprise = MagicMock()
        mock_apprise.notify.side_effect = [False, False, True]
        mock_apprise.__len__ = lambda self: 1
        manager._apprise_instances["default"] = mock_apprise

        result = await manager.send(channel="default", message="Test")

        assert result.success is True
        assert mock_apprise.notify.call_count == 3

    @pytest.mark.asyncio
    async def test_test_all_channels(self):
        manager = NotificationManager({
            "ch1": ["ntfy://t1"],
            "ch2": ["ntfy://t2"],
        })

        for name in ["ch1", "ch2"]:
            mock_apprise = MagicMock()
            mock_apprise.notify.return_value = True
            mock_apprise.__len__ = lambda self: 1
            manager._apprise_instances[name] = mock_apprise

        results = await manager.test_all_channels()

        assert len(results) == 2
        assert "ch1" in results
        assert "ch2" in results
        assert all(r.success for r in results.values())

    @pytest.mark.asyncio
    async def test_test_all_channels_empty(self):
        manager = NotificationManager({})
        results = await manager.test_all_channels()
        assert results == {}


class TestConvenienceFunctions:
    """Tests for send_warning_notification, send_grace_notification, send_trigger_notification."""

    @pytest.mark.asyncio
    async def test_send_warning_notification(self):
        manager = NotificationManager({"default": ["ntfy://test"]})
        mock_apprise = MagicMock()
        mock_apprise.notify.return_value = True
        mock_apprise.__len__ = lambda self: 1
        manager._apprise_instances["default"] = mock_apprise

        context = {"node_name": "test", "status": "warning"}
        result = await send_warning_notification(
            manager, "default", "Warning test", context,
        )

        assert result.success is True
        call_kwargs = mock_apprise.notify.call_args.kwargs
        assert call_kwargs["title"] == "Posthumous Warning"

    @pytest.mark.asyncio
    async def test_send_grace_notification(self):
        manager = NotificationManager({"default": ["ntfy://test"]})
        mock_apprise = MagicMock()
        mock_apprise.notify.return_value = True
        mock_apprise.__len__ = lambda self: 1
        manager._apprise_instances["default"] = mock_apprise

        context = {"node_name": "test", "status": "grace"}
        result = await send_grace_notification(
            manager, "default", "Grace test", context,
        )

        assert result.success is True
        call_kwargs = mock_apprise.notify.call_args.kwargs
        assert call_kwargs["title"] == "Posthumous - URGENT"

    @pytest.mark.asyncio
    async def test_send_trigger_notification(self):
        manager = NotificationManager({"default": ["ntfy://test"]})
        mock_apprise = MagicMock()
        mock_apprise.notify.return_value = True
        mock_apprise.__len__ = lambda self: 1
        manager._apprise_instances["default"] = mock_apprise

        context = {"node_name": "test", "status": "triggered"}
        result = await send_trigger_notification(
            manager, "default", "Trigger test", context,
        )

        assert result.success is True
        call_kwargs = mock_apprise.notify.call_args.kwargs
        assert call_kwargs["title"] == "Posthumous Activated"
