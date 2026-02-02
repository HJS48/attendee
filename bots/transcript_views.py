"""
Transcript viewer for meeting participants.

Token-based access - no login required. Users receive personalized links via email.
"""
import hashlib
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime

from django.conf import settings
from django.http import HttpResponse, HttpResponseForbidden, HttpResponseNotFound
from django.shortcuts import render
from django.views.decorators.http import require_GET

from bots.domain_wide.supabase_client import get_meeting, get_meeting_insights

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

from bots.transcript_config import (
    get_dev_bypass_email,
    get_internal_domain,
    get_transcript_base_url,
    format_duration,
)


def get_token_secret():
    """Get secret for signing tokens. Uses DJANGO_SECRET_KEY."""
    return getattr(settings, 'SECRET_KEY', '') or os.getenv('DJANGO_SECRET_KEY', '')


# =============================================================================
# Token Functions
# =============================================================================

def create_transcript_token(meeting_id: str, email: str) -> str:
    """
    Create a signed token for transcript access.

    Token format: base64(payload).signature
    Payload: {"meeting_id": "...", "email": "...", "exp": timestamp}
    Expiry: 30 days
    """
    import base64

    payload = {
        'meeting_id': meeting_id,
        'email': email.lower(),
        'exp': int(time.time()) + (30 * 24 * 60 * 60)  # 30 days
    }

    payload_json = json.dumps(payload, separators=(',', ':'))
    payload_b64 = base64.urlsafe_b64encode(payload_json.encode()).decode().rstrip('=')

    signature = hmac.new(
        get_token_secret().encode(),
        payload_json.encode(),
        hashlib.sha256
    ).hexdigest()[:32]  # Truncate for shorter URLs

    return f"{payload_b64}.{signature}"


def verify_transcript_token(token: str) -> dict | None:
    """
    Verify and decode a transcript access token.

    Returns: {"meeting_id": "...", "email": "..."} or None if invalid/expired
    """
    import base64

    if not token or '.' not in token:
        return None

    try:
        payload_b64, signature = token.rsplit('.', 1)

        # Restore base64 padding
        padding = 4 - (len(payload_b64) % 4)
        if padding != 4:
            payload_b64 += '=' * padding

        payload_json = base64.urlsafe_b64decode(payload_b64).decode()

        # Verify signature
        expected_sig = hmac.new(
            get_token_secret().encode(),
            payload_json.encode(),
            hashlib.sha256
        ).hexdigest()[:32]

        if not hmac.compare_digest(signature, expected_sig):
            logger.warning("Invalid token signature")
            return None

        payload = json.loads(payload_json)

        # Check expiry
        if payload.get('exp', 0) < time.time():
            logger.warning("Token expired")
            return None

        return {
            'meeting_id': payload.get('meeting_id'),
            'email': payload.get('email')
        }

    except Exception as e:
        logger.warning(f"Token verification failed: {e}")
        return None


# =============================================================================
# Helper Functions
# =============================================================================

def is_valid_uuid(s: str) -> bool:
    """Check if string is a valid UUID."""
    pattern = r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    return bool(re.match(pattern, s.lower()))


def escape_html(s: str) -> str:
    """Escape HTML special characters."""
    if not s:
        return ''
    return (str(s)
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;')
            .replace("'", '&#39;'))


def format_timestamp(ms: int) -> str:
    """Format milliseconds as [MM:SS]."""
    if ms is None:
        return '[00:00]'
    total_seconds = ms // 1000
    minutes = total_seconds // 60
    seconds = total_seconds % 60
    return f"[{minutes:02d}:{seconds:02d}]"


def format_date(date_str: str) -> str:
    """Format ISO date string for display."""
    if not date_str:
        return 'Unknown date'
    try:
        dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        return dt.strftime('%a, %b %d, %Y at %I:%M %p')
    except Exception:
        return 'Unknown date'


def get_public_video_url(recording_url: str) -> str:
    """Transform private R2 URL to public URL if needed."""
    if not recording_url:
        return ''

    # Check for R2 URL pattern
    match = re.search(r'r2\.cloudflarestorage\.com/[^/]+/(.+)$', recording_url)
    if match:
        return f"https://pub-b4590a75005946ca8c543dc5efb61b28.r2.dev/{match.group(1)}"

    return recording_url


def is_participant(email: str, meeting: dict) -> bool:
    """Check if email is a participant in the meeting."""
    if not email or not meeting:
        return False

    email_lower = email.lower()

    # Check organizer
    if meeting.get('organizer_email', '').lower() == email_lower:
        return True

    # Check participants list
    participants = meeting.get('participants') or []
    for p in participants:
        if isinstance(p, dict):
            if p.get('email', '').lower() == email_lower:
                return True
        elif isinstance(p, str):
            if p.lower() == email_lower:
                return True

    # Check attendees if available
    attendees = meeting.get('attendees') or []
    for a in attendees:
        if isinstance(a, dict):
            if a.get('email', '').lower() == email_lower:
                return True
        elif isinstance(a, str):
            if a.lower() == email_lower:
                return True

    return False


