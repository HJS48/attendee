"""
Safety-net cron: retry stuck meetings that should have insights but don't.

Runs every 15 minutes. Catches:
1. Recordings with COMPLETE transcription but no MeetingInsight (Claude failed, task lost, etc.)
2. MeetingInsights with empty supabase_meeting_id (Supabase sync hasn't run yet)
"""
import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Retry stuck meetings: missing insights or missing Supabase sync'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Print what would be retried without enqueuing')

    def handle(self, *args, **options):
        from bots.models import Recording, RecordingTranscriptionStates
        from bots.domain_wide.models import MeetingInsight

        dry_run = options['dry_run']
        cutoff = timezone.now() - timedelta(hours=48)

        # 1. Recordings with COMPLETE transcription but no MeetingInsight
        stuck_recordings = Recording.objects.filter(
            transcription_state=RecordingTranscriptionStates.COMPLETE,
            completed_at__gte=cutoff,
        ).exclude(
            insight__isnull=False,
        ).select_related('bot')

        insight_count = 0
        for recording in stuck_recordings:
            bot = recording.bot
            if not bot or not bot.meeting_url:
                continue
            bot_id = str(bot.object_id)
            if dry_run:
                self.stdout.write(f"[dry-run] Would enqueue process_meeting_insights for bot {bot_id}")
            else:
                from bots.tasks.process_meeting_insights_task import enqueue_process_meeting_insights_task
                enqueue_process_meeting_insights_task(bot_id)
                logger.info(f"Retrying process_meeting_insights for bot {bot_id}")
            insight_count += 1

        # 2. MeetingInsights with empty supabase_meeting_id
        stuck_syncs = MeetingInsight.objects.filter(
            supabase_meeting_id='',
            created_at__gte=cutoff,
        ).select_related('bot')

        sync_count = 0
        for insight in stuck_syncs:
            bot_id = str(insight.bot.object_id)
            if dry_run:
                self.stdout.write(f"[dry-run] Would enqueue sync_meeting_to_supabase for bot {bot_id}")
            else:
                from bots.domain_wide.tasks import enqueue_sync_meeting_to_supabase_task
                enqueue_sync_meeting_to_supabase_task(bot_id)
                logger.info(f"Retrying sync_meeting_to_supabase for bot {bot_id}")
            sync_count += 1

        summary = f"Stuck insights: {insight_count}, stuck syncs: {sync_count}"
        if dry_run:
            summary = f"[dry-run] {summary}"
        self.stdout.write(summary)
        logger.info(f"retry_stuck_insights: {summary}")
