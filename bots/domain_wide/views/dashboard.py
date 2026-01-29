"""
Dashboard views - main health dashboard and summary APIs.
"""
import logging
from collections import Counter
from datetime import datetime, timedelta

from django.db.models import Count
from django.db.models.functions import TruncDate
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views import View

from bots.models import (
    Bot, BotStates, Calendar, CalendarEvent, BotEvent, BotEventTypes,
    Recording, RecordingStates, RecordingTranscriptionStates,
)

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
        # Support filtering by specific date (using bot's scheduled join_at date)
        date_str = request.GET.get('date')
        if date_str:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
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

        # Map all BotEventSubTypes to short, readable labels
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
