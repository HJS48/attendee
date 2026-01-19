"""
Domain-wide calendar integration views.

Includes:
- Health dashboard (no auth)
- Google Calendar push notification webhook
- Transcript viewer (future)
- OAuth flows (future)
"""
import logging
from datetime import timedelta, datetime
from django.views import View
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.db.models import Count, Subquery, OuterRef, Avg, F
from django.db.models.functions import TruncDate
from bots.models import (
    Bot, BotStates, Calendar, CalendarEvent, BotEvent, BotEventTypes,
    Recording, RecordingStates, RecordingTranscriptionStates
)
from bots.tasks.sync_calendar_task import sync_calendar

logger = logging.getLogger(__name__)


class HealthDashboardView(View):
    """Public health dashboard - no auth required."""

    def get(self, request):
        return render(request, 'domain_wide/dashboard.html')


class HealthSummaryAPI(View):
    """Health summary API - returns all-time stats."""

    def get(self, request):
        # All-time event stats
        total_events = CalendarEvent.objects.filter(is_deleted=False).count()

        events_with_url = CalendarEvent.objects.filter(
            is_deleted=False,
            meeting_url__isnull=False
        ).exclude(meeting_url='').count()

        # Unique events = unique (meeting_url, date) combinations
        unique_events = CalendarEvent.objects.filter(
            is_deleted=False,
            meeting_url__isnull=False
        ).exclude(meeting_url='').annotate(
            event_date=TruncDate('start_time')
        ).values('meeting_url', 'event_date').distinct().count()

        # Bot stats - active bots (for non-deleted events) and total (for cleanup monitoring)
        active_bots = Bot.objects.filter(
            calendar_event__isnull=False,
            calendar_event__is_deleted=False
        )
        active_bots_count = active_bots.count()
        total_bots_count = Bot.objects.count()

        # Bot state breakdown - only active bots
        bot_states = dict(active_bots.values_list('state').annotate(count=Count('id')))
        state_names = {s.value: s.label for s in BotStates}
        bot_states_named = {state_names.get(k, f'Unknown({k})'): v for k, v in bot_states.items()}

        # Coverage = unique events that have at least one bot / unique events
        # This is approximate - we count unique URL+date combos that have any bot
        unique_with_bot = CalendarEvent.objects.filter(
            is_deleted=False,
            meeting_url__isnull=False,
            bots__isnull=False
        ).exclude(meeting_url='').annotate(
            event_date=TruncDate('start_time')
        ).values('meeting_url', 'event_date').distinct().count()

        coverage_rate = round((unique_with_bot / unique_events * 100), 1) if unique_events > 0 else 0

        # Calendar status
        calendars = dict(Calendar.objects.values_list('state').annotate(count=Count('id')))

        return JsonResponse({
            'all_time': {
                'total_events': total_events,
                'events_with_url': events_with_url,
                'unique_events_deduped': unique_events,
                'unique_events_with_bot': unique_with_bot,
                'active_bots': active_bots_count,
                'total_bots': total_bots_count,
                'coverage_rate': coverage_rate,
            },
            'bot_states': bot_states_named,
            'bot_states_raw': bot_states,
            'calendars': calendars,
            'timestamp': timezone.now().isoformat(),
        })


class RecentFailuresAPI(View):
    """Recent failures with details - no auth required."""

    def get(self, request):
        from collections import Counter

        days = int(request.GET.get('days', 7))
        cutoff = timezone.now() - timedelta(days=days)

        failures = BotEvent.objects.filter(
            event_type__in=[BotEventTypes.FATAL_ERROR, BotEventTypes.COULD_NOT_JOIN],
            created_at__gte=cutoff
        ).select_related('bot').order_by('-created_at')[:50]

        sub_type_names = {
            1: 'Room Full',
            2: 'Not Started',
            3: 'Has Ended',
            4: 'Invalid URL',
            5: 'Not Found',
            6: 'Requires Sign In',
            7: 'Requires Passcode',
            8: 'Account Not Found',
            9: 'Request Denied',
            10: 'Generic Error',
            11: 'Waiting Room Timeout',
            12: 'Heartbeat Timeout',
        }

        subtype_counts = Counter()
        failures_list = []

        for f in failures:
            event_type_label = 'Could Not Join' if f.event_type == BotEventTypes.COULD_NOT_JOIN else 'Fatal Error'
            reason = sub_type_names.get(f.event_sub_type, f'Unknown ({f.event_sub_type})')

            subtype_counts[reason] += 1

            failures_list.append({
                'bot_id': str(f.bot.object_id),
                'meeting_url': f.bot.meeting_url,
                'event_type': f.event_type,
                'event_type_label': event_type_label,
                'event_sub_type': f.event_sub_type,
                'reason': reason,
                'timestamp': f.created_at.isoformat(),
            })

        return JsonResponse({
            'failures': failures_list,
            'summary': dict(subtype_counts),
            'total': len(failures_list),
        })


