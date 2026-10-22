"""Tests for crash-safe file writes."""

import os
import stat
from unittest.mock import patch

import pytest

from posthumous.fileio import atomic_write


class TestAtomicWrite:
    """Tests for atomic_write."""

    def test_writes_content(self, tmp_path):
        path = tmp_path / "out.yaml"
        atomic_write(path, b"hello\n", prefix=".out_")
        assert path.read_bytes() == b"hello\n"

    def test_replaces_existing_file(self, tmp_path):
        path = tmp_path / "out.yaml"
        path.write_bytes(b"old")
        atomic_write(path, b"new", prefix=".out_")
        assert path.read_bytes() == b"new"

    def test_sets_owner_only_permissions(self, tmp_path):
        path = tmp_path / "out.yaml"
        atomic_write(path, b"secret", prefix=".out_")
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    def test_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "a" / "b" / "out.yaml"
        atomic_write(path, b"x", prefix=".out_")
        assert path.read_bytes() == b"x"

    def test_failure_keeps_old_file_and_removes_temp(self, tmp_path):
        path = tmp_path / "out.yaml"
        path.write_bytes(b"old")
        with patch("os.replace", side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                atomic_write(path, b"new", prefix=".out_")
        assert path.read_bytes() == b"old"
        assert list(tmp_path.glob(".out_*")) == []

    def test_failure_when_cleanup_also_fails_raises_original(self, tmp_path):
        path = tmp_path / "out.yaml"
        with patch("os.replace", side_effect=OSError("disk full")), \
             patch("os.unlink", side_effect=OSError("permission denied")):
            with pytest.raises(OSError, match="disk full"):
                atomic_write(path, b"new", prefix=".out_")
