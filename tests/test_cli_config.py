"""Tests for the `phm config` command group."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from posthumous.cli import main


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def valid_config_dir(tmp_path):
    """Create a minimal valid config directory."""
    config_dir = tmp_path / ".posthumous"
    config_dir.mkdir()
    (config_dir / "scripts").mkdir()
    (config_dir / "logs").mkdir()
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        "node_name: test-node\n"
        "secret_key: JBSWY3DPEHPK3PXP\n"
        "listen: '127.0.0.1:8420'\n"
        "checkin_interval: 7 days\n"
        "warning_start: 8 days\n"
        "grace_start: 12 days\n"
        "trigger_at: 14 days\n"
        "notifications:\n"
        "  default:\n"
        "    - 'ntfy://test-topic'\n"
        "actions:\n"
        "  on_warning:\n"
        "    - notify: default\n"
        "      message: 'Warning'\n"
        "  on_trigger:\n"
        "    - notify: default\n"
        "      message: 'Triggered!'\n"
    )
    return config_dir


class TestConfigPath:
    def test_shows_expected_paths(self, runner, valid_config_dir):
        config_path = valid_config_dir / "config.yaml"
        result = runner.invoke(main, ['-c', str(config_path), 'config', 'path'])
        assert result.exit_code == 0
        assert "Config file:" in result.output
        assert "State file:" in result.output
        assert "Scripts dir:" in result.output
        assert "Logs dir:" in result.output

    def test_marks_existing_paths(self, runner, valid_config_dir):
        config_path = valid_config_dir / "config.yaml"
        result = runner.invoke(main, ['-c', str(config_path), 'config', 'path'])
        assert "[exists]" in result.output
        # state.yaml doesn't exist yet
        assert "[missing]" in result.output

    def test_works_without_config_file(self, runner, tmp_path):
        """config path should work even if the config file doesn't exist."""
        config_path = tmp_path / "nonexistent" / "config.yaml"
        result = runner.invoke(main, ['-c', str(config_path), 'config', 'path'])
        assert result.exit_code == 0
        assert "[missing]" in result.output


class TestConfigShow:
    def test_redacts_secret_key(self, runner, valid_config_dir):
        config_path = valid_config_dir / "config.yaml"
        result = runner.invoke(main, ['-c', str(config_path), 'config', 'show'])
        assert result.exit_code == 0
        # Full secret should NOT appear
        assert "JBSWY3DPEHPK3PXP" not in result.output
        # Redacted form should appear
        assert "JBSW****" in result.output

    def test_shows_node_name(self, runner, valid_config_dir):
        config_path = valid_config_dir / "config.yaml"
        result = runner.invoke(main, ['-c', str(config_path), 'config', 'show'])
        assert "test-node" in result.output

    def test_shows_yaml_output(self, runner, valid_config_dir):
        config_path = valid_config_dir / "config.yaml"
        result = runner.invoke(main, ['-c', str(config_path), 'config', 'show'])
        assert "node_name:" in result.output
        assert "listen:" in result.output

    def test_exits_1_when_config_missing(self, runner, tmp_path):
        config_path = tmp_path / "missing.yaml"
        result = runner.invoke(main, ['-c', str(config_path), 'config', 'show'])
        assert result.exit_code == 1
        assert "Config not found" in result.output


class TestConfigValidate:
    def test_valid_config_exits_0(self, runner, valid_config_dir):
        config_path = valid_config_dir / "config.yaml"
        result = runner.invoke(main, ['-c', str(config_path), 'config', 'validate'])
        assert result.exit_code == 0
        assert "Config OK" in result.output

    def test_invalid_timing_exits_1(self, runner, tmp_path):
        """Config where warning_start <= checkin_interval should fail."""
        config_dir = tmp_path / ".posthumous"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            "node_name: bad-node\n"
            "secret_key: JBSWY3DPEHPK3PXP\n"
            "checkin_interval: 10 days\n"
            "warning_start: 5 days\n"
            "grace_start: 3 days\n"
            "trigger_at: 1 days\n"
        )
        result = runner.invoke(main, ['-c', str(config_path), 'config', 'validate'])
        assert result.exit_code == 1
        assert "warning_start" in result.output

    def test_invalid_secret_exits_1(self, runner, tmp_path):
        config_dir = tmp_path / ".posthumous"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            "node_name: bad-node\n"
            "secret_key: '!!!not-base32!!!'\n"
            "checkin_interval: 7 days\n"
            "warning_start: 8 days\n"
            "grace_start: 12 days\n"
            "trigger_at: 14 days\n"
        )
        result = runner.invoke(main, ['-c', str(config_path), 'config', 'validate'])
        assert result.exit_code == 1
        assert "secret_key" in result.output

    def test_missing_channel_exits_1(self, runner, tmp_path):
        config_dir = tmp_path / ".posthumous"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            "node_name: bad-node\n"
            "secret_key: JBSWY3DPEHPK3PXP\n"
            "checkin_interval: 7 days\n"
            "warning_start: 8 days\n"
            "grace_start: 12 days\n"
            "trigger_at: 14 days\n"
            "actions:\n"
            "  on_trigger:\n"
            "    - notify: nonexistent\n"
            "      message: 'oops'\n"
        )
        result = runner.invoke(main, ['-c', str(config_path), 'config', 'validate'])
        assert result.exit_code == 1
        assert "unknown channel" in result.output

    def test_exits_1_when_config_missing(self, runner, tmp_path):
        config_path = tmp_path / "missing.yaml"
        result = runner.invoke(main, ['-c', str(config_path), 'config', 'validate'])
        assert result.exit_code == 1


