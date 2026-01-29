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
    Recording, RecordingStates, RecordingTranscriptionStates, BotResourceSnapshot
)
from bots.tasks.sync_calendar_task import enqueue_sync_calendar_task

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

        # Support filtering by specific date (using bot's scheduled join_at date)
        date_str = request.GET.get('date')
        if date_str:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            # Filter by the bot's scheduled meeting date, not when the error was recorded
            failures = BotEvent.objects.filter(
                event_type__in=[BotEventTypes.FATAL_ERROR, BotEventTypes.COULD_NOT_JOIN],
                bot__join_at__date=target_date
            ).select_related('bot').order_by('-bot__join_at')[:100]
        else:
            days = int(request.GET.get('days', 7))
            cutoff = timezone.now() - timedelta(days=days)
            failures = BotEvent.objects.filter(
                event_type__in=[BotEventTypes.FATAL_ERROR, BotEventTypes.COULD_NOT_JOIN],
                bot__join_at__gte=cutoff
            ).select_related('bot').order_by('-bot__join_at')[:100]

        # Map all BotEventSubTypes to short, readable labels for the dashboard
        sub_type_names = {
            1: 'Waiting for Host',
            2: 'Process Terminated',
            3: 'Zoom Auth Failed',
            4: 'Zoom Status Failed',
            5: 'Unpublished Zoom App',
            6: 'RTMP Connection Failed',
            7: 'Zoom SDK Error',
            8: 'UI Element Not Found',
            9: 'Join Denied',
            10: 'User Requested Leave',
            11: 'Auto Leave (Silence)',
            12: 'Auto Leave (Alone)',
            13: 'Heartbeat Timeout',
            14: 'Meeting Not Found',
            15: 'Bot Not Launched',
            16: 'Waiting Room Timeout',
            17: 'Max Uptime Exceeded',
            18: 'Login Required',
            19: 'Login Failed',
            20: 'Out of Credits',
            21: 'Connection Failed',
            22: 'Internal Error',
            23: 'Recording Denied',
            24: 'Recording Request Timeout',
            25: 'Cannot Grant Permission',
            26: 'Closed Captions Failed',
            27: 'Auth User Not In Meeting',
            28: 'Blocked by Captcha',
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
                'timestamp': f.bot.join_at.isoformat() if f.bot.join_at else f.created_at.isoformat(),
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
                enqueue_sync_calendar_task(calendar)
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
                enqueue_sync_calendar_task(calendar)
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
                enqueue_sync_calendar_task(calendar)
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


class KubernetesEventsAPI(View):
    """API for recent Kubernetes cluster events (warnings, errors)."""

    def get(self, request):
        from django.conf import settings
        from kubernetes import client

        try:
            v1 = _init_kubernetes_client()

            namespaces = [
                getattr(settings, 'BOT_POD_NAMESPACE', 'attendee'),
                getattr(settings, 'WEBPAGE_STREAMER_POD_NAMESPACE', 'attendee-webpage-streamer'),
            ]

            events_list = []
            event_counts = {'Normal': 0, 'Warning': 0}

            for ns in namespaces:
                try:
                    events = v1.list_namespaced_event(
                        namespace=ns,
                        limit=100,
                    )

                    for event in events.items:
                        event_type = event.type or 'Normal'
                        event_counts[event_type] = event_counts.get(event_type, 0) + 1

                        # Calculate age
                        age_seconds = None
                        event_time = event.last_timestamp or event.event_time or event.metadata.creation_timestamp
                        if event_time:
                            age_seconds = int((timezone.now() - event_time).total_seconds())

                        # Only include recent events (last 2 hours) or warnings
                        if age_seconds and age_seconds > 7200 and event_type == 'Normal':
                            continue

                        events_list.append({
                            'namespace': ns,
                            'type': event_type,
                            'reason': event.reason,
                            'message': event.message[:200] if event.message else '',
                            'object': f"{event.involved_object.kind}/{event.involved_object.name}" if event.involved_object else '',
                            'count': event.count or 1,
                            'age_seconds': age_seconds,
                            'first_seen': event.first_timestamp.isoformat() if event.first_timestamp else None,
                            'last_seen': event_time.isoformat() if event_time else None,
                        })

                except client.ApiException as e:
                    logger.warning(f"Failed to list events in namespace {ns}: {e}")

            # Sort by recency (newest first)
            events_list.sort(key=lambda x: x['age_seconds'] or 0)

            # Separate warnings for prominence
            warnings = [e for e in events_list if e['type'] == 'Warning']
            normal = [e for e in events_list if e['type'] == 'Normal'][:20]

            return JsonResponse({
                'events': warnings + normal,
                'warnings': warnings,
                'counts': event_counts,
            })

        except Exception as e:
            logger.exception(f"Failed to get Kubernetes events: {e}")
            return JsonResponse({
                'error': str(e),
                'events': [],
                'warnings': [],
                'counts': {},
            }, status=500)


class KubernetesDeploymentsAPI(View):
    """API for Kubernetes deployment status."""

    def get(self, request):
        from django.conf import settings
        from kubernetes import client

        try:
            # Initialize kubernetes client first (loads config)
            _init_kubernetes_client()
            apps_v1 = client.AppsV1Api()

            namespaces = [
                getattr(settings, 'BOT_POD_NAMESPACE', 'attendee'),
            ]

            deployments_list = []

            for ns in namespaces:
                try:
                    deployments = apps_v1.list_namespaced_deployment(namespace=ns)

                    for dep in deployments.items:
                        name = dep.metadata.name
                        spec_replicas = dep.spec.replicas or 0
                        status = dep.status

                        ready_replicas = status.ready_replicas or 0
                        available_replicas = status.available_replicas or 0
                        updated_replicas = status.updated_replicas or 0

                        # Determine health status
                        health = 'healthy'
                        if ready_replicas < spec_replicas:
                            health = 'degraded'
                        if ready_replicas == 0 and spec_replicas > 0:
                            health = 'unhealthy'

                        # Check conditions for more details
                        conditions = []
                        progressing = None
                        available = None
                        for cond in (status.conditions or []):
                            conditions.append({
                                'type': cond.type,
                                'status': cond.status,
                                'reason': cond.reason,
                                'message': cond.message[:100] if cond.message else '',
                            })
                            if cond.type == 'Progressing':
                                progressing = cond.status == 'True'
                            if cond.type == 'Available':
                                available = cond.status == 'True'

                        deployments_list.append({
                            'namespace': ns,
                            'name': name,
                            'replicas': {
                                'desired': spec_replicas,
                                'ready': ready_replicas,
                                'available': available_replicas,
                                'updated': updated_replicas,
                            },
                            'health': health,
                            'progressing': progressing,
                            'available': available,
                            'conditions': conditions,
                            'image': dep.spec.template.spec.containers[0].image if dep.spec.template.spec.containers else '',
                        })

                except client.ApiException as e:
                    logger.warning(f"Failed to list deployments in namespace {ns}: {e}")

            # Summary counts
            summary = {
                'total': len(deployments_list),
                'healthy': len([d for d in deployments_list if d['health'] == 'healthy']),
                'degraded': len([d for d in deployments_list if d['health'] == 'degraded']),
                'unhealthy': len([d for d in deployments_list if d['health'] == 'unhealthy']),
            }

            return JsonResponse({
                'deployments': deployments_list,
                'summary': summary,
            })

        except Exception as e:
            logger.exception(f"Failed to get Kubernetes deployments: {e}")
            return JsonResponse({
                'error': str(e),
                'deployments': [],
                'summary': {'total': 0, 'healthy': 0, 'degraded': 0, 'unhealthy': 0},
            }, status=500)


class KubernetesResourceMetricsAPI(View):
    """API for actual resource usage from metrics-server (if available)."""

    def get(self, request):
        from django.conf import settings
        from kubernetes import client

        try:
            # Initialize kubernetes client first (loads config)
            _init_kubernetes_client()
            # Try to use metrics API (requires metrics-server)
            custom_api = client.CustomObjectsApi()

            namespace = getattr(settings, 'BOT_POD_NAMESPACE', 'attendee')

            # Get node metrics
            node_metrics = []
            try:
                nodes = custom_api.list_cluster_custom_object(
                    group="metrics.k8s.io",
                    version="v1beta1",
                    plural="nodes"
                )
                for node in nodes.get('items', []):
                    usage = node.get('usage', {})
                    node_metrics.append({
                        'name': node.get('metadata', {}).get('name'),
                        'cpu': usage.get('cpu'),
                        'memory': usage.get('memory'),
                    })
            except client.ApiException as e:
                if e.status == 404:
                    logger.info("Metrics-server not available for node metrics")
                else:
                    logger.warning(f"Failed to get node metrics: {e}")

            # Get pod metrics
            pod_metrics = []
            try:
                pods = custom_api.list_namespaced_custom_object(
                    group="metrics.k8s.io",
                    version="v1beta1",
                    namespace=namespace,
                    plural="pods"
                )
                for pod in pods.get('items', []):
                    containers = pod.get('containers', [])
                    total_cpu = 0
                    total_memory = 0
                    for container in containers:
                        usage = container.get('usage', {})
                        total_cpu += self._parse_cpu(usage.get('cpu', '0'))
                        total_memory += self._parse_memory(usage.get('memory', '0'))

                    pod_metrics.append({
                        'name': pod.get('metadata', {}).get('name'),
                        'cpu_millicores': total_cpu,
                        'memory_bytes': total_memory,
                    })
            except client.ApiException as e:
                if e.status == 404:
                    logger.info("Metrics-server not available for pod metrics")
                else:
                    logger.warning(f"Failed to get pod metrics: {e}")

            return JsonResponse({
                'metrics_available': len(node_metrics) > 0 or len(pod_metrics) > 0,
                'node_metrics': node_metrics,
                'pod_metrics': pod_metrics,
            })

        except Exception as e:
            logger.exception(f"Failed to get Kubernetes resource metrics: {e}")
            return JsonResponse({
                'error': str(e),
                'metrics_available': False,
                'node_metrics': [],
                'pod_metrics': [],
            }, status=500)

    def _parse_cpu(self, cpu_str):
        """Parse CPU string to millicores."""
        if not cpu_str:
            return 0
        cpu_str = str(cpu_str)
        if cpu_str.endswith('n'):
            return int(cpu_str[:-1]) // 1000000
        elif cpu_str.endswith('m'):
            return int(cpu_str[:-1])
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
            'Ki': 1024, 'Mi': 1024**2, 'Gi': 1024**3,
            'K': 1000, 'M': 1000**2, 'G': 1000**3,
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


class LogStreamView(View):
    """Server-Sent Events stream for live logs (Docker or Kubernetes)."""

    def _detect_mode(self):
        """Detect if running in Kubernetes or Docker mode."""
        import os
        force_mode = os.getenv('INFRASTRUCTURE_MODE')
        if force_mode in ('kubernetes', 'docker'):
            return force_mode
        if os.path.exists('/var/run/secrets/kubernetes.io/serviceaccount/token'):
            return 'kubernetes'
        if os.getenv('KUBERNETES_SERVICE_HOST'):
            return 'kubernetes'
        return 'docker'

    def get(self, request):
        from django.http import StreamingHttpResponse
        from django.conf import settings
        import re
        import json as json_module

        source = request.GET.get('source', 'scheduler')
        level = request.GET.get('level', 'INFO')
        mode = self._detect_mode()

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
            return min_level in ['DEBUG', 'INFO']

        def parse_log_line(line):
            """Parse a log line and extract timestamp, level, message."""
            import re
            log_level = 'INFO'

            # Skip diagnostic messages that contain 'error' in field names but aren't errors
            if 'PerParticipantNonStreamingAudioInputManager diagnostic' in line:
                log_level = 'DEBUG'
            elif 'diagnostic info:' in line.lower():
                log_level = 'DEBUG'
            else:
                # Look for log level indicators at word boundaries, not inside words
                for lvl in ['ERROR', 'WARNING', 'CRITICAL', 'DEBUG', 'INFO']:
                    # Match level as a standalone word (not part of field names like 'vad_error')
                    if re.search(rf'\b{lvl}\b', line.upper()):
                        log_level = lvl
                        break

            timestamp = ''
            ts_match = re.match(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})', line)
            if ts_match:
                timestamp = ts_match.group(1).split('T')[1][:8]
                line = line[ts_match.end():].strip()

            return timestamp, log_level, line[:500]

        if mode == 'kubernetes':
            # Kubernetes mode - stream from pod logs
            # Map source to K8s deployment/pod patterns
            k8s_patterns = {
                'scheduler': 'attendee-scheduler',
                'app': 'attendee-api',
                'worker': 'attendee-worker',
            }

            # Check if source is a direct bot pod name (starts with 'bot-')
            is_bot_pod = source.startswith('bot-')
            is_all_bots = source == 'all-bots'
            pod_pattern = source if is_bot_pod else k8s_patterns.get(source, source)
            namespace = getattr(settings, 'BOT_POD_NAMESPACE', 'attendee')

            def k8s_log_stream():
                try:
                    v1 = _init_kubernetes_client()
                    pods = v1.list_namespaced_pod(namespace=namespace)

                    if is_all_bots:
                        # Aggregate logs from all bot pods
                        import threading
                        import queue
                        from kubernetes import watch

                        log_queue = queue.Queue()
                        stop_event = threading.Event()

                        def stream_pod_logs(pod_name, container_name):
                            try:
                                w = watch.Watch()
                                for line in w.stream(
                                    v1.read_namespaced_pod_log,
                                    name=pod_name,
                                    namespace=namespace,
                                    container=container_name,
                                    follow=True,
                                    tail_lines=50,
                                    timestamps=True,
                                    _request_timeout=300
                                ):
                                    if stop_event.is_set():
                                        break
                                    if line:
                                        log_queue.put((pod_name, line))
                            except Exception as e:
                                log_queue.put((pod_name, f"[Stream error: {e}]"))

                        # Start threads for all bot pods
                        threads = []
                        bot_pods = [p for p in pods.items if p.metadata.name.startswith('bot-')]

                        if not bot_pods:
                            yield f"data: {json_module.dumps({'message': 'No bot pods found', 'level': 'INFO', 'time': ''})}\n\n"
                            return

                        for pod in bot_pods:
                            if pod.status.phase in ['Running', 'Succeeded', 'Failed']:
                                container = pod.spec.containers[0].name if pod.spec.containers else None
                                t = threading.Thread(target=stream_pod_logs, args=(pod.metadata.name, container), daemon=True)
                                t.start()
                                threads.append(t)

                        yield f"data: {json_module.dumps({'message': f'Streaming logs from {len(threads)} bot pods...', 'level': 'INFO', 'time': ''})}\n\n"

                        # Read from queue and yield
                        while True:
                            try:
                                pod_name, line = log_queue.get(timeout=1)
                                if not level_matches(line, level):
                                    continue
                                timestamp, log_level, message = parse_log_line(line)
                                # Shorten pod name for display
                                short_pod = pod_name.replace('bot-pod-', '').replace('bot-', '')[:20]
                                data = json_module.dumps({
                                    'time': timestamp,
                                    'level': log_level,
                                    'message': f"[{short_pod}] {message}",
                                    'pod': pod_name,
                                })
                                yield f"data: {data}\n\n"
                            except queue.Empty:
                                # Check if any threads are still alive
                                if not any(t.is_alive() for t in threads):
                                    break
                                continue

                        stop_event.set()
                        return

                    # Single pod streaming (existing logic)
                    target_pod = None
                    for pod in pods.items:
                        # For bot pods, match exact name; for others, use pattern matching
                        if is_bot_pod:
                            if pod.metadata.name == pod_pattern:
                                target_pod = pod
                                break
                        else:
                            if pod_pattern in pod.metadata.name and pod.status.phase == 'Running':
                                target_pod = pod
                                break

                    if not target_pod:
                        yield f"data: {json_module.dumps({'error': f'No pod found matching: {pod_pattern}'})}\n\n"
                        return

                    # Stream logs from the pod
                    pod_name = target_pod.metadata.name
                    container_name = target_pod.spec.containers[0].name if target_pod.spec.containers else None

                    # For non-running pods, get recent logs without follow
                    if target_pod.status.phase != 'Running':
                        try:
                            logs = v1.read_namespaced_pod_log(
                                name=pod_name,
                                namespace=namespace,
                                container=container_name,
                                tail_lines=200,
                                timestamps=True,
                            )
                            for line in logs.split('\n'):
                                if not line or not level_matches(line, level):
                                    continue
                                timestamp, log_level, message = parse_log_line(line)
                                data = json_module.dumps({
                                    'time': timestamp,
                                    'level': log_level,
                                    'message': message,
                                    'pod': pod_name,
                                })
                                yield f"data: {data}\n\n"
                            yield f"data: {json_module.dumps({'message': f'[End of logs - pod status: {target_pod.status.phase}]', 'level': 'INFO', 'time': ''})}\n\n"
                        except Exception as e:
                            yield f"data: {json_module.dumps({'error': f'Failed to get logs: {e}'})}\n\n"
                        return

                    # Use watch to stream logs for running pods
                    from kubernetes import watch
                    w = watch.Watch()

                    for line in w.stream(
                        v1.read_namespaced_pod_log,
                        name=pod_name,
                        namespace=namespace,
                        container=container_name,
                        follow=True,
                        tail_lines=100,
                        timestamps=True,
                        _request_timeout=300
                    ):
                        if not line:
                            continue

                        if not level_matches(line, level):
                            continue

                        timestamp, log_level, message = parse_log_line(line)

                        data = json_module.dumps({
                            'time': timestamp,
                            'level': log_level,
                            'message': message,
                            'pod': pod_name,
                        })
                        yield f"data: {data}\n\n"

                except Exception as e:
                    logger.warning(f"K8s log stream error: {e}")
                    yield f"data: {json_module.dumps({'error': str(e)})}\n\n"

            response = StreamingHttpResponse(k8s_log_stream(), content_type='text/event-stream')

        else:
            # Docker mode - stream from container logs
            container_patterns = {
                'worker': ['worker', 'celery'],
                'scheduler': ['scheduler', 'beat'],
                'app': ['app', 'web', 'django'],
            }

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

            def docker_log_stream():
                try:
                    for line in container.logs(stream=True, follow=True, tail=100, timestamps=True):
                        try:
                            line = line.decode('utf-8').strip()
                        except Exception:
                            continue

                        if not line:
                            continue

                        if not level_matches(line, level):
                            continue

                        timestamp, log_level, message = parse_log_line(line)

                        data = json_module.dumps({
                            'time': timestamp,
                            'level': log_level,
                            'message': message,
                        })
                        yield f"data: {data}\n\n"

                except Exception as e:
                    yield f"data: {json_module.dumps({'error': str(e)})}\n\n"

            response = StreamingHttpResponse(docker_log_stream(), content_type='text/event-stream')

        response['Cache-Control'] = 'no-cache'
        response['X-Accel-Buffering'] = 'no'
        return response


