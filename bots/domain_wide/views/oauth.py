"""
OAuth flows for Google and Microsoft calendar integration.
"""
import logging
import os
from datetime import timedelta

import requests
from django.conf import settings
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views import View

from bots.models import Calendar
from bots.tasks.sync_calendar_task import enqueue_sync_calendar_task

logger = logging.getLogger(__name__)


class GoogleOAuthStart(View):
    """Initiate Google OAuth flow for individual calendar users."""

    def get(self, request):
        import urllib.parse

        client_id = (
            getattr(settings, 'GOOGLE_CLIENT_ID', None)
            or os.getenv('GOOGLE_CLIENT_ID')
        )
        redirect_uri = (
            getattr(settings, 'GOOGLE_REDIRECT_URI', None)
            or os.getenv('GOOGLE_REDIRECT_URI')
        )

        if not client_id or not redirect_uri:
            return render(request, 'domain_wide/error.html', {
                'error': 'Google OAuth not configured'
            }, status=500)

        # Build OAuth URL
        params = {
            'client_id': client_id,
            'redirect_uri': redirect_uri,
            'response_type': 'code',
            'scope': 'https://www.googleapis.com/auth/calendar.readonly https://www.googleapis.com/auth/userinfo.email',
            'access_type': 'offline',
            'prompt': 'consent',  # Force consent to get refresh token
        }

        oauth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urllib.parse.urlencode(params)}"
        return redirect(oauth_url)


class GoogleOAuthCallback(View):
    """Handle Google OAuth callback."""

    def get(self, request):
        from ..models import OAuthCredential
        from ..utils import encrypt_token

        code = request.GET.get('code')
        error = request.GET.get('error')

        if error:
            logger.warning(f"Google OAuth error: {error}")
            return render(request, 'domain_wide/error.html', {
                'error': f'Google authorization failed: {error}'
            }, status=400)

        if not code:
            return render(request, 'domain_wide/error.html', {
                'error': 'Missing authorization code'
            }, status=400)

        # Get OAuth config
        client_id = getattr(settings, 'GOOGLE_CLIENT_ID', None) or os.getenv('GOOGLE_CLIENT_ID')
        client_secret = getattr(settings, 'GOOGLE_CLIENT_SECRET', None) or os.getenv('GOOGLE_CLIENT_SECRET')
        redirect_uri = getattr(settings, 'GOOGLE_REDIRECT_URI', None) or os.getenv('GOOGLE_REDIRECT_URI')

        if not all([client_id, client_secret, redirect_uri]):
            return render(request, 'domain_wide/error.html', {
                'error': 'Google OAuth not fully configured'
            }, status=500)

        # Exchange code for tokens
        try:
            token_response = requests.post(
                'https://oauth2.googleapis.com/token',
                data={
                    'code': code,
                    'client_id': client_id,
                    'client_secret': client_secret,
                    'redirect_uri': redirect_uri,
                    'grant_type': 'authorization_code',
                },
                timeout=30
            )
            token_response.raise_for_status()
            tokens = token_response.json()
        except requests.RequestException as e:
            logger.exception(f"Failed to exchange Google auth code: {e}")
            return render(request, 'domain_wide/error.html', {
                'error': 'Failed to complete authorization'
            }, status=500)

        access_token = tokens.get('access_token')
        refresh_token = tokens.get('refresh_token')
        expires_in = tokens.get('expires_in', 3600)

        if not access_token:
            return render(request, 'domain_wide/error.html', {
                'error': 'No access token received'
            }, status=500)

        # Get user email from Google
        try:
            userinfo_response = requests.get(
                'https://www.googleapis.com/oauth2/v2/userinfo',
                headers={'Authorization': f'Bearer {access_token}'},
                timeout=30
            )
            userinfo_response.raise_for_status()
            userinfo = userinfo_response.json()
            email = userinfo.get('email', '').lower()
        except requests.RequestException as e:
            logger.exception(f"Failed to get Google user info: {e}")
            return render(request, 'domain_wide/error.html', {
                'error': 'Failed to verify user identity'
            }, status=500)

        if not email:
            return render(request, 'domain_wide/error.html', {
                'error': 'Could not retrieve email address'
            }, status=500)

        # Store encrypted tokens
        try:
            credential, created = OAuthCredential.objects.update_or_create(
                email=email,
                provider='google',
                defaults={
                    'access_token_encrypted': encrypt_token(access_token),
                    'refresh_token_encrypted': encrypt_token(refresh_token) if refresh_token else '',
                    'token_expiry': timezone.now() + timedelta(seconds=expires_in),
                    'scopes': ['calendar.readonly', 'userinfo.email'],
                }
            )
            logger.info(f"{'Created' if created else 'Updated'} Google OAuth credential for {email}")
        except Exception as e:
            logger.exception(f"Failed to store Google credentials: {e}")
            return render(request, 'domain_wide/error.html', {
                'error': 'Failed to save authorization'
            }, status=500)

        # Create or link calendar in Attendee
        try:
            calendar, cal_created = Calendar.objects.get_or_create(
                deduplication_key=f"{email}-google-oauth",
                defaults={
                    'platform': 'Google',
                    'calendar_type': Calendar.GOOGLE_OAUTH if hasattr(Calendar, 'GOOGLE_OAUTH') else 1,
                    'state': 1,  # ACTIVE
                }
            )
            credential.calendar = calendar
            credential.save(update_fields=['calendar'])

            if cal_created:
                logger.info(f"Created calendar for {email}")
                # Trigger initial sync
                enqueue_sync_calendar_task(calendar)
        except Exception as e:
            logger.exception(f"Failed to create calendar for {email}: {e}")
            # Non-fatal - credentials are saved

        # Success page
        return render(request, 'domain_wide/oauth_success.html', {
            'provider': 'Google',
            'email': email,
        })


