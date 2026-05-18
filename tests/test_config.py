"""Tests for configuration loading and validation."""

import pytest
from datetime import timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile

from posthumous.config import (
    Config,
    NotificationAction,
    ScriptAction,
    ScheduledItem,
    parse_duration,
    generate_default_config,
)


class TestParseDuration:
    """Tests for duration parsing."""

    def test_parse_days(self):
        assert parse_duration("7 days") == timedelta(days=7)
        assert parse_duration("1 day") == timedelta(days=1)
        assert parse_duration("14 d") == timedelta(days=14)

    def test_parse_hours(self):
        assert parse_duration("12 hours") == timedelta(hours=12)
        assert parse_duration("1 hour") == timedelta(hours=1)
        assert parse_duration("24 h") == timedelta(hours=24)

    def test_parse_minutes(self):
        assert parse_duration("30 minutes") == timedelta(minutes=30)
        assert parse_duration("1 minute") == timedelta(minutes=1)
        assert parse_duration("15 min") == timedelta(minutes=15)

    def test_parse_weeks(self):
        assert parse_duration("2 weeks") == timedelta(weeks=2)
        assert parse_duration("1 week") == timedelta(weeks=1)

    def test_parse_numeric(self):
        assert parse_duration(3600) == timedelta(seconds=3600)
        assert parse_duration(60.5) == timedelta(seconds=60.5)

    def test_invalid_duration(self):
        with pytest.raises(ValueError):
            parse_duration("invalid")
        with pytest.raises(ValueError):
            parse_duration("5 bananas")


class TestConfigFromDict:
    """Tests for Config.from_dict()."""

    def test_minimal_config(self):
        data = {
            "node_name": "test-node",
            "secret_key": "JBSWY3DPEHPK3PXP",
        }
        config = Config.from_dict(data)

        assert config.node_name == "test-node"
        assert config.secret_key == "JBSWY3DPEHPK3PXP"
        assert config.listen == "127.0.0.1:8420"
        assert config.checkin_interval == timedelta(days=7)

    def test_full_config(self):
        data = {
            "node_name": "full-node",
            "secret_key": "JBSWY3DPEHPK3PXP",
            "listen": "127.0.0.1:9000",
            "checkin_interval": "3 days",
            "warning_start": "4 days",
            "grace_start": "6 days",
            "trigger_at": "7 days",
            "peers": ["https://peer1:8420", "https://peer2:8420"],
            "notifications": {
                "default": ["ntfy://test"],
                "urgent": ["mailto://test@test.com", "ntfy://urgent"],
            },
            "actions": {
                "on_warning": [
                    {"notify": "default", "message": "Warning!"},
                ],
                "on_trigger": [
                    {"notify": "urgent", "message": "Triggered!"},
                    {"script": "scripts/trigger.py"},
                ],
            },
            "post_trigger": [
                {
                    "name": "annual",
                    "when": "every year on trigger",
                    "notify": "default",
                    "message": "Annual message",
                },
            ],
        }
        config = Config.from_dict(data)

        assert config.listen == "127.0.0.1:9000"
        assert config.checkin_interval == timedelta(days=3)
        assert config.warning_start == timedelta(days=4)
        assert len(config.peers) == 2
        assert "default" in config.notifications
        assert len(config.notifications["urgent"]) == 2
        assert len(config.on_warning) == 1
        assert isinstance(config.on_warning[0], NotificationAction)
        assert len(config.on_trigger) == 2
        assert isinstance(config.on_trigger[1], ScriptAction)
        assert len(config.post_trigger) == 1
        assert config.post_trigger[0].name == "annual"

    def test_missing_required_fields(self):
        with pytest.raises(ValueError, match="node_name"):
            Config.from_dict({"secret_key": "test"})

        with pytest.raises(ValueError, match="secret_key"):
            Config.from_dict({"node_name": "test"})

    def test_default_listen_is_localhost(self):
        config = Config(node_name="test", secret_key="JBSWY3DPEHPK3PXP")
        assert config.listen == "127.0.0.1:8420"


