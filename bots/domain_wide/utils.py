"""
Utility functions for domain-wide integration.

Includes:
- Transcript access token generation/verification
- Encryption utilities for OAuth tokens
"""
import os
import json
import hmac
import hashlib
import base64
import time
import logging
from typing import Optional
from django.conf import settings
from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)


# =============================================================================
# Transcript Access Tokens
# =============================================================================

def get_transcript_token_secret() -> str:
    """Get the secret key for transcript tokens."""
    return (
        getattr(settings, 'TRANSCRIPT_TOKEN_SECRET', None)
        or os.getenv('TRANSCRIPT_TOKEN_SECRET')
        or os.getenv('WEBHOOK_SECRET')  # Fallback to webhook secret
        or 'insecure-default-key'
    )


def create_transcript_token(meeting_id: str, email: str, expires_days: int = 30) -> str:
    """
    Create a signed token for transcript access.

    Format: base64(payload).signature
    Payload: {"meetingId": "...", "email": "...", "exp": timestamp}
    """
    secret = get_transcript_token_secret()
    exp = int(time.time() * 1000) + (expires_days * 24 * 60 * 60 * 1000)

    payload = json.dumps({
        'meetingId': meeting_id,
        'email': email.lower(),
        'exp': exp
    })

    signature = hmac.new(
        secret.encode(),
        payload.encode(),
        hashlib.sha256
    ).digest()

    payload_b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip('=')
    sig_b64 = base64.urlsafe_b64encode(signature).decode().rstrip('=')

    return f"{payload_b64}.{sig_b64}"


def verify_transcript_token(token: str) -> Optional[dict]:
    """
    Verify a transcript access token.

    Returns payload dict if valid, None if invalid/expired.
    """
    try:
        parts = token.split('.')
        if len(parts) != 2:
            return None

        payload_b64, sig_b64 = parts
        secret = get_transcript_token_secret()

        # Decode payload (add padding if needed)
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += '=' * padding
        payload = base64.urlsafe_b64decode(payload_b64).decode()

        # Verify signature
        expected_sig = hmac.new(
            secret.encode(),
            payload.encode(),
            hashlib.sha256
        ).digest()

        # Decode provided signature
        padding = 4 - len(sig_b64) % 4
        if padding != 4:
            sig_b64 += '=' * padding
        provided_sig = base64.urlsafe_b64decode(sig_b64)

        if not hmac.compare_digest(expected_sig, provided_sig):
            logger.warning("Transcript token signature mismatch")
            return None

        # Parse and check expiry
        data = json.loads(payload)
        if data.get('exp', 0) < int(time.time() * 1000):
            logger.warning("Transcript token expired")
            return None

        return data

    except Exception as e:
        logger.warning(f"Failed to verify transcript token: {e}")
        return None


# =============================================================================
# OAuth Token Encryption
# =============================================================================

def get_encryption_key() -> bytes:
    """Get the Fernet encryption key for OAuth tokens."""
    key = (
        getattr(settings, 'CREDENTIALS_ENCRYPTION_KEY', None)
        or os.getenv('CREDENTIALS_ENCRYPTION_KEY')
    )

    if not key:
        raise ValueError("CREDENTIALS_ENCRYPTION_KEY not configured")

    # If key is not already base64, encode it
    if len(key) == 32:
        key = base64.urlsafe_b64encode(key.encode())
    elif not key.endswith('='):
        # Assume it's already a Fernet key
        key = key.encode()
    else:
        key = key.encode()

    return key


def encrypt_token(token: str) -> str:
    """Encrypt an OAuth token for storage."""
    if not token:
        return ''

    try:
        key = get_encryption_key()
        f = Fernet(key)
        return f.encrypt(token.encode()).decode()
    except Exception as e:
        logger.exception(f"Failed to encrypt token: {e}")
        raise


def decrypt_token(encrypted: str) -> str:
    """Decrypt an OAuth token from storage."""
    if not encrypted:
        return ''

    try:
        key = get_encryption_key()
        f = Fernet(key)
        return f.decrypt(encrypted.encode()).decode()
    except Exception as e:
        logger.exception(f"Failed to decrypt token: {e}")
        raise


# =============================================================================
# Validation Helpers
# =============================================================================

def is_valid_uuid(value: str) -> bool:
    """Check if a string is a valid UUID format."""
    import re
    if not isinstance(value, str):
        return False
    uuid_pattern = r'^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
    return bool(re.match(uuid_pattern, value, re.IGNORECASE))


def is_valid_email(value: str) -> bool:
    """Check if a string is a valid email format."""
    import re
    if not isinstance(value, str):
        return False
    email_pattern = r'^[^\s@]+@[^\s@]+\.[^\s@]+$'
    return bool(re.match(email_pattern, value)) and len(value) <= 254
