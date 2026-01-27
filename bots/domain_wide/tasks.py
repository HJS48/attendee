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


@shared_task
def sync_pending_meetings_to_supabase():
    """
    Cron task: Find ended bots with completed recordings that need syncing.

    Run every 5 minutes via celery beat or cron.
    Idempotent - safe to run multiple times.
    """
    from bots.models import Bot, BotStates, Recording, RecordingStates
    from django.db.models import Q, Exists, OuterRef
    from django.utils import timezone
    from datetime import timedelta

    # Find bots that:
    # 1. Are in ENDED state
    # 2. Have a COMPLETE recording
    # 3. Ended in the last 7 days (don't process ancient data)
    # 4. Haven't been synced recently (no supabase_synced_at or it's older than recording completion)
    seven_days_ago = timezone.now() - timedelta(days=7)

    has_complete_recording = Exists(
        Recording.objects.filter(
            bot=OuterRef('pk'),
            state=RecordingStates.COMPLETE
        )
    )

    bots_to_sync = Bot.objects.filter(
        state=BotStates.ENDED,
        updated_at__gte=seven_days_ago,
    ).annotate(
        has_recording=has_complete_recording
    ).filter(
        has_recording=True
    )

    # Further filter: only sync if not synced or recording completed after last sync
    # We'll track this via a simple approach: check if meeting exists in Supabase
    # For now, just sync all - upsert is idempotent

    synced = 0
    failed = 0
    for bot in bots_to_sync[:50]:  # Limit batch size
        try:
            result = sync_meeting_to_supabase(str(bot.object_id))
            if result.get('status') == 'success':
                synced += 1
            else:
                failed += 1
        except Exception as e:
            logger.exception(f"Failed to sync bot {bot.object_id}: {e}")
            failed += 1

    logger.info(f"Supabase sync complete: {synced} synced, {failed} failed")
    return {'synced': synced, 'failed': failed}


def sync_meeting_to_supabase_sync(bot_id: str):
    """
    Synchronous version of sync_meeting_to_supabase for direct execution without Celery.
    Used in Kubernetes mode where Celery worker is not available.
    """
    return _sync_meeting_to_supabase_impl(bot_id)


def enqueue_sync_meeting_to_supabase_task(bot_id: str):
    """Enqueue a sync meeting to supabase task."""
    from bots.task_executor import is_kubernetes_mode, task_executor

    if is_kubernetes_mode():
        task_executor.submit(sync_meeting_to_supabase_sync, bot_id)
    else:
        sync_meeting_to_supabase.delay(bot_id)


@shared_task
def sync_meeting_to_supabase(bot_id: str):
    """
    Sync meeting data to Supabase after bot has ended.

    Collects meeting metadata, transcript, and participants from Bot/Recording/Utterances,
    then upserts to Supabase meetings table for downstream automations.
    """
    return _sync_meeting_to_supabase_impl(bot_id)


def _sync_meeting_to_supabase_impl(bot_id: str):
    """
    Implementation of sync_meeting_to_supabase shared by both Celery and sync versions.
    """
    from bots.models import Bot, Recording, RecordingStates, Participant
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

        # Build transcript from utterances
        utterances = recording.utterances.select_related('participant').order_by('timestamp_ms')
        transcript_segments = []
        for u in utterances:
            if u.transcription:
                # Handle different transcription formats
                text = ''
                if isinstance(u.transcription, dict):
                    text = u.transcription.get('text', '') or u.transcription.get('transcript', '')
                elif isinstance(u.transcription, str):
                    text = u.transcription

                if text:
                    transcript_segments.append({
                        'speaker': u.participant.full_name if u.participant else 'Unknown',
                        'timestamp_ms': u.timestamp_ms,
                        'duration_ms': u.duration_ms,
                        'text': text,
                    })

        if transcript_segments:
            meeting_data['transcript'] = transcript_segments
            # Also create a plain text version
            meeting_data['transcript_text'] = '\n'.join([
                f"[{seg['speaker']}]: {seg['text']}"
                for seg in transcript_segments
            ])

    # Get participants from the bot's recordings
    participants = Participant.objects.filter(
        utterances__recording__bot=bot
    ).distinct()
    if participants.exists():
        meeting_data['participants'] = [
            {
                'name': p.full_name,
                'participant_id': str(p.id),
            }
            for p in participants
        ]

    # Add event details if available
    if event:
        meeting_data['organizer_email'] = getattr(event, 'organizer_email', None)

        # Get attendees from event and store as participants
        # Note: Supabase meetings table has 'participants' column, not 'attendees'
        attendees = event.attendees or []
        if isinstance(attendees, list):
            meeting_data['participants'] = [
                {'email': a.get('email')} for a in attendees
                if isinstance(a, dict) and a.get('email')
            ]

    # Upsert to Supabase
    result = upsert_meeting(meeting_data)
    meeting_title = meeting_data.get('title', '')

    # Log pipeline activity
    from bots.domain_wide.models import PipelineActivity
    if result:
        logger.info(f"Synced meeting to Supabase for bot {bot_id}: {len(transcript_segments if 'transcript_segments' in dir() else [])} utterances")
        PipelineActivity.log(
            event_type=PipelineActivity.EventType.SUPABASE_SYNC,
            status=PipelineActivity.Status.SUCCESS,
            bot_id=bot_id,
            meeting_id=result.get('id', ''),
            meeting_title=meeting_title,
        )

        # Chain: Extract insights from transcript
        meeting_id = result.get('id', '')
        if meeting_id:
            from bots.tasks.extract_meeting_insights_task import enqueue_extract_meeting_insights_task
            enqueue_extract_meeting_insights_task(meeting_id, bot_id)
            logger.info(f"Queued insight extraction for meeting {meeting_id}")

        return {'status': 'success', 'meeting_id': meeting_id}
    else:
        logger.warning(f"Failed to sync meeting to Supabase for bot {bot_id}")
        PipelineActivity.log(
            event_type=PipelineActivity.EventType.SUPABASE_SYNC,
            status=PipelineActivity.Status.FAILED,
            bot_id=bot_id,
            meeting_title=meeting_title,
            error='upsert failed',
        )
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