class TestConfigYaml:
    """Tests for YAML loading and saving."""

    def test_round_trip(self, tmp_path):
        config = generate_default_config("test", "JBSWY3DPEHPK3PXP")
        config.config_dir = tmp_path
        config.peers = ["https://peer1:8420"]

        config_path = tmp_path / "config.yaml"
        config.save(config_path)

        loaded = Config.from_yaml(config_path)

        assert loaded.node_name == config.node_name
        assert loaded.secret_key == config.secret_key
        assert loaded.peers == config.peers
        assert len(loaded.on_warning) == len(config.on_warning)

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            Config.from_yaml(Path("/nonexistent/config.yaml"))


class TestConfigValidation:
    """Tests for config validation."""

    def test_valid_config(self):
        config = generate_default_config("test", "JBSWY3DPEHPK3PXP")
        errors = config.validate()
        assert errors == []

    def test_invalid_timing(self):
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            checkin_interval=timedelta(days=10),  # Greater than warning
            warning_start=timedelta(days=8),
            grace_start=timedelta(days=12),
            trigger_at=timedelta(days=14),
        )
        errors = config.validate()
        assert any("warning_start" in e for e in errors)

    def test_invalid_secret_key(self):
        config = Config(
            node_name="test",
            secret_key="not-valid-base32!!!",
        )
        errors = config.validate()
        assert any("base32" in e for e in errors)

    def test_unknown_notification_channel(self):
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            notifications={"default": ["ntfy://test"]},
            on_warning=[NotificationAction(channel="unknown", message="test")],
        )
        errors = config.validate()
        assert any("unknown channel" in e for e in errors)


class TestGenerateDefaultConfig:
    """Tests for default config generation."""

    def test_generates_valid_config(self):
        config = generate_default_config("my-node", "JBSWY3DPEHPK3PXP")

        assert config.node_name == "my-node"
        assert config.secret_key == "JBSWY3DPEHPK3PXP"
        assert "default" in config.notifications
        assert len(config.on_warning) > 0
        assert len(config.on_grace) > 0
        assert len(config.on_trigger) > 0
        assert config.validate() == []


class TestConfigToDictExtended:
    """Additional tests for Config.to_dict()."""

    def test_to_dict_with_actions(self):
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            notifications={"default": ["ntfy://test"]},
            on_warning=[NotificationAction(channel="default", message="Warning!")],
            on_trigger=[
                NotificationAction(channel="default", message="Triggered!"),
                ScriptAction(script="scripts/trigger.py"),
            ],
        )
        data = config.to_dict()

        assert "actions" in data
        assert len(data["actions"]["on_warning"]) == 1
        assert data["actions"]["on_warning"][0]["notify"] == "default"
        assert len(data["actions"]["on_trigger"]) == 2
        assert data["actions"]["on_trigger"][1]["script"] == "scripts/trigger.py"

    def test_to_dict_with_post_trigger(self):
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            post_trigger=[
                ScheduledItem(name="annual", when="every year on trigger",
                              notify="default", message="Annual"),
                ScheduledItem(name="daily", when="every day after trigger",
                              script="scripts/daily.py"),
            ],
        )
        data = config.to_dict()

        assert "post_trigger" in data
        assert len(data["post_trigger"]) == 2
        assert data["post_trigger"][0]["name"] == "annual"
        assert "script" not in data["post_trigger"][0]  # None values omitted
        assert data["post_trigger"][1]["script"] == "scripts/daily.py"

    def test_to_dict_with_api_token(self):
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            api_token="my-secret-token",
        )
        data = config.to_dict()
        assert data["api_token"] == "my-secret-token"

    def test_to_dict_without_api_token(self):
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
        )
        data = config.to_dict()
        assert "api_token" not in data

    def test_to_dict_duration_formatting(self):
        """Duration formatting should pick the right unit."""
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            checkin_interval=timedelta(hours=12),
            warning_start=timedelta(days=1),
            grace_start=timedelta(minutes=90),
            trigger_at=timedelta(seconds=7200),
        )
        data = config.to_dict()
        assert data["checkin_interval"] == "12 hours"
        assert data["warning_start"] == "1 day"
        assert data["grace_start"] == "90 minutes"
        assert data["trigger_at"] == "2 hours"  # 7200s = 2h