# =============================================================================
# Health Dashboard New APIs
# =============================================================================

class ActiveIssuesAPI(View):
    """API for detecting active issues that need attention."""

    def get(self, request):
        from bots.models import (
            AsyncTranscription, AsyncTranscriptionStates,
            Utterance, ZoomOAuthConnection, ZoomOAuthConnectionStates
        )
        from .models import OAuthCredential

        now = timezone.now()
        issues = {
            'has_issues': False,
            'total_count': 0,
            'stuck_bots': {'count': 0, 'by_state': []},
            'heartbeat_timeout_bots': {'count': 0, 'bots': []},
            'orphaned_recordings': {'count': 0},
            'stalled_transcriptions': {'count': 0},
            'stuck_transcript_sync': {'count': 0, 'bots': []}
        }

        # Stuck bots - bots that have been in certain states too long
        stuck_thresholds = {
            BotStates.JOINING: 15,           # minutes
            BotStates.WAITING_ROOM: 30,
            BotStates.POST_PROCESSING: 60,
            BotStates.LEAVING: 10,
        }

        for state, threshold in stuck_thresholds.items():
            cutoff = now - timedelta(minutes=threshold)
            stuck = Bot.objects.filter(
                state=state,
                updated_at__lt=cutoff
            )
            count = stuck.count()
            if count > 0:
                oldest = stuck.order_by('updated_at').first()
                oldest_age = int((now - oldest.updated_at).total_seconds() / 60) if oldest else 0
                state_name = BotStates(state).label
                issues['stuck_bots']['by_state'].append({
                    'state': state_name,
                    'state_raw': state,
                    'threshold_minutes': threshold,
                    'count': count,
                    'oldest_age_minutes': oldest_age
                })
                issues['stuck_bots']['count'] += count

        # Heartbeat timeout bots - bots with stale heartbeats that aren't in post-meeting states
        heartbeat_timeout_seconds = 600  # 10 minutes
        heartbeat_cutoff = int(now.timestamp()) - heartbeat_timeout_seconds
        heartbeat_timeout = Bot.objects.filter(
            last_heartbeat_timestamp__lt=heartbeat_cutoff,
            last_heartbeat_timestamp__isnull=False
        ).exclude(state__in=BotStates.post_meeting_states())

        timeout_count = heartbeat_timeout.count()
        if timeout_count > 0:
            state_names = {s.value: s.label for s in BotStates}
            issues['heartbeat_timeout_bots']['count'] = timeout_count
            issues['heartbeat_timeout_bots']['bots'] = [
                {
                    'object_id': b.object_id,
                    'state': state_names.get(b.state, f'Unknown({b.state})'),
                    'last_heartbeat_age_seconds': int(now.timestamp()) - b.last_heartbeat_timestamp
                }
                for b in heartbeat_timeout[:10]
            ]

        # Orphaned recordings - recordings in progress/paused but bot is in post-meeting state
        orphaned = Recording.objects.filter(
            state__in=[RecordingStates.IN_PROGRESS, RecordingStates.PAUSED],
            bot__state__in=BotStates.post_meeting_states()
        ).count()
        issues['orphaned_recordings']['count'] = orphaned

        # Stalled transcriptions - in progress for more than 30 minutes
        stalled_cutoff = now - timedelta(minutes=30)
        stalled = AsyncTranscription.objects.filter(
            state=AsyncTranscriptionStates.IN_PROGRESS,
            updated_at__lt=stalled_cutoff
        ).count()
        issues['stalled_transcriptions']['count'] = stalled

        # Stuck transcript sync - recordings COMPLETE but transcription still IN_PROGRESS for >60 min
        # This indicates the event-driven pipeline may have missed a signal
        stuck_sync_cutoff = now - timedelta(minutes=60)
        stuck_sync = Recording.objects.filter(
            state=RecordingStates.COMPLETE,
            transcription_state=RecordingTranscriptionStates.IN_PROGRESS,
            updated_at__lt=stuck_sync_cutoff,
            bot__state=BotStates.ENDED
        ).select_related('bot')
        stuck_sync_count = stuck_sync.count()
        if stuck_sync_count > 0:
            issues['stuck_transcript_sync']['count'] = stuck_sync_count
            issues['stuck_transcript_sync']['bots'] = [
                {
                    'bot_id': r.bot.object_id,
                    'recording_id': r.id,
                    'age_minutes': int((now - r.updated_at).total_seconds() / 60)
                }
                for r in stuck_sync[:10]
            ]

        # Calculate totals
        issues['total_count'] = (
            issues['stuck_bots']['count'] +
            issues['heartbeat_timeout_bots']['count'] +
            issues['orphaned_recordings']['count'] +
            issues['stalled_transcriptions']['count'] +
            issues['stuck_transcript_sync']['count']
        )
        issues['has_issues'] = issues['total_count'] > 0

        return JsonResponse(issues)


