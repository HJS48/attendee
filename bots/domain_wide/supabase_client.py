"""
Supabase client for domain-wide integration.

Used to:
- Fetch meeting transcripts for viewer
- Sync meeting data for automations
"""
import os
import logging
from functools import lru_cache
from django.conf import settings

logger = logging.getLogger(__name__)

# Lazy import to avoid startup issues if supabase not installed
_supabase_client = None


def get_supabase_client():
    """Get or create Supabase client singleton."""
    global _supabase_client

    if _supabase_client is not None:
        return _supabase_client

    try:
        from supabase import create_client, Client
    except ImportError:
        logger.error("supabase package not installed. Run: pip install supabase")
        return None

    url = getattr(settings, 'SUPABASE_URL', None) or os.getenv('SUPABASE_URL')
    key = getattr(settings, 'SUPABASE_SERVICE_KEY', None) or os.getenv('SUPABASE_SERVICE_KEY')

    if not url or not key:
        logger.warning("SUPABASE_URL or SUPABASE_SERVICE_KEY not configured")
        return None

    try:
        _supabase_client = create_client(url, key)
        logger.info("Supabase client initialized")
        return _supabase_client
    except Exception as e:
        logger.exception(f"Failed to create Supabase client: {e}")
        return None


def get_meeting(meeting_id: str) -> dict | None:
    """Fetch a meeting by ID from Supabase."""
    client = get_supabase_client()
    if not client:
        return None

    try:
        result = client.table('meetings').select('*').eq('id', meeting_id).single().execute()
        return result.data
    except Exception as e:
        logger.exception(f"Failed to fetch meeting {meeting_id}: {e}")
        return None


def get_meeting_by_bot_id(bot_id: str) -> dict | None:
    """Fetch a meeting by Attendee bot ID."""
    client = get_supabase_client()
    if not client:
        return None

    try:
        result = client.table('meetings').select('*').eq('attendee_bot_id', bot_id).single().execute()
        return result.data
    except Exception as e:
        logger.exception(f"Failed to fetch meeting for bot {bot_id}: {e}")
        return None


def get_meeting_insights(meeting_id: str) -> list:
    """Fetch insights (summary, action items) for a meeting."""
    client = get_supabase_client()
    if not client:
        return []

    try:
        result = client.table('meeting_insights').select('*').eq('meeting_id', meeting_id).execute()
        return result.data or []
    except Exception as e:
        logger.exception(f"Failed to fetch insights for meeting {meeting_id}: {e}")
        return []


def get_attendee_emails_for_meeting(meeting_url: str) -> list:
    """Get attendee emails for a meeting URL from calendar events."""
    client = get_supabase_client()
    if not client:
        return []

    try:
        # Fetch calendar events with this meeting URL
        result = client.table('calendar_events').select('attendees').eq('meeting_url', meeting_url).execute()

        # Flatten attendees from all events
        all_attendees = []
        for event in result.data or []:
            attendees = event.get('attendees') or []
            if isinstance(attendees, list):
                all_attendees.extend(attendees)

        return all_attendees
    except Exception as e:
        logger.exception(f"Failed to fetch attendees for {meeting_url}: {e}")
        return []


def upsert_meeting(meeting_data: dict) -> dict | None:
    """Insert or update a meeting record."""
    client = get_supabase_client()
    if not client:
        return None

    try:
        result = client.table('meetings').upsert(meeting_data).execute()
        return result.data[0] if result.data else None
    except Exception as e:
        logger.exception(f"Failed to upsert meeting: {e}")
        return None
