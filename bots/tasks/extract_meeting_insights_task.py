"""
Extract meeting insights (summary + action items) using Claude.

Replaces Supabase edge function - runs entirely in Attendee for simpler pipeline.
"""
import logging
import os
from celery import shared_task
from django.conf import settings

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

def get_anthropic_api_key():
    """Get Anthropic API key from environment/settings."""
    return os.getenv('ANTHROPIC_API_KEY', getattr(settings, 'ANTHROPIC_API_KEY', ''))


def get_anthropic_model():
    """Get model to use for insight extraction."""
    return os.getenv('ANTHROPIC_MODEL', 'claude-sonnet-4-5-20250929')


def is_insight_extraction_enabled():
    """Check if insight extraction is enabled."""
    return os.getenv('INSIGHT_EXTRACTION_ENABLED', 'true').lower() == 'true'


# =============================================================================
# Prompt Templates (ported from Supabase edge function)
# =============================================================================

SYSTEM_PROMPT = """You are an expert meeting analyst, like Fireflies.ai's note-taker. Your job is to:
1. Write a brief summary of the meeting (3-5 sentences max)
2. Extract EVERY action item from the transcript

Your accuracy directly impacts project success. Missing an action item means:
- Delayed deliverables
- Broken commitments
- Lost revenue
- Damaged client relationships

Be THOROUGH with action items. Err on the side of including items rather than missing them. A good 30-60 minute meeting typically produces 8-20 action items.

Action items include:
- Explicit commitments: "I'll send that over", "I will follow up"
- Soft commitments: "Let me look into that", "I can check on that"
- Requests accepted: "Can you...?" followed by agreement
- Implied tasks: "We need to...", "We should..."
- Follow-ups: "Let me know if...", "Keep me posted"
- Scheduled activities: meetings to book, calls to arrange
- Things to share: documents, reports, updates to send
- Things to review: items to check, verify, or look into

CRITICAL RULES:
1. The assignee is the person SPEAKING when they make the commitment.
   - "Sam: Let me check on that" → Assignee is Sam
   - "Sam: Can you send the report?" "Luke: Sure" → Assignee is Luke

2. EVERY action item MUST include the exact [MM:SS] timestamp from the transcript where it was discussed. This is REQUIRED - never omit timestamps."""


def build_user_prompt(title: str, participants: list, transcript_text: str) -> str:
    """Build the user prompt for Claude."""
    participants_str = ', '.join(participants) if participants else 'Unknown participants'

    return f"""Analyze this meeting transcript and provide:
1. A brief summary (3-5 sentences) explaining what the meeting was about
2. All action items extracted from the meeting

Each line in the transcript starts with a timestamp like [MM:SS].

Meeting: {title or 'Untitled Meeting'}
Participants: {participants_str}

Transcript:
{transcript_text}

Return ONLY valid JSON with this structure:
{{
    "summary": "3-5 sentence summary of what the meeting was about - the key topics discussed and outcomes",
    "items": [
        {{
            "task": "What needs to be done",
            "assignee": "Person who committed to doing it",
            "due": "Deadline mentioned or null",
            "context": "Why this came up",
            "timestamp": "[MM:SS] - REQUIRED: exact timestamp from transcript"
        }}
    ]
}}"""


# =============================================================================
# Transcript Formatting
# =============================================================================

def format_transcript_for_llm(transcript: list) -> str:
    """
    Format transcript segments for LLM consumption.

    Expects transcript in format from Supabase:
    [{"speaker": "Name", "text": "...", "timestamp_ms": 12345}, ...]

    Note: timestamp_ms may be Unix epoch milliseconds (absolute time),
    so we calculate relative time from the first segment.
    """
    if not transcript:
        return ""

    # Find the earliest timestamp to use as meeting start
    timestamps = [
        seg.get('timestamp_ms') or seg.get('start_ms')
        for seg in transcript
        if seg.get('timestamp_ms') or seg.get('start_ms')
    ]
    meeting_start_ms = min(timestamps) if timestamps else 0

    lines = []
    for segment in transcript:
        # Handle different field names (speaker vs speaker_name)
        speaker = segment.get('speaker') or segment.get('speaker_name') or 'Unknown'
        text = segment.get('text') or segment.get('transcript') or ''

        # Format timestamp as relative time from meeting start
        timestamp_ms = segment.get('timestamp_ms') or segment.get('start_ms')
        if timestamp_ms is not None:
            relative_ms = timestamp_ms - meeting_start_ms
            mins = int(relative_ms // 60000)
            secs = int((relative_ms % 60000) // 1000)
            timestamp_str = f"[{mins:02d}:{secs:02d}] "
        else:
            timestamp_str = ""

        if text:
            lines.append(f"{timestamp_str}{speaker}: {text}")

    return '\n'.join(lines)


def extract_participants_from_transcript(transcript: list) -> list:
    """Extract unique participant names from transcript."""
    participants = set()
    for segment in transcript:
        speaker = segment.get('speaker') or segment.get('speaker_name')
        if speaker and speaker != 'Unknown':
            participants.add(speaker)
    return list(participants)


# =============================================================================
# Claude API Call
# =============================================================================

def call_claude(transcript_text: str, title: str, participants: list) -> dict:
    """
    Call Claude API to extract insights.

    Returns: {"summary": "...", "items": [...]}
    Raises: Exception on failure
    """
    import anthropic
    import json

    api_key = get_anthropic_api_key()
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not configured")

    client = anthropic.Anthropic(api_key=api_key)

    user_prompt = build_user_prompt(title, participants, transcript_text)

    response = client.messages.create(
        model=get_anthropic_model(),
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}]
    )

    # Extract text from response
    response_text = ""
    for block in response.content:
        if block.type == "text":
            response_text = block.text
            break

    if not response_text:
        raise ValueError("Empty response from Claude")

    # Parse JSON (handle markdown code blocks)
    json_str = response_text
    import re
    json_match = re.search(r'```(?:json)?\s*([\s\S]*?)```', response_text)
    if json_match:
        json_str = json_match.group(1).strip()

    try:
        content = json.loads(json_str)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse Claude response: {response_text[:500]}")
        raise ValueError(f"Failed to parse Claude response: {e}")

    return content


