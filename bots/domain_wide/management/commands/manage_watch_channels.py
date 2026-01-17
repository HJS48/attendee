"""
Management command to setup and renew Google Calendar watch channels.

Watch channels enable real-time push notifications from Google Calendar.
Channels expire after 7 days (Google's limit), so need periodic renewal.

Usage:
    python manage.py manage_watch_channels --setup-all       # Setup for all pilot users
    python manage.py manage_watch_channels --setup user@example.com
    python manage.py manage_watch_channels --renew-expiring  # Renew channels expiring in 48h
    python manage.py manage_watch_channels --list
    python manage.py manage_watch_channels --stop user@example.com
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone

import requests
from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account

from bots.domain_wide.models import GoogleWatchChannel
from bots.models import Calendar

logger = logging.getLogger(__name__)


def get_service_account_credentials():
    """Load service account credentials from settings."""
    # Get service account key from first calendar's credentials (domain-wide delegation)
    calendar = Calendar.objects.filter(
        deduplication_key__endswith='-google-domain'
    ).first()

    if not calendar:
        raise ValueError("No domain-wide calendar found. Cannot get service account.")

    credentials = calendar.get_credentials()
    if not credentials or credentials.get('auth_type') != 'service_account':
        raise ValueError("Calendar doesn't have service account credentials.")

    return credentials.get('service_account_key')


def get_access_token_for_user(user_email: str) -> str:
    """Get an access token for a user via service account impersonation."""
    sa_key = get_service_account_credentials()

    sa_credentials = service_account.Credentials.from_service_account_info(
        sa_key,
        scopes=["https://www.googleapis.com/auth/calendar.readonly"],
        subject=user_email
    )
    sa_credentials.refresh(GoogleAuthRequest())
    return sa_credentials.token


def create_watch_channel(user_email: str) -> dict:
    """
    Create a Google Calendar watch channel for a user.

    Returns dict with channel info on success, or raises exception on failure.
    """
    webhook_url = getattr(settings, 'GOOGLE_CALENDAR_WEBHOOK_URL', None)
    if not webhook_url:
        raise ValueError("GOOGLE_CALENDAR_WEBHOOK_URL not configured in settings")

    # Get access token via service account impersonation
    access_token = get_access_token_for_user(user_email)

    # Generate unique channel ID
    channel_id = f"domain-wide-{uuid.uuid4().hex}"

    # Watch channels can be up to 7 days (Google's max)
    # We set to 6 days to give buffer for renewal
    expiration_ms = int((timezone.now() + timedelta(days=6)).timestamp() * 1000)

    # Call Google Calendar API to create watch channel
    url = "https://www.googleapis.com/calendar/v3/calendars/primary/events/watch"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    body = {
        "id": channel_id,
        "type": "web_hook",
        "address": webhook_url,
        "expiration": str(expiration_ms),
    }

    logger.info(f"Creating watch channel for {user_email}: {channel_id}")

    response = requests.post(url, headers=headers, json=body, timeout=30)

    if response.status_code != 200:
        error_detail = response.json() if response.headers.get('content-type', '').startswith('application/json') else response.text
        raise Exception(f"Google API error {response.status_code}: {error_detail}")

    data = response.json()

    return {
        'channel_id': data['id'],
        'resource_id': data['resourceId'],
        'expiration': datetime.fromtimestamp(int(data['expiration']) / 1000, tz=dt_timezone.utc),
    }


def stop_watch_channel(channel: GoogleWatchChannel) -> bool:
    """
    Stop a Google Calendar watch channel.

    Returns True on success, False on failure.
    """
    try:
        access_token = get_access_token_for_user(channel.user_email)

        url = "https://www.googleapis.com/calendar/v3/channels/stop"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        body = {
            "id": channel.channel_id,
            "resourceId": channel.resource_id,
        }

        response = requests.post(url, headers=headers, json=body, timeout=30)

        # 204 = success, 404 = already stopped (which is fine)
        if response.status_code in [204, 404]:
            logger.info(f"Stopped watch channel {channel.channel_id} for {channel.user_email}")
            return True
        else:
            logger.warning(f"Failed to stop channel {channel.channel_id}: {response.status_code}")
            return False

    except Exception as e:
        logger.exception(f"Error stopping watch channel for {channel.user_email}: {e}")
        return False


class Command(BaseCommand):
    help = 'Manage Google Calendar watch channels for push notifications'

    def add_arguments(self, parser):
        parser.add_argument(
            '--setup',
            type=str,
            metavar='EMAIL',
            help='Setup watch channel for a specific user email',
        )
        parser.add_argument(
            '--setup-all',
            action='store_true',
            help='Setup watch channels for all pilot users',
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
            '--stop',
            type=str,
            metavar='EMAIL',
            help='Stop and delete watch channel for a specific user email',
        )
        parser.add_argument(
            '--stop-all',
            action='store_true',
            help='Stop and delete all watch channels',
        )

    def handle(self, *args, **options):
        if options['list']:
            self.list_channels()
        elif options['setup']:
            self.setup_channel(options['setup'])
        elif options['setup_all']:
            self.setup_all_channels()
        elif options['renew_expiring']:
            self.renew_expiring_channels()
        elif options['stop']:
            self.stop_channel(options['stop'])
        elif options['stop_all']:
            self.stop_all_channels()
        else:
            self.stdout.write('No action specified. Use --help for options.')

    def list_channels(self):
        """List all watch channels."""
        channels = GoogleWatchChannel.objects.all().order_by('expiration')

        if not channels.exists():
            self.stdout.write('No watch channels configured.')
            self.stdout.write('')
            self.stdout.write('To set up channels, run:')
            self.stdout.write('  python manage.py manage_watch_channels --setup-all')
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
                f'{channel.expiration.strftime("%Y-%m-%d %H:%M UTC"):<25} '
                f'{status}'
            )

        self.stdout.write('')

    def setup_channel(self, email: str):
        """Setup a new watch channel for a user."""
        self.stdout.write(f'Setting up watch channel for {email}...')

        # Check if channel already exists and is valid
        existing = GoogleWatchChannel.objects.filter(user_email=email).first()
        if existing and not existing.expires_soon:
            self.stdout.write(
                self.style.WARNING(
                    f'Valid channel already exists, expires: {existing.expiration}'
                )
            )
            return

        # Stop existing channel if any
        if existing:
            self.stdout.write(f'  Stopping existing channel...')
            stop_watch_channel(existing)
            existing.delete()

        # Find calendar for this user
        calendar = Calendar.objects.filter(
            deduplication_key__startswith=f"{email}-google"
        ).first()

        if not calendar:
            self.stdout.write(
                self.style.ERROR(f'No calendar found for {email}')
            )
            return

        try:
            # Create new watch channel
            channel_info = create_watch_channel(email)

            # Save to database
            GoogleWatchChannel.objects.create(
                user_email=email,
                channel_id=channel_info['channel_id'],
                resource_id=channel_info['resource_id'],
                expiration=channel_info['expiration'],
                calendar=calendar,
            )

            self.stdout.write(
                self.style.SUCCESS(
                    f'  Created channel: {channel_info["channel_id"][:20]}... '
                    f'expires {channel_info["expiration"].strftime("%Y-%m-%d %H:%M UTC")}'
                )
            )

        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f'  Failed to create channel: {e}')
            )

    def setup_all_channels(self):
        """Setup watch channels for all pilot users with calendars."""
        self.stdout.write('Setting up watch channels for all users...')
        self.stdout.write('')

        # Find all users with domain-wide calendars
        calendars = Calendar.objects.filter(
            deduplication_key__endswith='-google-domain',
            state=1,  # CONNECTED
        )

        if not calendars.exists():
            self.stdout.write(self.style.ERROR('No connected domain-wide calendars found.'))
            return

        success_count = 0
        fail_count = 0

        for calendar in calendars:
            # Extract email from deduplication_key (format: email-google-domain)
            email = calendar.deduplication_key.replace('-google-domain', '')

            self.stdout.write(f'\n{email}:')

            # Check if channel already exists and is valid
            existing = GoogleWatchChannel.objects.filter(user_email=email).first()
            if existing and not existing.expires_soon:
                self.stdout.write(
                    self.style.SUCCESS(
                        f'  Already has valid channel (expires {existing.expiration.strftime("%Y-%m-%d")})'
                    )
                )
                success_count += 1
                continue

            # Stop existing channel if any
            if existing:
                self.stdout.write(f'  Stopping expired/expiring channel...')
                stop_watch_channel(existing)
                existing.delete()

            try:
                # Create new watch channel
                channel_info = create_watch_channel(email)

                # Save to database
                GoogleWatchChannel.objects.create(
                    user_email=email,
                    channel_id=channel_info['channel_id'],
                    resource_id=channel_info['resource_id'],
                    expiration=channel_info['expiration'],
                    calendar=calendar,
                )

                self.stdout.write(
                    self.style.SUCCESS(
                        f'  Created channel (expires {channel_info["expiration"].strftime("%Y-%m-%d")})'
                    )
                )
                success_count += 1

            except Exception as e:
                self.stdout.write(self.style.ERROR(f'  Failed: {e}'))
                fail_count += 1

        self.stdout.write('')
        self.stdout.write(f'Summary: {success_count} success, {fail_count} failed')

    def renew_expiring_channels(self):
        """Renew all channels expiring within 48 hours."""
        threshold = timezone.now() + timedelta(hours=48)
        expiring = GoogleWatchChannel.objects.filter(expiration__lt=threshold)

        if not expiring.exists():
            self.stdout.write('No channels need renewal.')
            return

        self.stdout.write(f'Found {expiring.count()} channels to renew:')

        success_count = 0
        fail_count = 0

        for channel in expiring:
            self.stdout.write(f'\n{channel.user_email}:')

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

                self.stdout.write(
                    self.style.SUCCESS(
                        f'  Renewed (expires {channel_info["expiration"].strftime("%Y-%m-%d")})'
                    )
                )
                success_count += 1

            except Exception as e:
                self.stdout.write(self.style.ERROR(f'  Failed: {e}'))
                fail_count += 1

        self.stdout.write('')
        self.stdout.write(f'Summary: {success_count} renewed, {fail_count} failed')

    def stop_channel(self, email: str):
        """Stop a watch channel."""
        channel = GoogleWatchChannel.objects.filter(user_email=email).first()

        if not channel:
            self.stdout.write(self.style.ERROR(f'No channel found for {email}'))
            return

        if stop_watch_channel(channel):
            channel.delete()
            self.stdout.write(self.style.SUCCESS(f'Stopped and deleted channel for {email}'))
        else:
            # Delete anyway - channel may be already expired on Google's side
            channel.delete()
            self.stdout.write(self.style.WARNING(f'Channel deleted (may have already expired)'))

    def stop_all_channels(self):
        """Stop all watch channels."""
        channels = GoogleWatchChannel.objects.all()

        if not channels.exists():
            self.stdout.write('No channels to stop.')
            return

        self.stdout.write(f'Stopping {channels.count()} channels...')

        for channel in channels:
            stop_watch_channel(channel)
            channel.delete()
            self.stdout.write(f'  Stopped: {channel.user_email}')

        self.stdout.write(self.style.SUCCESS('All channels stopped.'))
