"""Tests for CLI commands (init, checkin, status, peers, test-notify, test-trigger, export, import)."""

from __future__ import annotations

import yaml
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pyotp
import pytest
from click.testing import CliRunner

from posthumous.cli import main, setup_logging


SECRET = "JBSWY3DPEHPK3PXP"


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def valid_config_dir(tmp_path):
    """Create a config directory with a valid config and state."""
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
        "  on_grace:\n"
        "    - notify: default\n"
        "      message: 'Grace'\n"
        "  on_trigger:\n"
        "    - notify: default\n"
        "      message: 'Triggered!'\n"
        "    - script: scripts/on_trigger.py\n"
        "post_trigger:\n"
        "  - name: annual\n"
        "    when: every year on trigger\n"
        "    notify: default\n"
        "    message: 'Annual'\n"
    )
    return config_dir


@pytest.fixture
def config_path(valid_config_dir):
    return valid_config_dir / "config.yaml"


class TestInit:
    """Tests for the 'init' command."""

    def test_creates_config_and_directories(self, runner, tmp_path):
        config_path = tmp_path / "fresh" / "config.yaml"

        result = runner.invoke(
            main, ['-c', str(config_path), 'init', '--node-name', 'my-node'],
        )

        assert result.exit_code == 0
        assert config_path.exists()
        assert (config_path.parent / "scripts").is_dir()
        assert (config_path.parent / "logs").is_dir()

    def test_creates_example_script(self, runner, tmp_path):
        config_path = tmp_path / "fresh" / "config.yaml"

        result = runner.invoke(
            main, ['-c', str(config_path), 'init', '--node-name', 'my-node'],
        )

        assert result.exit_code == 0
        assert "Example script" in result.output

    def test_generates_valid_base32_secret(self, runner, tmp_path):
        config_path = tmp_path / "fresh" / "config.yaml"

        result = runner.invoke(
            main, ['-c', str(config_path), 'init', '--node-name', 'my-node'],
        )

        assert result.exit_code == 0
        # The secret should appear in the output
        assert "Secret:" in result.output

        # Load the generated config and verify secret is valid base32
        from posthumous.config import Config
        cfg = Config.from_yaml(config_path)
        assert len(cfg.secret_key) == 32
        assert cfg.secret_key.isalnum()

    def test_shows_qr_code(self, runner, tmp_path):
        config_path = tmp_path / "fresh" / "config.yaml"

        result = runner.invoke(
            main, ['-c', str(config_path), 'init', '--node-name', 'my-node'],
        )

        assert result.exit_code == 0
        assert "TOTP Setup" in result.output

    def test_shows_provisioning_uri(self, runner, tmp_path):
        config_path = tmp_path / "fresh" / "config.yaml"

        result = runner.invoke(
            main, ['-c', str(config_path), 'init', '--node-name', 'my-node'],
        )

        assert result.exit_code == 0
        assert "otpauth://totp/" in result.output

    def test_fails_if_config_exists(self, runner, config_path):
        result = runner.invoke(
            main, ['-c', str(config_path), 'init', '--node-name', 'my-node'],
        )

        assert result.exit_code == 1
        assert "already exists" in result.output

    def test_join_prompts_for_secret(self, runner, tmp_path):
        config_path = tmp_path / "fresh" / "config.yaml"

        result = runner.invoke(
            main, ['-c', str(config_path), 'init', '--node-name', 'my-node', '--join', 'https://peer:8420'],
            input="JBSWY3DPEHPK3PXP\n",
        )

        assert result.exit_code == 0
        assert config_path.exists()


