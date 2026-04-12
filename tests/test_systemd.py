"""Tests for systemd integration -- sd_notify protocol and unit file generation."""

import os
import socket

import pytest

from posthumous.systemd import (
    _sd_notify,
    generate_unit_file,
    get_unit_path,
    is_service_installed,
    is_systemd_managed,
    notify_ready,
    notify_stopping,
    notify_watchdog,
    UNIT_TEMPLATE,
)


class TestIsSystemdManaged:
    """Tests for is_systemd_managed()."""

    def test_true_when_notify_socket_set(self, monkeypatch):
        monkeypatch.setenv("NOTIFY_SOCKET", "/run/systemd/notify")
        assert is_systemd_managed() is True

    def test_false_when_notify_socket_absent(self, monkeypatch):
        monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
        assert is_systemd_managed() is False


class TestSdNotify:
    """Tests for _sd_notify() and the public notify_* wrappers."""

    @pytest.fixture
    def unix_socket(self, tmp_path):
        """Create a real AF_UNIX datagram socket and return (path, server_sock)."""
        sock_path = str(tmp_path / "notify.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        server.bind(sock_path)
        yield sock_path, server
        server.close()

    def test_sd_notify_sends_message(self, monkeypatch, unix_socket):
        sock_path, server = unix_socket
        monkeypatch.setenv("NOTIFY_SOCKET", sock_path)

        _sd_notify("READY=1")

        server.settimeout(2.0)
        data = server.recv(256)
        assert data == b"READY=1"

    def test_sd_notify_noop_without_socket(self, monkeypatch):
        monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
        # Should not raise
        _sd_notify("READY=1")

    def test_sd_notify_handles_bad_path(self, monkeypatch, tmp_path):
        monkeypatch.setenv("NOTIFY_SOCKET", str(tmp_path / "nonexistent.sock"))
        # Should log warning but not raise
        _sd_notify("READY=1")

    def test_notify_ready_sends_correct_message(self, monkeypatch, unix_socket):
        sock_path, server = unix_socket
        monkeypatch.setenv("NOTIFY_SOCKET", sock_path)

        notify_ready()

        server.settimeout(2.0)
        data = server.recv(256)
        assert data == b"READY=1"

    def test_notify_watchdog_sends_correct_message(self, monkeypatch, unix_socket):
        sock_path, server = unix_socket
        monkeypatch.setenv("NOTIFY_SOCKET", sock_path)

        notify_watchdog()

        server.settimeout(2.0)
        data = server.recv(256)
        assert data == b"WATCHDOG=1"

    def test_notify_stopping_sends_correct_message(self, monkeypatch, unix_socket):
        sock_path, server = unix_socket
        monkeypatch.setenv("NOTIFY_SOCKET", sock_path)

        notify_stopping()

        server.settimeout(2.0)
        data = server.recv(256)
        assert data == b"STOPPING=1"

    def test_notify_ready_noop_without_socket(self, monkeypatch):
        monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
        notify_ready()  # No error

    def test_notify_watchdog_noop_without_socket(self, monkeypatch):
        monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
        notify_watchdog()  # No error

    def test_notify_stopping_noop_without_socket(self, monkeypatch):
        monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
        notify_stopping()  # No error


class TestGenerateUnitFile:
    """Tests for generate_unit_file()."""

    def test_interpolates_all_values(self):
        content = generate_unit_file(
            python_path="/usr/bin/python3",
            config_path="/home/user/.posthumous/config.yaml",
            node_name="my-node",
        )

        assert "Description=Posthumous Deadman Switch (my-node)" in content
        assert "ExecStart=/usr/bin/python3 -m posthumous run --config /home/user/.posthumous/config.yaml" in content
        # Config is passed via --config, no environment variable needed
        assert "POSTHUMOUS_CONFIG" not in content

    def test_contains_required_directives(self):
        content = generate_unit_file(
            python_path="/usr/bin/python3",
            config_path="/etc/posthumous/config.yaml",
            node_name="test",
        )

        assert "Type=notify" in content
        assert "WatchdogSec=30" in content
        assert "Restart=always" in content
        assert "RestartSec=5" in content
        assert "After=network-online.target" in content
        assert "Wants=network-online.target" in content
        assert "WantedBy=default.target" in content

    def test_result_is_valid_ini_structure(self):
        content = generate_unit_file(
            python_path="/usr/bin/python3",
            config_path="/home/user/.posthumous/config.yaml",
            node_name="node",
        )

        # Must have all three sections
        assert "[Unit]" in content
        assert "[Service]" in content
        assert "[Install]" in content

    def test_special_characters_in_node_name(self):
        content = generate_unit_file(
            python_path="/usr/bin/python3",
            config_path="/home/user/.posthumous/config.yaml",
            node_name="node-with-dashes_and_underscores",
        )
        assert "node-with-dashes_and_underscores" in content

    def test_python_path_with_spaces_raises(self):
        """systemd's ExecStart does not support whitespace in paths — reject, don't silently produce broken unit files."""
        with pytest.raises(ValueError, match="python_path contains whitespace"):
            generate_unit_file(
                python_path="/opt/my apps/python3",
                config_path="/home/user/.posthumous/config.yaml",
                node_name="test",
            )

    def test_config_path_with_spaces_raises(self):
        with pytest.raises(ValueError, match="config_path contains whitespace"):
            generate_unit_file(
                python_path="/usr/bin/python3",
                config_path="/home/user/my config/config.yaml",
                node_name="test",
            )


class TestGetUnitPath:
    """Tests for get_unit_path()."""

    def test_returns_correct_path(self):
        path = get_unit_path()
        expected = os.path.expanduser("~/.config/systemd/user/posthumous.service")
        assert str(path) == expected

    def test_returns_path_object(self):
        from pathlib import Path
        path = get_unit_path()
        assert isinstance(path, Path)

    def test_filename_is_posthumous_service(self):
        path = get_unit_path()
        assert path.name == "posthumous.service"


class TestIsServiceInstalled:
    """Tests for is_service_installed()."""

    def test_true_when_unit_file_exists(self, tmp_path, monkeypatch):
        unit_dir = tmp_path / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True)
        unit_file = unit_dir / "posthumous.service"
        unit_file.write_text("[Unit]\nDescription=test\n")

        # Patch get_unit_path to return our temp file
        from posthumous import systemd
        monkeypatch.setattr(systemd, "get_unit_path", lambda: unit_file)

        assert is_service_installed() is True

    def test_false_when_unit_file_absent(self, tmp_path, monkeypatch):
        unit_file = tmp_path / "nonexistent" / "posthumous.service"

        from posthumous import systemd
        monkeypatch.setattr(systemd, "get_unit_path", lambda: unit_file)

        assert is_service_installed() is False
