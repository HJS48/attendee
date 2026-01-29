"""
Webhook handlers for external services.
"""
import logging

from django.http import HttpResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from bots.tasks.sync_calendar_task import enqueue_sync_calendar_task

logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name='dispatch')
class GoogleCalendarWebhook(View):
    """
    Receive Google Calendar push notifications.

    Google sends POST requests when calendar events change.
    We look up the calendar by channel ID and trigger a sync.
    """

    def post(self, request):
        # Google sends these headers with push notifications
        channel_id = request.headers.get('X-Goog-Channel-Id')
        resource_state = request.headers.get('X-Goog-Resource-State')
        resource_id = request.headers.get('X-Goog-Resource-Id')

        logger.info(f"Google Calendar webhook: channel={channel_id}, state={resource_state}")

        # Respond immediately - Google expects quick response
        # Process async to avoid timeout

        # Ignore sync confirmations (sent when channel is created)
        if resource_state == 'sync':
            logger.debug("Ignoring sync confirmation")
            return HttpResponse('OK', status=200)

        if not channel_id:
            logger.warning("Missing channel_id in Google webhook")
            return HttpResponse('Missing channel ID', status=400)

        # Look up which calendar this webhook is for
        try:
            from ..models import GoogleWatchChannel
            watch_channel = GoogleWatchChannel.objects.filter(channel_id=channel_id).first()

            if not watch_channel:
                logger.warning(f"Unknown channel {channel_id}, ignoring webhook")
                return HttpResponse('OK', status=200)

            # Get the calendar and trigger sync
            calendar = watch_channel.calendar
            if calendar:
                logger.info(f"Triggering sync for {watch_channel.user_email} (calendar {calendar.object_id})")
                # Trigger async sync task
                enqueue_sync_calendar_task(calendar)
            else:
                logger.warning(f"No calendar linked to watch channel for {watch_channel.user_email}")

        except Exception as e:
            logger.exception(f"Error processing Google webhook: {e}")
            # Still return 200 to prevent Google from retrying
            return HttpResponse('OK', status=200)

        return HttpResponse('OK', status=200)
