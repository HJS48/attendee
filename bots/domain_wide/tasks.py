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


# =============================================================================
# Phase 1: Create Meeting Metadata (on Bot ENDED)
# =============================================================================

def create_meeting_metadata_sync(bot_id: str):
    """
    Synchronous version of create_meeting_metadata for direct execution without Celery.
    Used in Kubernetes mode where Celery worker is not available.
    """
    return _create_meeting_metadata_impl(bot_id)


def enqueue_create_meeting_metadata_task(bot_id: str):
    """Enqueue a create meeting metadata task (Phase 1)."""
    from bots.task_executor import is_kubernetes_mode, task_executor

    if is_kubernetes_mode():
        task_executor.submit(create_meeting_metadata_sync, bot_id)
    else:
        create_meeting_metadata.delay(bot_id)


@shared_task
def create_meeting_metadata(bot_id: str):
    """
    Phase 1: Create meeting row in Supabase with metadata only.

    Called when bot reaches ENDED state. Creates meeting row with:
    - bot_id, meeting_url, title, timestamps
    - transcript_status = 'pending'
    - Does NOT include transcript
    - Does NOT trigger insight extraction

    Transcript will be synced later when Recording.transcription_state → COMPLETE.
    """
    return _create_meeting_metadata_impl(bot_id)


def _create_meeting_metadata_impl(bot_id: str):
    """
    Implementation of create_meeting_metadata shared by both Celery and sync versions.
    """
    from bots.models import Bot, Recording, RecordingStates
    from .supabase_client import upsert_meeting
    from bots.domain_wide.models import PipelineActivity

    try:
        bot = Bot.objects.select_related('calendar_event').get(object_id=bot_id)
    except Bot.DoesNotExist:
        logger.error(f"Bot {bot_id} not found for meeting metadata creation")
        return {'status': 'error', 'reason': 'bot not found'}

    event = bot.calendar_event

    # Build meeting metadata for Supabase (no transcript yet)
    meeting_data = {
        'attendee_bot_id': str(bot.object_id),
        'meeting_url': bot.meeting_url,
        'title': event.name if event and event.name else '',
        'transcript_status': 'pending',  # Will be updated when transcript is ready
    }

    # Get recording if available (for timestamps)
    recording = Recording.objects.filter(
        bot=bot,
        state__in=[RecordingStates.COMPLETE, RecordingStates.IN_PROGRESS]
    ).order_by('-created_at').first()

    # Use recording timestamps if available
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

        # Get attendees from event and store as participants
        attendees = event.attendees or []
        if isinstance(attendees, list):
            meeting_data['participants'] = [
                {'email': a.get('email')} for a in attendees
                if isinstance(a, dict) and a.get('email')
            ]

    # Upsert to Supabase
    result = upsert_meeting(meeting_data)
    meeting_title = meeting_data.get('title', '')

    if result:
        logger.info(f"Created meeting metadata in Supabase for bot {bot_id} (transcript pending)")
        PipelineActivity.log(
            event_type=PipelineActivity.EventType.MEETING_CREATED,
            status=PipelineActivity.Status.SUCCESS,
            bot_id=bot_id,
            meeting_id=result.get('id', ''),
            meeting_title=meeting_title,
        )
        return {'status': 'success', 'meeting_id': result.get('id', ''), 'transcript_status': 'pending'}
    else:
        logger.warning(f"Failed to create meeting metadata in Supabase for bot {bot_id}")
        PipelineActivity.log(
            event_type=PipelineActivity.EventType.MEETING_CREATED,
            status=PipelineActivity.Status.FAILED,
            bot_id=bot_id,
            meeting_title=meeting_title,
            error='upsert failed',
        )
        return {'status': 'error', 'reason': 'upsert failed'}


# =============================================================================
# Phase 2: Sync Transcript (on Recording.transcription_state → COMPLETE)
# =============================================================================

def sync_transcript_sync(bot_id: str):
    """
    Synchronous version of sync_transcript for direct execution without Celery.
    """
    return _sync_transcript_impl(bot_id)


def enqueue_sync_transcript_task(bot_id: str):
    """Enqueue a sync transcript task (Phase 2)."""
    from bots.task_executor import is_kubernetes_mode, task_executor

    if is_kubernetes_mode():
        task_executor.submit(sync_transcript_sync, bot_id)
    else:
        sync_transcript.delay(bot_id)