class MeetingSyncStatusAPI(View):
    """
    API for meeting sync pipeline status.

    Shows the two-phase event-driven pipeline status:
    - Phase 1: Bot ENDED → Meeting created (metadata only, transcript_status='pending')
    - Phase 2: Transcription COMPLETE → Transcript synced (transcript_status='complete')
    """

    def get(self, request):
        from datetime import datetime
        from django.db.models import Count, Q
        from .supabase_client import get_supabase_client

        # Support filtering by date
        date_str = request.GET.get('date')
        if date_str:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        else:
            target_date = timezone.now().date()

        # Get all ended bots for the date (including fatal errors)
        ended_bots = Bot.objects.filter(
            state__in=[BotStates.ENDED, BotStates.FATAL_ERROR, BotStates.DATA_DELETED],
            updated_at__date=target_date,
            meeting_url__isnull=False
        ).exclude(meeting_url='')

        total_ended = ended_bots.count()

        # Get recording stats for ended bots
        from django.db.models import Exists, OuterRef
        ended_bot_ids = list(ended_bots.values_list('id', flat=True))

        recordings = Recording.objects.filter(bot_id__in=ended_bot_ids)

        # Recording states
        rec_complete = recordings.filter(state=RecordingStates.COMPLETE).count()
        # In progress shows ALL active recordings (not filtered by ended bots)
        rec_in_progress = Recording.objects.filter(state=RecordingStates.IN_PROGRESS).count()
        rec_failed = recordings.filter(state=RecordingStates.FAILED).count()

        # Transcription states
        trans_complete = recordings.filter(transcription_state=RecordingTranscriptionStates.COMPLETE).count()
        trans_in_progress = recordings.filter(transcription_state=RecordingTranscriptionStates.IN_PROGRESS).count()
        trans_failed = recordings.filter(transcription_state=RecordingTranscriptionStates.FAILED).count()
        trans_not_started = recordings.filter(transcription_state=RecordingTranscriptionStates.NOT_STARTED).count()

        # Try to get Supabase sync status
        supabase_stats = {
            'not_created': 0,
            'pending': 0,
            'complete': 0,
            'failed': 0,
            'available': False
        }

        client = get_supabase_client()
        if client:
            try:
                # Get bot IDs for lookup
                bot_object_ids = list(ended_bots.values_list('object_id', flat=True))

                if bot_object_ids:
                    # Query Supabase for meetings with these bot IDs
                    result = client.table('meetings').select(
                        'attendee_bot_id, transcript_status'
                    ).in_('attendee_bot_id', bot_object_ids).execute()

                    supabase_meetings = {m['attendee_bot_id']: m.get('transcript_status', 'pending')
                                         for m in (result.data or [])}

                    # Count sync statuses
                    for bot_id in bot_object_ids:
                        status = supabase_meetings.get(bot_id)
                        if not status:
                            supabase_stats['not_created'] += 1
                        elif status == 'pending':
                            supabase_stats['pending'] += 1
                        elif status == 'complete':
                            supabase_stats['complete'] += 1
                        elif status == 'failed':
                            supabase_stats['failed'] += 1

                    supabase_stats['available'] = True
            except Exception as e:
                logger.warning(f"Failed to fetch Supabase sync status: {e}")

        # Identify stuck meetings (ended >30 min ago, transcription complete, but not in Supabase or pending)
        thirty_mins_ago = timezone.now() - timedelta(minutes=30)
        stuck_count = 0

        if supabase_stats['available']:
            # Bots with complete transcription but not synced yet
            complete_trans_bots = recordings.filter(
                transcription_state=RecordingTranscriptionStates.COMPLETE
            ).values_list('bot__object_id', flat=True)

            for bot_id in complete_trans_bots:
                if bot_id not in [m['attendee_bot_id'] for m in (result.data or [])]:
                    stuck_count += 1
                elif supabase_meetings.get(bot_id) == 'pending':
                    stuck_count += 1

        return JsonResponse({
            'date': target_date.isoformat(),
            'total_ended_bots': total_ended,
            'recording_states': {
                'complete': rec_complete,
                'in_progress': rec_in_progress,
                'failed': rec_failed,
            },
            'transcription_states': {
                'complete': trans_complete,
                'in_progress': trans_in_progress,
                'failed': trans_failed,
                'not_started': trans_not_started,
            },
            'supabase_sync': supabase_stats,
            'stuck_meetings': stuck_count,
            'timestamp': timezone.now().isoformat(),
        })


