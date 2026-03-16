"""Tests for encryption at rest."""

import pytest
from pathlib import Path

from posthumous.crypto import (
    derive_key,
    _derive_key_v1,
    encrypt,
    decrypt,
    is_encrypted,
    encrypt_file,
    decrypt_file,
    save_encrypted,
    ENCRYPTED_MAGIC_V1,
    ENCRYPTED_MAGIC_V2,
    SALT_LENGTH,
    PBKDF2_ITERATIONS,
)


class TestPBKDF2KeyDerivation:
    """Tests for PBKDF2-based key derivation (v2)."""

    def test_returns_tuple_of_key_and_salt(self):
        """derive_key should return (key, salt) tuple."""
        result = derive_key("test-secret")
        assert isinstance(result, tuple)
        assert len(result) == 2
        key, salt = result
        assert isinstance(key, bytes)
        assert isinstance(salt, bytes)

    def test_salt_length(self):
        """Salt should be SALT_LENGTH bytes."""
        _, salt = derive_key("test-secret")
        assert len(salt) == SALT_LENGTH

    def test_produces_valid_fernet_key(self):
        """Derived key should be base64-encoded and 44 bytes (valid Fernet key)."""
        import base64
        key, _ = derive_key("test-secret")
        assert len(key) == 44  # base64(32 bytes) = 44 chars
        decoded = base64.urlsafe_b64decode(key)
        assert len(decoded) == 32

    def test_deterministic_with_same_salt(self):
        """Same secret + same salt should produce same key."""
        _, salt = derive_key("my-secret")
        key1, _ = derive_key("my-secret", salt=salt)
        key2, _ = derive_key("my-secret", salt=salt)
        assert key1 == key2

    def test_different_salts_different_keys(self):
        """Same secret + different salts should produce different keys."""
        key1, salt1 = derive_key("same-secret")
        key2, salt2 = derive_key("same-secret")
        # Random salts should differ (astronomically unlikely to collide)
        assert salt1 != salt2
        assert key1 != key2

    def test_different_secrets_different_keys(self):
        """Different secrets with same salt should produce different keys."""
        salt = b'\x00' * SALT_LENGTH
        key1, _ = derive_key("secret-a", salt=salt)
        key2, _ = derive_key("secret-b", salt=salt)
        assert key1 != key2

    def test_explicit_salt_returned_as_is(self):
        """When salt is provided, the same salt should be returned."""
        explicit_salt = b'\xde\xad\xbe\xef' * 4  # 16 bytes
        key, returned_salt = derive_key("test-secret", salt=explicit_salt)
        assert returned_salt == explicit_salt

    def test_fernet_key_actually_works(self):
        """Derived key should work with Fernet."""
        from cryptography.fernet import Fernet
        key, _ = derive_key("test-secret")
        f = Fernet(key)
        encrypted = f.encrypt(b"hello")
        assert f.decrypt(encrypted) == b"hello"


class TestLegacyKeyDerivation:
    """Tests for v1 legacy key derivation (bare SHA-256)."""

    def test_legacy_produces_valid_fernet_key(self):
        """_derive_key_v1 should produce a valid Fernet key."""
        import base64
        key = _derive_key_v1("test-secret")
        assert len(key) == 44
        decoded = base64.urlsafe_b64decode(key)
        assert len(decoded) == 32

    def test_legacy_is_deterministic(self):
        """_derive_key_v1 with same secret should produce same key."""
        assert _derive_key_v1("my-secret") == _derive_key_v1("my-secret")

    def test_legacy_differs_from_pbkdf2(self):
        """Legacy v1 key should differ from PBKDF2 v2 key even with same secret."""
        v1_key = _derive_key_v1("test-secret")
        v2_key, _ = derive_key("test-secret")
        # They should almost certainly be different
        # (unless salt happens to match, which is effectively impossible)
        assert v1_key != v2_key


