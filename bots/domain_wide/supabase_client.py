"""
Supabase client for meeting data sync.

IMPORTANT: This client is UPSERT-ONLY. It will never delete data from Supabase.
Supabase serves as a persistent mirror that survives container refreshes.

Tables used:
- meetings: Core meeting data with transcripts
- meeting_insights: Summary and action items
"""
import os
import logging
from django.conf import settings

logger = logging.getLogger(__name__)

_supabase_client = None


def get_supabase_client():
    """Get or create Supabase client singleton."""
    global _supabase_client

    if _supabase_client is not None:
        return _supabase_client

    try:
        from supabase import create_client
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


def upsert_meeting(meeting_data: dict) -> dict | None:
    """
    Insert or update a meeting record. NEVER deletes.

    Uses attendee_bot_id as the conflict key for upsert.
    Safe to call multiple times - will update existing record.
    """
    client = get_supabase_client()
    if not client:
        logger.error("Supabase client not available - check SUPABASE_URL and SUPABASE_SERVICE_KEY")
        return None

    try:
        result = client.table('meetings').upsert(
            meeting_data,
            on_conflict='attendee_bot_id'
        ).execute()
        return result.data[0] if result.data else None
    except Exception as e:
        logger.exception(f"Failed to upsert meeting: {e}")
        return None


def upsert_meeting_insights(meeting_id: str, summary: str = None, action_items: list = None) -> dict | None:
    """
    Insert or update meeting insights. NEVER deletes.

    Uses meeting_id as the conflict key for upsert.
    """
    from datetime import datetime

    client = get_supabase_client()
    if not client:
        logger.error("Supabase client not available for insights upsert")
        return None

    insights_data = {'meeting_id': meeting_id}
    if summary is not None:
        insights_data['summary'] = summary
    if action_items is not None:
        # Store with extraction timestamp (matches edge function format)
        insights_data['action_items'] = {
            'items': action_items,
            'extracted_at': datetime.utcnow().isoformat()
        }

    try:
        result = client.table('meeting_insights').upsert(
            insights_data,
            on_conflict='meeting_id'
        ).execute()
        logger.info(f"Upserted insights for meeting {meeting_id}")
        return result.data[0] if result.data else None
    except Exception as e:
        logger.exception(f"Failed to upsert meeting insights for {meeting_id}: {e}")
        return None