class SystemHealthAPI(View):
    """API for system-wide health status at a glance."""

    def _detect_mode(self):
        """Detect if running in Kubernetes or Docker mode."""
        import os
        force_mode = os.getenv('INFRASTRUCTURE_MODE')
        if force_mode in ('kubernetes', 'docker'):
            return force_mode
        if os.path.exists('/var/run/secrets/kubernetes.io/serviceaccount/token'):
            return 'kubernetes'
        if os.getenv('KUBERNETES_SERVICE_HOST'):
            return 'kubernetes'
        return 'docker'

    def get(self, request):
        import time
        from django.conf import settings
        from django.db import connection

        mode = self._detect_mode()
        health = {
            'scheduler': {'status': 'unknown', 'pod_running': False},
            'worker': {'status': 'unknown', 'pod_running': False, 'active_tasks': 0},
            'database': {'status': 'unknown', 'latency_ms': None},
            'redis': {'status': 'unknown', 'connected': False},
            'k8s_api': {'status': 'unknown', 'latency_ms': None, 'nodes_ready': '0/0'},
            'issues_count': 0,
            'mode': mode,
            'timestamp': timezone.now().isoformat()
        }

        # Database health - simple ping with timing
        try:
            start = time.time()
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
            latency = int((time.time() - start) * 1000)
            health['database']['status'] = 'healthy'
            health['database']['latency_ms'] = latency
        except Exception as e:
            health['database']['status'] = 'error'
            logger.warning(f"Database health check failed: {e}")

        # Redis/Celery health
        try:
            from attendee.celery import app as celery_app
            inspect = celery_app.control.inspect(timeout=2)
            ping_result = inspect.ping()
            if ping_result:
                health['redis']['status'] = 'healthy'
                health['redis']['connected'] = True

                # Get worker status
                active_workers = inspect.active()
                if active_workers:
                    health['worker']['pod_running'] = True
                    health['worker']['status'] = 'healthy'
                    health['worker']['active_tasks'] = sum(len(tasks) for tasks in active_workers.values())
                else:
                    health['worker']['status'] = 'degraded'
            else:
                health['redis']['status'] = 'error'
                health['worker']['status'] = 'error'
        except Exception as e:
            health['redis']['status'] = 'error'
            health['worker']['status'] = 'error'
            logger.warning(f"Celery/Redis health check failed: {e}")

        # Kubernetes health (if in K8s mode)
        if mode == 'kubernetes':
            try:
                from kubernetes import client
                v1 = _init_kubernetes_client()

                # API latency
                start = time.time()
                v1.list_namespace(limit=1)
                latency = int((time.time() - start) * 1000)
                health['k8s_api']['status'] = 'healthy'
                health['k8s_api']['latency_ms'] = latency

                # Node status
                nodes = v1.list_node()
                ready_count = 0
                total_count = len(nodes.items)
                for node in nodes.items:
                    for cond in (node.status.conditions or []):
                        if cond.type == 'Ready' and cond.status == 'True':
                            ready_count += 1
                            break
                health['k8s_api']['nodes_ready'] = f'{ready_count}/{total_count}'

                # Check scheduler pod
                namespace = getattr(settings, 'BOT_POD_NAMESPACE', 'attendee')
                pods = v1.list_namespaced_pod(namespace=namespace)
                for pod in pods.items:
                    name = pod.metadata.name
                    phase = pod.status.phase
                    if 'scheduler' in name:
                        health['scheduler']['pod_running'] = phase == 'Running'
                        health['scheduler']['status'] = 'healthy' if phase == 'Running' else 'error'
                    elif 'worker' in name and 'webpage' not in name:
                        if phase == 'Running':
                            health['worker']['pod_running'] = True
                            if health['worker']['status'] != 'healthy':
                                health['worker']['status'] = 'healthy'

            except Exception as e:
                health['k8s_api']['status'] = 'error'
                logger.warning(f"K8s health check failed: {e}")
        else:
            # Docker mode - check containers
            health['k8s_api']['status'] = 'n/a'
            try:
                import docker
                client = docker.from_env()
                for container in client.containers.list():
                    name = container.name.lower()
                    if 'scheduler' in name and container.status == 'running':
                        health['scheduler']['pod_running'] = True
                        health['scheduler']['status'] = 'healthy'
                    elif 'worker' in name and container.status == 'running':
                        health['worker']['pod_running'] = True
                client.close()
            except Exception as e:
                logger.warning(f"Docker health check failed: {e}")

        # Get issues count from ActiveIssuesAPI
        try:
            issues_response = ActiveIssuesAPI().get(request)
            import json
            issues_data = json.loads(issues_response.content)
            health['issues_count'] = issues_data.get('total_count', 0)
        except Exception as e:
            logger.warning(f"Failed to get issues count: {e}")

        return JsonResponse(health)