class TestConfigGetScriptPath:
    """Tests for Config.get_script_path()."""

    def test_relative_path(self, tmp_path):
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            config_dir=tmp_path,
        )
        result = config.get_script_path("scripts/trigger.py")
        assert result == tmp_path / "scripts/trigger.py"

    def test_absolute_path(self, tmp_path):
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            config_dir=tmp_path,
        )
        abs_path = Path("/usr/local/bin/trigger.sh")
        result = config.get_script_path(str(abs_path))
        assert result == abs_path


class TestConfigSaveExtended:
    """Additional tests for Config.save()."""

    def test_save_creates_parent_dirs(self, tmp_path):
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            config_dir=tmp_path / "new_dir",
        )
        config_path = tmp_path / "new_dir" / "config.yaml"
        config.save(config_path)

        assert config_path.exists()

    def test_save_default_path(self, tmp_path):
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            config_dir=tmp_path,
        )
        config.save()

        default_path = tmp_path / "config.yaml"
        assert default_path.exists()


class TestConfigValidationExtended:
    """Additional validation tests."""

    def test_post_trigger_unknown_channel(self):
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            notifications={"default": ["ntfy://test"]},
            post_trigger=[
                ScheduledItem(name="bad", when="trigger",
                              notify="nonexistent", message="test"),
            ],
        )
        errors = config.validate()
        assert any("nonexistent" in e for e in errors)

    def test_string_notification_url_coerced_to_list(self):
        """A single string URL should be coerced to a list during from_dict."""
        data = {
            "node_name": "test",
            "secret_key": "JBSWY3DPEHPK3PXP",
            "notifications": {
                "default": "ntfy://single-url",  # string, not list
            },
        }
        config = Config.from_dict(data)
        assert config.notifications["default"] == ["ntfy://single-url"]


class TestParseDurationExtended:
    """Additional parse_duration edge cases."""

    def test_parse_seconds(self):
        assert parse_duration("30 seconds") == timedelta(seconds=30)
        assert parse_duration("1 second") == timedelta(seconds=1)
        assert parse_duration("60 s") == timedelta(seconds=60)

    def test_parse_string_number(self):
        """Bare number string should be parsed as seconds."""
        assert parse_duration("3600") == timedelta(seconds=3600)


class TestConfigEdgeCases:
    """Edge-case tests for uncovered Config paths."""

    def test_save_exception_cleanup(self, tmp_path):
        """When os.replace fails during save, temp file should be cleaned up."""
        from unittest.mock import patch

        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            config_dir=tmp_path,
        )
        config_path = tmp_path / "config.yaml"

        # Patch os.replace to fail after temp file is created
        with patch('os.replace', side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                config.save(config_path)

        # Temp file should have been cleaned up
        import glob
        temp_files = glob.glob(str(tmp_path / ".config_*.yaml.tmp"))
        assert len(temp_files) == 0

    def test_format_duration_fractional(self):
        """Fractional seconds that don't evenly divide into larger units."""
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            checkin_interval=timedelta(seconds=90),  # 1.5 minutes
        )
        data = config.to_dict()
        # 90 seconds doesn't evenly divide into minutes (90/60=1.5)
        # So it should fall through to seconds
        assert "90 seconds" in data['checkin_interval']

    def test_get_default_config_path(self):
        """Verify default config path construction."""
        path = Config.get_default_config_path()
        assert path == Path.home() / ".posthumous" / "config.yaml"