class PipelineStatusAPI(View):
    """Pipeline status for a specific date."""

    def get(self, request):
        date_str = request.GET.get('date')
        if date_str:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        else:
            target_date = timezone.now().date()

        # Unique events (deduped by URL) for the date
        events = CalendarEvent.objects.filter(
            start_time__date=target_date,
            is_deleted=False,
            meeting_url__isnull=False
        ).exclude(meeting_url='')
        unique_events = events.values('meeting_url').distinct().count()

        # Bots for the date - only active (non-deleted) events
        bots_for_date = Bot.objects.filter(
            join_at__date=target_date,
            calendar_event__isnull=False,
            calendar_event__is_deleted=False
        )
        bots_count = bots_for_date.count()

        # Bot health breakdown by state
        state_names = {s.value: s.label for s in BotStates}
        bots_by_state = dict(bots_for_date.values_list('state').annotate(count=Count('id')))
        bots_by_state_named = {state_names.get(k, f'Unknown({k})'): v for k, v in bots_by_state.items()}

        # Recordings for bots on this date
        recordings = Recording.objects.filter(bot__in=bots_for_date)
        recordings_total = recordings.count()
        recordings_complete = recordings.filter(state=RecordingStates.COMPLETE).count()
        recordings_failed = recordings.filter(state=RecordingStates.FAILED).count()

        # Transcriptions
        transcriptions_complete = recordings.filter(
            transcription_state=RecordingTranscriptionStates.COMPLETE
        ).count()
        transcriptions_failed = recordings.filter(
            transcription_state=RecordingTranscriptionStates.FAILED
        ).count()

        # Average durations (only for completed recordings with timestamps)
        completed_recordings = recordings.filter(
            state=RecordingStates.COMPLETE,
            started_at__isnull=False,
            completed_at__isnull=False
        )

        # Calculate average recording duration in minutes
        avg_recording_duration = None
        if completed_recordings.exists():
            durations = [
                (r.completed_at - r.started_at).total_seconds() / 60
                for r in completed_recordings
                if r.completed_at and r.started_at
            ]
            if durations:
                avg_recording_duration = round(sum(durations) / len(durations), 1)

        # Average meeting duration from calendar events (end_time - start_time)
        avg_meeting_duration = None
        events_with_end = events.filter(end_time__isnull=False)
        if events_with_end.exists():
            # Get unique meetings by URL
            seen_urls = set()
            durations = []
            for e in events_with_end:
                if e.meeting_url not in seen_urls:
                    seen_urls.add(e.meeting_url)
                    duration = (e.end_time - e.start_time).total_seconds() / 60
                    durations.append(duration)
            if durations:
                avg_meeting_duration = round(sum(durations) / len(durations), 1)

        return JsonResponse({
            'date': target_date.isoformat(),
            'unique_events': unique_events,
            'bots_created': bots_count,
            'bot_states': bots_by_state_named,
            'recordings': {
                'total': recordings_total,
                'complete': recordings_complete,
                'failed': recordings_failed,
            },
            'transcriptions': {
                'complete': transcriptions_complete,
                'failed': transcriptions_failed,
            },
            'avg_meeting_duration_mins': avg_meeting_duration,
            'avg_recording_duration_mins': avg_recording_duration,
        })


class EventsBotListAPI(View):
    """List unique events with URLs and their bots for a specific date."""

    def get(self, request):
        date_str = request.GET.get('date')
        if date_str:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        else:
            target_date = timezone.now().date()

        # Get events with URLs for the date, dedup by meeting_url
        events = CalendarEvent.objects.filter(
            start_time__date=target_date,
            is_deleted=False,
            meeting_url__isnull=False
        ).exclude(meeting_url='').select_related('calendar').prefetch_related('bots').order_by('start_time')

        state_names = {s.value: s.label for s in BotStates}

        # Deduplicate by meeting_url
        seen_urls = set()
        events_list = []
        for event in events:
            if event.meeting_url in seen_urls:
                continue
            seen_urls.add(event.meeting_url)

            # Get the bot for this meeting URL (may be linked to a different CalendarEvent)
            bot = Bot.objects.filter(
                meeting_url=event.meeting_url,
                join_at__date=target_date
            ).first()

            # Get calendar owner email from credentials or dedup key
            calendar_owner = event.calendar.deduplication_key.replace('-google-sa', '') if event.calendar.deduplication_key else 'unknown'

            # Attendees count
            attendees = event.attendees or []
            attendees_count = len(attendees) if isinstance(attendees, list) else 0

            events_list.append({
                'event_id': event.object_id,
                'time': event.start_time.strftime('%H:%M'),
                'start_time': event.start_time.isoformat(),
                'title': event.name or '(No title)',
                'meeting_url': event.meeting_url,
                'calendar_owner': calendar_owner,
                'bot_id': bot.object_id if bot else None,
                'bot_status': state_names.get(bot.state) if bot else None,
                'bot_status_raw': bot.state if bot else None,
                'attendees_count': attendees_count,
            })

        return JsonResponse({
            'date': target_date.isoformat(),
            'count': len(events_list),
            'events': events_list,
        })


# =============================================================================
# Google Calendar Push Notifications
# =============================================================================

@method_decorator(csrf_exempt, name='dispatch')
class GoogleCalendarWebhook(View):
    """
    Receive Google Calendar push notifications.

    Google sends POST requests when calendar events change.
    We look up the calendar by channel ID and trigger a sync.
    """

    def post(self, request):
        # Google sends these headers with push notifications
        channel_id = request.headers.get('X-Goog-Channel-Id')
        resource_state = request.headers.get('X-Goog-Resource-State')
        resource_id = request.headers.get('X-Goog-Resource-Id')

        logger.info(f"Google Calendar webhook: channel={channel_id}, state={resource_state}")

        # Respond immediately - Google expects quick response
        # Process async to avoid timeout

        # Ignore sync confirmations (sent when channel is created)
        if resource_state == 'sync':
            logger.debug("Ignoring sync confirmation")
            return HttpResponse('OK', status=200)

        if not channel_id:
            logger.warning("Missing channel_id in Google webhook")
            return HttpResponse('Missing channel ID', status=400)

        # Look up which calendar this webhook is for
        try:
            from .models import GoogleWatchChannel
            watch_channel = GoogleWatchChannel.objects.filter(channel_id=channel_id).first()

            if not watch_channel:
                logger.warning(f"Unknown channel {channel_id}, ignoring webhook")
                return HttpResponse('OK', status=200)

            # Get the calendar and trigger sync
            calendar = watch_channel.calendar
            if calendar:
                logger.info(f"Triggering sync for {watch_channel.user_email} (calendar {calendar.object_id})")
                # Trigger async sync task
                sync_calendar.delay(str(calendar.pk))
            else:
                logger.warning(f"No calendar linked to watch channel for {watch_channel.user_email}")

        except Exception as e:
            logger.exception(f"Error processing Google webhook: {e}")
            # Still return 200 to prevent Google from retrying
            return HttpResponse('OK', status=200)

        return HttpResponse('OK', status=200)


# =============================================================================
# Transcript Viewer
# =============================================================================