class ProcessingPipelineAPI(View):
    """API for processing pipeline status - recordings and transcriptions."""

    def get(self, request):
        from bots.models import (
            AsyncTranscription, AsyncTranscriptionStates, Utterance,
            Credentials, TranscriptionFailureReasons
        )

        now = timezone.now()
        today = now.date()
        day_ago = now - timedelta(hours=24)

        # Get utterance failure breakdown (last 24h)
        failed_utterances_24h = Utterance.objects.filter(
            failure_data__isnull=False,
            updated_at__gte=day_ago
        )

        # Count failures by reason
        failure_reasons = {}
        for utterance in failed_utterances_24h[:500]:  # Limit to avoid memory issues
            if utterance.failure_data:
                reason = utterance.failure_data.get('reason', 'unknown')
                failure_reasons[reason] = failure_reasons.get(reason, 0) + 1

        # Utterances processed today (have transcription)
        utterances_completed_today = Utterance.objects.filter(
            transcription__isnull=False,
            updated_at__date=today
        ).count()

        # DeepGram credentials status
        deepgram_creds = Credentials.objects.filter(credential_type=Credentials.CredentialTypes.DEEPGRAM)
        deepgram_total = deepgram_creds.count()

        pipeline = {
            'recordings': {
                'in_progress': Recording.objects.filter(state=RecordingStates.IN_PROGRESS).count(),
                'completed_today': Recording.objects.filter(
                    state=RecordingStates.COMPLETE,
                    completed_at__date=today
                ).count(),
                'failed_today': Recording.objects.filter(
                    state=RecordingStates.FAILED,
                    updated_at__date=today
                ).count()
            },
            'transcriptions': {
                'in_progress': Recording.objects.filter(
                    transcription_state=RecordingTranscriptionStates.IN_PROGRESS
                ).count(),
                'pending_utterances': Utterance.objects.filter(
                    transcription__isnull=True,
                    failure_data__isnull=True
                ).count(),
                'failed_today': Recording.objects.filter(
                    transcription_state=RecordingTranscriptionStates.FAILED,
                    updated_at__date=today
                ).count()
            },
            'async_transcriptions': {
                'not_started': AsyncTranscription.objects.filter(
                    state=AsyncTranscriptionStates.NOT_STARTED
                ).count(),
                'in_progress': AsyncTranscription.objects.filter(
                    state=AsyncTranscriptionStates.IN_PROGRESS
                ).count(),
                'failed_today': AsyncTranscription.objects.filter(
                    state=AsyncTranscriptionStates.FAILED,
                    updated_at__date=today
                ).count()
            },
            'deepgram': {
                'credentials_configured': deepgram_total,
                'utterances_completed_today': utterances_completed_today,
                'utterances_failed_24h': failed_utterances_24h.count(),
                'failure_reasons_24h': failure_reasons
            },
            'timestamp': timezone.now().isoformat()
        }

        return JsonResponse(pipeline)


