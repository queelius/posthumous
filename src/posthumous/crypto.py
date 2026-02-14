"""Encryption at rest for Posthumous config and state files."""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Magic bytes to identify encrypted files
ENCRYPTED_MAGIC = b"PHM_ENC_v1\n"


def derive_key(secret: str) -> bytes:
    """Derive a Fernet-compatible key from a secret string.

    Uses SHA-256 + base64 to produce a 32-byte URL-safe base64-encoded key
    suitable for Fernet.
    """
    digest = hashlib.sha256(secret.encode()).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt(data: str, key: bytes) -> bytes:
    """Encrypt a string using Fernet.

    Returns bytes prefixed with magic header for identification.
    """
    from cryptography.fernet import Fernet
    f = Fernet(key)
    encrypted = f.encrypt(data.encode())
    return ENCRYPTED_MAGIC + encrypted


def decrypt(data: bytes, key: bytes) -> str:
    """Decrypt Fernet-encrypted data.

    Strips the magic header before decrypting.
    Raises ValueError if data is not encrypted or decryption fails.
    """
    if not is_encrypted(data):
        raise ValueError("Data is not encrypted (missing magic header)")

    from cryptography.fernet import Fernet, InvalidToken
    f = Fernet(key)
    encrypted = data[len(ENCRYPTED_MAGIC):]
    try:
        return f.decrypt(encrypted).decode()
    except InvalidToken:
        raise ValueError("Decryption failed: invalid key or corrupted data")


def is_encrypted(data: bytes) -> bool:
    """Check if data starts with the encryption magic header."""
    return data.startswith(ENCRYPTED_MAGIC)


def encrypt_file(path: Path, key: bytes) -> None:
    """Encrypt a file in place.

    If the file is already encrypted, this is a no-op.
    Uses atomic write (temp + rename) for safety.
    """
    import tempfile

    data = path.read_bytes()
    if is_encrypted(data):
        return  # Already encrypted

    encrypted = encrypt(data.decode(), key)

    fd, temp_path = tempfile.mkstemp(
        dir=path.parent,
        prefix='.enc_',
        suffix='.tmp',
    )
    try:
        os.write(fd, encrypted)
        os.close(fd)
        os.replace(temp_path, path)
        logger.info(f"Encrypted {path}")
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def decrypt_file(path: Path, key: bytes) -> str:
    """Read and decrypt a file.

    If the file is not encrypted, returns content as-is (migration support).
    """
    data = path.read_bytes()
    if is_encrypted(data):
        return decrypt(data, key)
    return data.decode()


def save_encrypted(path: Path, content: str, key: bytes) -> None:
    """Save content to a file with encryption.

    Uses atomic write (temp + rename) for safety.
    """
    import tempfile

    encrypted = encrypt(content, key)

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        dir=path.parent,
        prefix='.enc_',
        suffix='.tmp',
    )
    try:
        os.write(fd, encrypted)
        os.close(fd)
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