class TranscriptView(View):
    """
    Display meeting transcript for authorized participants.

    Access via token from email link or session auth.
    Fetches transcript from Supabase meetings table.
    """

    def get(self, request, meeting_id):
        from .utils import verify_transcript_token, is_valid_uuid
        from .supabase_client import get_meeting, get_meeting_insights, get_attendee_emails_for_meeting

        # Validate meeting ID format
        if not is_valid_uuid(meeting_id):
            return render(request, 'domain_wide/error.html', {
                'error': 'Invalid meeting ID'
            }, status=400)

        # Authenticate via token or session
        token = request.GET.get('token')
        user_email = None

        if token:
            token_data = verify_transcript_token(token)
            if not token_data:
                return render(request, 'domain_wide/error.html', {
                    'error': 'Invalid or expired access link'
                }, status=401)
            if token_data.get('meetingId') != meeting_id:
                return render(request, 'domain_wide/error.html', {
                    'error': 'Access link does not match this meeting'
                }, status=403)
            user_email = token_data.get('email', '').lower()
        elif request.user.is_authenticated:
            user_email = request.user.email.lower()
        else:
            return render(request, 'domain_wide/error.html', {
                'error': 'Access denied - use the link from your email'
            }, status=401)

        # Fetch meeting from Supabase
        meeting = get_meeting(meeting_id)
        if not meeting:
            return render(request, 'domain_wide/error.html', {
                'error': 'Meeting not found'
            }, status=404)

        # Authorization: check if user is participant or organizer
        attendees = get_attendee_emails_for_meeting(meeting.get('meeting_url', ''))
        attendee_emails = [
            a.get('email', '').lower() for a in attendees
            if isinstance(a, dict) and a.get('email')
        ]

        organizer_email = (meeting.get('organizer_email') or '').lower()
        is_participant = user_email in attendee_emails
        is_organizer = user_email == organizer_email

        if not is_participant and not is_organizer:
            return render(request, 'domain_wide/error.html', {
                'error': 'Access denied - you must be a meeting participant'
            }, status=403)

        # Fetch insights
        insights = get_meeting_insights(meeting_id)
        summary = insights[0].get('summary', '') if insights else ''
        action_items = []
        if insights and insights[0].get('action_items'):
            action_items = insights[0]['action_items'].get('items', [])

        # Build context for template
        transcript = meeting.get('transcript') or []
        participants = meeting.get('participants') or []

        # Format timestamps for each transcript segment
        for seg in transcript:
            start_ms = seg.get('start_ms', 0)
            total_seconds = start_ms // 1000
            minutes = total_seconds // 60
            seconds = total_seconds % 60
            seg['formatted_time'] = f"[{minutes:02d}:{seconds:02d}]"

        # Get unique speakers and assign colors
        speakers = list(set(seg.get('speaker_name', 'Unknown') for seg in transcript))
        speaker_colors = {speaker: idx % 8 for idx, speaker in enumerate(speakers)}

        # Add speaker color to each segment for template
        for seg in transcript:
            speaker = seg.get('speaker_name', 'Unknown')
            seg['speaker_color'] = speaker_colors.get(speaker, 0)

        # Format duration
        duration_seconds = meeting.get('duration_seconds', 0)
        if duration_seconds:
            mins = duration_seconds // 60
            hrs = mins // 60
            duration_display = f"{hrs}h {mins % 60}m" if hrs else f"{mins} minutes"
        else:
            duration_display = "Unknown duration"

        # Format participant names
        participant_names = [p.get('name', 'Unknown') for p in participants[:5]]
        if len(participants) > 5:
            participant_names.append(f"+{len(participants) - 5} more")

        context = {
            'meeting': meeting,
            'meeting_id': meeting_id,
            'title': meeting.get('title', 'Untitled Meeting'),
            'started_at': meeting.get('started_at'),
            'duration': duration_display,
            'participant_names': ', '.join(participant_names),
            'recording_url': meeting.get('recording_url', ''),
            'summary': summary,
            'action_items': action_items,
            'transcript': transcript,
            'speaker_colors': speaker_colors,
        }

        return render(request, 'domain_wide/transcript.html', context)


# =============================================================================
# Google OAuth
# =============================================================================

class GoogleOAuthStart(View):
    """Initiate Google OAuth flow for individual calendar users."""

    def get(self, request):
        import os
        import urllib.parse
        from django.shortcuts import redirect
        from django.conf import settings

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
        import os
        import requests
        from django.conf import settings
        from .models import OAuthCredential
        from .utils import encrypt_token

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
                sync_calendar.delay(str(calendar.pk))
        except Exception as e:
            logger.exception(f"Failed to create calendar for {email}: {e}")
            # Non-fatal - credentials are saved

        # Success page
        return render(request, 'domain_wide/oauth_success.html', {
            'provider': 'Google',
            'email': email,
        })


# =============================================================================
# Microsoft OAuth
# =============================================================================

class MicrosoftOAuthStart(View):
    """Initiate Microsoft OAuth flow for individual calendar users."""

    def get(self, request):
        import os
        import urllib.parse
        from django.shortcuts import redirect
        from django.conf import settings

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
        import os
        import requests
        from django.conf import settings
        from .models import OAuthCredential
        from .utils import encrypt_token

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
                sync_calendar.delay(str(calendar.pk))
        except Exception as e:
            logger.exception(f"Failed to create calendar for {email}: {e}")
            # Non-fatal - credentials are saved

        # Success page
        return render(request, 'domain_wide/oauth_success.html', {
            'provider': 'Microsoft',
            'email': email,
        })


# =============================================================================
# Debugging Dashboard APIs
# =============================================================================

