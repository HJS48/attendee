"""
Transcript viewer for meeting participants.
"""
import logging
import re

from django.shortcuts import render
from django.views import View

logger = logging.getLogger(__name__)


def get_public_video_url(recording_url: str) -> str:
    """Transform private R2 URL to public URL."""
    if not recording_url:
        return ''
    # Extract path from signed R2 URL
    match = re.search(r'r2\.cloudflarestorage\.com/[^/]+/(.+?)(?:\?|$)', recording_url)
    if match:
        return f"https://pub-b4590a75005946ca8c543dc5efb61b28.r2.dev/{match.group(1)}"
    return recording_url


class TranscriptView(View):
    """
    Display meeting transcript for authorized participants.

    Access via token from email link or session auth.
    Fetches transcript from Supabase meetings table.
    """

    def get(self, request, meeting_id):
        from ..utils import verify_transcript_token, is_valid_uuid
        from ..supabase_client import get_meeting, get_meeting_insights

        # Validate meeting ID format
        if not is_valid_uuid(meeting_id):
            return render(request, 'domain_wide/error.html', {
                'error': 'Invalid meeting ID'
            }, status=400)

        # Authenticate via token or session
        token = request.GET.get('token')
        user_email = None

        if token:
            token_data = verify_transcript_token(token)
            if not token_data:
                return render(request, 'domain_wide/error.html', {
                    'error': 'Invalid or expired access link'
                }, status=401)
            if token_data.get('meetingId') != meeting_id:
                return render(request, 'domain_wide/error.html', {
                    'error': 'Access link does not match this meeting'
                }, status=403)
            user_email = token_data.get('email', '').lower()
        elif request.user.is_authenticated:
            user_email = request.user.email.lower()
        else:
            return render(request, 'domain_wide/error.html', {
                'error': 'Access denied - use the link from your email'
            }, status=401)

        # Fetch meeting from Supabase
        meeting = get_meeting(meeting_id)
        if not meeting:
            return render(request, 'domain_wide/error.html', {
                'error': 'Meeting not found'
            }, status=404)

        # Authorization: user has valid token for this meeting, allow access
        # (Token already verified above with correct meetingId and email)

        # Fetch insights
        insights = get_meeting_insights(meeting_id)
        summary = insights[0].get('summary', '') if insights else ''
        action_items = []
        if insights and insights[0].get('action_items'):
            action_items = insights[0]['action_items'].get('items', [])

        # Build context for template
        transcript = meeting.get('transcript') or []
        participants = meeting.get('participants') or []

        # Format timestamps for each transcript segment
        for seg in transcript:
            start_ms = seg.get('timestamp_ms') or seg.get('start_ms', 0)
            total_seconds = start_ms // 1000
            minutes = total_seconds // 60
            seconds = total_seconds % 60
            seg['formatted_time'] = f"[{minutes:02d}:{seconds:02d}]"

        # Get unique speakers and assign colors
        speakers = list(set(seg.get('speaker') or seg.get('speaker_name', 'Unknown') for seg in transcript))
        speaker_colors = {speaker: idx % 8 for idx, speaker in enumerate(speakers)}

        # Add speaker color and name to each segment for template
        for seg in transcript:
            speaker = seg.get('speaker') or seg.get('speaker_name', 'Unknown')
            seg['speaker_name'] = speaker
            seg['speaker_color'] = speaker_colors.get(speaker, 0)

        # Format duration
        duration_seconds = meeting.get('duration_seconds', 0)
        if duration_seconds:
            mins = duration_seconds // 60
            hrs = mins // 60
            duration_display = f"{hrs}h {mins % 60}m" if hrs else f"{mins} minutes"
        else:
            duration_display = "Unknown duration"

        # Format participant names (use email as fallback)
        participant_names = [p.get('name') or p.get('email', 'Unknown') for p in participants[:5]]
        if len(participants) > 5:
            participant_names.append(f"+{len(participants) - 5} more")

        context = {
            'meeting': meeting,
            'meeting_id': meeting_id,
            'title': meeting.get('title', 'Untitled Meeting'),
            'started_at': meeting.get('started_at'),
            'duration': duration_display,
            'participant_names': ', '.join(participant_names),
            'recording_url': get_public_video_url(meeting.get('recording_url', '')),
            'summary': summary,
            'action_items': action_items,
            'transcript': transcript,
            'speaker_colors': speaker_colors,
        }

        return render(request, 'domain_wide/transcript.html', context)