class TestConfigEncryption:
    """Tests for encryption-related config methods."""

    def test_encrypt_at_rest_in_to_dict(self):
        """encrypt_at_rest=True should appear in to_dict output."""
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            encrypt_at_rest=True,
        )
        data = config.to_dict()
        assert data['encrypt_at_rest'] is True

    def test_encrypt_at_rest_false_not_in_to_dict(self):
        """encrypt_at_rest=False should NOT appear in to_dict output."""
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            encrypt_at_rest=False,
        )
        data = config.to_dict()
        assert 'encrypt_at_rest' not in data

    def test_get_encryption_secret_enabled(self):
        """get_encryption_secret should return the secret_key when encryption is enabled."""
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            encrypt_at_rest=True,
        )
        secret = config.get_encryption_secret()
        assert secret is not None
        assert isinstance(secret, str)
        assert secret == "JBSWY3DPEHPK3PXP"

    def test_get_encryption_secret_disabled(self):
        """get_encryption_secret should return None when encryption is disabled."""
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            encrypt_at_rest=False,
        )
        assert config.get_encryption_secret() is None


class TestConfigSaveExceptionHandling:
    """Tests for save() exception handler paths."""

    def test_save_replace_and_unlink_both_fail(self, tmp_path):
        """When os.replace and os.unlink both fail, exception propagates."""
        from unittest.mock import patch

        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            config_dir=tmp_path,
        )
        config_path = tmp_path / "config.yaml"

        with patch('os.replace', side_effect=OSError("disk full")), \
             patch('os.unlink', side_effect=OSError("permission denied")):
            with pytest.raises(OSError, match="disk full"):
                config.save(config_path)


class TestCheckInterval:
    """Tests for check_interval config field (Issue 8)."""

    def test_default_is_none(self):
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
        )
        assert config.check_interval is None

    def test_from_dict_with_check_interval(self):
        data = {
            "node_name": "test",
            "secret_key": "JBSWY3DPEHPK3PXP",
            "check_interval": "5 seconds",
        }
        config = Config.from_dict(data)
        assert config.check_interval == timedelta(seconds=5)

    def test_from_dict_without_check_interval(self):
        data = {
            "node_name": "test",
            "secret_key": "JBSWY3DPEHPK3PXP",
        }
        config = Config.from_dict(data)
        assert config.check_interval is None


class TestBaseUrl:
    """Tests for base_url config field."""

    def test_default_is_none(self):
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
        )
        assert config.base_url is None

    def test_from_dict_with_base_url(self):
        data = {
            "node_name": "test",
            "secret_key": "JBSWY3DPEHPK3PXP",
            "base_url": "https://posthumous.example.com:8420",
        }
        config = Config.from_dict(data)
        assert config.base_url == "https://posthumous.example.com:8420"

    def test_from_dict_without_base_url(self):
        data = {
            "node_name": "test",
            "secret_key": "JBSWY3DPEHPK3PXP",
        }
        config = Config.from_dict(data)
        assert config.base_url is None

    def test_to_dict_with_base_url(self):
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            base_url="https://example.com:8420",
        )
        data = config.to_dict()
        assert data["base_url"] == "https://example.com:8420"

    def test_to_dict_without_base_url(self):
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
        )
        data = config.to_dict()
        assert "base_url" not in data

    def test_validate_valid_http_url(self):
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            base_url="http://localhost:8420",
        )
        errors = config.validate()
        assert not any("base_url" in e for e in errors)

    def test_validate_valid_https_url(self):
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            base_url="https://posthumous.example.com",
        )
        errors = config.validate()
        assert not any("base_url" in e for e in errors)

    def test_validate_invalid_base_url(self):
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            base_url="ftp://example.com",
        )
        errors = config.validate()
        assert any("base_url" in e for e in errors)

    def test_validate_bare_hostname_invalid(self):
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            base_url="example.com:8420",
        )
        errors = config.validate()
        assert any("base_url" in e for e in errors)

    def test_get_base_url_explicit(self):
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            base_url="https://example.com:8420",
        )
        assert config.get_base_url() == "https://example.com:8420"

    def test_get_base_url_strips_trailing_slash(self):
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            base_url="https://example.com:8420/",
        )
        assert config.get_base_url() == "https://example.com:8420"

    def test_get_base_url_auto_from_listen(self):
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            listen="0.0.0.0:8420",
        )
        assert config.get_base_url() == "http://localhost:8420"

    def test_get_base_url_auto_custom_listen(self):
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            listen="192.168.1.5:9000",
        )
        assert config.get_base_url() == "http://192.168.1.5:9000"

    def test_round_trip_with_base_url(self, tmp_path):
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            base_url="https://example.com:8420",
            config_dir=tmp_path,
        )
        config_path = tmp_path / "config.yaml"
        config.save(config_path)

        loaded = Config.from_yaml(config_path)
        assert loaded.base_url == "https://example.com:8420"


