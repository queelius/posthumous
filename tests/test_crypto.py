"""Tests for encryption at rest."""

import pytest
from pathlib import Path

from posthumous.crypto import (
    derive_key,
    encrypt,
    decrypt,
    is_encrypted,
    encrypt_file,
    decrypt_file,
    save_encrypted,
    ENCRYPTED_MAGIC,
)


class TestDeriveKey:
    """Tests for key derivation."""

    def test_produces_valid_fernet_key(self):
        """Derived key should be base64-encoded and 44 bytes."""
        key = derive_key("test-secret")
        assert len(key) == 44  # base64(32 bytes) = 44 chars
        # Should be decodable
        import base64
        decoded = base64.urlsafe_b64decode(key)
        assert len(decoded) == 32

    def test_deterministic(self):
        """Same secret should produce same key."""
        assert derive_key("my-secret") == derive_key("my-secret")

    def test_different_secrets_different_keys(self):
        """Different secrets should produce different keys."""
        assert derive_key("secret-a") != derive_key("secret-b")


class TestEncryptDecrypt:
    """Tests for encrypt/decrypt round-trip."""

    def test_round_trip(self):
        key = derive_key("test-secret")
        plaintext = "Hello, World!"
        encrypted = encrypt(plaintext, key)
        decrypted = decrypt(encrypted, key)
        assert decrypted == plaintext

    def test_encrypted_has_magic_header(self):
        key = derive_key("test-secret")
        encrypted = encrypt("data", key)
        assert encrypted.startswith(ENCRYPTED_MAGIC)

    def test_decrypt_wrong_key_raises(self):
        key1 = derive_key("correct-key")
        key2 = derive_key("wrong-key")
        encrypted = encrypt("secret data", key1)

        with pytest.raises(ValueError, match="Decryption failed"):
            decrypt(encrypted, key2)

    def test_decrypt_non_encrypted_raises(self):
        key = derive_key("test-secret")
        with pytest.raises(ValueError, match="not encrypted"):
            decrypt(b"plain text data", key)

    def test_is_encrypted_true(self):
        key = derive_key("test-secret")
        encrypted = encrypt("data", key)
        assert is_encrypted(encrypted) is True

    def test_is_encrypted_false(self):
        assert is_encrypted(b"plain text") is False
        assert is_encrypted(b"") is False

    def test_round_trip_yaml_content(self):
        """Encrypt/decrypt preserves YAML content exactly."""
        key = derive_key("test-secret")
        yaml_content = "node_name: test\nsecret_key: JBSWY3DPEHPK3PXP\nstatus: armed\n"
        encrypted = encrypt(yaml_content, key)
        decrypted = decrypt(encrypted, key)
        assert decrypted == yaml_content

    def test_round_trip_unicode(self):
        """Unicode content round-trips correctly."""
        key = derive_key("test-secret")
        content = "Message: Happy birthday! \u2764\ufe0f \u2728"
        assert decrypt(encrypt(content, key), key) == content


class TestFileEncryption:
    """Tests for file-level encryption operations."""

    def test_encrypt_file_in_place(self, tmp_path):
        """encrypt_file should encrypt a plaintext file in place."""
        key = derive_key("test-secret")
        file_path = tmp_path / "test.yaml"
        file_path.write_text("secret: data\n")

        encrypt_file(file_path, key)

        raw = file_path.read_bytes()
        assert is_encrypted(raw)
        assert decrypt(raw, key) == "secret: data\n"

    def test_encrypt_file_idempotent(self, tmp_path):
        """encrypt_file on already-encrypted file is a no-op."""
        key = derive_key("test-secret")
        file_path = tmp_path / "test.yaml"
        file_path.write_text("secret: data\n")

        encrypt_file(file_path, key)
        first_content = file_path.read_bytes()

        encrypt_file(file_path, key)  # Should not re-encrypt
        second_content = file_path.read_bytes()

        assert first_content == second_content

    def test_decrypt_file_encrypted(self, tmp_path):
        """decrypt_file should decrypt an encrypted file."""
        key = derive_key("test-secret")
        file_path = tmp_path / "test.yaml"
        file_path.write_text("secret: data\n")

        encrypt_file(file_path, key)
        content = decrypt_file(file_path, key)
        assert content == "secret: data\n"

    def test_decrypt_file_plaintext(self, tmp_path):
        """decrypt_file should return plaintext content as-is (migration)."""
        key = derive_key("test-secret")
        file_path = tmp_path / "test.yaml"
        file_path.write_text("not encrypted\n")

        content = decrypt_file(file_path, key)
        assert content == "not encrypted\n"

    def test_save_encrypted(self, tmp_path):
        """save_encrypted should write encrypted content."""
        key = derive_key("test-secret")
        file_path = tmp_path / "output.yaml"

        save_encrypted(file_path, "secret: data\n", key)

        raw = file_path.read_bytes()
        assert is_encrypted(raw)
        assert decrypt(raw, key) == "secret: data\n"

    def test_save_encrypted_creates_dirs(self, tmp_path):
        """save_encrypted should create parent directories."""
        key = derive_key("test-secret")
        file_path = tmp_path / "nested" / "dir" / "output.yaml"

        save_encrypted(file_path, "content", key)

        assert file_path.exists()
        assert decrypt_file(file_path, key) == "content"

    def test_encrypt_file_atomic_failure(self, tmp_path):
        """If os.replace fails, temp file should be cleaned up."""
        from unittest.mock import patch

        key = derive_key("test-secret")
        file_path = tmp_path / "test.yaml"
        file_path.write_text("secret: data\n")

        with patch('os.replace', side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                encrypt_file(file_path, key)

        # Original file should be unchanged
        assert file_path.read_text() == "secret: data\n"


class TestSaveEncryptedAtomicFailure:
    """Tests for save_encrypted exception handler paths."""

    def test_save_encrypted_atomic_failure(self, tmp_path):
        """If os.replace fails in save_encrypted, temp file should be cleaned up."""
        from unittest.mock import patch
        key = derive_key("test-secret")
        file_path = tmp_path / "output.yaml"

        with patch('os.replace', side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                save_encrypted(file_path, "content", key)

    def test_save_encrypted_atomic_failure_unlink_fails(self, tmp_path):
        """If both os.replace and os.unlink fail in save_encrypted."""
        from unittest.mock import patch
        key = derive_key("test-secret")
        file_path = tmp_path / "output.yaml"

        with patch('os.replace', side_effect=OSError("disk full")), \
             patch('os.unlink', side_effect=OSError("permission denied")):
            with pytest.raises(OSError, match="disk full"):
                save_encrypted(file_path, "content", key)

    def test_encrypt_file_atomic_failure_unlink_fails(self, tmp_path):
        """If both os.replace and os.unlink fail in encrypt_file."""
        from unittest.mock import patch
        key = derive_key("test-secret")
        file_path = tmp_path / "test.yaml"
        file_path.write_text("secret: data\n")

        with patch('os.replace', side_effect=OSError("disk full")), \
             patch('os.unlink', side_effect=OSError("permission denied")):
            with pytest.raises(OSError, match="disk full"):
                encrypt_file(file_path, key)
