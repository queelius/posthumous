"""Systemd integration for Posthumous -- sd_notify protocol and unit file generation."""

from __future__ import annotations

import logging
import os
import socket
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# sd_notify protocol
# ---------------------------------------------------------------------------

def is_systemd_managed() -> bool:
    """Check if running under systemd (NOTIFY_SOCKET is set)."""
    return "NOTIFY_SOCKET" in os.environ


def _sd_notify(message: str) -> None:
    """Send a notification to systemd via NOTIFY_SOCKET. No-op if not set."""
    sock_path = os.environ.get("NOTIFY_SOCKET")
    if not sock_path:
        return
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.connect(sock_path)
            sock.sendall(message.encode())
        finally:
            sock.close()
    except OSError as e:
        logger.warning(f"Failed to send sd_notify({message}): {e}")


def notify_ready() -> None:
    """Tell systemd the service is ready."""
    _sd_notify("READY=1")


def notify_watchdog() -> None:
    """Ping the systemd watchdog to prevent restart."""
    _sd_notify("WATCHDOG=1")


def notify_stopping() -> None:
    """Tell systemd the service is stopping."""
    _sd_notify("STOPPING=1")


# ---------------------------------------------------------------------------
# Unit file generation
# ---------------------------------------------------------------------------

UNIT_TEMPLATE = """\
[Unit]
Description=Posthumous Deadman Switch ({node_name})
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
ExecStart={python_path} -m posthumous run --config {config_path}
WatchdogSec=30
Restart=always
RestartSec=5
Environment=POSTHUMOUS_CONFIG={config_path}

[Install]
WantedBy=default.target
"""


def get_unit_path() -> Path:
    """Get the path for the systemd user service unit file."""
    return Path.home() / ".config" / "systemd" / "user" / "posthumous.service"


def is_service_installed() -> bool:
    """Check if the systemd service unit file exists."""
    return get_unit_path().exists()


def generate_unit_file(python_path: str, config_path: str, node_name: str) -> str:
    """Generate a systemd unit file with the given parameters."""
    return UNIT_TEMPLATE.format(
        python_path=python_path,
        config_path=config_path,
        node_name=node_name,
    )