class TestDefaultConfigMessages:
    """Tests for default config notification messages."""

    def test_warning_message_includes_checkin_url(self):
        config = generate_default_config("test", "JBSWY3DPEHPK3PXP")
        assert "{checkin_url}" in config.on_warning[0].message

    def test_grace_message_includes_checkin_url(self):
        config = generate_default_config("test", "JBSWY3DPEHPK3PXP")
        assert "{checkin_url}" in config.on_grace[0].message

    def test_trigger_message_includes_dashboard_url(self):
        config = generate_default_config("test", "JBSWY3DPEHPK3PXP")
        assert "{dashboard_url}" in config.on_trigger[0].message


class TestParseActionsEdgeCases:
    """Tests for action parsing edge cases."""

    def test_unknown_action_type_skipped(self):
        """Actions with neither 'notify' nor 'script' should be skipped."""
        data = {
            "node_name": "test",
            "secret_key": "JBSWY3DPEHPK3PXP",
            "actions": {
                "on_warning": [
                    {"notify": "default", "message": "hello"},
                    {"unknown_key": "value"},  # Should be skipped
                ],
            },
        }
        config = Config.from_dict(data)
        assert len(config.on_warning) == 1


class TestConfigFilePermissions:
    """Tests for restrictive file permissions on saved config files."""

    def test_config_save_sets_restrictive_permissions(self, tmp_path):
        import stat
        config = Config(node_name="test", secret_key="JBSWY3DPEHPK3PXP")
        path = tmp_path / "config.yaml"
        config.save(path)
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600