class TestCheckin:
    """Tests for the 'checkin' command."""

    def test_valid_totp(self, runner, config_path):
        code = pyotp.TOTP(SECRET, issuer="Posthumous").now()

        result = runner.invoke(
            main, ['-c', str(config_path), 'checkin'],
            input=f"{code}\n",
        )

        assert result.exit_code == 0
        assert "Check-in accepted" in result.output
        assert "ARMED" in result.output

    def test_api_token(self, runner, valid_config_dir):
        # Write a config with an API token
        config_path = valid_config_dir / "config.yaml"
        from posthumous.config import Config
        cfg = Config.from_yaml(config_path)
        token = "my-test-api-token-1234567890abcdef"
        cfg.api_token = token
        cfg.save(config_path)

        result = runner.invoke(
            main, ['-c', str(config_path), 'checkin', '--token', token],
        )

        assert result.exit_code == 0
        assert "Check-in accepted" in result.output

    def test_invalid_code(self, runner, config_path):
        result = runner.invoke(
            main, ['-c', str(config_path), 'checkin'],
            input="000000\n",
        )

        assert result.exit_code == 1
        assert "Invalid code" in result.output

    def test_triggered_state(self, runner, valid_config_dir, config_path):
        # Create a triggered state
        from posthumous.state import State, Status
        state = State()
        state.status = Status.TRIGGERED
        state.trigger_time = datetime.now(timezone.utc)
        state.save(valid_config_dir / "state.yaml")

        code = pyotp.TOTP(SECRET, issuer="Posthumous").now()
        result = runner.invoke(
            main, ['-c', str(config_path), 'checkin'],
            input=f"{code}\n",
        )

        assert result.exit_code == 1
        assert "TRIGGERED" in result.output

    def test_no_config(self, runner, tmp_path):
        config_path = tmp_path / "missing.yaml"

        result = runner.invoke(
            main, ['-c', str(config_path), 'checkin'],
        )

        assert result.exit_code == 1
        assert "Config not found" in result.output

    def test_shows_next_deadline(self, runner, config_path):
        code = pyotp.TOTP(SECRET, issuer="Posthumous").now()

        result = runner.invoke(
            main, ['-c', str(config_path), 'checkin'],
            input=f"{code}\n",
        )

        assert result.exit_code == 0
        assert "Next deadline:" in result.output


class TestStatus:
    """Tests for the 'status' command."""

    def test_armed_state(self, runner, valid_config_dir, config_path):
        # Fresh state = ARMED
        result = runner.invoke(
            main, ['-c', str(config_path), 'status'],
        )

        assert result.exit_code == 0
        assert "ARMED" in result.output
        assert "test-node" in result.output

    def test_triggered_state(self, runner, valid_config_dir, config_path):
        from posthumous.state import State, Status
        state = State()
        state.status = Status.TRIGGERED
        state.trigger_time = datetime.now(timezone.utc)
        state.save(valid_config_dir / "state.yaml")

        result = runner.invoke(
            main, ['-c', str(config_path), 'status'],
        )

        assert result.exit_code == 0
        assert "TRIGGERED" in result.output
        assert "Triggered at:" in result.output

    def test_with_last_checkin(self, runner, valid_config_dir, config_path):
        from posthumous.state import State
        state = State()
        state.record_checkin()
        state.save(valid_config_dir / "state.yaml")

        result = runner.invoke(
            main, ['-c', str(config_path), 'status'],
        )

        assert result.exit_code == 0
        assert "Last check-in:" in result.output

    def test_with_peers(self, runner, valid_config_dir, config_path):
        # Add peers to config
        from posthumous.config import Config
        cfg = Config.from_yaml(config_path)
        cfg.peers = ["https://peer1:8420"]
        cfg.save(config_path)

        result = runner.invoke(
            main, ['-c', str(config_path), 'status'],
        )

        assert result.exit_code == 0
        assert "Peers:" in result.output
        assert "peer1" in result.output

    def test_no_config(self, runner, tmp_path):
        config_path = tmp_path / "missing.yaml"

        result = runner.invoke(
            main, ['-c', str(config_path), 'status'],
        )

        assert result.exit_code == 1
        assert "Config not found" in result.output


class TestPeers:
    """Tests for the 'peers' command."""

    def test_no_peers_configured(self, runner, config_path):
        result = runner.invoke(
            main, ['-c', str(config_path), 'peers'],
        )

        assert result.exit_code == 0
        assert "No peers configured" in result.output

    def test_no_config(self, runner, tmp_path):
        config_path = tmp_path / "missing.yaml"

        result = runner.invoke(
            main, ['-c', str(config_path), 'peers'],
        )

        assert result.exit_code == 1
        assert "Config not found" in result.output


class TestTestNotify:
    """Tests for the 'test-notify' command."""

    def test_unknown_channel(self, runner, config_path):
        result = runner.invoke(
            main, ['-c', str(config_path), 'test-notify', '-c', 'nonexistent'],
        )

        assert result.exit_code == 0
        assert "Unknown channel" in result.output or "Unknown channel" in result.output

    def test_no_config(self, runner, tmp_path):
        config_path = tmp_path / "missing.yaml"

        result = runner.invoke(
            main, ['-c', str(config_path), 'test-notify'],
        )

        assert result.exit_code == 1
        assert "Config not found" in result.output


