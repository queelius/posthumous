"""Shared pytest fixtures for Posthumous tests."""

import pytest
from pathlib import Path


@pytest.fixture
def tmp_config_dir(tmp_path):
    """Create a temporary config directory structure."""
    config_dir = tmp_path / ".posthumous"
    config_dir.mkdir()
    (config_dir / "scripts").mkdir()
    (config_dir / "logs").mkdir()
    return config_dir


@pytest.fixture
def sample_config_yaml(tmp_config_dir):
    """Create a sample config file."""
    config_path = tmp_config_dir / "config.yaml"
    config_path.write_text("""
node_name: test-node
secret_key: JBSWY3DPEHPK3PXP
listen: "127.0.0.1:8420"
checkin_interval: 7 days
warning_start: 8 days
grace_start: 12 days
trigger_at: 14 days

notifications:
  default:
    - "ntfy://test-topic"

actions:
  on_warning:
    - notify: default
      message: "Warning: {days_left} days remaining"
  on_trigger:
    - notify: default
      message: "Triggered!"
""")
    return config_path
