"""
Management command to setup and renew Google Calendar watch channels.

Watch channels expire after 7 days (Google's limit).
Run this daily to renew expiring channels.

Usage:
    python manage.py manage_watch_channels --setup user@example.com
    python manage.py manage_watch_channels --renew-expiring
    python manage.py manage_watch_channels --list
"""
import logging
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.conf import settings

from bots.domain_wide.models import GoogleWatchChannel

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Manage Google Calendar watch channels for push notifications'

    def add_arguments(self, parser):
        parser.add_argument(
            '--setup',
            type=str,
            help='Setup watch channel for a specific user email',
        )
        parser.add_argument(
            '--renew-expiring',
            action='store_true',
            help='Renew all channels expiring within 48 hours',
        )
        parser.add_argument(
            '--list',
            action='store_true',
            help='List all watch channels and their status',
        )
        parser.add_argument(
            '--delete',
            type=str,
            help='Delete watch channel for a specific user email',
        )

    def handle(self, *args, **options):
        if options['list']:
            self.list_channels()
        elif options['setup']:
            self.setup_channel(options['setup'])
        elif options['renew_expiring']:
            self.renew_expiring_channels()
        elif options['delete']:
            self.delete_channel(options['delete'])
        else:
            self.stdout.write('No action specified. Use --help for options.')

    def list_channels(self):
        """List all watch channels."""
        channels = GoogleWatchChannel.objects.all().order_by('expiration')

        if not channels.exists():
            self.stdout.write('No watch channels configured.')
            return

        self.stdout.write(f'\n{"Email":<40} {"Expires":<25} {"Status":<15}')
        self.stdout.write('-' * 80)

        for channel in channels:
            if channel.is_expired:
                status = self.style.ERROR('EXPIRED')
            elif channel.expires_soon:
                status = self.style.WARNING('EXPIRES SOON')
            else:
                status = self.style.SUCCESS('ACTIVE')

            self.stdout.write(
                f'{channel.user_email:<40} '
                f'{channel.expiration.strftime("%Y-%m-%d %H:%M"):<25} '
                f'{status}'
            )

        self.stdout.write('')

    def setup_channel(self, email):
        """Setup a new watch channel for a user."""
        self.stdout.write(f'Setting up watch channel for {email}...')

        # Check if channel already exists and is valid
        existing = GoogleWatchChannel.objects.filter(user_email=email).first()
        if existing and not existing.expires_soon:
            self.stdout.write(
                self.style.WARNING(f'Valid channel already exists, expires: {existing.expiration}')
            )
            return

        # TODO: Implement actual Google Calendar API call to create watch channel
        # This requires:
        # 1. OAuth credentials for the user OR service account impersonation
        # 2. Call to Google Calendar API channels.watch()
        # 3. Store the returned channel_id, resource_id, expiration

        self.stdout.write(
            self.style.WARNING(
                'Watch channel creation not yet implemented. '
                'Requires OAuth credentials or service account setup.'
            )
        )

    def renew_expiring_channels(self):
        """Renew all channels expiring within 48 hours."""
        threshold = timezone.now() + timedelta(hours=48)
        expiring = GoogleWatchChannel.objects.filter(expiration__lt=threshold)

        if not expiring.exists():
            self.stdout.write('No channels need renewal.')
            return

        self.stdout.write(f'Found {expiring.count()} channels to renew:')

        for channel in expiring:
            self.stdout.write(f'  - {channel.user_email} (expires {channel.expiration})')
            # TODO: Implement renewal
            # 1. Stop existing channel (optional, Google auto-stops on expiry)
            # 2. Create new channel
            # 3. Update database record

        self.stdout.write(
            self.style.WARNING('Automatic renewal not yet implemented.')
        )

    def delete_channel(self, email):
        """Delete a watch channel."""
        channel = GoogleWatchChannel.objects.filter(user_email=email).first()

        if not channel:
            self.stdout.write(self.style.ERROR(f'No channel found for {email}'))
            return

        # TODO: Call Google API to stop the channel
        # Then delete from database
        channel.delete()
        self.stdout.write(self.style.SUCCESS(f'Deleted channel for {email}'))
