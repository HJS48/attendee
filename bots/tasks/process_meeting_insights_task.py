"""
Unified meeting insights pipeline: extract insights, save to Postgres, mirror to Supabase, send email.

Single task replaces the old 4-phase chain (create_meeting_metadata → sync_transcript →
extract_meeting_insights → send_transcript_email). All data read from Postgres.
"""
import html
import logging
import os
import re
import smtplib
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from celery import shared_task
from django.conf import settings

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration (carried over from extract_meeting_insights_task.py)
# =============================================================================

def get_anthropic_api_key():
    return os.getenv('ANTHROPIC_API_KEY', getattr(settings, 'ANTHROPIC_API_KEY', ''))


def get_anthropic_model():
    return os.getenv('ANTHROPIC_MODEL', 'claude-sonnet-4-5-20250929')


def is_insight_extraction_enabled():
    return os.getenv('INSIGHT_EXTRACTION_ENABLED', 'true').lower() == 'true'


# =============================================================================
# Claude prompt templates
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
1. ONLY assign action items to people in the Participants list. If someone else is mentioned, assign to the speaker.

2. The assignee is the person SPEAKING when they make the commitment.
   - "Sam: Let me check on that" → Assignee is Sam
   - "Sam: Can you send the report?" "Luke: Sure" → Assignee is Luke

