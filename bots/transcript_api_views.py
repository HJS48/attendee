"""
Internal API endpoints for transcript notifications.

Called by Supabase Edge Function after insight extraction.
Uses X-Api-Key header authentication (service-to-service).
"""
import logging
import os

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from bots.tasks.send_transcript_email_task import enqueue_send_transcript_email_task

logger = logging.getLogger(__name__)


def get_service_key():
    """Get the service key for API authentication."""
    return os.getenv('SUPABASE_SERVICE_KEY', '')


@csrf_exempt
@require_POST
def notify_meeting(request):
    """
    Receive notification from Supabase Edge Function after insight extraction.

    Called when: Edge function extracts summary + action items from transcript.
    Action: Queue email task to notify internal participants.

    Request:
        POST /api/v1/internal/notify-meeting
        Headers:
            X-Api-Key: <SUPABASE_SERVICE_KEY>
            Content-Type: application/json
        Body:
            {
                "meeting_id": "uuid",
                "summary": "Meeting summary text",
                "action_items": [
                    {"task": "...", "assignee": "...", "timestamp": "[MM:SS]", "due": "..."},
                    ...
                ]
            }

    Response:
        200: {"status": "queued"}
        400: {"error": "..."}
        401: {"error": "Unauthorized"}
    """
    # Authenticate using X-Api-Key header
    api_key = request.headers.get('X-Api-Key', '')
    expected_key = get_service_key()

    if not expected_key:
        logger.error("SUPABASE_SERVICE_KEY not configured")
        return JsonResponse({'error': 'Server misconfigured'}, status=500)

    if not api_key or api_key != expected_key:
        logger.warning("Unauthorized notify-meeting request")
        return JsonResponse({'error': 'Unauthorized'}, status=401)

    # Parse request body
    import json
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    meeting_id = data.get('meeting_id')
    summary = data.get('summary', '')
    action_items = data.get('action_items', [])

    if not meeting_id:
        return JsonResponse({'error': 'meeting_id is required'}, status=400)

    if not isinstance(action_items, list):
        action_items = []

    logger.info(f"Received notify-meeting for {meeting_id} with {len(action_items)} action items")

    # Queue the email task
    try:
        enqueue_send_transcript_email_task(meeting_id, summary, action_items)
        return JsonResponse({'status': 'queued'})
    except Exception as e:
        logger.exception(f"Failed to queue email task for meeting {meeting_id}: {e}")
        return JsonResponse({'error': 'Failed to queue task'}, status=500)