class CalendarSyncHealthAPI(View):
    """API for calendar sync health status."""

    def get(self, request):
        from bots.models import CalendarStates

        calendars = Calendar.objects.all().order_by('-last_successful_sync_at')

        calendars_data = []
        for cal in calendars:
            # Determine state
            if cal.state == CalendarStates.CONNECTED:
                state = 'connected'
            else:
                state = 'disconnected'

            # Calculate sync age
            sync_age_minutes = None
            if cal.last_successful_sync_at:
                delta = timezone.now() - cal.last_successful_sync_at
                sync_age_minutes = int(delta.total_seconds() / 60)

            # Extract error details
            error = None
            first_failure = None
            days_disconnected = None

            if cal.connection_failure_data:
                error = cal.connection_failure_data.get('error', str(cal.connection_failure_data))
                failure_time = cal.connection_failure_data.get('first_failure_at')
                if failure_time:
                    first_failure = failure_time
                    try:
                        from dateutil.parser import parse as parse_date
                        failure_dt = parse_date(failure_time)
                        days_disconnected = (timezone.now() - failure_dt).days
                    except Exception:
                        pass

            # Get calendar owner from deduplication_key
            owner = cal.deduplication_key
            if owner:
                owner = owner.replace('-google-sa', '').replace('-google-oauth', '').replace('-microsoft-oauth', '')

            cal_data = {
                'id': cal.object_id,
                'owner': owner,
                'state': state,
                'last_sync': cal.last_successful_sync_at.isoformat() if cal.last_successful_sync_at else None,
                'sync_age_minutes': sync_age_minutes,
            }

            if state == 'disconnected':
                cal_data['error'] = error
                cal_data['first_failure'] = first_failure
                cal_data['days_disconnected'] = days_disconnected

            calendars_data.append(cal_data)

        return JsonResponse({'calendars': calendars_data})


def _init_kubernetes_client():
    """Initialize Kubernetes client with proper configuration."""
    import os
    from kubernetes import client, config

    # Load config
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()

    # Allow skipping TLS verification for dev environments
    if os.getenv('KUBERNETES_SKIP_TLS_VERIFY', '').lower() == 'true':
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        configuration = client.Configuration.get_default_copy()
        configuration.verify_ssl = False
        client.Configuration.set_default(configuration)

    return client.CoreV1Api()


class InfrastructureStatusAPI(View):
    """API for infrastructure status (containers/kubernetes + Celery)."""

    def _detect_mode(self):
        """Detect if running in Kubernetes or Docker mode."""
        import os
        # Allow explicit override via environment variable (useful for testing)
        force_mode = os.getenv('INFRASTRUCTURE_MODE')
        if force_mode in ('kubernetes', 'docker'):
            return force_mode
        # Check for Kubernetes service account (present when running in K8s)
        if os.path.exists('/var/run/secrets/kubernetes.io/serviceaccount/token'):
            return 'kubernetes'
        # Check for KUBERNETES_SERVICE_HOST env var
        if os.getenv('KUBERNETES_SERVICE_HOST'):
            return 'kubernetes'
        return 'docker'

    def _get_kubernetes_status(self):
        """Get Kubernetes cluster status."""
        import time
        from django.conf import settings
        from kubernetes import client

        try:
            v1 = _init_kubernetes_client()

            # Measure API latency
            start_time = time.time()
            v1.list_namespace(limit=1)
            api_latency_ms = int((time.time() - start_time) * 1000)

            # Get namespaces to monitor
            namespaces_to_check = [
                getattr(settings, 'BOT_POD_NAMESPACE', 'attendee'),
                getattr(settings, 'WEBPAGE_STREAMER_POD_NAMESPACE', 'attendee-webpage-streamer'),
            ]

            namespace_data = []
            for ns in namespaces_to_check:
                try:
                    pods = v1.list_namespaced_pod(namespace=ns)
                    pod_counts = {'total': 0, 'running': 0, 'pending': 0, 'failed': 0, 'succeeded': 0}
                    for pod in pods.items:
                        pod_counts['total'] += 1
                        phase = (pod.status.phase or '').lower()
                        if phase == 'running':
                            pod_counts['running'] += 1
                        elif phase == 'pending':
                            pod_counts['pending'] += 1
                        elif phase == 'failed':
                            pod_counts['failed'] += 1
                        elif phase == 'succeeded':
                            pod_counts['succeeded'] += 1
                    namespace_data.append({'name': ns, 'pods': pod_counts})
                except client.ApiException as e:
                    logger.warning(f"Failed to get pods for namespace {ns}: {e}")
                    namespace_data.append({'name': ns, 'pods': None, 'error': str(e)})

            # Get node status
            nodes = v1.list_node()
            node_counts = {'total': 0, 'ready': 0, 'not_ready': 0}
            cpu_allocatable = 0
            cpu_requested = 0
            memory_allocatable = 0
            memory_requested = 0

            for node in nodes.items:
                node_counts['total'] += 1
                is_ready = False
                for condition in (node.status.conditions or []):
                    if condition.type == 'Ready':
                        is_ready = condition.status == 'True'
                        break
                if is_ready:
                    node_counts['ready'] += 1
                else:
                    node_counts['not_ready'] += 1

                # Resource tracking
                allocatable = node.status.allocatable or {}
                cpu_str = allocatable.get('cpu', '0')
                mem_str = allocatable.get('memory', '0')
                cpu_allocatable += self._parse_cpu(cpu_str)
                memory_allocatable += self._parse_memory(mem_str)

            # Get resource requests from pods
            for ns in namespaces_to_check:
                try:
                    pods = v1.list_namespaced_pod(namespace=ns)
                    for pod in pods.items:
                        if pod.status.phase not in ['Running', 'Pending']:
                            continue
                        for container in (pod.spec.containers or []):
                            requests = (container.resources.requests or {}) if container.resources else {}
                            cpu_requested += self._parse_cpu(requests.get('cpu', '0'))
                            memory_requested += self._parse_memory(requests.get('memory', '0'))
                except Exception:
                    pass

            return {
                'api_healthy': True,
                'api_latency_ms': api_latency_ms,
                'namespaces': namespace_data,
                'nodes': node_counts,
                'resource_usage': {
                    'cpu_requested_millicores': cpu_requested,
                    'cpu_allocatable_millicores': cpu_allocatable,
                    'memory_requested_bytes': memory_requested,
                    'memory_allocatable_bytes': memory_allocatable,
                }
            }

        except Exception as e:
            logger.exception(f"Failed to get Kubernetes status: {e}")
            return {
                'api_healthy': False,
                'api_latency_ms': None,
                'error': str(e),
                'namespaces': [],
                'nodes': {'total': 0, 'ready': 0, 'not_ready': 0},
                'resource_usage': None
            }

    def _parse_cpu(self, cpu_str):
        """Parse CPU string to millicores."""
        if not cpu_str:
            return 0
        cpu_str = str(cpu_str)
        if cpu_str.endswith('m'):
            return int(cpu_str[:-1])
        elif cpu_str.endswith('n'):
            return int(cpu_str[:-1]) // 1000000
        else:
            try:
                return int(float(cpu_str) * 1000)
            except ValueError:
                return 0

    def _parse_memory(self, mem_str):
        """Parse memory string to bytes."""
        if not mem_str:
            return 0
        mem_str = str(mem_str)
        multipliers = {
            'Ki': 1024, 'Mi': 1024**2, 'Gi': 1024**3, 'Ti': 1024**4,
            'K': 1000, 'M': 1000**2, 'G': 1000**3, 'T': 1000**4,
        }
        for suffix, mult in multipliers.items():
            if mem_str.endswith(suffix):
                try:
                    return int(float(mem_str[:-len(suffix)]) * mult)
                except ValueError:
                    return 0
        try:
            return int(mem_str)
        except ValueError:
            return 0

    def _get_docker_containers(self):
        """Get Docker container status."""
        containers = []
        try:
            import docker
            client = docker.from_env()

            for container in client.containers.list(all=True):
                name = container.name
                # Filter to relevant containers
                if any(x in name.lower() for x in ['attendee', 'worker', 'scheduler', 'redis', 'postgres']):
                    status = container.status
                    running = status == 'running'

                    # Get uptime from container attrs
                    uptime = None
                    if running:
                        try:
                            started_at = container.attrs.get('State', {}).get('StartedAt', '')
                            if started_at:
                                from dateutil.parser import parse as parse_date
                                start_time = parse_date(started_at)
                                delta = timezone.now() - start_time
                                days = delta.days
                                hours = delta.seconds // 3600
                                if days > 0:
                                    uptime = f"{days} day{'s' if days != 1 else ''}"
                                elif hours > 0:
                                    uptime = f"{hours} hour{'s' if hours != 1 else ''}"
                                else:
                                    mins = delta.seconds // 60
                                    uptime = f"{mins} min{'s' if mins != 1 else ''}"
                        except Exception:
                            uptime = "Unknown"

                    # Simplify container name
                    simple_name = name
                    for prefix in ['attendee-attendee-', 'attendee-', 'meetings-']:
                        if simple_name.startswith(prefix):
                            simple_name = simple_name[len(prefix):]
                    for suffix in ['-local-1', '-1']:
                        if simple_name.endswith(suffix):
                            simple_name = simple_name[:-len(suffix)]

                    containers.append({
                        'name': simple_name,
                        'status': 'running' if running else 'stopped',
                        'uptime': uptime,
                    })

            client.close()
        except Exception as e:
            logger.warning(f"Failed to get container status: {e}")
            containers = [{'name': 'docker', 'status': 'unavailable', 'uptime': None}]

        return containers

    def _get_celery_status(self):
        """Get Celery worker and queue status."""
        celery_status = {
            'workers': 0,
            'active_tasks': 0,
            'pending_tasks': 0,
            'failed_recent': 0,
            'retrying': 0,
        }

        try:
            from attendee.celery import app as celery_app

            # Inspect workers
            inspect = celery_app.control.inspect()

            # Active workers
            active_workers = inspect.active()
            if active_workers:
                celery_status['workers'] = len(active_workers)
                celery_status['active_tasks'] = sum(len(tasks) for tasks in active_workers.values())

            # Reserved (pending) tasks
            reserved = inspect.reserved()
            if reserved:
                celery_status['pending_tasks'] = sum(len(tasks) for tasks in reserved.values())

            # Get failed task count from last 24h using Celery events or Redis
            # This is a simplified version - full implementation would use flower or celery events
            try:
                # Try to get queue length from Redis
                import redis
                r = redis.from_url('redis://localhost:6379/0')
                celery_status['pending_tasks'] = r.llen('celery')
            except Exception:
                pass

        except Exception as e:
            logger.warning(f"Failed to get Celery status: {e}")

        return celery_status

    def get(self, request):
        mode = self._detect_mode()
        celery_status = self._get_celery_status()

        response_data = {
            'mode': mode,
            'celery': celery_status,
        }

        if mode == 'kubernetes':
            response_data['kubernetes'] = self._get_kubernetes_status()
        else:
            response_data['containers'] = self._get_docker_containers()

        return JsonResponse(response_data)


