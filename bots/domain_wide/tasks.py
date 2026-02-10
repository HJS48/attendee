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

    Looks up calendars by deduplication_key pattern: "{email}-google-domain"
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
            deduplication_key=f"{email}-google-domain"
        ).first()

        if calendar:
            from bots.tasks.sync_calendar_task import enqueue_sync_calendar_task
            enqueue_sync_calendar_task(calendar)
            results.append({'email': email, 'status': 'queued'})
            logger.info(f"Queued sync for {email}")
        else:
            results.append({'email': email, 'status': 'no_calendar'})
            logger.warning(f"No calendar found for {email}")

    synced_count = len([r for r in results if r['status'] == 'queued'])
    logger.info(f"Pilot sync: queued {synced_count}/{len(pilot_users)} calendars")

    return {'synced': synced_count, 'results': results}


# =============================================================================
# Sync meeting data to Supabase (fire-and-forget mirror)
# =============================================================================

def sync_meeting_to_supabase_sync(bot_id: str):
    """Synchronous version for Kubernetes mode."""
    return _sync_meeting_to_supabase_impl(bot_id)


def enqueue_sync_meeting_to_supabase_task(bot_id: str):
    """Enqueue Supabase mirror sync."""
    from bots.task_executor import is_kubernetes_mode, task_executor
    if is_kubernetes_mode():
        task_executor.submit(sync_meeting_to_supabase_sync, bot_id)
    else:
        sync_meeting_to_supabase.delay(bot_id)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def sync_meeting_to_supabase(self, bot_id: str):
    """Mirror meeting + transcript to Supabase. Fire-and-forget with retries."""
    try:
        return _sync_meeting_to_supabase_impl(bot_id)
    except Exception as exc:
        logger.exception(f"sync_meeting_to_supabase failed for bot {bot_id}: {exc}")
        raise self.retry(exc=exc)


def _sync_meeting_to_supabase_impl(bot_id: str):
    """
    Build meeting data from Postgres, upsert to Supabase.
    If MeetingInsight exists, backfill supabase_meeting_id and mirror insights.
    """
    from bots.models import Bot, Recording, RecordingTranscriptionStates
    from .supabase_client import upsert_meeting, upsert_meeting_insights
    from bots.domain_wide.models import PipelineActivity, MeetingInsight
    from bots.domain_wide.transcript_utils import build_transcript_segments, get_meeting_metadata

    try:
        bot = Bot.objects.select_related('calendar_event').get(object_id=bot_id)
    except Bot.DoesNotExist:
        logger.error(f"Bot {bot_id} not found for Supabase sync")
        return {'status': 'error', 'reason': 'bot not found'}

    recording = Recording.objects.filter(
        bot=bot,
        transcription_state=RecordingTranscriptionStates.COMPLETE
    ).order_by('-created_at').first()

    if not recording:
        logger.info(f"Skipping Supabase sync for bot {bot_id} - no complete transcription")
        return {'status': 'skipped', 'reason': 'no complete transcription'}

    metadata = get_meeting_metadata(bot)
    segments = build_transcript_segments(recording)

    meeting_data = {
        'attendee_bot_id': str(bot.object_id),
        'meeting_url': metadata.get('meeting_url', ''),
        'title': metadata.get('title', ''),
        'transcript_status': 'complete',
        'started_at': metadata.get('started_at'),
        'ended_at': metadata.get('ended_at'),
        'duration_seconds': metadata.get('duration_seconds'),
        'organizer_email': metadata.get('organizer_email'),
    }

    if metadata.get('recording_url'):
        meeting_data['recording_url'] = metadata['recording_url']

    # Attendees from calendar event
    if metadata.get('attendees'):
        meeting_data['participants'] = [
            {'email': a.get('email')} for a in metadata['attendees']
            if a.get('email')
        ]

    # Transcript segments
    if segments:
        meeting_data['transcript'] = segments
        meeting_data['transcript_text'] = '\n'.join(
            f"[{s['speaker']}]: {s['text']}" for s in segments
        )

    result = upsert_meeting(meeting_data)
    meeting_title = metadata.get('title', '')

    if not result:
        logger.warning(f"Failed to sync meeting to Supabase for bot {bot_id}")
        PipelineActivity.log(
            event_type=PipelineActivity.EventType.SUPABASE_SYNC,
            status=PipelineActivity.Status.FAILED,
            bot_id=bot_id,
            meeting_title=meeting_title,
            error='upsert failed',
        )
        return {'status': 'error', 'reason': 'upsert failed'}

    supabase_meeting_id = result.get('id', '')
    logger.info(f"Synced meeting to Supabase for bot {bot_id}: {len(segments)} segments")

    PipelineActivity.log(
        event_type=PipelineActivity.EventType.SUPABASE_SYNC,
        status=PipelineActivity.Status.SUCCESS,
        bot_id=bot_id,
        meeting_id=supabase_meeting_id,
        meeting_title=meeting_title,
    )

    # Backfill supabase_meeting_id on MeetingInsight if it exists
    try:
        insight = MeetingInsight.objects.get(recording=recording)
        if not insight.supabase_meeting_id and supabase_meeting_id:
            insight.supabase_meeting_id = supabase_meeting_id
            insight.save(update_fields=['supabase_meeting_id'])
            logger.info(f"Backfilled supabase_meeting_id on MeetingInsight for bot {bot_id}")

            # Check if email was never sent - if so, re-enqueue insights task to send it
            email_sent = PipelineActivity.objects.filter(
                event_type=PipelineActivity.EventType.EMAIL_SENT,
                status=PipelineActivity.Status.SUCCESS,
                bot_id=bot_id,
            ).exists()
            if not email_sent:
                from bots.tasks.process_meeting_insights_task import enqueue_process_meeting_insights_task
                enqueue_process_meeting_insights_task(bot_id)
                logger.info(f"Re-enqueued process_meeting_insights for bot {bot_id} to send email")

        # Also mirror insights to Supabase
        if supabase_meeting_id:
            upsert_meeting_insights(supabase_meeting_id, insight.summary, insight.action_items)
    except MeetingInsight.DoesNotExist:
        pass

    return {'status': 'success', 'meeting_id': supabase_meeting_id, 'segments': len(segments)}


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