class TestConfigEdit:
    def test_calls_editor_and_validates(self, runner, valid_config_dir):
        config_path = valid_config_dir / "config.yaml"
        with patch('posthumous.cli.subprocess.call', return_value=0) as mock_call:
            result = runner.invoke(
                main, ['-c', str(config_path), 'config', 'edit'],
                env={'EDITOR': 'nano'},
            )
        assert result.exit_code == 0
        assert "Config OK" in result.output
        mock_call.assert_called_once_with(['nano', str(config_path)])

    def test_uses_visual_over_editor(self, runner, valid_config_dir):
        config_path = valid_config_dir / "config.yaml"
        with patch('posthumous.cli.subprocess.call', return_value=0) as mock_call:
            result = runner.invoke(
                main, ['-c', str(config_path), 'config', 'edit'],
                env={'VISUAL': 'code', 'EDITOR': 'nano'},
            )
        mock_call.assert_called_once_with(['code', str(config_path)])

    def test_falls_back_to_vi(self, runner, valid_config_dir):
        config_path = valid_config_dir / "config.yaml"
        with patch('posthumous.cli.subprocess.call', return_value=0) as mock_call:
            # Explicitly clear VISUAL and EDITOR
            result = runner.invoke(
                main, ['-c', str(config_path), 'config', 'edit'],
                env={},
            )
        mock_call.assert_called_once_with(['vi', str(config_path)])

    def test_reports_validation_errors_after_edit(self, runner, tmp_path):
        config_dir = tmp_path / ".posthumous"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        # Start with valid config
        config_path.write_text(
            "node_name: test-node\n"
            "secret_key: JBSWY3DPEHPK3PXP\n"
            "checkin_interval: 7 days\n"
            "warning_start: 8 days\n"
            "grace_start: 12 days\n"
            "trigger_at: 14 days\n"
        )

        def break_config(args):
            """Simulate editor making config invalid."""
            config_path.write_text(
                "node_name: test-node\n"
                "secret_key: JBSWY3DPEHPK3PXP\n"
                "checkin_interval: 10 days\n"
                "warning_start: 5 days\n"
                "grace_start: 3 days\n"
                "trigger_at: 1 days\n"
            )
            return 0

        with patch('posthumous.cli.subprocess.call', side_effect=break_config):
            result = runner.invoke(main, ['-c', str(config_path), 'config', 'edit'], env={})

        assert "Validation errors after edit" in result.output

    def test_reports_parse_error_after_edit(self, runner, tmp_path):
        config_dir = tmp_path / ".posthumous"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            "node_name: test-node\n"
            "secret_key: JBSWY3DPEHPK3PXP\n"
            "checkin_interval: 7 days\n"
            "warning_start: 8 days\n"
            "grace_start: 12 days\n"
            "trigger_at: 14 days\n"
        )

        def corrupt_config(args):
            """Simulate editor breaking YAML syntax."""
            config_path.write_text("not: valid: yaml: [[[")
            return 0

        with patch('posthumous.cli.subprocess.call', side_effect=corrupt_config):
            result = runner.invoke(main, ['-c', str(config_path), 'config', 'edit'], env={})

        assert "Failed to parse config" in result.output

    def test_exits_1_when_config_missing(self, runner, tmp_path):
        config_path = tmp_path / "missing.yaml"
        result = runner.invoke(main, ['-c', str(config_path), 'config', 'edit'])
        assert result.exit_code == 1
        assert "Config not found" in result.output


class TestRedactSecret:
    def test_redacts_normal_secret(self):
        from posthumous.cli import _redact_secret
        assert _redact_secret("JBSWY3DPEHPK3PXP") == "JBSW****"

    def test_redacts_short_secret(self):
        from posthumous.cli import _redact_secret
        assert _redact_secret("ABC") == "****"

    def test_redacts_empty_secret(self):
        from posthumous.cli import _redact_secret
        assert _redact_secret("") == "****"