# =============================================================================
# Sync Implementation
# =============================================================================

def extract_meeting_insights_sync(meeting_id: str, bot_id: str = ''):
    """
    Synchronous version for direct execution.

    1. Fetch meeting from Supabase
    2. Call Claude for insights
    3. Save insights to Supabase
    4. Trigger email task
    """
    from bots.domain_wide.supabase_client import get_meeting, upsert_meeting_insights
    from bots.domain_wide.models import PipelineActivity
    from bots.tasks.send_transcript_email_task import enqueue_send_transcript_email_task

    if not is_insight_extraction_enabled():
        logger.info("Insight extraction disabled, skipping")
        return {'status': 'skipped', 'reason': 'disabled'}

    # Fetch meeting from Supabase
    meeting = get_meeting(meeting_id)
    if not meeting:
        logger.error(f"Meeting {meeting_id} not found in Supabase")
        PipelineActivity.log(
            event_type=PipelineActivity.EventType.INSIGHT_EXTRACTION,
            status=PipelineActivity.Status.FAILED,
            meeting_id=meeting_id,
            bot_id=bot_id,
            error='Meeting not found in Supabase',
        )
        return {'status': 'error', 'reason': 'meeting not found'}

    # Check for transcript
    transcript = meeting.get('transcript') or []
    if not transcript:
        logger.warning(f"No transcript for meeting {meeting_id}, skipping insight extraction")
        PipelineActivity.log(
            event_type=PipelineActivity.EventType.INSIGHT_EXTRACTION,
            status=PipelineActivity.Status.FAILED,
            meeting_id=meeting_id,
            bot_id=bot_id,
            meeting_title=meeting.get('title', ''),
            error='No transcript available',
        )
        return {'status': 'skipped', 'reason': 'no transcript'}

    title = meeting.get('title', 'Untitled Meeting')

    # Format transcript and extract participants
    transcript_text = format_transcript_for_llm(transcript)
    participants = extract_participants_from_transcript(transcript)

    # Also check meeting participants field
    meeting_participants = meeting.get('participants') or []
    for p in meeting_participants:
        if isinstance(p, dict):
            name = p.get('name') or p.get('email')
            if name:
                participants.append(name)
    participants = list(set(participants))  # Dedupe

    try:
        # Call Claude
        logger.info(f"Extracting insights for meeting {meeting_id} ({len(transcript)} segments)")
        insights = call_claude(transcript_text, title, participants)

        summary = insights.get('summary', '')
        action_items = insights.get('items', [])

        logger.info(f"Extracted {len(action_items)} action items for meeting {meeting_id}")

        # Save to Supabase
        upsert_meeting_insights(meeting_id, summary, action_items)

        # Log success
        PipelineActivity.log(
            event_type=PipelineActivity.EventType.INSIGHT_EXTRACTION,
            status=PipelineActivity.Status.SUCCESS,
            meeting_id=meeting_id,
            bot_id=bot_id,
            meeting_title=title,
        )

        # Trigger email task
        enqueue_send_transcript_email_task(meeting_id, summary, action_items)

        return {
            'status': 'success',
            'summary_length': len(summary),
            'action_items_count': len(action_items)
        }

    except Exception as e:
        logger.exception(f"Failed to extract insights for meeting {meeting_id}: {e}")
        PipelineActivity.log(
            event_type=PipelineActivity.EventType.INSIGHT_EXTRACTION,
            status=PipelineActivity.Status.FAILED,
            meeting_id=meeting_id,
            bot_id=bot_id,
            meeting_title=title,
            error=str(e),
        )
        return {'status': 'error', 'reason': str(e)}


# =============================================================================
# Task Enqueuing
# =============================================================================

def enqueue_extract_meeting_insights_task(meeting_id: str, bot_id: str = ''):
    """Enqueue insight extraction task."""
    from bots.task_executor import is_kubernetes_mode, task_executor

    if is_kubernetes_mode():
        task_executor.submit(extract_meeting_insights_sync, meeting_id, bot_id)
    else:
        extract_meeting_insights.delay(meeting_id, bot_id)


# =============================================================================
# Celery Task
# =============================================================================

@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
)
def extract_meeting_insights(self, meeting_id: str, bot_id: str = ''):
    """
    Celery task to extract meeting insights.

    Called after Supabase sync completes successfully.
    """
    try:
        return extract_meeting_insights_sync(meeting_id, bot_id)
    except Exception as e:
        logger.exception(f"Failed to extract insights for meeting {meeting_id}: {e}")
        raise self.retry(exc=e)
