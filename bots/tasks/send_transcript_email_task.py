"""
Send transcript notification emails to meeting participants.

Filters to internal participants (configurable domain) + dev bypass email.
Sends personalized links with signed tokens.
"""
import html
import logging
import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from celery import shared_task
from django.conf import settings

from bots.domain_wide.supabase_client import get_meeting
from bots.transcript_views import create_transcript_token

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

def get_internal_domain():
    """Get internal email domain for filtering participants."""
    return os.getenv('INTERNAL_EMAIL_DOMAIN', 'soarwithus.co')


def get_dev_bypass_email():
    """Get dev bypass email that always receives notifications."""
    return os.getenv('DEV_BYPASS_EMAIL', 'harryschmidt042@gmail.com')


def get_transcript_base_url():
    """Get base URL for transcript links."""
    return os.getenv('TRANSCRIPT_BASE_URL', 'https://wayfarrow.info')


def get_smtp_config():
    """Get SMTP configuration from environment/settings."""
    return {
        'host': os.getenv('EMAIL_HOST', getattr(settings, 'EMAIL_HOST', 'smtp.resend.com')),
        'port': int(os.getenv('EMAIL_PORT', getattr(settings, 'EMAIL_PORT', 587))),
        'user': os.getenv('EMAIL_HOST_USER', getattr(settings, 'EMAIL_HOST_USER', '')),
        'password': os.getenv('EMAIL_HOST_PASSWORD', getattr(settings, 'EMAIL_HOST_PASSWORD', '')),
        'from_email': os.getenv('DEFAULT_FROM_EMAIL', getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@soarwithus.co')),
        'use_tls': os.getenv('EMAIL_USE_TLS', 'true').lower() == 'true',
    }


# =============================================================================
# Helpers
# =============================================================================

def format_duration(seconds: int) -> str:
    """Format duration in seconds for display."""
    if not seconds:
        return 'Unknown duration'
    mins = seconds // 60
    hrs = mins // 60
    if hrs > 0:
        return f"{hrs}h {mins % 60}m"
    return f"{mins} minutes"


def get_internal_recipients(meeting: dict) -> list[str]:
    """
    Extract internal participants from meeting data.

    Returns emails that:
    - End with the internal domain (@soarwithus.co by default)
    - OR match the dev bypass email
    """
    internal_domain = get_internal_domain().lower()
    dev_bypass = get_dev_bypass_email()
    dev_bypass_lower = dev_bypass.lower() if dev_bypass else None

    recipients = set()

    # Check organizer
    organizer_email = meeting.get('organizer_email', '')
    if organizer_email:
        email_lower = organizer_email.lower()
        if email_lower.endswith(f'@{internal_domain}'):
            recipients.add(organizer_email)
        elif dev_bypass_lower and email_lower == dev_bypass_lower:
            recipients.add(organizer_email)

    # Check participants
    participants = meeting.get('participants') or []
    for p in participants:
        email = ''
        if isinstance(p, dict):
            email = p.get('email', '')
        elif isinstance(p, str) and '@' in p:
            email = p

        if email:
            email_lower = email.lower()
            if email_lower.endswith(f'@{internal_domain}'):
                recipients.add(email)
            elif dev_bypass_lower and email_lower == dev_bypass_lower:
                recipients.add(email)

    # Check attendees
    attendees = meeting.get('attendees') or []
    for a in attendees:
        email = ''
        if isinstance(a, dict):
            email = a.get('email', '')
        elif isinstance(a, str) and '@' in a:
            email = a

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


def build_email_html(meeting: dict, summary: str, action_items: list, transcript_url: str) -> str:
    """Build the HTML email body."""
    title = html.escape(meeting.get('title', 'Untitled Meeting'))
    duration = format_duration(meeting.get('duration_seconds'))

    # Parse meeting date
    meeting_date = 'Unknown date'
    started_at = meeting.get('started_at')
    if started_at:
        try:
            dt = datetime.fromisoformat(started_at.replace('Z', '+00:00'))
            meeting_date = dt.strftime('%B %d, %Y')
        except Exception:
            pass

    # Summary section
    summary_html = ''
    if summary:
        summary_html = f'''
        <div style="background: #f8f9fa; border-radius: 6px; padding: 16px; margin: 16px 0;">
            <h2 style="color: #333; font-size: 16px; margin: 0 0 10px 0;">Summary</h2>
            <p style="color: #333; margin: 0; line-height: 1.5;">{html.escape(summary)}</p>
        </div>'''

    # Action items section
    action_items_html = ''
    if action_items:
        # Group by assignee
        by_assignee = {}
        unassigned = []
        for item in action_items:
            assignee = item.get('assignee')
            if assignee:
                if assignee not in by_assignee:
                    by_assignee[assignee] = []
                by_assignee[assignee].append(item)
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
                timestamp_text = f" <span style='color: #888; font-family: monospace;'>{html.escape(timestamp)}</span>" if timestamp else ""
                action_items_html += f"<li style='margin: 5px 0; color: #333;'>{task}{timestamp_text}</li>"
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


def build_email_plain(meeting: dict, summary: str, action_items: list, transcript_url: str) -> str:
    """Build the plain text email body."""
    title = meeting.get('title', 'Untitled Meeting')
    duration = format_duration(meeting.get('duration_seconds'))

    meeting_date = 'Unknown date'
    started_at = meeting.get('started_at')
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
# Sync version (for Kubernetes mode)
# =============================================================================

def send_transcript_email_sync(meeting_id: str, summary: str, action_items: list):
    """
    Synchronous version for direct execution without Celery.
    Used in Kubernetes mode where Celery worker may not be available.
    """
    # Fetch meeting from Supabase
    meeting = get_meeting(meeting_id)
    if not meeting:
        logger.error(f"Meeting {meeting_id} not found in Supabase")
        return

    # Get internal recipients
    recipients = get_internal_recipients(meeting)
    if not recipients:
        logger.info(f"No internal recipients for meeting {meeting_id}")
        return

    logger.info(f"Sending transcript emails for meeting {meeting_id} to {len(recipients)} recipients")

    smtp_config = get_smtp_config()
    base_url = get_transcript_base_url()

    if not smtp_config['user'] or not smtp_config['password']:
        logger.error("SMTP credentials not configured")
        return

    try:
        # Connect to SMTP server
        if smtp_config['use_tls']:
            server = smtplib.SMTP(smtp_config['host'], smtp_config['port'])
            server.starttls()
        else:
            server = smtplib.SMTP_SSL(smtp_config['host'], smtp_config['port'])

        server.login(smtp_config['user'], smtp_config['password'])

        sent_count = 0
        meeting_title = meeting.get('title', 'Untitled Meeting')

        # Import for pipeline logging
        from bots.domain_wide.models import PipelineActivity

        for recipient in recipients:
            try:
                # Dedup check - skip if already sent to this recipient for this meeting
                already_sent = PipelineActivity.objects.filter(
                    event_type=PipelineActivity.EventType.EMAIL_SENT,
                    status=PipelineActivity.Status.SUCCESS,
                    meeting_id=meeting_id,
                    recipient=recipient,
                ).exists()
                if already_sent:
                    logger.debug(f"Skipping duplicate email to {recipient} for meeting {meeting_id}")
                    continue

                # Generate personalized token
                token = create_transcript_token(meeting_id, recipient)
                transcript_url = f"{base_url}/transcripts/{meeting_id}?token={token}"

                # Build email
                msg = MIMEMultipart('alternative')
                msg['Subject'] = f"Meeting Summary: {meeting_title}"
                msg['From'] = smtp_config['from_email']
                msg['To'] = recipient

                plain_body = build_email_plain(meeting, summary, action_items, transcript_url)
                html_body = build_email_html(meeting, summary, action_items, transcript_url)

                msg.attach(MIMEText(plain_body, 'plain'))
                msg.attach(MIMEText(html_body, 'html'))

                server.send_message(msg)
                sent_count += 1
                logger.info(f"Sent transcript email to {recipient}")

                # Log success
                PipelineActivity.log(
                    event_type=PipelineActivity.EventType.EMAIL_SENT,
                    status=PipelineActivity.Status.SUCCESS,
                    meeting_id=meeting_id,
                    meeting_title=meeting_title,
                    recipient=recipient,
                )

            except Exception as e:
                logger.error(f"Failed to send email to {recipient}: {e}")
                # Log failure
                PipelineActivity.log(
                    event_type=PipelineActivity.EventType.EMAIL_SENT,
                    status=PipelineActivity.Status.FAILED,
                    meeting_id=meeting_id,
                    meeting_title=meeting_title,
                    recipient=recipient,
                    error=str(e),
                )

        server.quit()
        logger.info(f"Sent {sent_count}/{len(recipients)} transcript emails for meeting {meeting_id}")

    except Exception as e:
        logger.exception(f"SMTP error sending transcript emails: {e}")


# =============================================================================
# Task enqueuing
# =============================================================================

def enqueue_send_transcript_email_task(meeting_id: str, summary: str, action_items: list):
    """Enqueue a send transcript email task."""
    from bots.task_executor import is_kubernetes_mode, task_executor

    if is_kubernetes_mode():
        task_executor.submit(send_transcript_email_sync, meeting_id, summary, action_items)
    else:
        send_transcript_email.delay(meeting_id, summary, action_items)


# =============================================================================
# Celery task
# =============================================================================

@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
)
def send_transcript_email(self, meeting_id: str, summary: str, action_items: list):
    """
    Send transcript notification emails to internal meeting participants.

    Args:
        meeting_id: Supabase meeting ID
        summary: Meeting summary text
        action_items: List of action item dicts with 'task', 'assignee', 'timestamp' keys
    """
    try:
        send_transcript_email_sync(meeting_id, summary, action_items)
    except Exception as e:
        logger.exception(f"Failed to send transcript emails for meeting {meeting_id}: {e}")
        raise self.retry(exc=e)
