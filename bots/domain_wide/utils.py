"""
Utility functions for domain-wide integration.

Includes:
- Encryption utilities for OAuth tokens
- Validation helpers
"""
import os
import base64
import logging
from django.conf import settings
from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)


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