class KubernetesPodsAPI(View):
    """API for listing all bot pods with detailed status."""

    def get(self, request):
        from django.conf import settings
        from kubernetes import client

        try:
            v1 = _init_kubernetes_client()

            namespaces = [
                getattr(settings, 'BOT_POD_NAMESPACE', 'attendee'),
                getattr(settings, 'WEBPAGE_STREAMER_POD_NAMESPACE', 'attendee-webpage-streamer'),
            ]

            pods_list = []
            summary = {
                'total': 0,
                'by_phase': {},
                'by_issue': {'CrashLoopBackOff': 0, 'ImagePullBackOff': 0, 'OOMKilled': 0, 'Pending': 0}
            }

            for ns in namespaces:
                try:
                    pods = v1.list_namespaced_pod(namespace=ns)
                    for pod in pods.items:
                        summary['total'] += 1
                        phase = pod.status.phase or 'Unknown'
                        summary['by_phase'][phase] = summary['by_phase'].get(phase, 0) + 1

                        # Calculate age
                        age_seconds = None
                        if pod.metadata.creation_timestamp:
                            age_seconds = int((timezone.now() - pod.metadata.creation_timestamp).total_seconds())

                        # Extract bot_id from pod name (format: bot-{id}-{uuid})
                        bot_id = None
                        pod_name = pod.metadata.name
                        if pod_name.startswith('bot-'):
                            parts = pod_name.split('-')
                            if len(parts) >= 2:
                                # Try to find bot_xxx pattern
                                for i, part in enumerate(parts):
                                    if part.startswith('bot_'):
                                        bot_id = part
                                        break

                        # Container statuses
                        container_statuses = []
                        for cs in (pod.status.container_statuses or []):
                            state = 'unknown'
                            reason = None

                            if cs.state:
                                if cs.state.running:
                                    state = 'running'
                                elif cs.state.waiting:
                                    state = 'waiting'
                                    reason = cs.state.waiting.reason
                                    # Track issues
                                    if reason == 'CrashLoopBackOff':
                                        summary['by_issue']['CrashLoopBackOff'] += 1
                                    elif reason in ['ImagePullBackOff', 'ErrImagePull']:
                                        summary['by_issue']['ImagePullBackOff'] += 1
                                elif cs.state.terminated:
                                    state = 'terminated'
                                    reason = cs.state.terminated.reason
                                    if reason == 'OOMKilled':
                                        summary['by_issue']['OOMKilled'] += 1

                            container_statuses.append({
                                'name': cs.name,
                                'ready': cs.ready,
                                'restart_count': cs.restart_count,
                                'state': state,
                                'reason': reason,
                            })

                        # Track pending pods
                        if phase == 'Pending' and age_seconds and age_seconds > 300:
                            summary['by_issue']['Pending'] += 1

                        pods_list.append({
                            'name': pod_name,
                            'namespace': ns,
                            'phase': phase,
                            'bot_id': bot_id,
                            'node': pod.spec.node_name,
                            'age_seconds': age_seconds,
                            'container_statuses': container_statuses,
                        })

                except client.ApiException as e:
                    logger.warning(f"Failed to list pods in namespace {ns}: {e}")

            return JsonResponse({
                'pods': pods_list,
                'summary': summary,
            })

        except Exception as e:
            logger.exception(f"Failed to get Kubernetes pods: {e}")
            return JsonResponse({
                'error': str(e),
                'pods': [],
                'summary': {'total': 0, 'by_phase': {}, 'by_issue': {}}
            }, status=500)