3. EVERY action item MUST include the exact [MM:SS] timestamp from the transcript where it was discussed. This is REQUIRED - never omit timestamps."""


def build_user_prompt(title, participants, transcript_text):
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
# Transcript formatting
# =============================================================================

def format_transcript_for_llm(segments):
    """Format transcript segments (from build_transcript_segments) for Claude."""
    if not segments:
        return ""

    timestamps = [s['timestamp_ms'] for s in segments if s.get('timestamp_ms')]
    meeting_start_ms = min(timestamps) if timestamps else 0

    lines = []
    for seg in segments:
        speaker = seg.get('speaker') or 'Unknown'
        text = seg.get('text') or ''
        ts = seg.get('timestamp_ms')
        if ts is not None:
            rel = ts - meeting_start_ms
            mins = int(rel // 60000)
            secs = int((rel % 60000) // 1000)
            prefix = f"[{mins:02d}:{secs:02d}] "
        else:
            prefix = ""
        if text:
            lines.append(f"{prefix}{speaker}: {text}")
    return '\n'.join(lines)


def extract_participants_from_transcript(segments):
    """Extract unique speaker names from transcript segments."""
    return list({
        s['speaker'] for s in segments
        if s.get('speaker') and s['speaker'] != 'Unknown'
    })


# =============================================================================
# Claude API call
# =============================================================================

def call_claude(transcript_text, title, participants):
    import anthropic
    import json as json_mod

    api_key = get_anthropic_api_key()
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not configured")

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=get_anthropic_model(),
        max_tokens=16384,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_user_prompt(title, participants, transcript_text)}]
    )

    response_text = ""
    for block in response.content:
        if block.type == "text":
            response_text = block.text
            break

    if not response_text:
        raise ValueError("Empty response from Claude")

    json_str = response_text
    json_match = re.search(r'```(?:json)?\s*([\s\S]*?)```', response_text)
    if json_match:
        json_str = json_match.group(1).strip()

    try:
        return json_mod.loads(json_str)
    except json_mod.JSONDecodeError as e:
        logger.error(f"Failed to parse Claude response: {response_text[:500]}")
        raise ValueError(f"Failed to parse Claude response: {e}")


# =============================================================================
# Email helpers (adapted from send_transcript_email_task.py)
# =============================================================================

from bots.transcript_config import (
    get_internal_domain,
    get_dev_bypass_email,
    get_transcript_base_url,
    format_duration,
)


def get_smtp_config():
    return {
        'host': os.getenv('EMAIL_HOST', getattr(settings, 'EMAIL_HOST', 'smtp.resend.com')),
        'port': int(os.getenv('EMAIL_PORT', getattr(settings, 'EMAIL_PORT', 587))),
        'user': os.getenv('EMAIL_HOST_USER', getattr(settings, 'EMAIL_HOST_USER', '')),
        'password': os.getenv('EMAIL_HOST_PASSWORD', getattr(settings, 'EMAIL_HOST_PASSWORD', '')),
        'from_email': os.getenv('DEFAULT_FROM_EMAIL', getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@soarwithus.co')),
        'use_tls': os.getenv('EMAIL_USE_TLS', 'true').lower() == 'true',
    }


def get_internal_recipients(metadata):
    """
    Filter meeting metadata attendees to internal domain + dev bypass.
    Takes metadata dict from get_meeting_metadata() (Postgres-sourced).
    """
    internal_domain = get_internal_domain().lower()
    dev_bypass = get_dev_bypass_email()
    dev_bypass_lower = dev_bypass.lower() if dev_bypass else None

    recipients = set()

    # Organizer
    organizer_email = metadata.get('organizer_email', '')
    if organizer_email:
        email_lower = organizer_email.lower()
        if email_lower.endswith(f'@{internal_domain}'):
            recipients.add(organizer_email)
        elif dev_bypass_lower and email_lower == dev_bypass_lower:
            recipients.add(organizer_email)

    # Attendees (from CalendarEvent.attendees JSON)
    for a in metadata.get('attendees') or []:
        email = a.get('email', '') if isinstance(a, dict) else ''
        if email:
            email_lower = email.lower()
            if email_lower.endswith(f'@{internal_domain}'):
                recipients.add(email)
            elif dev_bypass_lower and email_lower == dev_bypass_lower:
                recipients.add(email)

    # Always include dev bypass if set
    if dev_bypass and dev_bypass not in recipients:
        recipients.add(dev_bypass)

    return list(recipients)


def build_email_html(metadata, summary, action_items, transcript_url):
    title = html.escape(metadata.get('title') or 'Untitled Meeting')
    duration = format_duration(metadata.get('duration_seconds'))

    meeting_date = 'Unknown date'
    started_at = metadata.get('started_at')
    if started_at:
        try:
            dt = datetime.fromisoformat(started_at.replace('Z', '+00:00'))
            meeting_date = dt.strftime('%B %d, %Y')
        except Exception:
            pass

    summary_html = ''
    if summary:
        summary_html = f'''
        <div style="background: #f8f9fa; border-radius: 6px; padding: 16px; margin: 16px 0;">
            <h2 style="color: #333; font-size: 16px; margin: 0 0 10px 0;">Summary</h2>
            <p style="color: #333; margin: 0; line-height: 1.5;">{html.escape(summary)}</p>
        </div>'''

    action_items_html = ''
    if action_items:
        by_assignee = {}
        unassigned = []
        for item in action_items:
            assignee = item.get('assignee')
            if assignee:
                by_assignee.setdefault(assignee, []).append(item)
            else:
                unassigned.append(item)

        action_items_html = "<h2 style='color: #333; font-size: 16px; margin: 20px 0 10px 0;'>Action Items</h2>"
        for assignee in sorted(by_assignee.keys()):
            items = by_assignee[assignee]
            action_items_html += f"<p style='font-weight: 600; color: #333; margin: 15px 0 5px 0;'>{html.escape(assignee.upper())}</p>"
            action_items_html += "<ul style='margin: 0; padding-left: 20px;'>"
            for item in items:
                task = html.escape(item.get('task', ''))
                timestamp = item.get('timestamp')
                ts_text = f" <span style='color: #888; font-family: monospace;'>{html.escape(timestamp)}</span>" if timestamp else ""
                action_items_html += f"<li style='margin: 5px 0; color: #333;'>{task}{ts_text}</li>"
            action_items_html += "</ul>"
        if unassigned:
            action_items_html += "<p style='font-weight: 600; color: #333; margin: 15px 0 5px 0;'>UNASSIGNED</p>"
            action_items_html += "<ul style='margin: 0; padding-left: 20px;'>"
            for item in unassigned:
                task = html.escape(item.get('task', ''))
                action_items_html += f"<li style='margin: 5px 0; color: #333;'>{task}</li>"
            action_items_html += "</ul>"
    else:
        action_items_html = "<p style='color: #666; font-style: italic;'>No action items identified.</p>"

    return f'''<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 0; padding: 20px; background: #f5f5f5;">
    <div style="max-width: 600px; margin: 0 auto; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1);">
        <div style="padding: 30px;">
            <h1 style="color: #1a1a1a; font-size: 22px; margin: 0 0 8px 0;">{title}</h1>
            <p style="color: #666; font-size: 14px; margin: 0;">{meeting_date} &bull; {duration}</p>
            <p style="margin: 12px 0 0 0;"><a href="{transcript_url}" style="color: #007bff; text-decoration: none; font-size: 14px;">View Full Transcript &amp; Recording &rarr;</a></p>
            {summary_html}
            <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
            {action_items_html}
            <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
            <div style="text-align: center; margin: 25px 0;">
                <a href="{transcript_url}" style="display: inline-block; padding: 12px 24px; background: #007bff; color: white; text-decoration: none; border-radius: 6px; font-weight: 500;">View Full Transcript &amp; Recording</a>
            </div>
        </div>
    </div>
</body>
</html>'''


def build_email_plain(metadata, summary, action_items, transcript_url):
    title = metadata.get('title') or 'Untitled Meeting'
    duration = format_duration(metadata.get('duration_seconds'))

    meeting_date = 'Unknown date'
    started_at = metadata.get('started_at')
    if started_at:
        try:
            dt = datetime.fromisoformat(started_at.replace('Z', '+00:00'))
            meeting_date = dt.strftime('%B %d, %Y')
        except Exception:
            pass

    text = f"{title}\n{meeting_date} - {duration}\n\n"
    text += f"View full transcript: {transcript_url}\n\n"
    if summary:
        text += f"SUMMARY\n{summary}\n\n"
    if action_items:
        text += "ACTION ITEMS\n"
        for item in action_items:
            assignee = item.get('assignee', 'Unassigned')
            task = item.get('task', '')
            text += f"- [{assignee}] {task}\n"
    return text


# =============================================================================
# Main implementation
# =============================================================================

def _process_meeting_insights_impl(bot_id):
    """
    Unified insights pipeline:
    1. Load bot + recording from Postgres
    2. Idempotency check (skip Claude if MeetingInsight exists)
    3. Build transcript, call Claude, save MeetingInsight
    4. Mirror insights to Supabase (best-effort)
    5. Send email
    """
    from bots.models import Bot, Recording, RecordingTranscriptionStates
    from bots.domain_wide.models import PipelineActivity, MeetingInsight
    from bots.domain_wide.transcript_utils import build_transcript_segments, get_meeting_metadata
    from bots.domain_wide.supabase_client import get_meeting_by_bot_id, upsert_meeting_insights
    from bots.transcript_views import create_transcript_token

    try:
        bot = Bot.objects.select_related('calendar_event').get(object_id=bot_id)
    except Bot.DoesNotExist:
        logger.error(f"Bot {bot_id} not found")
        return {'status': 'error', 'reason': 'bot not found'}

    recording = Recording.objects.filter(
        bot=bot,
        transcription_state=RecordingTranscriptionStates.COMPLETE
    ).order_by('-created_at').first()

    if not recording:
        logger.warning(f"No completed transcription for bot {bot_id}")
        return {'status': 'skipped', 'reason': 'no completed transcription'}

    metadata = get_meeting_metadata(bot)
    title = metadata.get('title', '')

    # --- Idempotency: skip Claude if insight already exists ---
    try:
        insight = recording.insight
        logger.info(f"MeetingInsight already exists for bot {bot_id}, skipping Claude")
        summary = insight.summary
        action_items = insight.action_items
    except MeetingInsight.DoesNotExist:
        insight = None

    if insight is None:
        # Build transcript and call Claude
        segments = build_transcript_segments(recording)
        if not segments:
            logger.warning(f"No transcript segments for bot {bot_id}")
            return {'status': 'skipped', 'reason': 'empty transcript'}

        if not is_insight_extraction_enabled():
            logger.info("Insight extraction disabled")
            summary = ''
            action_items = []
        else:
            transcript_text = format_transcript_for_llm(segments)
            participants = extract_participants_from_transcript(segments)

            # Also add meeting-level participants
            for p in metadata.get('participants') or []:
                name = p.get('name')
                if name:
                    participants.append(name)
            participants = list(set(participants))

            try:
                logger.info(f"Extracting insights for bot {bot_id} ({len(segments)} segments)")
                result = call_claude(transcript_text, title, participants)
                summary = result.get('summary', '')
                action_items = result.get('items', [])
                logger.info(f"Extracted {len(action_items)} action items for bot {bot_id}")
            except Exception as e:
                logger.exception(f"Claude call failed for bot {bot_id}: {e}")
                PipelineActivity.log(
                    event_type=PipelineActivity.EventType.INSIGHT_EXTRACTION,
                    status=PipelineActivity.Status.FAILED,
                    bot_id=bot_id,
                    meeting_title=title,
                    error=str(e),
                )
                raise

        # Save to Postgres (idempotent via OneToOne)
        insight = MeetingInsight.objects.create(
            recording=recording,
            bot=bot,
            summary=summary,
            action_items=action_items,
        )

    # --- Mirror to Supabase (best-effort) ---
    supabase_meeting_id = insight.supabase_meeting_id
    if not supabase_meeting_id:
        try:
            sb_meeting = get_meeting_by_bot_id(str(bot.object_id))
            if sb_meeting:
                supabase_meeting_id = sb_meeting.get('id', '')
                if supabase_meeting_id:
                    upsert_meeting_insights(supabase_meeting_id, summary, action_items)
                    insight.supabase_meeting_id = supabase_meeting_id
                    insight.save(update_fields=['supabase_meeting_id'])
        except Exception as e:
            logger.warning(f"Supabase mirror failed for bot {bot_id}: {e}")

    # Log insight extraction
    PipelineActivity.log(
        event_type=PipelineActivity.EventType.INSIGHT_EXTRACTION,
        status=PipelineActivity.Status.SUCCESS,
        bot_id=bot_id,
        meeting_title=title,
    )

    # --- Send email ---
    if not supabase_meeting_id:
        logger.info(f"No supabase_meeting_id for bot {bot_id}, skipping email (cron will retry)")
        return {
            'status': 'success',
            'insight_created': True,
            'email_sent': False,
            'reason': 'no supabase_meeting_id for transcript link',
        }

    recipients = get_internal_recipients(metadata)
    if not recipients:
        logger.info(f"No internal recipients for bot {bot_id}")
        return {'status': 'success', 'insight_created': True, 'email_sent': False}

    smtp_config = get_smtp_config()
    base_url = get_transcript_base_url()

    if not smtp_config['user'] or not smtp_config['password']:
        logger.error("SMTP credentials not configured, skipping email")
        return {'status': 'success', 'insight_created': True, 'email_sent': False}

    meeting_title = title or 'Untitled Meeting'

    try:
        if smtp_config['use_tls']:
            server = smtplib.SMTP(smtp_config['host'], smtp_config['port'])
            server.starttls()
        else:
            server = smtplib.SMTP_SSL(smtp_config['host'], smtp_config['port'])
        server.login(smtp_config['user'], smtp_config['password'])

        sent_count = 0
        for recipient in recipients:
            try:
                # Dedup
                already_sent = PipelineActivity.objects.filter(
                    event_type=PipelineActivity.EventType.EMAIL_SENT,
                    status=PipelineActivity.Status.SUCCESS,
                    bot_id=bot_id,
                    recipient=recipient,
                ).exists()
                if already_sent:
                    logger.debug(f"Skipping duplicate email to {recipient} for bot {bot_id}")
                    continue

                token = create_transcript_token(supabase_meeting_id, recipient)
                transcript_url = f"{base_url}/transcripts/{supabase_meeting_id}?token={token}"

                msg = MIMEMultipart('alternative')
                msg['Subject'] = f"Meeting Summary: {meeting_title}"
                msg['From'] = smtp_config['from_email']
                msg['To'] = recipient

                msg.attach(MIMEText(build_email_plain(metadata, summary, action_items, transcript_url), 'plain'))
                msg.attach(MIMEText(build_email_html(metadata, summary, action_items, transcript_url), 'html'))

                server.send_message(msg)
                sent_count += 1
                logger.info(f"Sent transcript email to {recipient}")
                time.sleep(1)

                PipelineActivity.log(
                    event_type=PipelineActivity.EventType.EMAIL_SENT,
                    status=PipelineActivity.Status.SUCCESS,
                    bot_id=bot_id,
                    meeting_title=meeting_title,
                    recipient=recipient,
                )

            except Exception as e:
                logger.error(f"Failed to send email to {recipient}: {e}")
                PipelineActivity.log(
                    event_type=PipelineActivity.EventType.EMAIL_SENT,
                    status=PipelineActivity.Status.FAILED,
                    bot_id=bot_id,
                    meeting_title=meeting_title,
                    recipient=recipient,
                    error=str(e),
                )

        server.quit()
        logger.info(f"Sent {sent_count}/{len(recipients)} emails for bot {bot_id}")

    except Exception as e:
        logger.exception(f"SMTP error for bot {bot_id}: {e}")

    return {'status': 'success', 'insight_created': True, 'email_sent': True}


# =============================================================================
# Task dispatch
# =============================================================================

def process_meeting_insights_sync(bot_id):
    return _process_meeting_insights_impl(bot_id)


def enqueue_process_meeting_insights_task(bot_id):
    from bots.task_executor import is_kubernetes_mode, task_executor
    if is_kubernetes_mode():
        task_executor.submit(process_meeting_insights_sync, bot_id)
    else:
        process_meeting_insights.delay(bot_id)


@shared_task(bind=True, max_retries=3, default_retry_delay=120)
def process_meeting_insights(self, bot_id):
    """Celery task: extract insights, save to Postgres, mirror to Supabase, send email."""
    try:
        return _process_meeting_insights_impl(bot_id)
    except Exception as exc:
        logger.exception(f"process_meeting_insights failed for bot {bot_id}: {exc}")
        raise self.retry(exc=exc)