class TestOnPeerDown:
    """Tests for on_peer_down config field."""

    def test_default_is_empty_list(self):
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
        )
        assert config.on_peer_down == []

    def test_from_dict_parses_on_peer_down(self):
        data = {
            "node_name": "test",
            "secret_key": "JBSWY3DPEHPK3PXP",
            "notifications": {"default": ["ntfy://test"]},
            "actions": {
                "on_peer_down": [
                    {"notify": "default", "message": "Peer {peer_url} is down!"},
                    {"script": "scripts/peer_down.sh"},
                ],
            },
        }
        config = Config.from_dict(data)

        assert len(config.on_peer_down) == 2
        assert isinstance(config.on_peer_down[0], NotificationAction)
        assert config.on_peer_down[0].channel == "default"
        assert "{peer_url}" in config.on_peer_down[0].message
        assert isinstance(config.on_peer_down[1], ScriptAction)
        assert config.on_peer_down[1].script == "scripts/peer_down.sh"

    def test_from_dict_without_on_peer_down(self):
        data = {
            "node_name": "test",
            "secret_key": "JBSWY3DPEHPK3PXP",
            "actions": {
                "on_warning": [{"notify": "default", "message": "Warning!"}],
            },
            "notifications": {"default": ["ntfy://test"]},
        }
        config = Config.from_dict(data)
        assert config.on_peer_down == []

    def test_to_dict_includes_on_peer_down(self):
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            on_peer_down=[
                NotificationAction(channel="default", message="Peer down!"),
                ScriptAction(script="scripts/alert.sh"),
            ],
        )
        data = config.to_dict()

        assert "actions" in data
        assert "on_peer_down" in data["actions"]
        assert len(data["actions"]["on_peer_down"]) == 2
        assert data["actions"]["on_peer_down"][0]["notify"] == "default"
        assert data["actions"]["on_peer_down"][1]["script"] == "scripts/alert.sh"

    def test_to_dict_omits_empty_on_peer_down(self):
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
        )
        data = config.to_dict()
        assert "actions" not in data or "on_peer_down" not in data.get("actions", {})

    def test_round_trip_with_on_peer_down(self, tmp_path):
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            notifications={"default": ["ntfy://test"]},
            on_peer_down=[
                NotificationAction(channel="default", message="Peer {peer_url} down for {peer_downtime}"),
            ],
            config_dir=tmp_path,
        )
        config_path = tmp_path / "config.yaml"
        config.save(config_path)

        loaded = Config.from_yaml(config_path)
        assert len(loaded.on_peer_down) == 1
        assert isinstance(loaded.on_peer_down[0], NotificationAction)
        assert loaded.on_peer_down[0].channel == "default"
        assert "{peer_url}" in loaded.on_peer_down[0].message

    def test_validate_checks_on_peer_down_channels(self):
        config = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            notifications={"default": ["ntfy://test"]},
            on_peer_down=[
                NotificationAction(channel="nonexistent", message="Peer down!"),
            ],
        )
        errors = config.validate()
        assert any("nonexistent" in e and "on_peer_down" in e for e in errors)


class TestQuorumConfig:
    """Tests for the optional quorum config block (v0.7)."""

    def test_quorum_default_is_none(self):
        from posthumous.config import Config
        cfg = Config(node_name="test", secret_key="JBSWY3DPEHPK3PXP")
        assert cfg.quorum is None

    def test_parse_quorum_block(self, tmp_path):
        import yaml
        from posthumous.config import Config
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump({
            "node_name": "test",
            "secret_key": "JBSWY3DPEHPK3PXP",
            "checkin_interval": "7 days",
            "warning_start": "8 days",
            "grace_start": "12 days",
            "trigger_at": "14 days",
            "peers": ["http://peer1:8420", "http://peer2:8420"],
            "quorum": {"required": 2, "window_seconds": 45},
        }))
        cfg = Config.from_yaml(path)
        assert cfg.quorum is not None
        assert cfg.quorum.required == 2
        assert cfg.quorum.window_seconds == 45

    def test_quorum_window_default_is_30(self, tmp_path):
        import yaml
        from posthumous.config import Config
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump({
            "node_name": "test",
            "secret_key": "JBSWY3DPEHPK3PXP",
            "checkin_interval": "7 days",
            "warning_start": "8 days",
            "grace_start": "12 days",
            "trigger_at": "14 days",
            "peers": ["http://peer1:8420", "http://peer2:8420"],
            "quorum": {"required": 2},
        }))
        cfg = Config.from_yaml(path)
        assert cfg.quorum.window_seconds == 30

    def test_to_dict_round_trips_quorum(self):
        from posthumous.config import Config, QuorumConfig
        cfg = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            peers=["http://peer1:8420"],
            quorum=QuorumConfig(required=2, window_seconds=20),
        )
        data = cfg.to_dict()
        assert data["quorum"] == {"required": 2, "window_seconds": 20}

    def test_validate_quorum_required_must_be_at_least_one(self):
        from posthumous.config import Config, QuorumConfig
        cfg = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            peers=["http://peer1:8420"],
            quorum=QuorumConfig(required=0, window_seconds=30),
        )
        errors = cfg.validate()
        assert any("quorum.required" in e for e in errors)

    def test_validate_quorum_required_cannot_exceed_federation_size(self):
        from posthumous.config import Config, QuorumConfig
        cfg = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            peers=["http://peer1:8420"],
            quorum=QuorumConfig(required=5, window_seconds=30),
        )
        errors = cfg.validate()
        assert any("exceeds federation size" in e for e in errors)

    def test_validate_quorum_window_seconds_must_be_positive(self):
        from posthumous.config import Config, QuorumConfig
        cfg = Config(
            node_name="test",
            secret_key="JBSWY3DPEHPK3PXP",
            peers=["http://peer1:8420"],
            quorum=QuorumConfig(required=2, window_seconds=0),
        )
        errors = cfg.validate()
        assert any("window_seconds" in e for e in errors)