class KubernetesAlertsAPI(View):
    """API for generating alerts from current cluster state."""

    def get(self, request):
        from django.conf import settings
        from kubernetes import client

        alerts = []

        try:
            v1 = _init_kubernetes_client()

            # Check API health
            try:
                v1.list_namespace(limit=1)
            except Exception as e:
                alerts.append({
                    'severity': 'critical',
                    'type': 'api_unreachable',
                    'message': f'Kubernetes API unreachable: {str(e)[:100]}',
                    'resource': 'k8s-api',
                })
                return JsonResponse({
                    'alerts': alerts,
                    'summary': {'critical': 1, 'warning': 0, 'info': 0}
                })

            # Check nodes
            nodes = v1.list_node()
            for node in nodes.items:
                node_name = node.metadata.name
                for condition in (node.status.conditions or []):
                    if condition.type == 'Ready' and condition.status != 'True':
                        alerts.append({
                            'severity': 'critical',
                            'type': 'node_not_ready',
                            'message': f'Node {node_name} is NotReady: {condition.reason}',
                            'resource': node_name,
                        })
                    elif condition.type in ['DiskPressure', 'MemoryPressure', 'PIDPressure']:
                        if condition.status == 'True':
                            alerts.append({
                                'severity': 'warning',
                                'type': 'node_pressure',
                                'message': f'Node {node_name} has {condition.type}',
                                'resource': node_name,
                            })

            # Check pods
            namespaces = [
                getattr(settings, 'BOT_POD_NAMESPACE', 'attendee'),
                getattr(settings, 'WEBPAGE_STREAMER_POD_NAMESPACE', 'attendee-webpage-streamer'),
            ]

            cpu_requested = 0
            cpu_allocatable = 0
            memory_requested = 0
            memory_allocatable = 0

            # Get allocatable resources from nodes
            for node in nodes.items:
                allocatable = node.status.allocatable or {}
                cpu_allocatable += self._parse_cpu(allocatable.get('cpu', '0'))
                memory_allocatable += self._parse_memory(allocatable.get('memory', '0'))

            for ns in namespaces:
                try:
                    pods = v1.list_namespaced_pod(namespace=ns)
                    for pod in pods.items:
                        pod_name = pod.metadata.name
                        phase = pod.status.phase

                        # Track resource requests
                        if phase in ['Running', 'Pending']:
                            for container in (pod.spec.containers or []):
                                requests = (container.resources.requests or {}) if container.resources else {}
                                cpu_requested += self._parse_cpu(requests.get('cpu', '0'))
                                memory_requested += self._parse_memory(requests.get('memory', '0'))

                        # Check for evicted/failed pods
                        if phase == 'Failed':
                            reason = pod.status.reason or 'Unknown'
                            alerts.append({
                                'severity': 'warning',
                                'type': 'pod_failed',
                                'message': f'Pod {pod_name} failed: {reason}',
                                'resource': pod_name,
                            })

                        # Check container statuses
                        for cs in (pod.status.container_statuses or []):
                            # CrashLoopBackOff
                            if cs.restart_count > 3:
                                alerts.append({
                                    'severity': 'critical',
                                    'type': 'pod_crash_loop',
                                    'message': f'Pod {pod_name} container {cs.name} restarted {cs.restart_count} times',
                                    'resource': pod_name,
                                })

                            if cs.state:
                                # ImagePullBackOff
                                if cs.state.waiting:
                                    reason = cs.state.waiting.reason
                                    if reason in ['ImagePullBackOff', 'ErrImagePull']:
                                        alerts.append({
                                            'severity': 'critical',
                                            'type': 'pod_image_pull_error',
                                            'message': f'Pod {pod_name} has {reason}',
                                            'resource': pod_name,
                                        })

                                # OOMKilled
                                if cs.state.terminated and cs.state.terminated.reason == 'OOMKilled':
                                    alerts.append({
                                        'severity': 'warning',
                                        'type': 'pod_oom_killed',
                                        'message': f'Pod {pod_name} container {cs.name} was OOMKilled',
                                        'resource': pod_name,
                                    })

                        # Pod pending too long (>5 min)
                        if phase == 'Pending' and pod.metadata.creation_timestamp:
                            age_seconds = (timezone.now() - pod.metadata.creation_timestamp).total_seconds()
                            if age_seconds > 300:
                                age_mins = int(age_seconds / 60)
                                alerts.append({
                                    'severity': 'warning',
                                    'type': 'pod_pending_long',
                                    'message': f'Pod {pod_name} pending for {age_mins} minutes',
                                    'resource': pod_name,
                                })

                except client.ApiException as e:
                    logger.warning(f"Failed to check pods in namespace {ns}: {e}")

            # Check resource exhaustion (>85%)
            if cpu_allocatable > 0:
                cpu_pct = (cpu_requested / cpu_allocatable) * 100
                if cpu_pct > 85:
                    alerts.append({
                        'severity': 'warning',
                        'type': 'resource_exhaustion',
                        'message': f'CPU usage at {cpu_pct:.1f}% of allocatable',
                        'resource': 'cluster',
                    })

            if memory_allocatable > 0:
                mem_pct = (memory_requested / memory_allocatable) * 100
                if mem_pct > 85:
                    alerts.append({
                        'severity': 'warning',
                        'type': 'resource_exhaustion',
                        'message': f'Memory usage at {mem_pct:.1f}% of allocatable',
                        'resource': 'cluster',
                    })

            # Count alerts by severity
            summary = {'critical': 0, 'warning': 0, 'info': 0}
            for alert in alerts:
                severity = alert.get('severity', 'info')
                summary[severity] = summary.get(severity, 0) + 1

            return JsonResponse({
                'alerts': alerts,
                'summary': summary,
            })

        except Exception as e:
            logger.exception(f"Failed to generate Kubernetes alerts: {e}")
            return JsonResponse({
                'error': str(e),
                'alerts': [],
                'summary': {'critical': 0, 'warning': 0, 'info': 0}
            }, status=500)

    def _parse_cpu(self, cpu_str):
        """Parse CPU string to millicores."""
        if not cpu_str:
            return 0
        cpu_str = str(cpu_str)
        if cpu_str.endswith('m'):
            return int(cpu_str[:-1])
        elif cpu_str.endswith('n'):
            return int(cpu_str[:-1]) // 1000000
        else:
            try:
                return int(float(cpu_str) * 1000)
            except ValueError:
                return 0

    def _parse_memory(self, mem_str):
        """Parse memory string to bytes."""
        if not mem_str:
            return 0
        mem_str = str(mem_str)
        multipliers = {
            'Ki': 1024, 'Mi': 1024**2, 'Gi': 1024**3, 'Ti': 1024**4,
            'K': 1000, 'M': 1000**2, 'G': 1000**3, 'T': 1000**4,
        }
        for suffix, mult in multipliers.items():
            if mem_str.endswith(suffix):
                try:
                    return int(float(mem_str[:-len(suffix)]) * mult)
                except ValueError:
                    return 0
        try:
            return int(mem_str)
        except ValueError:
            return 0


