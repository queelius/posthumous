"""Notification system using Apprise."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import apprise

logger = logging.getLogger(__name__)


@dataclass
class NotificationResult:
    """Result of a notification attempt."""
    success: bool
    channel: str
    urls_tried: int
    urls_succeeded: int
    error: str | None = None


class NotificationManager:
    """Manages notification channels and sending via Apprise."""

    def __init__(
        self,
        channels: dict[str, list[str]],
        retry_count: int = 3,
        retry_delay: float = 5.0,
    ):
        """Initialize notification manager.

        Args:
            channels: Mapping of channel names to Apprise URL lists
            retry_count: Number of retry attempts per notification
            retry_delay: Seconds between retries
        """
        self.channels = channels
        self.retry_count = retry_count
        self.retry_delay = retry_delay

        # Pre-create Apprise instances for each channel
        self._apprise_instances: dict[str, apprise.Apprise] = {}
        for name, urls in channels.items():
            ap = apprise.Apprise()
            for url in urls:
                ap.add(url)
            self._apprise_instances[name] = ap

    def _get_apprise(self, channel: str) -> apprise.Apprise:
        """Get Apprise instance for a channel."""
        if channel not in self._apprise_instances:
            raise ValueError(f"Unknown notification channel: {channel}")
        return self._apprise_instances[channel]

    def format_message(
        self,
        template: str,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Format a message template with context variables.

        Available variables:
        - {days_left}: Days until trigger
        - {hours_left}: Hours until trigger
        - {node_name}: Name of this node
        - {trigger_time}: When trigger occurred (ISO format)
        - {status}: Current status
        - {base_url}: Base URL of the web UI
        - {checkin_url}: Direct link to check-in page
        - {dashboard_url}: Direct link to dashboard page
        """
        context = context or {}

        # Safe formatting that substitutes missing keys with "N/A"
        try:
            return template.format_map(defaultdict(lambda: "N/A", context))
        except (KeyError, ValueError, IndexError) as e:
            logger.warning(f"Error formatting template: {e}")
            return template

    async def send(
        self,
        channel: str,
        message: str,
        title: str = "Posthumous",
        context: dict[str, Any] | None = None,
    ) -> NotificationResult:
        """Send a notification to a channel with retries.

        Args:
            channel: Name of the notification channel
            message: Message body (can include template variables)
            title: Notification title
            context: Variables for template formatting

        Returns:
            NotificationResult with success status and details
        """
        try:
            ap = self._get_apprise(channel)
        except ValueError as e:
            return NotificationResult(
                success=False,
                channel=channel,
                urls_tried=0,
                urls_succeeded=0,
                error=str(e),
            )

        # Format message with context
        formatted_message = self.format_message(message, context)

        urls_count = len(ap)
        last_error = None

        for attempt in range(self.retry_count):
            try:
                # Apprise.notify is synchronous, run in executor
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None,
                    lambda: ap.notify(
                        title=title,
                        body=formatted_message,
                    )
                )

                if result:
                    logger.info(f"Notification sent to channel '{channel}'")
                    return NotificationResult(
                        success=True,
                        channel=channel,
                        urls_tried=urls_count,
                        urls_succeeded=urls_count,  # Apprise doesn't give per-URL results
                    )
                else:
                    last_error = "Apprise returned False"

            except Exception as e:
                last_error = str(e)
                logger.warning(
                    f"Notification attempt {attempt + 1}/{self.retry_count} "
                    f"to '{channel}' failed: {e}"
                )

            if attempt < self.retry_count - 1:
                await asyncio.sleep(self.retry_delay)

        logger.error(f"All notification attempts to '{channel}' failed")
        return NotificationResult(
            success=False,
            channel=channel,
            urls_tried=urls_count,
            urls_succeeded=0,
            error=last_error,
        )

    async def send_to_multiple(
        self,
        channels: list[str],
        message: str,
        title: str = "Posthumous",
        context: dict[str, Any] | None = None,
    ) -> list[NotificationResult]:
        """Send notification to multiple channels concurrently."""
        tasks = [
            self.send(channel, message, title, context)
            for channel in channels
        ]
        return await asyncio.gather(*tasks)

    async def test_channel(self, channel: str) -> NotificationResult:
        """Send a test notification to verify channel configuration."""
        return await self.send(
            channel=channel,
            message="This is a test notification from Posthumous.",
            title="Posthumous Test",
            context={"status": "test"},
        )

    async def test_all_channels(self) -> dict[str, NotificationResult]:
        """Test all configured channels."""
        results = {}
        for channel in self.channels:
            results[channel] = await self.test_channel(channel)
        return results

    def get_channel_urls(self, channel: str) -> list[str]:
        """Get the URLs configured for a channel (for display)."""
        return self.channels.get(channel, [])

    def list_channels(self) -> list[str]:
        """List all configured channel names."""
        return list(self.channels.keys())