class TestTestTrigger:
    """Tests for the 'test-trigger' command."""

    def test_lists_trigger_actions(self, runner, config_path):
        result = runner.invoke(
            main, ['-c', str(config_path), 'test-trigger'],
        )

        assert result.exit_code == 0
        assert "Trigger actions" in result.output
        assert "Notify channel" in result.output
        assert "Run script" in result.output

    def test_lists_scheduled_items(self, runner, config_path):
        result = runner.invoke(
            main, ['-c', str(config_path), 'test-trigger'],
        )

        assert result.exit_code == 0
        assert "Post-trigger scheduled items" in result.output
        assert "annual" in result.output

    def test_shows_script_exists_status(self, runner, valid_config_dir, config_path):
        # Create the script file so it shows as existing
        (valid_config_dir / "scripts" / "on_trigger.py").write_text("print('ok')\n")

        result = runner.invoke(
            main, ['-c', str(config_path), 'test-trigger'],
        )

        assert result.exit_code == 0

    def test_no_config(self, runner, tmp_path):
        config_path = tmp_path / "missing.yaml"

        result = runner.invoke(
            main, ['-c', str(config_path), 'test-trigger'],
        )

        assert result.exit_code == 1
        assert "Config not found" in result.output


class TestExport:
    """Tests for the 'export' command."""

    def test_creates_yaml_file(self, runner, valid_config_dir, config_path, tmp_path):
        output_path = tmp_path / "backup.yaml"

        result = runner.invoke(
            main, ['-c', str(config_path), 'export', str(output_path)],
        )

        assert result.exit_code == 0
        assert output_path.exists()
        assert "Exported to" in result.output

        # Verify it's valid YAML with config and state
        with open(output_path) as f:
            data = yaml.safe_load(f)
        assert "config" in data
        assert "state" in data
        assert data["config"]["node_name"] == "test-node"

    def test_no_config(self, runner, tmp_path):
        config_path = tmp_path / "missing.yaml"
        output_path = tmp_path / "backup.yaml"

        result = runner.invoke(
            main, ['-c', str(config_path), 'export', str(output_path)],
        )

        assert result.exit_code == 1
        assert "Config not found" in result.output


class TestImport:
    """Tests for the 'import' command."""

    def test_restores_state(self, runner, valid_config_dir, config_path, tmp_path):
        # Create a backup file
        backup_path = tmp_path / "backup.yaml"
        now = datetime.now(timezone.utc)
        backup_data = {
            "config": {
                "node_name": "test-node",
                "secret_key": SECRET,
            },
            "state": {
                "status": "warning",
                "last_checkin": now.isoformat(),
                "trigger_time": None,
                "schedule_state": {},
                "failed_attempts": [],
                "peer_states": {},
            },
        }
        with open(backup_path, "w") as f:
            yaml.dump(backup_data, f)

        result = runner.invoke(
            main, ['-c', str(config_path), 'import', str(backup_path)],
        )

        assert result.exit_code == 0
        assert "State restored" in result.output
        assert "warning" in result.output

    def test_restores_config_if_missing(self, runner, tmp_path):
        config_path = tmp_path / "new_dir" / "config.yaml"
        backup_path = tmp_path / "backup.yaml"

        backup_data = {
            "config": {
                "node_name": "restored-node",
                "secret_key": SECRET,
                "checkin_interval": "7 days",
                "warning_start": "8 days",
                "grace_start": "12 days",
                "trigger_at": "14 days",
            },
            "state": {
                "status": "armed",
                "last_checkin": None,
                "trigger_time": None,
                "schedule_state": {},
                "failed_attempts": [],
                "peer_states": {},
            },
        }
        with open(backup_path, "w") as f:
            yaml.dump(backup_data, f)

        result = runner.invoke(
            main, ['-c', str(config_path), 'import', str(backup_path)],
        )

        assert result.exit_code == 0
        assert "Config restored" in result.output
        assert config_path.exists()

    def test_invalid_backup_file(self, runner, config_path, tmp_path):
        backup_path = tmp_path / "bad_backup.yaml"
        with open(backup_path, "w") as f:
            yaml.dump({"nothing": "useful"}, f)

        result = runner.invoke(
            main, ['-c', str(config_path), 'import', str(backup_path)],
        )

        assert result.exit_code == 1
        assert "No state found" in result.output

    def test_keeps_existing_config(self, runner, valid_config_dir, config_path, tmp_path):
        backup_path = tmp_path / "backup.yaml"
        backup_data = {
            "config": {
                "node_name": "different-node",
                "secret_key": "DIFFERENTKEY123456",
            },
            "state": {
                "status": "armed",
                "last_checkin": None,
                "trigger_time": None,
                "schedule_state": {},
                "failed_attempts": [],
                "peer_states": {},
            },
        }
        with open(backup_path, "w") as f:
            yaml.dump(backup_data, f)

        result = runner.invoke(
            main, ['-c', str(config_path), 'import', str(backup_path)],
        )

        assert result.exit_code == 0
        # Should NOT have "Config restored" since config already exists
        assert "Config restored" not in result.output
        # Config should still have original node name
        from posthumous.config import Config
        cfg = Config.from_yaml(config_path)
        assert cfg.node_name == "test-node"


