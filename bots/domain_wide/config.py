"""Domain-wide configuration - loaded from environment."""
import os
import json
import base64
import logging

logger = logging.getLogger(__name__)


def get_pilot_users():
    """
    Get list of pilot users from environment.

    Set PILOT_USERS as comma-separated list of emails:
    PILOT_USERS=user1@example.com,user2@example.com
    """
    users = os.getenv('PILOT_USERS', '')
    return [e.strip().lower() for e in users.split(',') if e.strip()]


def get_service_account_key():
    """
    Get service account key from environment.

    Set GOOGLE_SERVICE_ACCOUNT_KEY as either:
    - Raw JSON string
    - Base64-encoded JSON string
    """
    key = os.getenv('GOOGLE_SERVICE_ACCOUNT_KEY', '')
    if not key:
        return None

    # Handle base64 encoded or raw JSON
    try:
        return json.loads(key)
    except json.JSONDecodeError:
        try:
            return json.loads(base64.b64decode(key))
        except Exception as e:
            logger.error(f"Failed to parse GOOGLE_SERVICE_ACCOUNT_KEY: {e}")
            return None