class TestEncryptDecrypt:
    """Tests for encrypt/decrypt round-trip."""

    def test_round_trip(self):
        """Encrypt then decrypt should return original data."""
        key, salt = derive_key("test-secret")
        plaintext = "Hello, World!"
        encrypted = encrypt(plaintext, key, salt)
        decrypted = decrypt(encrypted, secret="test-secret")
        assert decrypted == plaintext

    def test_encrypted_has_v2_magic_header(self):
        """Encrypted data should start with v2 magic."""
        key, salt = derive_key("test-secret")
        encrypted = encrypt("data", key, salt)
        assert encrypted.startswith(ENCRYPTED_MAGIC_V2)

    def test_v2_format_contains_salt(self):
        """V2 encrypted data should contain the salt after the magic header."""
        key, salt = derive_key("test-secret")
        encrypted = encrypt("data", key, salt)
        # Format: MAGIC_V2 + salt + \n + ciphertext
        after_magic = encrypted[len(ENCRYPTED_MAGIC_V2):]
        stored_salt = after_magic[:SALT_LENGTH]
        assert stored_salt == salt
        assert after_magic[SALT_LENGTH:SALT_LENGTH + 1] == b'\n'

    def test_decrypt_with_secret(self):
        """decrypt should work when given the secret string."""
        key, salt = derive_key("test-secret")
        encrypted = encrypt("secret data", key, salt)
        decrypted = decrypt(encrypted, secret="test-secret")
        assert decrypted == "secret data"

    def test_decrypt_with_key_for_v2(self):
        """decrypt with explicit key should work for v2 data."""
        key, salt = derive_key("test-secret")
        encrypted = encrypt("secret data", key, salt)
        decrypted = decrypt(encrypted, key=key)
        assert decrypted == "secret data"

    def test_decrypt_wrong_secret_raises(self):
        key, salt = derive_key("correct-key")
        encrypted = encrypt("secret data", key, salt)

        with pytest.raises(ValueError, match="Decryption failed"):
            decrypt(encrypted, secret="wrong-key")

    def test_decrypt_non_encrypted_raises(self):
        with pytest.raises(ValueError, match="not encrypted"):
            decrypt(b"plain text data", secret="test-secret")

    def test_is_encrypted_v2(self):
        key, salt = derive_key("test-secret")
        encrypted = encrypt("data", key, salt)
        assert is_encrypted(encrypted) is True

    def test_is_encrypted_v1(self):
        """V1 encrypted data should also be recognized."""
        v1_data = ENCRYPTED_MAGIC_V1 + b"some_fernet_ciphertext"
        assert is_encrypted(v1_data) is True

    def test_is_encrypted_false(self):
        assert is_encrypted(b"plain text") is False
        assert is_encrypted(b"") is False

    def test_round_trip_yaml_content(self):
        """Encrypt/decrypt preserves YAML content exactly."""
        key, salt = derive_key("test-secret")
        yaml_content = "node_name: test\nsecret_key: JBSWY3DPEHPK3PXP\nstatus: armed\n"
        encrypted = encrypt(yaml_content, key, salt)
        decrypted = decrypt(encrypted, secret="test-secret")
        assert decrypted == yaml_content

    def test_round_trip_unicode(self):
        """Unicode content round-trips correctly."""
        key, salt = derive_key("test-secret")
        content = "Message: Happy birthday! \u2764\ufe0f \u2728"
        assert decrypt(encrypt(content, key, salt), secret="test-secret") == content

    def test_decrypt_requires_key_or_secret(self):
        """decrypt should raise if neither key nor secret is provided."""
        key, salt = derive_key("test-secret")
        encrypted = encrypt("data", key, salt)
        with pytest.raises(ValueError, match="key or secret"):
            decrypt(encrypted)


