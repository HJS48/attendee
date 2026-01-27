"""
Management command to sync stuck transcripts.

Defensive cron job that catches transcriptions that missed the signal.
Runs every 15 minutes to find recordings with completed transcription
that haven't been synced to Supabase yet.

Usage:
    python manage.py sync_stuck_transcripts
"""
from django.core.management.base import BaseCommand
from bots.domain_wide.tasks import sync_stuck_transcripts


class Command(BaseCommand):
    help = 'Sync stuck transcripts to Supabase (defensive cron job)'

    def handle(self, *args, **options):
        self.stdout.write('Starting sync_stuck_transcripts...')

        result = sync_stuck_transcripts()

        self.stdout.write(
            f"Completed: checked={result.get('checked', 0)}, "
            f"synced={result.get('synced', 0)}, "
            f"skipped={result.get('skipped', 0)}, "
            f"failed={result.get('failed', 0)}"
        )

        if result.get('status') == 'success':
            self.stdout.write(self.style.SUCCESS('sync_stuck_transcripts completed successfully'))
        else:
            self.stdout.write(self.style.ERROR(f"sync_stuck_transcripts failed: {result}"))
