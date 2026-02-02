"""
Signal handlers for domain-wide auto-bot creation and cleanup.
Hooks into CalendarEvent signals without modifying upstream code.
"""
import logging
from datetime import timedelta
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone
from django.conf import settings

from bots.models import CalendarEvent, Bot, BotStates, Credentials, Recording, RecordingTranscriptionStates
from bots.bots_api_utils import create_bot, BotCreationSource

logger = logging.getLogger(__name__)


def get_transcription_settings_for_project(project):
    """
    Determine transcription settings for a project.
    Uses Deepgram if credentials exist, otherwise falls back to platform captions.
    """
    has_deepgram = Credentials.objects.filter(
        project=project,
        credential_type=Credentials.CredentialTypes.DEEPGRAM
    ).exists()

    if has_deepgram:
        return {'deepgram': {'language': 'en'}}
    else:
        logger.info(f"No Deepgram credentials for project {project.object_id}, using platform captions")
        return {'meeting_closed_captions': {}}


@receiver(post_save, sender=CalendarEvent)
def auto_create_bot_for_event(sender, instance, created, **kwargs):
    """Auto-create a bot when a new CalendarEvent with meeting_url is created."""

    # Only for new events with meeting URLs
    if not created or not instance.meeting_url:
        return

    # Check if auto-bot is enabled (via settings)
    if not getattr(settings, 'AUTO_CREATE_BOTS', True):
        return

    # Skip if event is in the past (before today)
    today = timezone.now().date()
    event_date = instance.start_time.date()
    if event_date < today:
        logger.debug(f"Skipping bot creation for past event {instance.object_id}")
        return

    # Check if any ACTIVE bot exists for this meeting_url + date combo
    # (could be linked to a different CalendarEvent for the same meeting)
    active_bot = Bot.objects.filter(
        meeting_url=instance.meeting_url,
        join_at__date=event_date
    ).exclude(state__in=[BotStates.FATAL_ERROR, BotStates.ENDED]).first()

    if active_bot:
        # Link orphan bot to this event if it has no calendar_event
        if not active_bot.calendar_event:
            active_bot.calendar_event = instance
            active_bot.save(update_fields=['calendar_event'])
            logger.info(f"Linked orphan bot {active_bot.object_id} to event {instance.object_id}")
        return

    # Check if an Ended bot exists that we can reactivate
    # (happens when original event was deleted but another attendee's event still exists)
    event_date_str = instance.start_time.strftime('%Y-%m-%d')
    meeting_url_truncated = instance.meeting_url[:50] if instance.meeting_url else ''
    dedup_key = f"auto-{event_date_str}-{meeting_url_truncated}"

    ended_bot = Bot.objects.filter(
        meeting_url=instance.meeting_url,
        join_at__date=event_date,
        state=BotStates.ENDED
    ).first()

    if ended_bot:
        # Reactivate the ended bot and link to this event
        ended_bot.state = BotStates.SCHEDULED
        ended_bot.calendar_event = instance
        ended_bot.save(update_fields=['state', 'calendar_event'])
        logger.info(f"Reactivated bot {ended_bot.object_id} for event {instance.object_id}")
        return

    # Create new bot - use Deepgram if available, fallback to platform captions
    transcription_settings = get_transcription_settings_for_project(instance.calendar.project)
    bot_data = {
        'bot_name': 'Meeting Assistant',
        'deduplication_key': dedup_key,
        'calendar_event_id': str(instance.object_id),
        'transcription_settings': transcription_settings,
    }

    try:
        bot, error = create_bot(
            data=bot_data,
            source=BotCreationSource.SCHEDULER,
            project=instance.calendar.project
        )

        if bot:
            logger.info(f"Auto-created bot {bot.object_id} for event {instance.object_id}")
        elif error:
            if 'deduplication' not in str(error).lower():
                logger.warning(f"Failed to create bot for event {instance.object_id}: {error}")
            else:
                logger.debug(f"Duplicate bot skipped for event {instance.object_id}")
    except Exception as e:
        logger.exception(f"Error auto-creating bot for event {instance.object_id}: {e}")


@receiver(pre_save, sender=CalendarEvent)
def handle_event_deletion(sender, instance, **kwargs):
    """When event is deleted, re-link bot to another attendee's event or cancel if none exist."""

    if not instance.pk:
        return

    try:
        old_instance = CalendarEvent.objects.get(pk=instance.pk)
    except CalendarEvent.DoesNotExist:
        return

    # Only act if is_deleted changed from False to True
    if not (not old_instance.is_deleted and instance.is_deleted):
        return

    # Find scheduled bots linked to this event
    bots = Bot.objects.filter(
        calendar_event=instance,
        state=BotStates.SCHEDULED
    )

    for bot in bots:
        # Look for another active event with the same URL+date
        alternate_event = CalendarEvent.objects.filter(
            meeting_url=bot.meeting_url,
            start_time__date=bot.join_at.date() if bot.join_at else instance.start_time.date(),
            is_deleted=False
        ).exclude(pk=instance.pk).first()

        if alternate_event:
            # Re-link bot to the alternate event
            bot.calendar_event = alternate_event
            bot.save(update_fields=['calendar_event'])
            logger.info(f"Re-linked bot {bot.object_id} from deleted event to {alternate_event.object_id}")
        else:
            # No other event exists - cancel the bot
            bot.state = BotStates.ENDED
            bot.save(update_fields=['state'])
            logger.info(f"Cancelled bot {bot.object_id} - no other events for this meeting")