@shared_task
def sync_transcript(bot_id: str):
    """
    Phase 2: Sync transcript to Supabase and trigger insight extraction.

    Called when Recording.transcription_state changes to COMPLETE.
    Updates meeting row with:
    - transcript data from utterances
    - transcript_status = 'complete'
    - THEN triggers insight extraction → email
    """
    return _sync_transcript_impl(bot_id)


def _sync_transcript_impl(bot_id: str):
    """
    Implementation of sync_transcript shared by both Celery and sync versions.
    """
    from bots.models import Bot, Recording, RecordingTranscriptionStates, Participant
    from .supabase_client import upsert_meeting
    from bots.domain_wide.models import PipelineActivity

    try:
        bot = Bot.objects.select_related('calendar_event').get(object_id=bot_id)
    except Bot.DoesNotExist:
        logger.error(f"Bot {bot_id} not found for transcript sync")
        return {'status': 'error', 'reason': 'bot not found'}

    # Get recording with completed transcription
    recording = Recording.objects.filter(
        bot=bot,
        transcription_state=RecordingTranscriptionStates.COMPLETE
    ).order_by('-created_at').first()

    if not recording:
        logger.warning(f"No completed transcription found for bot {bot_id}")
        return {'status': 'skipped', 'reason': 'no completed transcription'}

    event = bot.calendar_event

    # Build transcript data
    meeting_data = {
        'attendee_bot_id': str(bot.object_id),
        'transcript_status': 'complete',
    }

    # Update timestamps from recording (may be more accurate now)
    meeting_data['started_at'] = recording.started_at.isoformat() if recording.started_at else None
    meeting_data['ended_at'] = recording.completed_at.isoformat() if recording.completed_at else None

    if recording.started_at and recording.completed_at:
        duration = (recording.completed_at - recording.started_at).total_seconds()
        meeting_data['duration_seconds'] = int(duration)

    # Get recording URL from file field
    if recording.file:
        try:
            meeting_data['recording_url'] = recording.file.url
        except Exception:
            pass

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
        meeting_data['transcript_text'] = '\n'.join([
            f"[{seg['speaker']}]: {seg['text']}"
            for seg in transcript_segments
        ])

    # Get participants from utterances
    participants = Participant.objects.filter(
        utterances__recording=recording
    ).distinct()
    if participants.exists():
        meeting_data['participants'] = [
            {
                'name': p.full_name,
                'participant_id': str(p.id),
            }
            for p in participants
        ]

    # Upsert to Supabase
    result = upsert_meeting(meeting_data)
    meeting_title = event.name if event and event.name else ''

    if result:
        logger.info(f"Synced transcript to Supabase for bot {bot_id}: {len(transcript_segments)} utterances")
        PipelineActivity.log(
            event_type=PipelineActivity.EventType.TRANSCRIPT_SYNCED,
            status=PipelineActivity.Status.SUCCESS,
            bot_id=bot_id,
            meeting_id=result.get('id', ''),
            meeting_title=meeting_title,
        )

        # Chain: Extract insights from transcript (only after transcript is synced)
        meeting_id = result.get('id', '')
        if meeting_id and transcript_segments:
            from bots.tasks.extract_meeting_insights_task import enqueue_extract_meeting_insights_task
            enqueue_extract_meeting_insights_task(meeting_id, bot_id)
            logger.info(f"Queued insight extraction for meeting {meeting_id}")

        return {'status': 'success', 'meeting_id': meeting_id, 'utterances': len(transcript_segments)}
    else:
        logger.warning(f"Failed to sync transcript to Supabase for bot {bot_id}")
        PipelineActivity.log(
            event_type=PipelineActivity.EventType.TRANSCRIPT_SYNCED,
            status=PipelineActivity.Status.FAILED,
            bot_id=bot_id,
            meeting_title=meeting_title,
            error='upsert failed',
        )
        return {'status': 'error', 'reason': 'upsert failed'}


# =============================================================================
# Phase 2 (failure): Mark Transcript Failed
# =============================================================================

def mark_transcript_failed_sync(bot_id: str):
    """
    Synchronous version of mark_transcript_failed for direct execution without Celery.
    """
    return _mark_transcript_failed_impl(bot_id)