class TestPublicUrl:
    """Tests for the canonical-identity `public_url` field (v0.7.2)."""

    def test_self_url_falls_back_to_listen(self):
        from posthumous.config import Config
        cfg = Config(
            node_name="test", secret_key="JBSWY3DPEHPK3PXP",
            listen="lex-laptop.local:8420",
        )
        assert cfg.self_url() == "http://lex-laptop.local:8420"

    def test_self_url_preserves_http_prefix_in_listen(self):
        from posthumous.config import Config
        cfg = Config(
            node_name="test", secret_key="JBSWY3DPEHPK3PXP",
            listen="https://node.example.com:8420",
        )
        assert cfg.self_url() == "https://node.example.com:8420"

    def test_self_url_uses_public_url_when_set(self):
        from posthumous.config import Config
        cfg = Config(
            node_name="test", secret_key="JBSWY3DPEHPK3PXP",
            listen="0.0.0.0:8420",
            public_url="https://node.example.com:8420",
        )
        assert cfg.self_url() == "https://node.example.com:8420"

    def test_self_url_strips_trailing_slash(self):
        from posthumous.config import Config
        cfg = Config(
            node_name="test", secret_key="JBSWY3DPEHPK3PXP",
            public_url="https://node.example.com:8420/",
        )
        assert cfg.self_url() == "https://node.example.com:8420"

    def test_validate_rejects_wildcard_listen_without_public_url_when_quorum(self):
        from posthumous.config import Config, QuorumConfig
        cfg = Config(
            node_name="test", secret_key="JBSWY3DPEHPK3PXP",
            listen="0.0.0.0:8420",
            peers=["http://peer1:8420"],
            quorum=QuorumConfig(required=2),
        )
        errors = cfg.validate()
        assert any("public_url is required" in e for e in errors)

    def test_validate_allows_wildcard_listen_when_quorum_disabled(self):
        from posthumous.config import Config
        cfg = Config(
            node_name="test", secret_key="JBSWY3DPEHPK3PXP",
            listen="0.0.0.0:8420",
        )
        # No quorum, no public_url needed.
        assert not any("public_url" in e for e in cfg.validate())

    def test_validate_accepts_wildcard_listen_with_public_url(self):
        from posthumous.config import Config, QuorumConfig
        cfg = Config(
            node_name="test", secret_key="JBSWY3DPEHPK3PXP",
            listen="0.0.0.0:8420",
            public_url="https://node.example.com:8420",
            peers=["http://peer1:8420"],
            quorum=QuorumConfig(required=2),
        )
        assert not any("public_url" in e for e in cfg.validate())

    def test_validate_rejects_malformed_public_url(self):
        from posthumous.config import Config
        cfg = Config(
            node_name="test", secret_key="JBSWY3DPEHPK3PXP",
            public_url="node.example.com:8420",  # no scheme
        )
        errors = cfg.validate()
        assert any("public_url must start with http" in e for e in errors)

    def test_to_dict_round_trips_public_url(self, tmp_path):
        import yaml
        from posthumous.config import Config
        cfg = Config(
            node_name="test", secret_key="JBSWY3DPEHPK3PXP",
            public_url="https://node.example.com:8420",
        )
        data = cfg.to_dict()
        assert data["public_url"] == "https://node.example.com:8420"

        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump(data))
        loaded = Config.from_yaml(path)
        assert loaded.public_url == "https://node.example.com:8420"