# =============================================================================
# Phase 1: Create Meeting Metadata on Bot End or Failure
# =============================================================================

@receiver(post_save, sender=Bot)
def create_meeting_on_bot_end(sender, instance, **kwargs):
    """
    Phase 1: Create/update meeting in Supabase when bot reaches terminal state.

    - ENDED: Creates meeting with transcript_status='pending', transcript synced later
    - FATAL_ERROR: Creates/updates meeting with transcript_status='failed'
    """

    # Skip if no meeting URL (nothing to sync)
    if not instance.meeting_url:
        return

    # Bot ended successfully - create meeting with pending transcript
    if instance.state == BotStates.ENDED:
        try:
            from .tasks import enqueue_create_meeting_metadata_task
            enqueue_create_meeting_metadata_task(str(instance.object_id))
            logger.debug(f"Queued meeting metadata creation for bot {instance.object_id}")
        except ImportError:
            logger.warning("enqueue_create_meeting_metadata_task not found, skipping Phase 1")
        except Exception as e:
            logger.exception(f"Failed to queue meeting metadata creation for bot {instance.object_id}: {e}")

    # Bot failed - mark meeting as failed
    elif instance.state == BotStates.FATAL_ERROR:
        try:
            from .tasks import enqueue_mark_transcript_failed_task
            enqueue_mark_transcript_failed_task(str(instance.object_id))
            logger.debug(f"Queued meeting failure marking for bot {instance.object_id}")
        except ImportError:
            logger.warning("enqueue_mark_transcript_failed_task not found, skipping failure marking")
        except Exception as e:
            logger.exception(f"Failed to queue meeting failure marking for bot {instance.object_id}: {e}")


# =============================================================================
# Phase 2: Sync Transcript on Recording Transcription Complete
# =============================================================================

@receiver(pre_save, sender=Recording)
def track_transcription_state_change(sender, instance, **kwargs):
    """
    Track transcription_state changes before save.

    Stores old transcription_state on the instance so post_save can detect transitions.
    """
    if not instance.pk:
        # New instance, no previous state
        instance._old_transcription_state = None
        return

    try:
        old_instance = Recording.objects.get(pk=instance.pk)
        instance._old_transcription_state = old_instance.transcription_state
    except Recording.DoesNotExist:
        instance._old_transcription_state = None


@receiver(post_save, sender=Recording)
def sync_transcript_on_transcription_complete(sender, instance, **kwargs):
    """
    Phase 2: Sync transcript to Supabase when transcription completes.

    Triggers when Recording.transcription_state transitions TO COMPLETE or FAILED.
    This ensures we only sync transcript after it's fully ready, preventing the
    race condition where bot ENDED fires before transcription is done.
    """
    old_state = getattr(instance, '_old_transcription_state', None)
    new_state = instance.transcription_state

    # Only act on state transitions
    if old_state == new_state:
        return

    # Get the bot for this recording
    bot = instance.bot
    if not bot or not bot.meeting_url:
        return

    # Only trigger for bots that have ended (to avoid premature syncs)
    if bot.state != BotStates.ENDED:
        logger.debug(f"Skipping transcript sync for recording {instance.pk} - bot {bot.object_id} not ended yet")
        return

    # Transcription completed → sync transcript (Phase 2)
    if new_state == RecordingTranscriptionStates.COMPLETE:
        try:
            from .tasks import enqueue_sync_transcript_task
            enqueue_sync_transcript_task(str(bot.object_id))
            logger.info(f"Queued transcript sync for bot {bot.object_id} (transcription complete)")
        except ImportError:
            logger.warning("enqueue_sync_transcript_task not found, skipping Phase 2")
        except Exception as e:
            logger.exception(f"Failed to queue transcript sync for bot {bot.object_id}: {e}")

    # Transcription failed → mark as failed
    elif new_state == RecordingTranscriptionStates.FAILED:
        try:
            from .tasks import enqueue_mark_transcript_failed_task
            enqueue_mark_transcript_failed_task(str(bot.object_id))
            logger.info(f"Queued transcript failed marking for bot {bot.object_id}")
        except ImportError:
            logger.warning("enqueue_mark_transcript_failed_task not found, skipping failure marking")
        except Exception as e:
            logger.exception(f"Failed to queue transcript failed marking for bot {bot.object_id}: {e}")