class PipelineActivityAPI(View):
    """API for pipeline activity - Supabase syncs, insights, emails."""

    def get(self, request):
        from .models import PipelineActivity
        from .supabase_client import get_supabase_client

        today = timezone.now().date()

        # Get bot_ids for bots that ended today (to filter to today's pipeline only)
        today_ended_bots = Bot.objects.filter(
            state=BotStates.ENDED,
            updated_at__date=today,
            meeting_url__isnull=False
        ).exclude(meeting_url='').values_list('object_id', flat=True)
        today_bot_ids = set(str(bid) for bid in today_ended_bots)

        # Get meeting_ids for today's bots from Supabase
        today_meeting_ids = set()
        client = get_supabase_client()
        if client and today_bot_ids:
            try:
                result = client.table('meetings').select(
                    'id, attendee_bot_id'
                ).in_('attendee_bot_id', list(today_bot_ids)).execute()
                for m in (result.data or []):
                    if m.get('id'):
                        today_meeting_ids.add(m['id'])
            except Exception as e:
                logger.warning(f"Failed to fetch today's meeting IDs: {e}")

        # Get today's activities, filtered to only today's meetings
        today_activities = PipelineActivity.objects.filter(created_at__date=today)
        if today_meeting_ids:
            today_activities_for_today_meetings = today_activities.filter(
                meeting_id__in=today_meeting_ids
            )
        else:
            # Fallback: filter by bot_id if we couldn't get meeting_ids
            today_activities_for_today_meetings = today_activities.filter(
                bot_id__in=today_bot_ids
            )

        # Helper to count unique meetings by final outcome (most recent status wins)
        def count_by_final_outcome(queryset):
            """Count unique meetings by their final (most recent) status."""
            from django.db.models import Max, Subquery

            # Get the most recent activity ID for each meeting
            latest_per_meeting = queryset.filter(
                meeting_id__isnull=False
            ).exclude(meeting_id='').values('meeting_id').annotate(
                latest_id=Max('id')
            ).values('latest_id')

            # Filter to only the most recent activity per meeting
            final_outcomes = queryset.filter(id__in=Subquery(latest_per_meeting))

            return {
                'success': final_outcomes.filter(status=PipelineActivity.Status.SUCCESS).count(),
                'failed': final_outcomes.filter(status=PipelineActivity.Status.FAILED).count(),
            }

        # Filter by event type (using today's meetings only)
        insight_extraction = today_activities_for_today_meetings.filter(
            event_type=PipelineActivity.EventType.INSIGHT_EXTRACTION
        )
        emails = today_activities_for_today_meetings.filter(
            event_type=PipelineActivity.EventType.EMAIL_SENT
        )

        # Get recent email details (last 20, from today's meetings)
        recent_emails = emails.order_by('-created_at')[:20].values(
            'recipient', 'meeting_title', 'meeting_id', 'status', 'error', 'created_at'
        )

        return JsonResponse({
            'insight_extraction': count_by_final_outcome(insight_extraction),
            'emails': count_by_final_outcome(emails),
            'emails_detail': {
                'recent': list(recent_emails),
            },
            'timestamp': timezone.now().isoformat()
        })