def enqueue_mark_transcript_failed_task(bot_id: str):
    """Enqueue a mark transcript failed task."""
    from bots.task_executor import is_kubernetes_mode, task_executor

    if is_kubernetes_mode():
        task_executor.submit(mark_transcript_failed_sync, bot_id)
    else:
        mark_transcript_failed.delay(bot_id)


@shared_task
def mark_transcript_failed(bot_id: str):
    """
    Mark meeting's transcript as failed in Supabase.

    Called when Recording.transcription_state changes to FAILED.
    """
    return _mark_transcript_failed_impl(bot_id)


def _mark_transcript_failed_impl(bot_id: str):
    """
    Implementation of mark_transcript_failed shared by both Celery and sync versions.
    """
    from bots.models import Bot
    from .supabase_client import upsert_meeting
    from bots.domain_wide.models import PipelineActivity

    try:
        bot = Bot.objects.select_related('calendar_event').get(object_id=bot_id)
    except Bot.DoesNotExist:
        logger.error(f"Bot {bot_id} not found for transcript failed marking")
        return {'status': 'error', 'reason': 'bot not found'}

    event = bot.calendar_event

    # Update transcript status to failed
    meeting_data = {
        'attendee_bot_id': str(bot.object_id),
        'transcript_status': 'failed',
    }

    result = upsert_meeting(meeting_data)
    meeting_title = event.name if event and event.name else ''

    if result:
        logger.info(f"Marked transcript as failed in Supabase for bot {bot_id}")
        PipelineActivity.log(
            event_type=PipelineActivity.EventType.TRANSCRIPT_SYNCED,
            status=PipelineActivity.Status.FAILED,
            bot_id=bot_id,
            meeting_id=result.get('id', ''),
            meeting_title=meeting_title,
            error='transcription failed',
        )
        return {'status': 'success', 'meeting_id': result.get('id', ''), 'transcript_status': 'failed'}
    else:
        logger.warning(f"Failed to mark transcript as failed in Supabase for bot {bot_id}")
        return {'status': 'error', 'reason': 'upsert failed'}


# =============================================================================
# Legacy: Full sync (for cron job and backwards compatibility)
# =============================================================================

def sync_meeting_to_supabase_sync(bot_id: str):
    """
    Synchronous version of sync_meeting_to_supabase for direct execution without Celery.
    Used in Kubernetes mode where Celery worker is not available.
    """
    return _sync_meeting_to_supabase_impl(bot_id)


def enqueue_sync_meeting_to_supabase_task(bot_id: str):
    """Enqueue a sync meeting to supabase task (full sync, used by cron)."""
    from bots.task_executor import is_kubernetes_mode, task_executor

    if is_kubernetes_mode():
        task_executor.submit(sync_meeting_to_supabase_sync, bot_id)
    else:
        sync_meeting_to_supabase.delay(bot_id)


@shared_task
def sync_meeting_to_supabase(bot_id: str):
    """
    Full sync of meeting data to Supabase (legacy, used by cron job).

    This combines Phase 1 and Phase 2 into a single operation.
    Used by sync_pending_meetings_to_supabase cron job for catchup.
    """
    return _sync_meeting_to_supabase_impl(bot_id)


def _sync_meeting_to_supabase_impl(bot_id: str):
    """
    Implementation of sync_meeting_to_supabase shared by both Celery and sync versions.
    Full sync combining metadata + transcript in one operation.
    """
    from bots.models import Bot, Recording, RecordingStates, RecordingTranscriptionStates, Participant
    from .supabase_client import upsert_meeting

    try:
        bot = Bot.objects.select_related('calendar_event').get(object_id=bot_id)
    except Bot.DoesNotExist:
        logger.error(f"Bot {bot_id} not found for Supabase sync")
        return {'status': 'error', 'reason': 'bot not found'}

    event = bot.calendar_event

    # Get completed recording with completed transcription
    recording = Recording.objects.filter(
        bot=bot,
        state=RecordingStates.COMPLETE,
        transcription_state=RecordingTranscriptionStates.COMPLETE
    ).order_by('-created_at').first()

    # Don't sync until transcription is complete
    if not recording:
        logger.info(f"Skipping Supabase sync for bot {bot_id} - no complete transcription yet")
        return {'status': 'skipped', 'reason': 'no complete transcription'}

    # Build meeting data for Supabase
    meeting_data = {
        'attendee_bot_id': str(bot.object_id),
        'meeting_url': bot.meeting_url,
        'title': event.name if event and event.name else '',
        'transcript_status': 'complete',
    }

    # Use recording timestamps
    meeting_data['started_at'] = recording.started_at.isoformat() if recording.started_at else None
    meeting_data['ended_at'] = recording.completed_at.isoformat() if recording.completed_at else None

    if recording.started_at and recording.completed_at:
        duration = (recording.completed_at - recording.started_at).total_seconds()
        meeting_data['duration_seconds'] = int(duration)

    # Get recording URL from file field
    if recording.file:
        try:
            meeting_data['recording_url'] = recording.file.url
        except Exception:
            pass

    # Build transcript from utterances
    utterances = recording.utterances.select_related('participant').order_by('timestamp_ms')
    transcript_segments = []
    for u in utterances:
        if u.transcription:
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
        logger.info(f"Synced meeting to Supabase for bot {bot_id}: {len(transcript_segments)} utterances")
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