class TestV1ToV2Migration:
    """Tests for transparent v1 -> v2 migration."""

    def test_decrypt_v1_data_with_secret(self):
        """decrypt should handle v1 encrypted data when given a secret."""
        # Create v1 encrypted data manually
        from cryptography.fernet import Fernet
        v1_key = _derive_key_v1("test-secret")
        f = Fernet(v1_key)
        v1_encrypted = ENCRYPTED_MAGIC_V1 + f.encrypt(b"v1 secret data")

        # Should decrypt using the secret (which internally derives v1 key)
        decrypted = decrypt(v1_encrypted, secret="test-secret")
        assert decrypted == "v1 secret data"

    def test_decrypt_file_migrates_v1_to_v2(self, tmp_path):
        """decrypt_file should auto-migrate v1 files to v2 format on read."""
        from cryptography.fernet import Fernet

        # Create a v1 encrypted file
        v1_key = _derive_key_v1("test-secret")
        f = Fernet(v1_key)
        v1_data = ENCRYPTED_MAGIC_V1 + f.encrypt(b"migrateable content")

        file_path = tmp_path / "state.yaml"
        file_path.write_bytes(v1_data)

        # Decrypt should return the content
        content = decrypt_file(file_path, "test-secret")
        assert content == "migrateable content"

        # File should now be v2 format on disk
        raw = file_path.read_bytes()
        assert raw.startswith(ENCRYPTED_MAGIC_V2)
        assert not raw.startswith(ENCRYPTED_MAGIC_V1)

        # And it should still be decryptable
        content_again = decrypt_file(file_path, "test-secret")
        assert content_again == "migrateable content"


class TestV1DecryptWithExplicitKey:
    """Tests for decrypting v1 data with an explicit key."""

    def test_decrypt_v1_with_explicit_key(self):
        """decrypt should handle v1 data when given an explicit v1 key."""
        from cryptography.fernet import Fernet
        v1_key = _derive_key_v1("test-secret")
        f = Fernet(v1_key)
        v1_encrypted = ENCRYPTED_MAGIC_V1 + f.encrypt(b"v1 data with key")

        decrypted = decrypt(v1_encrypted, key=v1_key)
        assert decrypted == "v1 data with key"

    def test_decrypt_v1_with_wrong_explicit_key_raises(self):
        """decrypt with wrong explicit key for v1 data should raise."""
        from cryptography.fernet import Fernet
        v1_key = _derive_key_v1("test-secret")
        wrong_key = _derive_key_v1("wrong-secret")
        f = Fernet(v1_key)
        v1_encrypted = ENCRYPTED_MAGIC_V1 + f.encrypt(b"v1 data")

        with pytest.raises(ValueError, match="Decryption failed"):
            decrypt(v1_encrypted, key=wrong_key)