class KubernetesBotLookupAPI(View):
    """API for cross-referencing bot database record with Kubernetes pod."""

    def get(self, request):
        from django.conf import settings

        bot_id = request.GET.get('bot_id')
        pod_name = request.GET.get('pod_name')

        if not bot_id and not pod_name:
            return JsonResponse({
                'error': 'Either bot_id or pod_name is required',
                'found': False,
            }, status=400)

        result = {'found': False, 'bot': None, 'pod': None, 'events': []}

        # Look up bot from database
        bot = None
        if bot_id:
            bot = Bot.objects.filter(object_id=bot_id).first()
        elif pod_name:
            # Try to extract bot_id from pod name
            # Pod name format is like: bot-123-bot_abc123
            if 'bot_' in pod_name:
                extracted_id = pod_name[pod_name.index('bot_'):]
                # Remove any trailing parts after the bot ID
                if '-' in extracted_id:
                    extracted_id = extracted_id.split('-')[0]
                bot = Bot.objects.filter(object_id=extracted_id).first()

        if bot:
            result['found'] = True

            # Calculate heartbeat age
            heartbeat_age_seconds = None
            if bot.last_heartbeat_timestamp:
                heartbeat_age_seconds = int((timezone.now() - bot.last_heartbeat_timestamp).total_seconds())

            state_names = {s.value: s.label for s in BotStates}

            result['bot'] = {
                'object_id': bot.object_id,
                'state': state_names.get(bot.state, f'Unknown({bot.state})'),
                'state_raw': bot.state,
                'meeting_url': bot.meeting_url,
                'last_heartbeat': bot.last_heartbeat_timestamp.isoformat() if bot.last_heartbeat_timestamp else None,
                'heartbeat_age_seconds': heartbeat_age_seconds,
                'join_at': bot.join_at.isoformat() if bot.join_at else None,
            }

            # Get recent bot events
            recent_events = BotEvent.objects.filter(bot=bot).order_by('-created_at')[:10]
            event_type_names = {e.value: e.label for e in BotEventTypes}
            result['events'] = [
                {
                    'type': event_type_names.get(e.event_type, f'Unknown({e.event_type})'),
                    'sub_type': e.event_sub_type,
                    'timestamp': e.created_at.isoformat(),
                }
                for e in recent_events
            ]

        # Look up pod from Kubernetes
        try:
            from kubernetes import client
            v1 = _init_kubernetes_client()

            namespaces = [
                getattr(settings, 'BOT_POD_NAMESPACE', 'attendee'),
                getattr(settings, 'WEBPAGE_STREAMER_POD_NAMESPACE', 'attendee-webpage-streamer'),
            ]

            target_pod = None
            target_ns = None

            # Search for pod
            for ns in namespaces:
                try:
                    if pod_name:
                        # Direct lookup by pod name
                        try:
                            target_pod = v1.read_namespaced_pod(name=pod_name, namespace=ns)
                            target_ns = ns
                            break
                        except client.ApiException:
                            continue
                    elif bot_id:
                        # Search pods for matching bot_id
                        pods = v1.list_namespaced_pod(namespace=ns)
                        for pod in pods.items:
                            if bot_id in pod.metadata.name:
                                target_pod = pod
                                target_ns = ns
                                break
                        if target_pod:
                            break
                except client.ApiException as e:
                    logger.warning(f"Failed to search pods in namespace {ns}: {e}")

            if target_pod:
                result['found'] = True

                # Get pod events
                pod_events = []
                try:
                    events = v1.list_namespaced_event(
                        namespace=target_ns,
                        field_selector=f'involvedObject.name={target_pod.metadata.name}'
                    )
                    for event in events.items[-10:]:  # Last 10 events
                        pod_events.append({
                            'type': event.type,
                            'reason': event.reason,
                            'message': event.message,
                            'timestamp': event.last_timestamp.isoformat() if event.last_timestamp else None,
                        })
                except Exception as e:
                    logger.warning(f"Failed to get pod events: {e}")

                result['pod'] = {
                    'name': target_pod.metadata.name,
                    'namespace': target_ns,
                    'phase': target_pod.status.phase,
                    'node': target_pod.spec.node_name,
                    'created': target_pod.metadata.creation_timestamp.isoformat() if target_pod.metadata.creation_timestamp else None,
                    'events': pod_events,
                }

        except Exception as e:
            logger.warning(f"Failed to look up Kubernetes pod: {e}")
            result['pod_error'] = str(e)

        return JsonResponse(result)