# =============================================================================
# Views
# =============================================================================

@require_GET
def transcript_view(request, meeting_id):
    """
    Render the transcript viewer page.

    Access control:
    - Requires valid token in ?token= query param
    - Token must match the meeting_id
    - User must be a participant OR be the dev bypass email
    """
    # Validate meeting ID format
    if not is_valid_uuid(meeting_id):
        return HttpResponse('Invalid meeting ID', status=400)

    # Get and verify token
    token = request.GET.get('token')
    if not token:
        return HttpResponse('Access denied - use the link from your email', status=401)

    token_data = verify_transcript_token(token)
    if not token_data:
        return HttpResponse('Invalid or expired access link', status=401)

    if token_data['meeting_id'] != meeting_id:
        return HttpResponseForbidden('Access link does not match this meeting')

    user_email = token_data['email']
    dev_bypass_email = get_dev_bypass_email()
    is_dev_bypass = dev_bypass_email and user_email.lower() == dev_bypass_email.lower()

    # Fetch meeting from Supabase
    meeting = get_meeting(meeting_id)
    logger.info(f"Meeting data for {meeting_id}: title={meeting.get('title') if meeting else 'None'}, has_transcript={bool(meeting.get('transcript')) if meeting else False}")
    if not meeting:
        return HttpResponseNotFound('Meeting not found')

    # Check participant access (skip for dev bypass)
    if not is_dev_bypass and not is_participant(user_email, meeting):
        return HttpResponseForbidden('Access denied - you are not a participant of this meeting')

    # Fetch insights
    insights = get_meeting_insights(meeting_id)
    summary = ''
    action_items = []

    if insights:
        insight = insights[0]
        summary = insight.get('summary', '')
        ai_data = insight.get('action_items', {})
        if isinstance(ai_data, dict):
            action_items = ai_data.get('items', [])
        elif isinstance(ai_data, list):
            action_items = ai_data

    # Build transcript HTML
    # Note: Supabase transcript uses 'speaker' and 'timestamp_ms', not 'speaker_name' and 'start_ms'
    transcript = meeting.get('transcript') or []
    speakers = list(set(seg.get('speaker') or seg.get('speaker_name', 'Unknown') for seg in transcript))
    speaker_colors = {speaker: idx % 8 for idx, speaker in enumerate(speakers)}

    transcript_segments = []
    for idx, seg in enumerate(transcript):
        speaker = seg.get('speaker') or seg.get('speaker_name', 'Unknown')
        start_ms = seg.get('timestamp_ms') or seg.get('start_ms', 0)
        duration_ms = seg.get('duration_ms', 0)
        end_ms = start_ms + duration_ms if duration_ms else start_ms
        transcript_segments.append({
            'speaker_name': speaker,
            'text': seg.get('text', ''),
            'start_ms': start_ms,
            'end_ms': end_ms,
            'color_class': f"speaker-color-{speaker_colors.get(speaker, 0)}",
            'timestamp': format_timestamp(start_ms),
            'index': idx,
        })

    # Build action items by assignee
    action_items_by_assignee = {}
    unassigned_items = []

    for item in action_items:
        assignee = item.get('assignee')
        if assignee:
            if assignee not in action_items_by_assignee:
                action_items_by_assignee[assignee] = []
            action_items_by_assignee[assignee].append(item)
        else:
            unassigned_items.append(item)

    # Add unassigned to the dict
    if unassigned_items:
        action_items_by_assignee['Unassigned'] = unassigned_items

    # Get participant names
    participants = meeting.get('participants') or []
    participant_names = []
    for p in participants[:5]:
        if isinstance(p, dict):
            participant_names.append(p.get('name', p.get('email', 'Unknown')))
        else:
            participant_names.append(str(p))

    if len(participants) > 5:
        participant_names.append(f"+{len(participants) - 5} more")

    context = {
        'meeting_id': meeting_id,
        'meeting_title': meeting.get('title', 'Untitled Meeting'),
        'meeting_date': format_date(meeting.get('started_at')),
        'meeting_duration': format_duration(meeting.get('duration_seconds')),
        'participant_names': ', '.join(participant_names) if participant_names else 'No participants listed',
        'video_url': get_public_video_url(meeting.get('recording_url', '')),
        'summary': summary,
        'action_items_by_assignee': action_items_by_assignee,
        'has_action_items': bool(action_items),
        'transcript_segments': transcript_segments,
    }

    logger.info(f"Rendering transcript for {meeting_id}: title={context['meeting_title']}, segments={len(transcript_segments)}")
    return render(request, 'transcript.html', context)