def build_context(
    node_name: str,
    status: str,
    last_checkin: datetime | None = None,
    trigger_time: datetime | None = None,
    trigger_at: timedelta | None = None,
    base_url: str | None = None,
    peer_urls: list[str] | None = None,
    peer_states: dict | None = None,
    peer_down_threshold: timedelta | None = None,
) -> dict[str, Any]:
    """Build a context dictionary for message formatting.

    Args:
        node_name: Name of this node
        status: Current status string
        last_checkin: Last check-in timestamp
        trigger_time: When trigger occurred (if triggered)
        trigger_at: Duration from checkin to trigger
        base_url: Base URL for constructing check-in/dashboard links
        peer_urls: Configured peer URLs (for federation health vars)
        peer_states: dict[str, PeerState] tracking observed peer status
        peer_down_threshold: Threshold for considering a peer dead

    Returns:
        Context dictionary with formatted values
    """
    now = datetime.now(timezone.utc)
    context: dict[str, Any] = {
        "node_name": node_name,
        "status": status,
        "now": now.isoformat(),
    }

    if trigger_time:
        context["trigger_time"] = trigger_time.isoformat()

    if last_checkin and trigger_at:
        deadline = last_checkin + trigger_at
        remaining = deadline - now

        if remaining.total_seconds() > 0:
            context["days_left"] = remaining.days
            context["hours_left"] = int(remaining.total_seconds() // 3600)
            context["minutes_left"] = int(remaining.total_seconds() // 60)
        else:
            context["days_left"] = 0
            context["hours_left"] = 0
            context["minutes_left"] = 0

    if base_url:
        url = base_url.rstrip('/')
        context["base_url"] = url
        context["checkin_url"] = f"{url}/checkin"
        context["dashboard_url"] = f"{url}/dashboard"

    # Federation health: count healthy vs dead peers so users can write
    # loud degradation alerts ("federation at {healthy_peers}/{total_peers}").
    if peer_urls is not None:
        total = len(peer_urls)
        healthy = 0
        if peer_states is not None:
            threshold = peer_down_threshold or timedelta(hours=6)
            for url in peer_urls:
                ps = peer_states.get(url)
                if ps is None or ps.last_seen is None:
                    continue
                if ps.consecutive_failures > 0:
                    continue
                if (now - ps.last_seen) > threshold:
                    continue
                healthy += 1
        context["total_peers"] = total
        context["healthy_peers"] = healthy
        context["dead_peers"] = total - healthy

    return context


# Convenience functions for common notification patterns

async def send_warning_notification(
    manager: NotificationManager,
    channel: str,
    message: str,
    context: dict[str, Any],
) -> NotificationResult:
    """Send a warning notification."""
    return await manager.send(
        channel=channel,
        message=message,
        title="Posthumous Warning",
        context=context,
    )


async def send_grace_notification(
    manager: NotificationManager,
    channel: str,
    message: str,
    context: dict[str, Any],
) -> NotificationResult:
    """Send a grace period notification."""
    return await manager.send(
        channel=channel,
        message=message,
        title="Posthumous - URGENT",
        context=context,
    )


async def send_trigger_notification(
    manager: NotificationManager,
    channel: str,
    message: str,
    context: dict[str, Any],
) -> NotificationResult:
    """Send a trigger notification."""
    return await manager.send(
        channel=channel,
        message=message,
        title="Posthumous Activated",
        context=context,
    )
