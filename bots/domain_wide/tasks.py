"""Celery tasks for domain-wide calendar sync."""
import logging
from celery import shared_task
from bots.models import Calendar, Project
from bots.domain_wide.config import get_pilot_users

logger = logging.getLogger(__name__)


@shared_task
def sync_all_pilot_calendars():
    """
    Sync all calendars for pilot users.

    Looks up calendars by deduplication_key pattern: "{email}-google-sa"
    Triggers async sync for each found calendar.
    """
    pilot_users = get_pilot_users()
    if not pilot_users:
        logger.info("No pilot users configured (PILOT_USERS env var)")
        return {'status': 'skipped', 'reason': 'no pilot users configured'}

    project = Project.objects.first()
    if not project:
        logger.error("No project found for pilot sync")
        return {'status': 'error', 'reason': 'no project found'}

    results = []
    for email in pilot_users:
        calendar = Calendar.objects.filter(
            project=project,
            deduplication_key=f"{email}-google-sa"
        ).first()

        if calendar:
            from bots.tasks.sync_calendar_task import sync_calendar
            sync_calendar.delay(calendar.id)
            results.append({'email': email, 'status': 'queued'})
            logger.info(f"Queued sync for {email}")
        else:
            results.append({'email': email, 'status': 'no_calendar'})
            logger.warning(f"No calendar found for {email}")

    synced_count = len([r for r in results if r['status'] == 'queued'])
    logger.info(f"Pilot sync: queued {synced_count}/{len(pilot_users)} calendars")

    return {'synced': synced_count, 'results': results}


@shared_task
def sync_meeting_to_supabase(bot_id: str):
    """
    Sync meeting data to Supabase after bot has ended.

    Collects meeting metadata from Bot and CalendarEvent,
    then upserts to Supabase meetings table for downstream automations.
    """
    from bots.models import Bot, Recording, RecordingStates
    from .supabase_client import upsert_meeting

    try:
        bot = Bot.objects.select_related('calendar_event').get(object_id=bot_id)
    except Bot.DoesNotExist:
        logger.error(f"Bot {bot_id} not found for Supabase sync")
        return {'status': 'error', 'reason': 'bot not found'}

    event = bot.calendar_event

    # Get completed recording if available
    recording = Recording.objects.filter(
        bot=bot,
        state=RecordingStates.COMPLETE
    ).order_by('-created_at').first()

    # Build meeting data for Supabase
    meeting_data = {
        'attendee_bot_id': str(bot.object_id),
        'meeting_url': bot.meeting_url,
        'title': event.name if event else None,
    }

    # Use recording timestamps if available (more accurate than bot timestamps)
    if recording:
        meeting_data['started_at'] = recording.started_at.isoformat() if recording.started_at else None
        meeting_data['ended_at'] = recording.completed_at.isoformat() if recording.completed_at else None

        # Calculate duration from recording
        if recording.started_at and recording.completed_at:
            duration = (recording.completed_at - recording.started_at).total_seconds()
            meeting_data['duration_seconds'] = int(duration)

        # Get recording URL from file field
        if recording.file:
            try:
                meeting_data['recording_url'] = recording.file.url
            except Exception:
                pass  # File may not have a URL

    # Add event details if available
    if event:
        meeting_data['organizer_email'] = getattr(event, 'organizer_email', None)

        # Get attendees from event
        attendees = event.attendees or []
        if isinstance(attendees, list):
            meeting_data['attendees'] = [
                a.get('email') for a in attendees
                if isinstance(a, dict) and a.get('email')
            ]

    # Upsert to Supabase
    result = upsert_meeting(meeting_data)
    if result:
        logger.info(f"Synced meeting to Supabase for bot {bot_id}")
        return {'status': 'success', 'meeting_id': result.get('id')}
    else:
        logger.warning(f"Failed to sync meeting to Supabase for bot {bot_id}")
        return {'status': 'error', 'reason': 'upsert failed'}


@shared_task
def renew_expiring_watch_channels():
    """
    Renew Google Calendar watch channels that are expiring within 48 hours.

    Watch channels have a max lifetime of 7 days (Google's limit).
    This task renews channels before they expire to maintain push notifications.
    """
    from datetime import timedelta
    from django.utils import timezone
    from bots.domain_wide.models import GoogleWatchChannel
    from bots.domain_wide.management.commands.manage_watch_channels import (
        create_watch_channel,
        stop_watch_channel,
    )

    threshold = timezone.now() + timedelta(hours=48)
    expiring = GoogleWatchChannel.objects.filter(expiration__lt=threshold)

    if not expiring.exists():
        logger.info("No watch channels need renewal")
        return {'status': 'success', 'renewed': 0, 'failed': 0}

    logger.info(f"Found {expiring.count()} watch channels to renew")

    success_count = 0
    fail_count = 0

    for channel in expiring:
        try:
            # Stop existing channel
            stop_watch_channel(channel)

            # Create new channel
            channel_info = create_watch_channel(channel.user_email)

            # Update database record
            channel.channel_id = channel_info['channel_id']
            channel.resource_id = channel_info['resource_id']
            channel.expiration = channel_info['expiration']
            channel.save()

            logger.info(
                f"Renewed watch channel for {channel.user_email}, "
                f"expires {channel_info['expiration'].strftime('%Y-%m-%d %H:%M UTC')}"
            )
            success_count += 1

        except Exception as e:
            logger.exception(f"Failed to renew watch channel for {channel.user_email}: {e}")
            fail_count += 1

    logger.info(f"Watch channel renewal complete: {success_count} renewed, {fail_count} failed")
    return {'status': 'success', 'renewed': success_count, 'failed': fail_count}
