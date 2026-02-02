"""
Activity logging utility for bot debugging.
Logs UI milestones to database for post-mortem analysis.
"""
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Global start times per bot (in-memory, reset on pod restart)
_bot_start_times: dict[str, float] = {}


def set_bot_start_time(bot_id: str):
    """Call when bot is staged to start timing."""
    _bot_start_times[bot_id] = time.time()
    logger.info(f"[{bot_id}] Activity timer started")


def log_activity(
    bot,
    activity_type: int,
    message: str = "",
    metadata: Optional[dict] = None,
):
    """
    Log a UI milestone or activity for debugging.

    Args:
        bot: Bot instance or bot_id string (object_id)
        activity_type: BotActivityLog.ActivityType value
        message: Optional human-readable message
        metadata: Optional dict with extra context
    """
    from bots.models import Bot, BotActivityLog

    # Handle bot_id string
    bot_id = None
    if isinstance(bot, str):
        bot_id = bot
        try:
            bot = Bot.objects.get(object_id=bot)
        except Bot.DoesNotExist:
            logger.warning(f"Cannot log activity for unknown bot: {bot}")
            return None
    else:
        bot_id = str(bot.object_id)

    # Calculate elapsed time
    elapsed_ms = None
    if bot_id in _bot_start_times:
        elapsed_ms = int((time.time() - _bot_start_times[bot_id]) * 1000)

    try:
        activity = BotActivityLog.objects.create(
            bot=bot,
            activity_type=activity_type,
            message=message[:500] if message else "",
            metadata=metadata or {},
            elapsed_ms=elapsed_ms,
        )

        # Also log to stdout for pod logs (while we have them)
        type_name = BotActivityLog.ActivityType(activity_type).label
        elapsed_str = f" (+{elapsed_ms}ms)" if elapsed_ms else ""
        logger.info(f"[{bot_id}] ACTIVITY: {type_name}: {message}{elapsed_str}")

        return activity
    except Exception as e:
        logger.exception(f"Failed to log activity for {bot_id}: {e}")
        return None


def clear_bot_start_time(bot_id: str):
    """Clean up when bot terminates."""
    _bot_start_times.pop(bot_id, None)
