"""
Active bots APIs for dashboard - bot-centric view with calendar event links.
"""
import logging
import os
from datetime import timedelta

from django.http import JsonResponse
from django.utils import timezone
from django.views import View

from bots.models import Bot, BotStates

logger = logging.getLogger(__name__)


# States for running bots (in meeting or about to join)
RUNNING_STATES = [
    BotStates.JOINING,
    BotStates.JOINED_NOT_RECORDING,
    BotStates.JOINED_RECORDING,
    BotStates.LEAVING,
    BotStates.WAITING_ROOM,
    BotStates.JOINED_RECORDING_PAUSED,
    BotStates.JOINING_BREAKOUT_ROOM,
    BotStates.LEAVING_BREAKOUT_ROOM,
    BotStates.JOINED_RECORDING_PERMISSION_DENIED,
]

# States for pending bots (scheduled/staged but not yet in meeting)
PENDING_STATES = [
    BotStates.READY,
    BotStates.SCHEDULED,
    BotStates.STAGED,
]


def _get_resource_limits():
    """Get bot resource limits from environment."""
    cpu_request = os.getenv('BOT_CPU_REQUEST', '3500m')
    cpu_limit = os.getenv('BOT_CPU_LIMIT', '4000m')
    memory_request = os.getenv('BOT_MEMORY_REQUEST', '2Gi')
    memory_limit = os.getenv('BOT_MEMORY_LIMIT', '8Gi')

    # Parse values
    def parse_cpu(val):
        val = str(val)
        if val.endswith('m'):
            return int(val[:-1])
        return int(float(val) * 1000)

    def parse_memory(val):
        val = str(val)
        if val.endswith('Gi'):
            return int(float(val[:-2]) * 1024 * 1024 * 1024)
        if val.endswith('Mi'):
            return int(float(val[:-2]) * 1024 * 1024)
        if val.endswith('G'):
            return int(float(val[:-1]) * 1000 * 1000 * 1000)
        return int(val)

    return {
        'cpu_request_millicores': parse_cpu(cpu_request),
        'cpu_limit_millicores': parse_cpu(cpu_limit),
        'memory_request_bytes': parse_memory(memory_request),
        'memory_limit_bytes': parse_memory(memory_limit),
    }


def _get_bot_state_label(bot):
    """Get a human-friendly state label."""
    state = bot.state
    if state == BotStates.JOINING:
        return 'Joining'
    if state == BotStates.JOINED_NOT_RECORDING:
        return 'In meeting'
    if state == BotStates.JOINED_RECORDING:
        return 'Recording'
    if state == BotStates.LEAVING:
        return 'Leaving'
    if state == BotStates.WAITING_ROOM:
        return 'Waiting room'
    if state == BotStates.JOINED_RECORDING_PAUSED:
        return 'Paused'
    if state == BotStates.JOINING_BREAKOUT_ROOM:
        return 'Breakout'
    if state == BotStates.LEAVING_BREAKOUT_ROOM:
        return 'Leaving breakout'
    if state == BotStates.JOINED_RECORDING_PERMISSION_DENIED:
        return 'Permission denied'
    if state == BotStates.READY:
        return 'Ready'
    if state == BotStates.SCHEDULED:
        return 'Scheduled'
    if state == BotStates.STAGED:
        return 'Staged'
    if state == BotStates.ENDED:
        return 'Ended'
    if state == BotStates.FATAL_ERROR:
        return 'Failed'
    return BotStates(state).label if state in BotStates.values else f'State {state}'


class ActiveBotsAPI(View):
    """API for currently running/pending bots with calendar event info and resource usage."""

    def get(self, request):
        limits = _get_resource_limits()

        # Get running bots
        running_bots = Bot.objects.filter(
            state__in=[s.value for s in RUNNING_STATES]
        ).select_related('calendar_event').order_by('-join_at')

        # Get pending bots
        pending_bots = Bot.objects.filter(
            state__in=[s.value for s in PENDING_STATES]
        ).select_related('calendar_event').order_by('join_at')

        bots_data = []

        for bot in list(running_bots) + list(pending_bots):
            # Get latest resource snapshot
            latest_snapshot = bot.resource_snapshots.order_by('-created_at').first()
            cpu_actual = None
            memory_actual = None
            if latest_snapshot and latest_snapshot.data:
                cpu_actual = latest_snapshot.data.get('cpu_usage_millicores')
                memory_actual_mb = latest_snapshot.data.get('ram_usage_megabytes')
                if memory_actual_mb is not None:
                    memory_actual = int(memory_actual_mb * 1024 * 1024)  # Convert MB to bytes

            # Get calendar event info
            calendar_event = bot.calendar_event
            meeting_name = None
            calendar_event_id = None
            if calendar_event:
                meeting_name = calendar_event.name or 'Untitled Event'
                calendar_event_id = calendar_event.object_id

            bots_data.append({
                'object_id': bot.object_id,
                'meeting_name': meeting_name,
                'calendar_event_id': calendar_event_id,
                'cpu_actual_millicores': cpu_actual,
                'cpu_request_millicores': limits['cpu_request_millicores'],
                'cpu_limit_millicores': limits['cpu_limit_millicores'],
                'memory_actual_bytes': memory_actual,
                'memory_request_bytes': limits['memory_request_bytes'],
                'memory_limit_bytes': limits['memory_limit_bytes'],
                'state': _get_bot_state_label(bot),
                'state_raw': bot.state,
                'is_running': bot.state in [s.value for s in RUNNING_STATES],
                'join_at': bot.join_at.isoformat() if bot.join_at else None,
            })

        # Get bot pool status
        bot_pool = _get_bot_pool_status()

        return JsonResponse({
            'bots': bots_data,
            'bot_pool': bot_pool,
            'running_count': len(running_bots),
            'pending_count': len(pending_bots),
            'limits': limits,
            'timestamp': timezone.now().isoformat(),
        })


