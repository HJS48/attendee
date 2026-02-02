"""
Resource monitoring APIs for bot CPU/memory usage.
"""
import logging
import os
from datetime import timedelta

from django.http import JsonResponse
from django.utils import timezone
from django.views import View

from bots.models import Bot

logger = logging.getLogger(__name__)


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
            # Support both numeric ID and object_id (case-insensitive)
            if bot_id.startswith('bot_') or bot_id.startswith('bot-'):
                bot = Bot.objects.get(object_id__iexact=bot_id)
            else:
                bot = Bot.objects.get(id=int(bot_id))
        except (Bot.DoesNotExist, ValueError):
            return JsonResponse({'error': 'Bot not found'}, status=404)

        # Get current resource limits
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


class BotActivityLogAPI(View):
    """Activity timeline for a bot - shows UI milestones for debugging failed joins."""

    def get(self, request):
        from bots.models import BotActivityLog, BotStates

        bot_id = request.GET.get('bot_id')
        if not bot_id:
            return JsonResponse({'error': 'bot_id parameter required'}, status=400)

        try:
            if bot_id.startswith('bot_') or bot_id.startswith('bot-'):
                bot = Bot.objects.get(object_id__iexact=bot_id)
            else:
                bot = Bot.objects.get(id=int(bot_id))
        except (Bot.DoesNotExist, ValueError):
            return JsonResponse({'error': 'Bot not found'}, status=404)

        # Get activity logs
        activities = BotActivityLog.objects.filter(bot=bot).order_by('created_at')

        # Determine if we should show the timeline (only for failed bots)
        is_failed = bot.state == BotStates.FATAL_ERROR

        return JsonResponse({
            'bot_id': bot.object_id,
            'state': bot.get_state_display(),
            'is_failed': is_failed,
            'show_timeline': is_failed or activities.exists(),  # Show if failed OR has any logs
            'activities': [
                {
                    'timestamp': a.created_at.isoformat(),
                    'time_display': a.created_at.strftime('%H:%M:%S'),
                    'type': a.get_activity_type_display(),
                    'type_code': a.activity_type,
                    'message': a.message,
                    'elapsed_ms': a.elapsed_ms,
                    'elapsed_display': f'+{a.elapsed_ms}ms' if a.elapsed_ms else None,
                    'is_error': a.activity_type >= 10 and a.activity_type < 20,
                }
                for a in activities
            ],
            'timestamp': timezone.now().isoformat(),
        })