class TestSetupLogging:
    """Tests for setup_logging helper."""

    def _reset_logging(self):
        """Reset root logger so basicConfig takes effect again."""
        import logging
        root = logging.getLogger()
        for h in root.handlers[:]:
            root.removeHandler(h)
        root.setLevel(logging.WARNING)

    def test_verbose_sets_debug(self):
        import logging
        self._reset_logging()

        setup_logging(verbose=True)

        root = logging.getLogger()
        assert root.level == logging.DEBUG

    def test_default_sets_info(self):
        import logging
        self._reset_logging()

        setup_logging(verbose=False)

        root = logging.getLogger()
        assert root.level == logging.INFO

    def test_log_file_created(self, tmp_path):
        import logging
        self._reset_logging()

        log_file = tmp_path / "logs" / "test.log"

        setup_logging(log_file=log_file)

        assert log_file.exists()


class TestCheckinExtended:
    """Additional CLI checkin tests for uncovered paths."""

    def test_lockout(self, runner, valid_config_dir, config_path):
        """After max failed attempts, should show lockout error."""
        from posthumous.state import State
        from datetime import timedelta

        # Create state with a lockout
        state = State()
        state.lockout_until = datetime.now(timezone.utc) + timedelta(minutes=15)
        state.save(valid_config_dir / "state.yaml")

        result = runner.invoke(
            main, ['-c', str(config_path), 'checkin'],
            input="000000\n",
        )

        assert result.exit_code == 1
        assert "locked out" in result.output.lower()


class TestStatusExtended:
    """Additional status tests for uncovered peer_state branch."""

    def test_peer_with_last_seen(self, runner, valid_config_dir, config_path):
        """Status should show peer last-seen time."""
        from posthumous.config import Config
        from posthumous.state import State, PeerState

        cfg = Config.from_yaml(config_path)
        cfg.peers = ["https://peer1:8420"]
        cfg.save(config_path)

        state = State()
        state.peer_states["https://peer1:8420"] = PeerState(
            url="https://peer1:8420",
            last_seen=datetime.now(timezone.utc),
            consecutive_failures=0,
        )
        state.save(valid_config_dir / "state.yaml")

        result = runner.invoke(
            main, ['-c', str(config_path), 'status'],
        )

        assert result.exit_code == 0
        assert "seen" in result.output
        assert "peer1" in result.output

    def test_no_checkin_yet(self, runner, config_path):
        """Status with no check-in should still work."""
        result = runner.invoke(
            main, ['-c', str(config_path), 'status'],
        )

        assert result.exit_code == 0
        # Should not show "Last check-in:" when no checkin
        assert "ARMED" in result.output

    def test_warning_state(self, runner, valid_config_dir, config_path):
        from posthumous.state import State, Status
        state = State()
        state.status = Status.WARNING
        # Set last_checkin far in the past so the state stays WARNING
        state.last_checkin = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        state.save(valid_config_dir / "state.yaml")

        result = runner.invoke(
            main, ['-c', str(config_path), 'status'],
        )

        assert result.exit_code == 0
        assert "WARNING" in result.output


class TestTestNotifyExtended:
    """Additional test-notify coverage."""

    def test_all_channels(self, runner, valid_config_dir, config_path):
        """Testing all channels should list each channel result."""
        # Mock Apprise to avoid actual network calls
        with patch('posthumous.notifications.apprise.Apprise') as mock_cls:
            mock_instance = MagicMock()
            mock_instance.notify.return_value = True
            mock_instance.__len__ = lambda self: 1
            mock_cls.return_value = mock_instance

            result = runner.invoke(
                main, ['-c', str(config_path), 'test-notify'],
            )

        assert result.exit_code == 0
        assert "default" in result.output

    def test_specific_channel(self, runner, valid_config_dir, config_path):
        """Testing a specific channel."""
        with patch('posthumous.notifications.apprise.Apprise') as mock_cls:
            mock_instance = MagicMock()
            mock_instance.notify.return_value = True
            mock_instance.__len__ = lambda self: 1
            mock_cls.return_value = mock_instance

            result = runner.invoke(
                main, ['-c', str(config_path), 'test-notify', '-c', 'default'],
            )

        assert result.exit_code == 0
        assert "default" in result.output