class CompletedBotsAPI(View):
    """API for completed bots (ended today) with peak resource usage."""

    def get(self, request):
        # Get date filter (default: today)
        date_str = request.GET.get('date')
        if date_str:
            try:
                from datetime import datetime
                target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            except ValueError:
                target_date = timezone.now().date()
        else:
            target_date = timezone.now().date()

        # Query bots that ended on the target date
        start_of_day = timezone.make_aware(
            timezone.datetime.combine(target_date, timezone.datetime.min.time())
        )
        end_of_day = start_of_day + timedelta(days=1)

        # Get ended/failed bots for the day
        completed_bots = Bot.objects.filter(
            state__in=[BotStates.ENDED, BotStates.FATAL_ERROR, BotStates.DATA_DELETED],
            resource_snapshots__created_at__gte=start_of_day,
            resource_snapshots__created_at__lt=end_of_day,
        ).select_related('calendar_event').distinct().order_by('-id')

        limits = _get_resource_limits()
        bots_data = []

        for bot in completed_bots:
            # Get peak resource usage from snapshots
            snapshots = bot.resource_snapshots.filter(
                created_at__gte=start_of_day,
                created_at__lt=end_of_day,
            )

            peak_cpu = 0
            peak_memory_mb = 0
            ended_at = None

            for snapshot in snapshots:
                data = snapshot.data or {}
                cpu = data.get('cpu_usage_millicores', 0) or 0
                memory = data.get('ram_usage_megabytes', 0) or 0
                if cpu > peak_cpu:
                    peak_cpu = cpu
                if memory > peak_memory_mb:
                    peak_memory_mb = memory
                # Track latest snapshot as approximate end time
                if ended_at is None or snapshot.created_at > ended_at:
                    ended_at = snapshot.created_at

            peak_memory_bytes = int(peak_memory_mb * 1024 * 1024)

            # Calculate percentages of limit
            cpu_pct = round((peak_cpu / limits['cpu_limit_millicores']) * 100, 1) if limits['cpu_limit_millicores'] > 0 else 0
            memory_pct = round((peak_memory_bytes / limits['memory_limit_bytes']) * 100, 1) if limits['memory_limit_bytes'] > 0 else 0

            # Determine outcome
            if bot.state == BotStates.FATAL_ERROR:
                last_event = bot.bot_events.order_by('-created_at').first()
                if last_event and last_event.event_sub_type == 13:  # HEARTBEAT_TIMEOUT
                    outcome = 'Crashed'
                else:
                    outcome = 'Failed'
            elif bot.state == BotStates.DATA_DELETED:
                outcome = 'Deleted'
            else:
                outcome = 'Completed'

            # Get calendar event info
            calendar_event = bot.calendar_event
            meeting_name = None
            calendar_event_id = None
            if calendar_event:
                meeting_name = calendar_event.name or 'Untitled Event'
                calendar_event_id = calendar_event.object_id

            bots_data.append({
                'object_id': bot.object_id,
                'meeting_name': meeting_name,
                'calendar_event_id': calendar_event_id,
                'peak_cpu_millicores': peak_cpu,
                'peak_cpu_pct': cpu_pct,
                'peak_memory_bytes': peak_memory_bytes,
                'peak_memory_pct': memory_pct,
                'outcome': outcome,
                'ended_at': ended_at.isoformat() if ended_at else None,
            })

        return JsonResponse({
            'bots': bots_data,
            'total_count': len(bots_data),
            'date': target_date.isoformat(),
            'limits': limits,
            'timestamp': timezone.now().isoformat(),
        })


class CalendarEventDetailAPI(View):
    """API for calendar event details (for modal popup)."""

    def get(self, request, event_id):
        from bots.models import CalendarEvent

        try:
            event = CalendarEvent.objects.get(object_id=event_id)
        except CalendarEvent.DoesNotExist:
            return JsonResponse({'error': 'Event not found'}, status=404)

        # Get attendees (stored as JSON)
        attendees = event.attendees or []

        return JsonResponse({
            'event_id': event.object_id,
            'name': event.name or 'Untitled Event',
            'start_time': event.start_time.isoformat() if event.start_time else None,
            'end_time': event.end_time.isoformat() if event.end_time else None,
            'meeting_url': event.meeting_url,
            'attendees': attendees[:20],  # Limit to first 20
            'attendees_count': len(attendees),
            'calendar_owner': event.calendar.user.email if event.calendar and event.calendar.user else None,
        })


def _get_bot_pool_status():
    """Get bot pool node status from Kubernetes."""
    try:
        from bots.domain_wide.views.kubernetes import _init_kubernetes_client

        v1 = _init_kubernetes_client()

        # Count nodes with workload=bots label
        nodes = v1.list_node(label_selector='workload=bots')
        active_nodes = 0
        for node in nodes.items:
            # Check if node is Ready
            for condition in (node.status.conditions or []):
                if condition.type == 'Ready' and condition.status == 'True':
                    active_nodes += 1
                    break

        # Get max nodes from autoscaler configmap
        max_nodes = int(os.getenv('BOT_POOL_MAX_NODES', '20'))

        return {
            'active_nodes': active_nodes,
            'max_nodes': max_nodes,
        }

    except Exception as e:
        logger.debug(f"Could not get bot pool status: {e}")
        return {
            'active_nodes': None,
            'max_nodes': None,
        }


class BotPoolStatusAPI(View):
    """API for bot pool node status."""

    def get(self, request):
        status = _get_bot_pool_status()
        return JsonResponse({
            **status,
            'timestamp': timezone.now().isoformat(),
        })
