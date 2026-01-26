"""
Standalone script: Call edge function + send transcript email

Usage:
    python notify_meeting.py <meeting_id> <recipient_emails_comma_separated>

Requires env vars:
    SUPABASE_URL, SUPABASE_SERVICE_KEY, SMTP_USER, SMTP_PASS, CALENDAR_APP_URL
"""
import os
import sys
import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
import html


# ============================================================
# 1. CALL SUPABASE EDGE FUNCTION
# ============================================================

def extract_insights_sync(meeting_id: str) -> tuple[str, list]:
    """Call edge function and wait for result. Returns (summary, action_items)."""
    try:
        edge_function_url = f"{os.environ['SUPABASE_URL']}/functions/v1/extract-insights"
        service_key = os.environ["SUPABASE_SERVICE_KEY"]

        response = requests.post(
            edge_function_url,
            json={"meeting_id": meeting_id, "insight_type": "action_items"},
            headers={
                "Authorization": f"Bearer {service_key}",
                "Content-Type": "application/json"
            },
            timeout=60
        )

        if response.ok:
            result = response.json()
            content = result.get("content", {})
            summary = content.get("summary", "")
            items = content.get("items", [])
            print(f"Extracted summary ({len(summary)} chars) and {len(items)} action items")
            return summary, items

        print(f"Insight extraction failed: {response.status_code} - {response.text[:200]}")
        return "", []

    except Exception as e:
        print(f"Error extracting insights: {e}")
        return "", []


# ============================================================
# 2. GET MEETING DATA FROM SUPABASE
# ============================================================

def get_meeting_data(meeting_id: str) -> dict:
    """Fetch meeting from Supabase."""
    from supabase import create_client

    supabase = create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_KEY"]
    )

    result = supabase.table("meetings").select("*").eq("id", meeting_id).execute()
    if result.data:
        return result.data[0]
    return {}


# ============================================================
# 3. GET TRANSCRIPT TOKENS (personalized URLs)
# ============================================================

def get_transcript_tokens(meeting_id: str, emails: list) -> dict:
    """Get personalized access tokens from calendar-app API."""
    try:
        calendar_app_url = os.environ.get("CALENDAR_APP_URL", "https://wayfarrow.info")
        service_key = os.environ["SUPABASE_SERVICE_KEY"]

        response = requests.post(
            f"{calendar_app_url}/api/transcript-tokens",
            json={"meeting_id": meeting_id, "emails": emails},
            headers={
                "X-Api-Key": service_key,
                "Content-Type": "application/json"
            },
            timeout=10
        )

        if response.ok:
            return response.json().get("tokens", {})

        print(f"Failed to get transcript tokens: {response.status_code}")
        return {}
    except Exception as e:
        print(f"Error getting transcript tokens: {e}")
        return {}


# ============================================================
# 4. FORMAT HELPERS
# ============================================================

def format_duration(seconds: int) -> str:
    if not seconds:
        return "Unknown duration"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes} minutes"


# ============================================================
# 5. SEND TRANSCRIPT EMAIL
# ============================================================

