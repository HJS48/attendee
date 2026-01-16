"""
Management command to ensure all unique meetings have bots.

Finds events with URLs that are missing bots and creates them.
Run this periodically (e.g., every 15 mins) to catch any missed events.

Usage:
    python manage.py ensure_bots_exist
    python manage.py ensure_bots_exist --days 7  # Check next 7 days
    python manage.py ensure_bots_exist --dry-run
"""
import logging
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db.models import Exists, OuterRef

from bots.models import CalendarEvent, Bot, BotStates
from bots.bots_api_utils import create_bot, BotCreationSource

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Ensure all unique meetings have bots created'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=1,
            help='Number of days ahead to check (default: 1 = today only)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be created without creating',
        )

    def handle(self, *args, **options):
        today = timezone.now().date()
        end_date = today + timedelta(days=options['days'])

        self.stdout.write(f'Checking events from {today} to {end_date}')

        # Find unique meeting URLs that don't have any active bot
        # Group by meeting_url + date, find those without bots
        events = CalendarEvent.objects.filter(
            start_time__date__gte=today,
            start_time__date__lt=end_date,
            is_deleted=False,
            meeting_url__isnull=False,
        ).exclude(meeting_url='').order_by('start_time')

        # Track which URL+date combos we've processed
        seen = set()
        created = 0
        skipped = 0
        already_has_bot = 0

        for event in events:
            event_date = event.start_time.date()
            key = (event.meeting_url, event_date)

            if key in seen:
                continue
            seen.add(key)

            # Check if ANY bot exists for this URL+date
            existing_bot = Bot.objects.filter(
                meeting_url=event.meeting_url,
                join_at__date=event_date,
            ).exclude(state__in=[BotStates.FATAL_ERROR, BotStates.ENDED]).first()

            if existing_bot:
                already_has_bot += 1
                continue

            # No bot exists - create one
            if options['dry_run']:
                self.stdout.write(f'  WOULD CREATE: {event.start_time.strftime("%Y-%m-%d %H:%M")} | {event.name[:40]}')
                created += 1
                continue

            # Create bot
            dedup_key = f"auto-{event_date}-{event.meeting_url[:50]}"
            bot_data = {
                'bot_name': 'Meeting Assistant',
                'deduplication_key': dedup_key,
                'calendar_event_id': str(event.object_id),
            }

            try:
                bot, error = create_bot(
                    data=bot_data,
                    source=BotCreationSource.SCHEDULER,
                    project=event.calendar.project
                )

                if bot:
                    self.stdout.write(self.style.SUCCESS(
                        f'  CREATED: {bot.object_id} for {event.start_time.strftime("%H:%M")} | {event.name[:40]}'
                    ))
                    created += 1
                elif error:
                    if 'deduplication' in str(error).lower():
                        skipped += 1
                    else:
                        self.stdout.write(self.style.ERROR(f'  ERROR: {event.name[:40]} - {error}'))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'  EXCEPTION: {event.name[:40]} - {e}'))

        self.stdout.write('')
        self.stdout.write(f'Unique meetings checked: {len(seen)}')
        self.stdout.write(f'Already have bot: {already_has_bot}')
        self.stdout.write(f'Created: {created}')
        self.stdout.write(f'Skipped (dedup): {skipped}')