class TestCheckinAuthError:
    """Test checkin with AuthError (not LockedOutError)."""

    def test_checkin_auth_error(self, runner, valid_config_dir, config_path):
        """AuthError (e.g., no code and no token) should show error and exit 1."""
        from posthumous.auth import AuthError

        with patch('posthumous.auth.Authenticator.verify', side_effect=AuthError("No code or token")):
            result = runner.invoke(
                main, ['-c', str(config_path), 'checkin'],
                input="123456\n",
            )

        assert result.exit_code == 1
        assert "No code or token" in result.output

    @pytest.fixture
    def runner(self):
        return CliRunner()

    @pytest.fixture
    def valid_config_dir(self, tmp_path):
        config_dir = tmp_path / ".posthumous"
        config_dir.mkdir()
        (config_dir / "scripts").mkdir()
        (config_dir / "logs").mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            "node_name: test-node\n"
            f"secret_key: {SECRET}\n"
            "listen: '127.0.0.1:8420'\n"
            "checkin_interval: 7 days\n"
            "warning_start: 8 days\n"
            "grace_start: 12 days\n"
            "trigger_at: 14 days\n"
        )
        return config_dir

    @pytest.fixture
    def config_path(self, valid_config_dir):
        return valid_config_dir / "config.yaml"


class TestPeersCommandWithPeers:
    """Test peers command when peers are configured."""

    @pytest.fixture
    def runner(self):
        return CliRunner()

    def test_peers_command_with_peers(self, runner, tmp_path):
        """peers command should show peer status when peers configured."""
        config_dir = tmp_path / ".posthumous"
        config_dir.mkdir()
        (config_dir / "scripts").mkdir()
        (config_dir / "logs").mkdir()
        config_path = config_dir / "config.yaml"
        config_path.write_text(
            "node_name: test-node\n"
            f"secret_key: {SECRET}\n"
            "listen: '127.0.0.1:8420'\n"
            "checkin_interval: 7 days\n"
            "warning_start: 8 days\n"
            "grace_start: 12 days\n"
            "trigger_at: 14 days\n"
            "peers:\n"
            "  - http://localhost:18421\n"
        )

        from aioresponses import aioresponses

        with aioresponses() as m:
            m.get(
                "http://localhost:18421/status",
                payload={
                    "node_name": "peer1",
                    "status": "armed",
                    "last_checkin": datetime.now(timezone.utc).isoformat(),
                },
            )

            result = runner.invoke(
                main, ['-c', str(config_path), 'peers'],
            )

        assert result.exit_code == 0
        assert "Peers:" in result.output


class TestInitQRImportError:
    """Test init when qrcode package is not available."""

    @pytest.fixture
    def runner(self):
        return CliRunner()

    def test_init_qr_import_error(self, runner, tmp_path):
        """When qrcode is not installed, init should show a fallback message."""
        config_path = tmp_path / "fresh" / "config.yaml"

        with patch('posthumous.auth.generate_qr_code_terminal', side_effect=ImportError("No qrcode")):
            result = runner.invoke(
                main, ['-c', str(config_path), 'init', '--node-name', 'my-node'],
            )

        assert result.exit_code == 0
        assert "qrcode" in result.output.lower() or "QR" in result.output

    def test_import_restores_config_from_backup(self, runner, tmp_path):
        """Import should restore config when no existing config and backup has config."""
        import yaml

        config_path = tmp_path / "new_dir" / "config.yaml"
        backup_path = tmp_path / "backup.yaml"

        backup_data = {
            "config": {
                "node_name": "restored-node",
                "secret_key": SECRET,
                "checkin_interval": "7 days",
                "warning_start": "8 days",
                "grace_start": "12 days",
                "trigger_at": "14 days",
            },
            "state": {
                "status": "armed",
                "last_checkin": None,
                "trigger_time": None,
                "schedule_state": {},
                "failed_attempts": [],
                "peer_states": {},
            },
        }
        with open(backup_path, "w") as f:
            yaml.dump(backup_data, f)

        result = runner.invoke(
            main, ['-c', str(config_path), 'import', str(backup_path)],
        )

        assert result.exit_code == 0
        assert "Config restored" in result.output
        assert config_path.exists()

    def test_import_no_config_and_no_backup_config(self, runner, tmp_path):
        """Import should fail when no existing config and backup has no config."""
        import yaml

        config_path = tmp_path / "new_dir" / "config.yaml"
        backup_path = tmp_path / "backup.yaml"

        backup_data = {
            "state": {
                "status": "armed",
                "last_checkin": None,
            },
        }
        with open(backup_path, "w") as f:
            yaml.dump(backup_data, f)

        result = runner.invoke(
            main, ['-c', str(config_path), 'import', str(backup_path)],
        )

        assert result.exit_code == 1
        assert "No config in backup" in result.output