class ExternalIntegrationsAPI(View):
    """API for external integrations status - OAuth and webhooks."""

    def get(self, request):
        from bots.models import (
            WebhookDeliveryAttempt, WebhookDeliveryAttemptStatus,
            ZoomOAuthConnection, ZoomOAuthConnectionStates
        )
        from .models import OAuthCredential

        now = timezone.now()
        day_ago = now - timedelta(hours=24)

        # Webhook stats
        webhooks_24h = WebhookDeliveryAttempt.objects.filter(created_at__gte=day_ago)
        success_count = webhooks_24h.filter(status=WebhookDeliveryAttemptStatus.SUCCESS).count()
        total_count = webhooks_24h.count()
        failed_count = webhooks_24h.filter(status=WebhookDeliveryAttemptStatus.FAILURE).count()
        pending_count = WebhookDeliveryAttempt.objects.filter(
            status=WebhookDeliveryAttemptStatus.PENDING
        ).count()

        integrations = {
            'oauth': {
                'zoom': {
                    'total_connections': ZoomOAuthConnection.objects.count(),
                    'connected': ZoomOAuthConnection.objects.filter(
                        state=ZoomOAuthConnectionStates.CONNECTED
                    ).count(),
                    'disconnected': ZoomOAuthConnection.objects.filter(
                        state=ZoomOAuthConnectionStates.DISCONNECTED
                    ).count()
                },
                'google': {
                    'total_credentials': OAuthCredential.objects.filter(provider='google').count(),
                    'valid': OAuthCredential.objects.filter(
                        provider='google',
                        token_expiry__gt=now
                    ).count(),
                    'expired': OAuthCredential.objects.filter(
                        provider='google',
                        token_expiry__lte=now
                    ).count()
                },
                'microsoft': {
                    'total_credentials': OAuthCredential.objects.filter(provider='microsoft').count(),
                    'valid': OAuthCredential.objects.filter(
                        provider='microsoft',
                        token_expiry__gt=now
                    ).count(),
                    'expired': OAuthCredential.objects.filter(
                        provider='microsoft',
                        token_expiry__lte=now
                    ).count()
                }
            },
            'webhooks': {
                'pending': pending_count,
                'success_rate_24h': round((success_count / total_count * 100), 1) if total_count > 0 else 100.0,
                'failed_24h': failed_count,
                'total_24h': total_count
            },
            'timestamp': now.isoformat()
        }

        return JsonResponse(integrations)


