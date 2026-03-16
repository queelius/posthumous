"""TOTP authentication for Posthumous."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pyotp

if TYPE_CHECKING:
    from posthumous.state import State


def generate_secret() -> str:
    """Generate a new TOTP secret (base32 encoded).

    Uses 160 bits of entropy (20 bytes) as recommended by RFC 4226.
    """
    return pyotp.random_base32(length=32)


def get_totp(secret: str) -> pyotp.TOTP:
    """Get a TOTP instance for the given secret."""
    return pyotp.TOTP(secret, issuer="Posthumous")


def verify_totp(secret: str, code: str, window: int = 1) -> bool:
    """Verify a TOTP code.

    Args:
        secret: The shared TOTP secret (base32 encoded)
        code: The code to verify
        window: Number of time periods to allow on either side (default 1)

    Returns:
        True if the code is valid, False otherwise
    """
    totp = get_totp(secret)
    return totp.verify(code, valid_window=window)


def get_provisioning_uri(secret: str, node_name: str) -> str:
    """Get the provisioning URI for authenticator apps."""
    totp = get_totp(secret)
    return totp.provisioning_uri(name=node_name, issuer_name="Posthumous")


def generate_qr_code(secret: str, node_name: str) -> bytes:
    """Generate a QR code image for the TOTP secret.

    Returns PNG image bytes.
    """
    try:
        import qrcode
    except ImportError:
        raise ImportError("qrcode package required for QR code generation")

    uri = get_provisioning_uri(secret, node_name)

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(uri)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")

    buffer = io.BytesIO()
    img.save(buffer, format='PNG')
    return buffer.getvalue()


def generate_qr_code_terminal(secret: str, node_name: str) -> str:
    """Generate a QR code for terminal display using Unicode blocks.

    Returns a string that can be printed to the terminal.
    """
    try:
        import qrcode
    except ImportError:
        raise ImportError("qrcode package required for QR code generation")

    uri = get_provisioning_uri(secret, node_name)

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=1,
        border=2,
    )
    qr.add_data(uri)
    qr.make(fit=True)

    # Use Unicode block characters for terminal display
    # Each character represents 2 rows (upper/lower half blocks)
    matrix = qr.get_matrix()
    lines = []

    # Process 2 rows at a time
    for y in range(0, len(matrix), 2):
        line = ""
        for x in range(len(matrix[0])):
            upper = matrix[y][x] if y < len(matrix) else False
            lower = matrix[y + 1][x] if y + 1 < len(matrix) else False

            if upper and lower:
                line += "█"  # Full block
            elif upper:
                line += "▀"  # Upper half block
            elif lower:
                line += "▄"  # Lower half block
            else:
                line += " "  # Empty

        lines.append(line)

    return "\n".join(lines)


def generate_api_token() -> str:
    """Generate a secure API token for automated check-ins.

    This is a static token that doesn't change (unlike TOTP).
    Use with caution - prefer TOTP when possible.
    """
    return secrets.token_urlsafe(32)


def verify_api_token(expected: str | None, provided: str) -> bool:
    """Verify an API token using constant-time comparison."""
    if expected is None:
        return False
    return hmac.compare_digest(expected, provided)


class AuthError(Exception):
    """Authentication error."""
    pass


class LockedOutError(AuthError):
    """Raised when authentication is locked out due to too many failures."""

    def __init__(self, until: datetime):
        self.until = until
        remaining = until - datetime.now(timezone.utc)
        minutes = int(remaining.total_seconds() // 60)
        super().__init__(f"Authentication locked out for {minutes} more minutes")


class Authenticator:
    """Handles authentication with brute force protection."""

    def __init__(
        self,
        secret: str,
        api_token: str | None = None,
        max_attempts: int = 5,
        lockout_duration: timedelta = timedelta(minutes=15),
    ):
        self.secret = secret
        self.api_token = api_token
        self.max_attempts = max_attempts
        self.lockout_duration = lockout_duration

    def verify(
        self,
        code: str | None = None,
        token: str | None = None,
        state: "State | None" = None,
        source: str = "unknown",
    ) -> bool:
        """Verify authentication via TOTP code or API token.

        Args:
            code: TOTP code (if authenticating via TOTP)
            token: API token (if authenticating via static token)
            state: State object for tracking failed attempts
            source: Identifier for the authentication source (e.g., IP address)

        Returns:
            True if authentication succeeded

        Raises:
            LockedOutError: If too many failed attempts
            AuthError: If neither code nor token provided
        """
        # Check lockout
        if state and state.is_locked_out():
            raise LockedOutError(state.lockout_until)  # type: ignore

        if code is None and token is None:
            raise AuthError("Either TOTP code or API token required")

        success = False

        if token is not None:
            success = verify_api_token(self.api_token, token)
        elif code is not None:
            success = verify_totp(self.secret, code)

        if not success and state:
            state.record_failed_attempt(source)
            state.check_and_set_lockout(self.max_attempts, self.lockout_duration)

        if success and state:
            state.failed_attempts = []
            state.lockout_until = None

        return success

    def get_current_code(self) -> str:
        """Get the current TOTP code (for testing/debugging)."""
        return get_totp(self.secret).now()


def sign_message(secret: str, message: str) -> str:
    """Sign a message with the shared secret.

    Used for peer-to-peer message authentication.
    """
    key = base64.b32decode(secret.upper())
    signature = hmac.new(key, message.encode(), hashlib.sha256).hexdigest()
    return signature


def verify_signature(secret: str, message: str, signature: str) -> bool:
    """Verify a message signature."""
    expected = sign_message(secret, message)
    return hmac.compare_digest(expected, signature)