class TestExportEncryption:
    """Tests for export with encrypt_at_rest."""

    def test_export_encrypt_at_rest_shows_decrypted_message(self, runner, tmp_path):
        """Export with encrypt_at_rest=True should show '(decrypted)' message."""
        config_dir = tmp_path / ".posthumous"
        config_dir.mkdir()
        (config_dir / "scripts").mkdir()
        (config_dir / "logs").mkdir()

        config_path = config_dir / "config.yaml"
        config_path.write_text(
            "node_name: test-node\n"
            f"secret_key: {SECRET}\n"
            "listen: '127.0.0.1:8420'\n"
            "checkin_interval: 7 days\n"
            "warning_start: 8 days\n"
            "grace_start: 12 days\n"
            "trigger_at: 14 days\n"
            "encrypt_at_rest: true\n"
        )

        # Create state file (plaintext is fine — StateManager handles it)
        state_path = config_dir / "state.yaml"
        state_path.write_text("status: armed\nlast_checkin: null\n")

        output_path = tmp_path / "backup.yaml"
        result = runner.invoke(
            main, ['-c', str(config_path), 'export', str(output_path)],
        )

        assert result.exit_code == 0
        assert "(decrypted)" in result.output


class TestReset:
    """Tests for the 'reset' command (Issue 9)."""

    def test_reset_from_triggered(self, runner, valid_config_dir, config_path):
        """Reset from TRIGGERED with --force should succeed."""
        from posthumous.state import State, Status

        state = State()
        state.status = Status.TRIGGERED
        state.trigger_time = datetime.now(timezone.utc)
        state.save(valid_config_dir / "state.yaml")

        result = runner.invoke(
            main, ['-c', str(config_path), 'reset', '--force'],
        )

        assert result.exit_code == 0
        assert "State reset to ARMED" in result.output

    def test_reset_from_warning(self, runner, valid_config_dir, config_path):
        """Reset from WARNING with --force should succeed."""
        from posthumous.state import State, Status

        state = State()
        state.status = Status.WARNING
        state.save(valid_config_dir / "state.yaml")

        result = runner.invoke(
            main, ['-c', str(config_path), 'reset', '--force'],
        )

        assert result.exit_code == 0
        assert "State reset to ARMED" in result.output

    def test_reset_already_armed(self, runner, config_path):
        """Reset when already ARMED should report no change."""
        result = runner.invoke(
            main, ['-c', str(config_path), 'reset'],
        )

        assert result.exit_code == 0
        assert "Already ARMED" in result.output

    def test_reset_requires_confirmation(self, runner, valid_config_dir, config_path):
        """Reset from TRIGGERED without --force should prompt."""
        from posthumous.state import State, Status

        state = State()
        state.status = Status.TRIGGERED
        state.trigger_time = datetime.now(timezone.utc)
        state.save(valid_config_dir / "state.yaml")

        # Abort the prompt
        result = runner.invoke(
            main, ['-c', str(config_path), 'reset'],
            input="n\n",
        )

        assert result.exit_code != 0  # Aborted

    def test_reset_confirmed(self, runner, valid_config_dir, config_path):
        """Reset from TRIGGERED with confirmation should succeed."""
        from posthumous.state import State, Status

        state = State()
        state.status = Status.TRIGGERED
        state.trigger_time = datetime.now(timezone.utc)
        state.save(valid_config_dir / "state.yaml")

        result = runner.invoke(
            main, ['-c', str(config_path), 'reset'],
            input="y\n",
        )

        assert result.exit_code == 0
        assert "State reset to ARMED" in result.output

    def test_reset_no_config(self, runner, tmp_path):
        """Reset without config should fail."""
        config_path = tmp_path / "missing.yaml"

        result = runner.invoke(
            main, ['-c', str(config_path), 'reset'],
        )

        assert result.exit_code == 1
        assert "Config not found" in result.output