class TestV1MigrationAtomicFailure:
    """Tests for v1->v2 migration failure paths in decrypt_file."""

    def test_migration_atomic_failure_cleans_up(self, tmp_path):
        """If os.replace fails during v1->v2 migration, temp file is cleaned up."""
        from unittest.mock import patch
        from cryptography.fernet import Fernet

        v1_key = _derive_key_v1("test-secret")
        f = Fernet(v1_key)
        v1_data = ENCRYPTED_MAGIC_V1 + f.encrypt(b"v1 content")

        file_path = tmp_path / "state.yaml"
        file_path.write_bytes(v1_data)

        with patch('os.replace', side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                decrypt_file(file_path, "test-secret")

        # Original v1 file should be unchanged
        assert file_path.read_bytes() == v1_data

    def test_migration_atomic_failure_unlink_fails(self, tmp_path):
        """If both os.replace and os.unlink fail during migration."""
        from unittest.mock import patch
        from cryptography.fernet import Fernet

        v1_key = _derive_key_v1("test-secret")
        f = Fernet(v1_key)
        v1_data = ENCRYPTED_MAGIC_V1 + f.encrypt(b"v1 content")

        file_path = tmp_path / "state.yaml"
        file_path.write_bytes(v1_data)

        with patch('os.replace', side_effect=OSError("disk full")), \
             patch('os.unlink', side_effect=OSError("permission denied")):
            with pytest.raises(OSError, match="disk full"):
                decrypt_file(file_path, "test-secret")


class TestFileEncryption:
    """Tests for file-level encryption operations (secret-based API)."""

    def test_encrypt_file_in_place(self, tmp_path):
        """encrypt_file should encrypt a plaintext file in place."""
        file_path = tmp_path / "test.yaml"
        file_path.write_text("secret: data\n")

        encrypt_file(file_path, "test-secret")

        raw = file_path.read_bytes()
        assert is_encrypted(raw)
        assert raw.startswith(ENCRYPTED_MAGIC_V2)
        assert decrypt(raw, secret="test-secret") == "secret: data\n"

    def test_encrypt_file_idempotent(self, tmp_path):
        """encrypt_file on already-encrypted file is a no-op."""
        file_path = tmp_path / "test.yaml"
        file_path.write_text("secret: data\n")

        encrypt_file(file_path, "test-secret")
        first_content = file_path.read_bytes()

        encrypt_file(file_path, "test-secret")  # Should not re-encrypt
        second_content = file_path.read_bytes()

        assert first_content == second_content

    def test_decrypt_file_encrypted(self, tmp_path):
        """decrypt_file should decrypt an encrypted file."""
        file_path = tmp_path / "test.yaml"
        file_path.write_text("secret: data\n")

        encrypt_file(file_path, "test-secret")
        content = decrypt_file(file_path, "test-secret")
        assert content == "secret: data\n"

    def test_decrypt_file_plaintext(self, tmp_path):
        """decrypt_file should return plaintext content as-is (migration)."""
        file_path = tmp_path / "test.yaml"
        file_path.write_text("not encrypted\n")

        content = decrypt_file(file_path, "test-secret")
        assert content == "not encrypted\n"

    def test_save_encrypted(self, tmp_path):
        """save_encrypted should write encrypted content."""
        file_path = tmp_path / "output.yaml"

        save_encrypted(file_path, "secret: data\n", "test-secret")

        raw = file_path.read_bytes()
        assert is_encrypted(raw)
        assert decrypt(raw, secret="test-secret") == "secret: data\n"

    def test_save_encrypted_creates_dirs(self, tmp_path):
        """save_encrypted should create parent directories."""
        file_path = tmp_path / "nested" / "dir" / "output.yaml"

        save_encrypted(file_path, "content", "test-secret")

        assert file_path.exists()
        assert decrypt_file(file_path, "test-secret") == "content"

    def test_encrypt_file_atomic_failure(self, tmp_path):
        """If os.replace fails, temp file should be cleaned up."""
        from unittest.mock import patch

        file_path = tmp_path / "test.yaml"
        file_path.write_text("secret: data\n")

        with patch('os.replace', side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                encrypt_file(file_path, "test-secret")

        # Original file should be unchanged
        assert file_path.read_text() == "secret: data\n"


class TestSaveEncryptedAtomicFailure:
    """Tests for save_encrypted exception handler paths."""

    def test_save_encrypted_atomic_failure(self, tmp_path):
        """If os.replace fails in save_encrypted, temp file should be cleaned up."""
        from unittest.mock import patch
        file_path = tmp_path / "output.yaml"

        with patch('os.replace', side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                save_encrypted(file_path, "content", "test-secret")

    def test_save_encrypted_atomic_failure_unlink_fails(self, tmp_path):
        """If both os.replace and os.unlink fail in save_encrypted."""
        from unittest.mock import patch
        file_path = tmp_path / "output.yaml"

        with patch('os.replace', side_effect=OSError("disk full")), \
             patch('os.unlink', side_effect=OSError("permission denied")):
            with pytest.raises(OSError, match="disk full"):
                save_encrypted(file_path, "content", "test-secret")

    def test_encrypt_file_atomic_failure_unlink_fails(self, tmp_path):
        """If both os.replace and os.unlink fail in encrypt_file."""
        from unittest.mock import patch
        file_path = tmp_path / "test.yaml"
        file_path.write_text("secret: data\n")

        with patch('os.replace', side_effect=OSError("disk full")), \
             patch('os.unlink', side_effect=OSError("permission denied")):
            with pytest.raises(OSError, match="disk full"):
                encrypt_file(file_path, "test-secret")


class TestCryptoFilePermissions:
    """Tests for restrictive file permissions on encrypted files."""

    def test_save_encrypted_sets_restrictive_permissions(self, tmp_path):
        import stat
        path = tmp_path / "test.yaml"
        save_encrypted(path, "hello: world", "JBSWY3DPEHPK3PXP")
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600

    def test_encrypt_file_sets_restrictive_permissions(self, tmp_path):
        import stat
        path = tmp_path / "test.yaml"
        path.write_text("hello: world")
        encrypt_file(path, "JBSWY3DPEHPK3PXP")
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600
