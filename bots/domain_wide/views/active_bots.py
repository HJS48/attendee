"""
Active pod APIs for dashboard - K8s pod-centric view with live metrics.
"""
import logging
import os
import re

from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone
from django.views import View
from datetime import timedelta

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


def _parse_cpu(cpu_str):
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


def _parse_memory(mem_str):
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


def _extract_bot_id_from_pod_name(pod_name):
    """
    Extract bot_id from pod name.
    Format: bot-pod-{id}-bot-{object_id_without_prefix}
    Example: bot-pod-31279-bot-ihkmphklu20clt07 -> bot_ihkMpHKLU20ClT07

    The object_id in pod name has _ replaced with - and is lowercased.
    We extract it and convert back to bot_ prefix format for DB lookup.
    """
    # Match: bot-pod-{numeric_id}-bot-{alphanum}
    match = re.search(r'bot-pod-\d+-bot-([a-z0-9]+)', pod_name, re.IGNORECASE)
    if match:
        # Convert back to bot_ format (DB lookup is case-insensitive)
        return f"bot_{match.group(1)}"
    return None


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


class ActiveBotPodsAPI(View):
    """API for active bot pods with live K8s metrics."""

    def get(self, request):
        from bots.domain_wide.views.kubernetes import _init_kubernetes_client

        limits = _get_resource_limits()
        namespace = getattr(settings, 'BOT_POD_NAMESPACE', 'attendee')

        running_pods = []
        pending_pods = []

        try:
            from kubernetes import client

            v1 = _init_kubernetes_client()

            # Query pods with app=bot-proc label (matches bot_pod_creator.py)
            pods = v1.list_namespaced_pod(
                namespace=namespace,
                label_selector='app=bot-proc'
            )

            # Get metrics from metrics-server
            metrics_by_pod = {}
            try:
                custom_api = client.CustomObjectsApi()
                pod_metrics = custom_api.list_namespaced_custom_object(
                    group="metrics.k8s.io",
                    version="v1beta1",
                    namespace=namespace,
                    plural="pods"
                )
                for pm in pod_metrics.get('items', []):
                    pod_name = pm.get('metadata', {}).get('name')
                    containers = pm.get('containers', [])
                    total_cpu = 0
                    total_memory = 0
                    for c in containers:
                        usage = c.get('usage', {})
                        total_cpu += _parse_cpu(usage.get('cpu', '0'))
                        total_memory += _parse_memory(usage.get('memory', '0'))
                    metrics_by_pod[pod_name] = {
                        'cpu_millicores': total_cpu,
                        'memory_bytes': total_memory,
                    }
            except Exception as e:
                logger.debug(f"Could not fetch pod metrics: {e}")

            # Extract bot_ids to look up DB info
            pod_bot_ids = []
            for pod in pods.items:
                bot_id = _extract_bot_id_from_pod_name(pod.metadata.name)
                if bot_id:
                    pod_bot_ids.append(bot_id)

            # Query DB for bot info (calendar events, state)
            # Note: pod names have lowercase bot_ids, DB has mixed case
            bots_by_id = {}
            if pod_bot_ids:
                # Use iregex for case-insensitive matching
                regex_pattern = '|'.join(f'^{bid}$' for bid in pod_bot_ids)
                bots = Bot.objects.filter(
                    object_id__iregex=regex_pattern
                ).select_related('calendar_event')
                for bot in bots:
                    # Store by lowercase key for lookup
                    bots_by_id[bot.object_id.lower()] = bot

            # Process each pod
            for pod in pods.items:
                pod_name = pod.metadata.name
                phase = (pod.status.phase or '').lower()
                bot_id = _extract_bot_id_from_pod_name(pod_name)

                # Get metrics
                metrics = metrics_by_pod.get(pod_name, {})
                cpu_millicores = metrics.get('cpu_millicores')
                memory_bytes = metrics.get('memory_bytes')

                # Calculate percentages
                cpu_pct = None
                memory_pct = None
                if cpu_millicores is not None and limits['cpu_limit_millicores'] > 0:
                    cpu_pct = round((cpu_millicores / limits['cpu_limit_millicores']) * 100, 1)
                if memory_bytes is not None and limits['memory_limit_bytes'] > 0:
                    memory_pct = round((memory_bytes / limits['memory_limit_bytes']) * 100, 1)

                # Get DB info if available (lookup by lowercase key)
                meeting_name = None
                calendar_event_id = None
                status = 'Running' if phase == 'running' else 'Pending'
                actual_bot_id = bot_id  # Will be updated if found in DB

                bot = bots_by_id.get(bot_id.lower() if bot_id else None)
                if bot:
                    actual_bot_id = bot.object_id  # Use actual case from DB
                    if bot.calendar_event:
                        meeting_name = bot.calendar_event.name or 'Untitled Event'
                        calendar_event_id = bot.calendar_event.object_id
                    status = _get_bot_state_label(bot)

                pod_data = {
                    'pod_name': pod_name,
                    'bot_id': actual_bot_id,
                    'meeting_name': meeting_name,
                    'calendar_event_id': calendar_event_id,
                    'cpu_millicores': cpu_millicores,
                    'cpu_pct': cpu_pct,
                    'memory_bytes': memory_bytes,
                    'memory_pct': memory_pct,
                    'status': status,
                }

                if phase == 'running':
                    running_pods.append(pod_data)
                elif phase == 'pending':
                    pending_pods.append(pod_data)

        except Exception as e:
            logger.exception(f"Failed to get K8s pod data: {e}")

        # Get bot pool status
        bot_pool = _get_bot_pool_status()

        return JsonResponse({
            'running': running_pods,
            'pending': pending_pods,
            'bot_pool': bot_pool,
            'limits': limits,
            'timestamp': timezone.now().isoformat(),
        })


# Keep old API as alias for backwards compatibility
class ActiveBotsAPI(ActiveBotPodsAPI):
    """Backwards-compatible alias for ActiveBotPodsAPI."""
    pass


class CompletedBotsAPI(View):
    """API for completed bots (ended today) with peak resource usage from DB."""

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