class KubernetesNodesAPI(View):
    """API for node health details and capacity planning."""

    def get(self, request):
        from django.conf import settings
        from kubernetes import client

        try:
            v1 = _init_kubernetes_client()

            nodes_list = []
            nodes = v1.list_node()

            # Get pod counts per node
            namespaces = [
                getattr(settings, 'BOT_POD_NAMESPACE', 'attendee'),
                getattr(settings, 'WEBPAGE_STREAMER_POD_NAMESPACE', 'attendee-webpage-streamer'),
            ]
            pods_by_node = {}
            for ns in namespaces:
                try:
                    pods = v1.list_namespaced_pod(namespace=ns)
                    for pod in pods.items:
                        node_name = pod.spec.node_name
                        if node_name:
                            pods_by_node[node_name] = pods_by_node.get(node_name, 0) + 1
                except Exception:
                    pass

            for node in nodes.items:
                node_name = node.metadata.name

                # Get conditions
                conditions = []
                status = 'Unknown'
                for cond in (node.status.conditions or []):
                    conditions.append({
                        'type': cond.type,
                        'status': cond.status,
                        'reason': cond.reason,
                        'message': cond.message,
                    })
                    if cond.type == 'Ready':
                        status = 'Ready' if cond.status == 'True' else 'NotReady'

                # Get capacity and allocatable
                capacity = node.status.capacity or {}
                allocatable = node.status.allocatable or {}

                nodes_list.append({
                    'name': node_name,
                    'status': status,
                    'conditions': conditions,
                    'capacity': {
                        'cpu': capacity.get('cpu'),
                        'memory': capacity.get('memory'),
                        'pods': capacity.get('pods'),
                    },
                    'allocatable': {
                        'cpu': allocatable.get('cpu'),
                        'memory': allocatable.get('memory'),
                        'pods': allocatable.get('pods'),
                    },
                    'pod_count': pods_by_node.get(node_name, 0),
                })

            return JsonResponse({
                'nodes': nodes_list,
            })

        except Exception as e:
            logger.exception(f"Failed to get Kubernetes nodes: {e}")
            return JsonResponse({
                'error': str(e),
                'nodes': [],
            }, status=500)


class LogStreamView(View):
    """Server-Sent Events stream for live logs."""

    def get(self, request):
        from django.http import StreamingHttpResponse
        import re
        import json as json_module

        source = request.GET.get('source', 'worker')
        level = request.GET.get('level', 'INFO')

        # Map source to container name pattern
        container_patterns = {
            'worker': ['worker', 'celery'],
            'scheduler': ['scheduler', 'beat'],
            'app': ['app', 'web', 'django'],
        }

        # Find the matching container using Docker SDK
        container = None
        try:
            import docker
            client = docker.from_env()

            patterns = container_patterns.get(source, [source])
            for c in client.containers.list():
                if any(p in c.name.lower() for p in patterns):
                    container = c
                    break
        except Exception as e:
            logger.warning(f"Failed to find container for {source}: {e}")

        if not container:
            def error_stream():
                yield f"data: {json_module.dumps({'error': f'Container not found for source: {source}'})}\n\n"
            response = StreamingHttpResponse(error_stream(), content_type='text/event-stream')
            response['Cache-Control'] = 'no-cache'
            response['X-Accel-Buffering'] = 'no'
            return response

        def level_matches(line, min_level):
            """Check if log line meets minimum level."""
            levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
            try:
                min_idx = levels.index(min_level)
            except ValueError:
                min_idx = 1  # Default to INFO

            for i, lvl in enumerate(levels):
                if lvl in line.upper():
                    return i >= min_idx
            # If no level found, include the line (likely INFO)
            return min_level in ['DEBUG', 'INFO']

        def log_stream():
            try:
                # Use Docker SDK to stream logs
                for line in container.logs(stream=True, follow=True, tail=100, timestamps=True):
                    try:
                        line = line.decode('utf-8').strip()
                    except Exception:
                        continue

                    if not line:
                        continue

                    if not level_matches(line, level):
                        continue

                    # Parse log line and extract level
                    log_level = 'INFO'
                    for lvl in ['ERROR', 'WARNING', 'CRITICAL', 'DEBUG', 'INFO']:
                        if lvl in line.upper():
                            log_level = lvl
                            break

                    # Extract timestamp if present (Docker format: 2026-01-18T10:32:45.123Z)
                    timestamp = ''
                    ts_match = re.match(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})', line)
                    if ts_match:
                        timestamp = ts_match.group(1).split('T')[1][:8]
                        line = line[ts_match.end():].strip()

                    data = json_module.dumps({
                        'time': timestamp,
                        'level': log_level,
                        'message': line[:500],  # Truncate very long lines
                    })
                    yield f"data: {data}\n\n"

            except Exception as e:
                yield f"data: {json_module.dumps({'error': str(e)})}\n\n"

        response = StreamingHttpResponse(log_stream(), content_type='text/event-stream')
        response['Cache-Control'] = 'no-cache'
        response['X-Accel-Buffering'] = 'no'
        return response