class ResourceSummaryAPI(View):
    """Resource usage summary - shows peak CPU/memory for recent bots."""

    def get(self, request):
        # Configurable time range: 1h, 6h, 24h, 7d
        hours_param = request.GET.get('hours', '24')
        try:
            hours = int(hours_param)
            if hours > 168:  # Max 7 days
                hours = 168
        except ValueError:
            hours = 24

        cutoff = timezone.now() - timedelta(hours=hours)

        # Get current resource limits from environment
        import os
        cpu_limit = int(os.getenv('BOT_CPU_REQUEST', '1500').rstrip('m'))
        memory_limit_gb = float(os.getenv('BOT_MEMORY_LIMIT', '3Gi').rstrip('Gi'))
        memory_limit_mb = int(memory_limit_gb * 1024)

        # Get bots with resource snapshots in the time range
        bots_with_snapshots = Bot.objects.filter(
            resource_snapshots__created_at__gte=cutoff
        ).distinct()

        bot_resources = []
        overall_peak_memory = 0
        overall_peak_cpu = 0

        for bot in bots_with_snapshots:
            snapshots = bot.resource_snapshots.filter(created_at__gte=cutoff)
            if not snapshots.exists():
                continue

            peak_memory = 0
            peak_cpu = 0
            snapshot_count = 0

            for snapshot in snapshots:
                data = snapshot.data
                ram = data.get('ram_usage_megabytes', 0)
                cpu = data.get('cpu_usage_millicores', 0)
                if ram > peak_memory:
                    peak_memory = ram
                if cpu > peak_cpu:
                    peak_cpu = cpu
                snapshot_count += 1

            # Determine outcome
            if bot.state == 7:  # FATAL_ERROR
                # Check if it was OOM
                last_event = bot.bot_events.order_by('-created_at').first()
                if last_event and last_event.event_sub_type == 13:  # HEARTBEAT_TIMEOUT
                    outcome = 'Crashed'
                else:
                    outcome = 'Failed'
            elif bot.state == 9:  # ENDED
                outcome = 'Completed'
            elif bot.state in [2, 3, 4]:  # Active states
                outcome = 'Running'
            else:
                outcome = 'Other'

            # Calculate percentages
            memory_pct = round((peak_memory / memory_limit_mb) * 100, 1) if memory_limit_mb > 0 else 0
            cpu_pct = round((peak_cpu / cpu_limit) * 100, 1) if cpu_limit > 0 else 0

            bot_resources.append({
                'bot_id': bot.id,
                'object_id': bot.object_id,
                'peak_memory_mb': peak_memory,
                'peak_memory_pct': memory_pct,
                'peak_cpu_millicores': peak_cpu,
                'peak_cpu_pct': cpu_pct,
                'snapshot_count': snapshot_count,
                'outcome': outcome,
                'state': bot.state,
                'join_at': bot.join_at.isoformat() if bot.join_at else None,
            })

            if peak_memory > overall_peak_memory:
                overall_peak_memory = peak_memory
            if peak_cpu > overall_peak_cpu:
                overall_peak_cpu = peak_cpu

        # Sort by peak memory descending
        bot_resources.sort(key=lambda x: x['peak_memory_mb'], reverse=True)

        # Count bots approaching limits
        high_memory_count = sum(1 for b in bot_resources if b['peak_memory_pct'] >= 80)
        high_cpu_count = sum(1 for b in bot_resources if b['peak_cpu_pct'] >= 80)

        return JsonResponse({
            'time_range_hours': hours,
            'limits': {
                'cpu_millicores': cpu_limit,
                'memory_mb': memory_limit_mb,
            },
            'summary': {
                'total_bots_with_data': len(bot_resources),
                'peak_memory_mb': overall_peak_memory,
                'peak_memory_pct': round((overall_peak_memory / memory_limit_mb) * 100, 1) if memory_limit_mb > 0 else 0,
                'peak_cpu_millicores': overall_peak_cpu,
                'peak_cpu_pct': round((overall_peak_cpu / cpu_limit) * 100, 1) if cpu_limit > 0 else 0,
                'high_memory_count': high_memory_count,
                'high_cpu_count': high_cpu_count,
            },
            'bots': bot_resources[:50],  # Limit to top 50
            'timestamp': timezone.now().isoformat(),
        })


class BotResourcesAPI(View):
    """Detailed resource time-series for a specific bot."""

    def get(self, request):
        bot_id = request.GET.get('bot_id')
        if not bot_id:
            return JsonResponse({'error': 'bot_id parameter required'}, status=400)

        try:
            # Support both numeric ID and object_id
            if bot_id.startswith('bot_'):
                bot = Bot.objects.get(object_id=bot_id)
            else:
                bot = Bot.objects.get(id=int(bot_id))
        except (Bot.DoesNotExist, ValueError):
            return JsonResponse({'error': 'Bot not found'}, status=404)

        # Get current resource limits
        import os
        cpu_limit = int(os.getenv('BOT_CPU_REQUEST', '1500').rstrip('m'))
        memory_limit_gb = float(os.getenv('BOT_MEMORY_LIMIT', '3Gi').rstrip('Gi'))
        memory_limit_mb = int(memory_limit_gb * 1024)

        # Get all snapshots for this bot
        snapshots = bot.resource_snapshots.order_by('created_at')

        time_series = []
        peak_memory = 0
        peak_cpu = 0
        peak_processes = []

        for snapshot in snapshots:
            data = snapshot.data
            ram = data.get('ram_usage_megabytes', 0)
            cpu = data.get('cpu_usage_millicores', 0)
            processes = data.get('processes', [])

            time_series.append({
                'timestamp': snapshot.created_at.isoformat(),
                'memory_mb': ram,
                'cpu_millicores': cpu,
            })

            if ram > peak_memory:
                peak_memory = ram
                peak_processes = processes
            if cpu > peak_cpu:
                peak_cpu = cpu

        # Get bot events for context
        events = []
        for event in bot.bot_events.order_by('created_at'):
            event_type_names = {
                1: 'Waiting Room', 2: 'Joined', 3: 'Recording Granted',
                4: 'Meeting Ended', 5: 'Left', 6: 'Join Requested',
                7: 'Fatal Error', 9: 'Could Not Join', 12: 'Staged'
            }
            events.append({
                'timestamp': event.created_at.isoformat(),
                'type': event_type_names.get(event.event_type, f'Event {event.event_type}'),
                'sub_type': event.event_sub_type,
            })

        # Determine outcome
        if bot.state == 7:
            last_event = bot.bot_events.order_by('-created_at').first()
            if last_event and last_event.event_sub_type == 13:
                outcome = 'Crashed (Heartbeat Timeout)'
            else:
                outcome = 'Failed'
        elif bot.state == 9:
            outcome = 'Completed'
        elif bot.state in [2, 3, 4]:
            outcome = 'Running'
        else:
            outcome = f'State {bot.state}'

        return JsonResponse({
            'bot': {
                'id': bot.id,
                'object_id': bot.object_id,
                'state': bot.state,
                'outcome': outcome,
                'join_at': bot.join_at.isoformat() if bot.join_at else None,
                'meeting_url': bot.meeting_url,
            },
            'limits': {
                'cpu_millicores': cpu_limit,
                'memory_mb': memory_limit_mb,
            },
            'peak': {
                'memory_mb': peak_memory,
                'memory_pct': round((peak_memory / memory_limit_mb) * 100, 1) if memory_limit_mb > 0 else 0,
                'cpu_millicores': peak_cpu,
                'cpu_pct': round((peak_cpu / cpu_limit) * 100, 1) if cpu_limit > 0 else 0,
                'processes': peak_processes,
            },
            'time_series': time_series,
            'events': events,
            'timestamp': timezone.now().isoformat(),
        })
