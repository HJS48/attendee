"""
Shared transcript utilities for building transcript data from Postgres.
Used by both process_meeting_insights and sync_meeting_to_supabase.
"""
import logging

logger = logging.getLogger(__name__)


def build_transcript_segments(recording):
    """
    Build transcript segments from Postgres Utterances.

    Returns list of dicts: {speaker, timestamp_ms, duration_ms, text}
    """
    utterances = recording.utterances.select_related('participant').order_by('timestamp_ms')
    segments = []
    for u in utterances:
        if not u.transcription:
            continue

        if isinstance(u.transcription, dict):
            text = u.transcription.get('text', '') or u.transcription.get('transcript', '')
        elif isinstance(u.transcription, str):
            text = u.transcription
        else:
            text = ''

        if text:
            segments.append({
                'speaker': u.participant.full_name if u.participant else 'Unknown',
                'timestamp_ms': u.timestamp_ms,
                'duration_ms': u.duration_ms,
                'text': text,
            })

    return segments


def _extract_organizer_email(event):
    """
    Extract organizer email from CalendarEvent.raw JSON.

    The event model doesn't have an organizer_email field — the old code used
    getattr(event, 'organizer_email') which always returned None.
    """
    if not event or not event.raw:
        return None
    try:
        return event.raw.get('organizer', {}).get('email')
    except (AttributeError, TypeError):
        return None


def get_meeting_metadata(bot):
    """
    Build meeting metadata dict from Postgres models.

    Returns dict with: title, organizer_email, attendees, participants,
    started_at, ended_at, duration_seconds, recording_url
    """
    from bots.models import Recording, RecordingTranscriptionStates, Participant

    event = bot.calendar_event
    recording = Recording.objects.filter(
        bot=bot,
        transcription_state=RecordingTranscriptionStates.COMPLETE
    ).order_by('-created_at').first()

    metadata = {
        'title': event.name if event and event.name else '',
        'organizer_email': _extract_organizer_email(event),
        'attendees': [],
        'participants': [],
        'started_at': None,
        'ended_at': None,
        'duration_seconds': None,
        'recording_url': None,
        'meeting_url': bot.meeting_url,
    }

    # Event attendees
    if event and event.attendees and isinstance(event.attendees, list):
        metadata['attendees'] = [
            a for a in event.attendees
            if isinstance(a, dict) and a.get('email')
        ]

    # Recording timestamps
    if recording:
        metadata['started_at'] = recording.started_at.isoformat() if recording.started_at else None
        metadata['ended_at'] = recording.completed_at.isoformat() if recording.completed_at else None
        if recording.started_at and recording.completed_at:
            metadata['duration_seconds'] = int(
                (recording.completed_at - recording.started_at).total_seconds()
            )
        if recording.file:
            try:
                metadata['recording_url'] = recording.file.url
            except Exception:
                pass

    # Participants from utterances
    if recording:
        participants = Participant.objects.filter(
            utterances__recording=recording
        ).distinct()
        metadata['participants'] = [
            {'name': p.full_name, 'participant_id': str(p.id)}
            for p in participants
        ]

    return metadata