def send_transcript_notification(meeting_id: str, meeting_data: dict, summary: str, action_items: list, recipients: list):
    """Send HTML email with summary, action items, and transcript link."""
    if not recipients:
        print("No recipients")
        return False

    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASS")
    smtp_from = os.environ.get("SMTP_FROM") or smtp_user
    smtp_host = os.environ.get("SMTP_HOST", "smtp.resend.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "465"))
    calendar_app_url = os.environ.get("CALENDAR_APP_URL", "https://wayfarrow.info")

    if not smtp_user or not smtp_pass:
        print("SMTP credentials not configured")
        return False

    # Get personalized tokens
    tokens = get_transcript_tokens(meeting_id, recipients)
    if not tokens:
        print("Failed to get access tokens")
        return False

    try:
        # Parse meeting date
        meeting_date = "Unknown date"
        if meeting_data.get("started_at"):
            try:
                dt = datetime.fromisoformat(meeting_data["started_at"].replace("Z", "+00:00"))
                meeting_date = dt.strftime("%B %d, %Y")
            except Exception:
                pass

        duration = format_duration(meeting_data.get("duration_seconds"))
        title = html.escape(meeting_data.get("title", "Untitled Meeting"))

        # Group action items by assignee
        items_by_assignee = {}
        unassigned = []
        for item in action_items:
            assignee = item.get("assignee")
            if assignee:
                if assignee not in items_by_assignee:
                    items_by_assignee[assignee] = []
                items_by_assignee[assignee].append(item)
            else:
                unassigned.append(item)

        # Build action items HTML
        action_items_html = ""
        if action_items:
            action_items_html = "<h2 style='color: #333; font-size: 16px; margin: 20px 0 10px 0;'>Action Items</h2>"
            for assignee in sorted(items_by_assignee.keys()):
                items = items_by_assignee[assignee]
                action_items_html += f"<p style='font-weight: 600; color: #333; margin: 15px 0 5px 0;'>{html.escape(assignee.upper())}</p>"
                action_items_html += "<ul style='margin: 0; padding-left: 20px;'>"
                for item in items:
                    task = html.escape(item.get("task", ""))
                    timestamp = item.get("timestamp")
                    timestamp_text = f" <span style='color: #888; font-family: monospace;'>{html.escape(timestamp)}</span>" if timestamp else ""
                    action_items_html += f"<li style='margin: 5px 0; color: #333;'>{task}{timestamp_text}</li>"
                action_items_html += "</ul>"
            if unassigned:
                action_items_html += "<p style='font-weight: 600; color: #333; margin: 15px 0 5px 0;'>UNASSIGNED</p>"
                action_items_html += "<ul style='margin: 0; padding-left: 20px;'>"
                for item in unassigned:
                    task = html.escape(item.get("task", ""))
                    action_items_html += f"<li style='margin: 5px 0; color: #333;'>{task}</li>"
                action_items_html += "</ul>"
        else:
            action_items_html = "<p style='color: #666; font-style: italic;'>No action items identified.</p>"

        # Send to each recipient
        sent_count = 0
        with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
            server.login(smtp_user, smtp_pass)

            for recipient in recipients:
                token = tokens.get(recipient)
                if not token:
                    print(f"No token for {recipient}, skipping")
                    continue

                # BUILD THE URL
                transcript_url = f"{calendar_app_url}/transcripts/{meeting_id}?token={token}"

                # Summary HTML
                summary_html = ""
                if summary:
                    summary_html = f"""
            <div style="background: #f8f9fa; border-radius: 6px; padding: 16px; margin: 16px 0;">
                <h2 style="color: #333; font-size: 16px; margin: 0 0 10px 0;">Summary</h2>
                <p style="color: #333; margin: 0; line-height: 1.5;">{html.escape(summary)}</p>
            </div>"""

                # Full email HTML
                html_body = f"""
<!DOCTYPE html>
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
</html>"""

                # Plain text fallback
                plain_text = f"{meeting_data.get('title', 'Untitled Meeting')}\n{meeting_date} - {duration}\n\n"
                plain_text += f"View full transcript: {transcript_url}\n\n"
                if summary:
                    plain_text += f"SUMMARY\n{summary}\n\n"
                if action_items:
                    plain_text += "ACTION ITEMS\n"
                    for item in action_items:
                        assignee = item.get("assignee", "Unassigned")
                        task = item.get("task", "")
                        plain_text += f"- [{assignee}] {task}\n"

                msg = MIMEMultipart("alternative")
                msg["Subject"] = f"Meeting Summary: {meeting_data.get('title', 'Untitled Meeting')}"
                msg["From"] = smtp_from
                msg["To"] = recipient
                msg.attach(MIMEText(plain_text, "plain"))
                msg.attach(MIMEText(html_body, "html"))

                server.send_message(msg)
                sent_count += 1

        print(f"Sent to {sent_count} recipients")
        return True

    except Exception as e:
        print(f"Failed to send: {e}")
        return False


# ============================================================
# MAIN
# ============================================================

def main(meeting_id: str, recipients: list):
    """Full flow: get meeting -> extract insights -> send email."""
    print(f"Processing meeting {meeting_id}...")

    # 1. Get meeting data
    meeting_data = get_meeting_data(meeting_id)
    if not meeting_data:
        print(f"Meeting {meeting_id} not found")
        return

    # 2. Call edge function to extract insights
    summary, action_items = extract_insights_sync(meeting_id)

    # 3. Send email with transcript URL
    send_transcript_notification(meeting_id, meeting_data, summary, action_items, recipients)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python notify_meeting.py <meeting_id> <email1,email2,...>")
        sys.exit(1)

    meeting_id = sys.argv[1]
    recipients = [e.strip() for e in sys.argv[2].split(",")]
    main(meeting_id, recipients)
