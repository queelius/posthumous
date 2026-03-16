"""Encryption at rest for Posthumous config and state files."""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Magic bytes to identify encrypted files
ENCRYPTED_MAGIC_V1 = b"PHM_ENC_v1\n"
ENCRYPTED_MAGIC_V2 = b"PHM_ENC_v2\n"

# PBKDF2 parameters
SALT_LENGTH = 16
PBKDF2_ITERATIONS = 600_000


def _derive_key_v1(secret: str) -> bytes:
    """Legacy v1 key derivation: bare SHA-256 + base64.

    Kept only for reading/migrating v1 encrypted files.
    """
    digest = hashlib.sha256(secret.encode()).digest()
    return base64.urlsafe_b64encode(digest)


def derive_key(secret: str, salt: bytes | None = None) -> tuple[bytes, bytes]:
    """Derive a Fernet-compatible key from a secret string using PBKDF2-HMAC-SHA256.

    Args:
        secret: The secret string to derive a key from.
        salt: Optional 16-byte salt. If None, a random salt is generated.

    Returns:
        A tuple of (key, salt) where key is a 44-byte base64-encoded Fernet key
        and salt is the 16-byte salt used for derivation.
    """
    if salt is None:
        salt = os.urandom(SALT_LENGTH)

    digest = hashlib.pbkdf2_hmac(
        'sha256',
        secret.encode(),
        salt,
        PBKDF2_ITERATIONS,
    )
    key = base64.urlsafe_b64encode(digest)
    return key, salt


def encrypt(data: str, key: bytes, salt: bytes) -> bytes:
    """Encrypt a string using Fernet with v2 format.

    Returns bytes in v2 format: MAGIC_V2 + salt + \\n + fernet_ciphertext
    """
    from cryptography.fernet import Fernet
    f = Fernet(key)
    encrypted = f.encrypt(data.encode())
    return ENCRYPTED_MAGIC_V2 + salt + b'\n' + encrypted


def decrypt(data: bytes, key: bytes | None = None, secret: str | None = None) -> str:
    """Decrypt Fernet-encrypted data.

    Handles both v1 and v2 formats. For v2, extracts salt from the data
    to re-derive the key from the secret. For v1, uses legacy SHA-256 derivation.

    Args:
        data: Encrypted bytes (v1 or v2 format).
        key: Explicit Fernet key (only works for v2 if the key matches).
        secret: Secret string used to derive the key (works for both v1 and v2).

    Raises:
        ValueError: If data is not encrypted, decryption fails, or no key/secret given.
    """
    if not is_encrypted(data):
        raise ValueError("Data is not encrypted (missing magic header)")

    if key is None and secret is None:
        raise ValueError("Either key or secret must be provided")

    from cryptography.fernet import Fernet, InvalidToken

    if data.startswith(ENCRYPTED_MAGIC_V2):
        # V2 format: MAGIC_V2 + salt(16) + \n + ciphertext
        after_magic = data[len(ENCRYPTED_MAGIC_V2):]
        stored_salt = after_magic[:SALT_LENGTH]
        # Skip the \n separator
        ciphertext = after_magic[SALT_LENGTH + 1:]

        if key is None:
            # Derive key from secret + stored salt
            key, _ = derive_key(secret, salt=stored_salt)

        f = Fernet(key)
        try:
            return f.decrypt(ciphertext).decode()
        except InvalidToken:
            raise ValueError("Decryption failed: invalid key or corrupted data")

    elif data.startswith(ENCRYPTED_MAGIC_V1):
        # V1 format: MAGIC_V1 + ciphertext
        ciphertext = data[len(ENCRYPTED_MAGIC_V1):]

        if key is None:
            # Derive legacy v1 key from secret
            key = _derive_key_v1(secret)

        f = Fernet(key)
        try:
            return f.decrypt(ciphertext).decode()
        except InvalidToken:
            raise ValueError("Decryption failed: invalid key or corrupted data")

    else:
        raise ValueError("Data is not encrypted (missing magic header)")


def is_encrypted(data: bytes) -> bool:
    """Check if data starts with either v1 or v2 encryption magic header."""
    return data.startswith(ENCRYPTED_MAGIC_V1) or data.startswith(ENCRYPTED_MAGIC_V2)


def encrypt_file(path: Path, secret: str) -> None:
    """Encrypt a file in place using v2 format.

    If the file is already encrypted (v1 or v2), this is a no-op.
    Uses atomic write (temp + rename) for safety.
    """
    import tempfile

    data = path.read_bytes()
    if is_encrypted(data):
        return  # Already encrypted

    key, salt = derive_key(secret)
    encrypted = encrypt(data.decode(), key, salt)

    fd, temp_path = tempfile.mkstemp(
        dir=path.parent,
        prefix='.enc_',
        suffix='.tmp',
    )
    try:
        os.write(fd, encrypted)
        os.close(fd)
        os.replace(temp_path, path)
        os.chmod(path, 0o600)
        logger.info(f"Encrypted {path}")
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def decrypt_file(path: Path, secret: str) -> str:
    """Read and decrypt a file.

    If the file is not encrypted, returns content as-is (migration support).
    If the file is v1 encrypted, auto-migrates to v2 format on disk.
    """
    data = path.read_bytes()

    if data.startswith(ENCRYPTED_MAGIC_V1):
        # V1 file: decrypt, then re-encrypt as v2 (auto-migration)
        plaintext = decrypt(data, secret=secret)
        # Re-encrypt as v2
        key, salt = derive_key(secret)
        v2_data = encrypt(plaintext, key, salt)
        # Atomic write back
        import tempfile
        fd, temp_path = tempfile.mkstemp(
            dir=path.parent,
            prefix='.enc_',
            suffix='.tmp',
        )
        try:
            os.write(fd, v2_data)
            os.close(fd)
            os.replace(temp_path, path)
            logger.info(f"Migrated {path} from v1 to v2 encryption format")
        except Exception:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise
        return plaintext

    if data.startswith(ENCRYPTED_MAGIC_V2):
        return decrypt(data, secret=secret)

    # Not encrypted - return as-is (plaintext migration support)
    return data.decode()


def save_encrypted(path: Path, content: str, secret: str) -> None:
    """Save content to a file with v2 encryption.

    Uses atomic write (temp + rename) for safety.
    """
    import tempfile

    key, salt = derive_key(secret)
    encrypted = encrypt(content, key, salt)

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
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
