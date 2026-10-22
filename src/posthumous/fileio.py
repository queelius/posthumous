"""Crash-safe file writes shared by config, state and encrypted files."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write(path: Path, data: bytes, prefix: str, suffix: str = ".tmp") -> None:
    """Write `data` to `path` atomically, readable only by the owner.

    Writes to a temp file in the same directory and renames it over `path`, so
    a crash leaves either the old file or the new one, never a partial write.
    The temp file is removed if anything fails.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(dir=path.parent, prefix=prefix, suffix=suffix)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        os.replace(temp_path, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