# =============================================================================
# Defensive Cron Job: Sync Stuck Transcripts
# =============================================================================

@shared_task
def sync_stuck_transcripts():
    """
    Defensive cron job: Catch transcriptions that missed the signal.

    Runs every 15 minutes to find recordings that:
    1. Have transcription_state = COMPLETE
    2. Belong to bots that are ENDED
    3. May not have been synced to Supabase yet

    This handles edge cases where the Recording post_save signal might miss
    due to race conditions, server restarts, or other transient issues.

    Idempotent - safe to run multiple times.
    """
    from bots.models import Bot, Recording, RecordingStates, RecordingTranscriptionStates, BotStates
    from django.utils import timezone
    from datetime import timedelta
    from .supabase_client import get_supabase_client

    now = timezone.now()

    # Only look at recordings completed in the last 7 days
    seven_days_ago = now - timedelta(days=7)

    # Find recordings with complete transcription that might need syncing
    # Only consider recordings where the bot has ended (to avoid premature syncs)
    complete_recordings = Recording.objects.filter(
        transcription_state=RecordingTranscriptionStates.COMPLETE,
        state=RecordingStates.COMPLETE,
        bot__state=BotStates.ENDED,
        updated_at__gte=seven_days_ago
    ).select_related('bot').order_by('-updated_at')[:100]  # Limit batch size

    if not complete_recordings.exists():
        logger.info("sync_stuck_transcripts: No completed transcriptions to check")
        return {'status': 'success', 'checked': 0, 'synced': 0, 'skipped': 0}

    # Get Supabase client to check what's already synced
    client = get_supabase_client()

    # Get all bot IDs that need checking
    bot_ids = list(set(r.bot.object_id for r in complete_recordings))

    # Check which are already synced with transcript_status='complete'
    already_synced = set()
    if client:
        try:
            result = client.table('meetings').select(
                'attendee_bot_id, transcript_status'
            ).in_('attendee_bot_id', bot_ids).execute()

            for meeting in (result.data or []):
                if meeting.get('transcript_status') == 'complete':
                    already_synced.add(meeting['attendee_bot_id'])
        except Exception as e:
            logger.warning(f"sync_stuck_transcripts: Failed to check Supabase: {e}")
            # Continue anyway - sync_transcript is idempotent

    # Sync recordings that aren't in the already_synced set
    synced = 0
    skipped = 0
    failed = 0

    for recording in complete_recordings:
        bot_id = recording.bot.object_id

        if bot_id in already_synced:
            skipped += 1
            continue

        try:
            result = _sync_transcript_impl(bot_id)
            if result.get('status') == 'success':
                synced += 1
                already_synced.add(bot_id)  # Don't sync same bot twice
                logger.info(f"sync_stuck_transcripts: Synced transcript for bot {bot_id}")
            else:
                skipped += 1
        except Exception as e:
            logger.exception(f"sync_stuck_transcripts: Failed to sync bot {bot_id}: {e}")
            failed += 1

    logger.info(f"sync_stuck_transcripts: Checked {len(bot_ids)}, synced {synced}, skipped {skipped}, failed {failed}")
    return {'status': 'success', 'checked': len(bot_ids), 'synced': synced, 'skipped': skipped, 'failed': failed}
