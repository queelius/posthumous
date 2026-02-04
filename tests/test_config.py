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
        assert config.listen == "0.0.0.0:8420"
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
