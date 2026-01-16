"""
Management command to create Calendar objects for pilot users.

Usage:
    python manage.py setup_pilot_calendars

Requires:
    - PILOT_USERS env var (comma-separated emails)
    - GOOGLE_SERVICE_ACCOUNT_KEY env var (base64 encoded JSON)
"""
import json
import base64
import logging
from django.core.management.base import BaseCommand
from django.conf import settings
from bots.models import Calendar, CalendarStates, Project
from bots.domain_wide.config import get_pilot_users, get_service_account_key

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Create Calendar objects for pilot users using service account auth'

    def add_arguments(self, parser):
        parser.add_argument(
            '--sync',
            action='store_true',
            help='Trigger sync after creating calendars',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be created without creating',
        )

    def handle(self, *args, **options):
        pilot_users = get_pilot_users()
        if not pilot_users:
            self.stderr.write(self.style.ERROR('PILOT_USERS env var not set or empty'))
            return

        sa_key = get_service_account_key()
        if not sa_key:
            self.stderr.write(self.style.ERROR('GOOGLE_SERVICE_ACCOUNT_KEY env var not set or invalid'))
            return

        # Get or create project
        project = Project.objects.first()
        if not project:
            self.stderr.write(self.style.ERROR('No project found. Create a project first.'))
            return

        self.stdout.write(f'Project: {project}')
        self.stdout.write(f'Pilot users: {len(pilot_users)}')
        self.stdout.write(f'Service account: {sa_key.get("client_email", "unknown")}')
        self.stdout.write('')

        created = 0
        skipped = 0

        for email in pilot_users:
            dedup_key = f"{email}-google-sa"

            existing = Calendar.objects.filter(
                project=project,
                deduplication_key=dedup_key
            ).first()

            if existing:
                self.stdout.write(f'  SKIP: {email} (calendar {existing.id} exists)')
                skipped += 1
                continue

            if options['dry_run']:
                self.stdout.write(f'  WOULD CREATE: {email}')
                created += 1
                continue

            # Create calendar with service account auth
            calendar = Calendar(
                project=project,
                platform='google',
                deduplication_key=dedup_key,
                auth_type=Calendar.AUTH_TYPE_SERVICE_ACCOUNT,
                client_id=sa_key.get('client_id', 'service-account'),
                state=CalendarStates.CONNECTED,
            )
            calendar.save()

            # Store encrypted credentials
            calendar.set_credentials({
                'auth_type': 'service_account',
                'service_account_key': sa_key,
                'impersonate_email': email,
            })

            self.stdout.write(self.style.SUCCESS(f'  CREATED: {email} (calendar {calendar.id})'))
            created += 1

        self.stdout.write('')
        self.stdout.write(f'Created: {created}, Skipped: {skipped}')

        if options['sync'] and not options['dry_run'] and created > 0:
            self.stdout.write('')
            self.stdout.write('Triggering sync...')
            from bots.domain_wide.tasks import sync_all_pilot_calendars
            result = sync_all_pilot_calendars()
            self.stdout.write(f'Sync result: {result}')
