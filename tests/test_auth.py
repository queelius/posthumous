"""Tests for TOTP authentication."""

import pytest
from datetime import datetime, timedelta, timezone

from posthumous.auth import (
    generate_secret,
    verify_totp,
    get_totp,
    generate_api_token,
    verify_api_token,
    sign_message,
    verify_signature,
    Authenticator,
    AuthError,
    LockedOutError,
)
from posthumous.state import State


class TestTOTP:
    """Tests for TOTP generation and verification."""

    def test_generate_secret_length(self):
        secret = generate_secret()
        assert len(secret) == 32
        # Should be valid base32
        assert secret.isalnum()

    def test_verify_current_code(self):
        secret = generate_secret()
        totp = get_totp(secret)
        current_code = totp.now()

        assert verify_totp(secret, current_code)

    def test_verify_invalid_code(self):
        secret = generate_secret()
        assert not verify_totp(secret, "000000")
        assert not verify_totp(secret, "invalid")

    def test_verify_with_window(self):
        secret = generate_secret()
        totp = get_totp(secret)

        # Current code should work
        current_code = totp.now()
        assert verify_totp(secret, current_code, window=1)

    def test_unique_secrets(self):
        secrets = [generate_secret() for _ in range(100)]
        assert len(set(secrets)) == 100


class TestAPIToken:
    """Tests for API token authentication."""

    def test_generate_token_length(self):
        token = generate_api_token()
        # URL-safe base64: ~43 chars for 32 bytes
        assert len(token) >= 40

    def test_verify_valid_token(self):
        token = generate_api_token()
        assert verify_api_token(token, token)

    def test_verify_invalid_token(self):
        token = generate_api_token()
        assert not verify_api_token(token, "wrong-token")

    def test_verify_none_expected(self):
        assert not verify_api_token(None, "any-token")


class TestSignature:
    """Tests for message signing."""

    def test_sign_verify(self):
        secret = generate_secret()
        message = "checkin:2026-01-15T12:00:00Z"

        signature = sign_message(secret, message)
        assert verify_signature(secret, message, signature)

    def test_tampered_message_fails(self):
        secret = generate_secret()
        message = "checkin:2026-01-15T12:00:00Z"

        signature = sign_message(secret, message)
        assert not verify_signature(secret, message + "x", signature)

    def test_wrong_secret_fails(self):
        secret1 = generate_secret()
        secret2 = generate_secret()
        message = "test message"

        signature = sign_message(secret1, message)
        assert not verify_signature(secret2, message, signature)


class TestAuthenticator:
    """Tests for the Authenticator class."""

    def test_verify_totp(self):
        secret = generate_secret()
        auth = Authenticator(secret)

        totp = get_totp(secret)
        code = totp.now()

        assert auth.verify(code=code)

    def test_verify_api_token(self):
        secret = generate_secret()
        token = generate_api_token()
        auth = Authenticator(secret, api_token=token)

        assert auth.verify(token=token)

    def test_verify_requires_code_or_token(self):
        auth = Authenticator(generate_secret())

        with pytest.raises(AuthError, match="required"):
            auth.verify()

    def test_failed_attempts_tracked(self):
        secret = generate_secret()
        auth = Authenticator(secret)
        state = State()

        auth.verify(code="000000", state=state)

        assert len(state.failed_attempts) == 1
        assert state.failed_attempts[0].source == "unknown"

    def test_lockout_after_max_attempts(self):
        secret = generate_secret()
        auth = Authenticator(secret, max_attempts=3, lockout_duration=timedelta(minutes=15))
        state = State()

        for _ in range(3):
            auth.verify(code="000000", state=state)

        with pytest.raises(LockedOutError):
            auth.verify(code="000000", state=state)

    def test_successful_auth_clears_failures(self):
        secret = generate_secret()
        auth = Authenticator(secret)
        state = State()
        totp = get_totp(secret)

        # Generate some failed attempts
        auth.verify(code="000000", state=state)
        auth.verify(code="000000", state=state)

        # Successful auth should work
        assert auth.verify(code=totp.now(), state=state)

        # Note: failures are cleared by state.record_checkin(), not by Authenticator
        # This tests that successful auth works even with prior failures

    def test_api_token_success_clears_failed_attempts(self):
        auth = Authenticator(secret="JBSWY3DPEHPK3PXP", api_token="valid-token")
        state = State()

        # Burn 4 attempts
        for _ in range(4):
            auth.verify(code="000000", state=state, source="test")
        assert len(state.failed_attempts) == 4

        # Succeed via API token
        result = auth.verify(token="valid-token", state=state, source="test")
        assert result is True
        assert len(state.failed_attempts) == 0
        assert state.lockout_until is None

    def test_get_current_code(self):
        secret = generate_secret()
        auth = Authenticator(secret)

        code = auth.get_current_code()
        assert len(code) == 6
        assert code.isdigit()


class TestQRCode:
    """Tests for QR code generation."""

    def test_provisioning_uri(self):
        from posthumous.auth import get_provisioning_uri

        secret = "JBSWY3DPEHPK3PXP"
        uri = get_provisioning_uri(secret, "test-node")

        assert uri.startswith("otpauth://totp/")
        assert "test-node" in uri
        assert "Posthumous" in uri
        assert secret in uri

    def test_qr_code_terminal(self):
        from posthumous.auth import generate_qr_code_terminal

        secret = "JBSWY3DPEHPK3PXP"
        qr = generate_qr_code_terminal(secret, "test-node")

        # Should be a multi-line string with block characters
        assert "\n" in qr
        assert any(c in qr for c in "█▀▄")

    def test_qr_code_image(self):
        from posthumous.auth import generate_qr_code

        secret = "JBSWY3DPEHPK3PXP"
        img_bytes = generate_qr_code(secret, "test-node")

        # Should be PNG image data
        assert img_bytes.startswith(b'\x89PNG')

    def test_qr_code_image_missing_package(self):
        """When qrcode package is not importable, should raise ImportError."""
        from posthumous.auth import generate_qr_code
        from unittest.mock import patch
        import builtins

        original_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "qrcode":
                raise ImportError("mocked")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            with pytest.raises(ImportError, match="qrcode"):
                generate_qr_code("JBSWY3DPEHPK3PXP", "test-node")

    def test_qr_code_terminal_missing_package(self):
        """When qrcode package is not importable, should raise ImportError."""
        from posthumous.auth import generate_qr_code_terminal
        from unittest.mock import patch
        import builtins

        original_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "qrcode":
                raise ImportError("mocked")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            with pytest.raises(ImportError, match="qrcode"):
                generate_qr_code_terminal("JBSWY3DPEHPK3PXP", "test-node")
