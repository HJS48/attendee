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

from bots.models import CalendarEvent, Bot, BotStates, Credentials
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
        logger.debug(f"Active bot {active_bot.object_id} already exists for URL+date")
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
# Supabase Sync on Bot End
# =============================================================================

@receiver(post_save, sender=Bot)
def sync_meeting_to_supabase_on_end(sender, instance, **kwargs):
    """Sync meeting data to Supabase when bot reaches ENDED state."""

    # Only sync when bot has ended (not for other state changes)
    if instance.state != BotStates.ENDED:
        return

    # Skip if no meeting URL (nothing to sync)
    if not instance.meeting_url:
        return

    # Queue async task to avoid blocking
    try:
        from .tasks import sync_meeting_to_supabase
        sync_meeting_to_supabase.delay(str(instance.object_id))
        logger.debug(f"Queued Supabase sync for bot {instance.object_id}")
    except ImportError:
        logger.warning("sync_meeting_to_supabase task not found, skipping Supabase sync")
    except Exception as e:
        logger.exception(f"Failed to queue Supabase sync for bot {instance.object_id}: {e}")