class MicrosoftOAuthStart(View):
    """Initiate Microsoft OAuth flow for individual calendar users."""

    def get(self, request):
        import urllib.parse

        client_id = (
            getattr(settings, 'MICROSOFT_CLIENT_ID', None)
            or os.getenv('MICROSOFT_CLIENT_ID')
        )
        redirect_uri = (
            getattr(settings, 'MICROSOFT_REDIRECT_URI', None)
            or os.getenv('MICROSOFT_REDIRECT_URI')
        )
        tenant_id = (
            getattr(settings, 'MICROSOFT_TENANT_ID', None)
            or os.getenv('MICROSOFT_TENANT_ID', 'common')
        )

        if not client_id or not redirect_uri:
            return render(request, 'domain_wide/error.html', {
                'error': 'Microsoft OAuth not configured'
            }, status=500)

        # Build OAuth URL
        params = {
            'client_id': client_id,
            'redirect_uri': redirect_uri,
            'response_type': 'code',
            'scope': 'openid email profile Calendars.Read offline_access',
            'response_mode': 'query',
        }

        oauth_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize?{urllib.parse.urlencode(params)}"
        return redirect(oauth_url)


class MicrosoftOAuthCallback(View):
    """Handle Microsoft OAuth callback."""

    def get(self, request):
        from ..models import OAuthCredential
        from ..utils import encrypt_token

        code = request.GET.get('code')
        error = request.GET.get('error')
        error_description = request.GET.get('error_description', '')

        if error:
            logger.warning(f"Microsoft OAuth error: {error} - {error_description}")
            return render(request, 'domain_wide/error.html', {
                'error': f'Microsoft authorization failed: {error_description or error}'
            }, status=400)

        if not code:
            return render(request, 'domain_wide/error.html', {
                'error': 'Missing authorization code'
            }, status=400)

        # Get OAuth config
        client_id = getattr(settings, 'MICROSOFT_CLIENT_ID', None) or os.getenv('MICROSOFT_CLIENT_ID')
        client_secret = getattr(settings, 'MICROSOFT_CLIENT_SECRET', None) or os.getenv('MICROSOFT_CLIENT_SECRET')
        redirect_uri = getattr(settings, 'MICROSOFT_REDIRECT_URI', None) or os.getenv('MICROSOFT_REDIRECT_URI')
        tenant_id = getattr(settings, 'MICROSOFT_TENANT_ID', None) or os.getenv('MICROSOFT_TENANT_ID', 'common')

        if not all([client_id, client_secret, redirect_uri]):
            return render(request, 'domain_wide/error.html', {
                'error': 'Microsoft OAuth not fully configured'
            }, status=500)

        # Exchange code for tokens
        try:
            token_response = requests.post(
                f'https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token',
                data={
                    'code': code,
                    'client_id': client_id,
                    'client_secret': client_secret,
                    'redirect_uri': redirect_uri,
                    'grant_type': 'authorization_code',
                    'scope': 'openid email profile Calendars.Read offline_access',
                },
                timeout=30
            )
            token_response.raise_for_status()
            tokens = token_response.json()
        except requests.RequestException as e:
            logger.exception(f"Failed to exchange Microsoft auth code: {e}")
            return render(request, 'domain_wide/error.html', {
                'error': 'Failed to complete authorization'
            }, status=500)

        access_token = tokens.get('access_token')
        refresh_token = tokens.get('refresh_token')
        expires_in = tokens.get('expires_in', 3600)

        if not access_token:
            return render(request, 'domain_wide/error.html', {
                'error': 'No access token received'
            }, status=500)

        # Get user email from Microsoft Graph
        try:
            userinfo_response = requests.get(
                'https://graph.microsoft.com/v1.0/me',
                headers={'Authorization': f'Bearer {access_token}'},
                timeout=30
            )
            userinfo_response.raise_for_status()
            userinfo = userinfo_response.json()
            # Microsoft returns email in 'mail' or 'userPrincipalName'
            email = (userinfo.get('mail') or userinfo.get('userPrincipalName', '')).lower()
        except requests.RequestException as e:
            logger.exception(f"Failed to get Microsoft user info: {e}")
            return render(request, 'domain_wide/error.html', {
                'error': 'Failed to verify user identity'
            }, status=500)

        if not email:
            return render(request, 'domain_wide/error.html', {
                'error': 'Could not retrieve email address'
            }, status=500)

        # Store encrypted tokens
        try:
            credential, created = OAuthCredential.objects.update_or_create(
                email=email,
                provider='microsoft',
                defaults={
                    'access_token_encrypted': encrypt_token(access_token),
                    'refresh_token_encrypted': encrypt_token(refresh_token) if refresh_token else '',
                    'token_expiry': timezone.now() + timedelta(seconds=expires_in),
                    'scopes': ['Calendars.Read', 'offline_access'],
                }
            )
            logger.info(f"{'Created' if created else 'Updated'} Microsoft OAuth credential for {email}")
        except Exception as e:
            logger.exception(f"Failed to store Microsoft credentials: {e}")
            return render(request, 'domain_wide/error.html', {
                'error': 'Failed to save authorization'
            }, status=500)

        # Create or link calendar in Attendee
        try:
            calendar, cal_created = Calendar.objects.get_or_create(
                deduplication_key=f"{email}-microsoft-oauth",
                defaults={
                    'platform': 'Microsoft',
                    'calendar_type': Calendar.MICROSOFT_OAUTH if hasattr(Calendar, 'MICROSOFT_OAUTH') else 2,
                    'state': 1,  # ACTIVE
                }
            )
            credential.calendar = calendar
            credential.save(update_fields=['calendar'])

            if cal_created:
                logger.info(f"Created Microsoft calendar for {email}")
                # Trigger initial sync
                enqueue_sync_calendar_task(calendar)
        except Exception as e:
            logger.exception(f"Failed to create calendar for {email}: {e}")
            # Non-fatal - credentials are saved

        # Success page
        return render(request, 'domain_wide/oauth_success.html', {
            'provider': 'Microsoft',
            'email': email,
        })
