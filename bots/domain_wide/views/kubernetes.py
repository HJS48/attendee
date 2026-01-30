"""
Kubernetes monitoring and infrastructure status APIs.
"""
import logging
import os
import time

from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone
from django.views import View

from bots.models import Bot, BotStates, BotEvent, BotEventTypes

logger = logging.getLogger(__name__)


def _init_kubernetes_client():
    """Initialize Kubernetes client with proper configuration."""
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


class InfrastructureStatusAPI(View):
    """API for infrastructure status (containers/kubernetes + Celery)."""

    def _detect_mode(self):
        """Detect if running in Kubernetes or Docker mode."""
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
            memory_allocatable = 0

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
                cpu_allocatable += _parse_cpu(cpu_str)
                memory_allocatable += _parse_memory(mem_str)

            # Get resource requests from pods
            cpu_requested = 0
            memory_requested = 0
            for ns in namespaces_to_check:
                try:
                    pods = v1.list_namespaced_pod(namespace=ns)
                    for pod in pods.items:
                        if pod.status.phase not in ['Running', 'Pending']:
                            continue
                        for container in (pod.spec.containers or []):
                            requests = (container.resources.requests or {}) if container.resources else {}
                            cpu_requested += _parse_cpu(requests.get('cpu', '0'))
                            memory_requested += _parse_memory(requests.get('memory', '0'))
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

            # Try to get queue length from Redis
            try:
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

                        # Extract bot_id from pod name
                        bot_id = None
                        pod_name = pod.metadata.name
                        if pod_name.startswith('bot-'):
                            parts = pod_name.split('-')
                            if len(parts) >= 2:
                                for part in parts:
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
                cpu_allocatable += _parse_cpu(allocatable.get('cpu', '0'))
                memory_allocatable += _parse_memory(allocatable.get('memory', '0'))

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
                                cpu_requested += _parse_cpu(requests.get('cpu', '0'))
                                memory_requested += _parse_memory(requests.get('memory', '0'))

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
                            if cs.restart_count > 3:
                                alerts.append({
                                    'severity': 'critical',
                                    'type': 'pod_crash_loop',
                                    'message': f'Pod {pod_name} container {cs.name} restarted {cs.restart_count} times',
                                    'resource': pod_name,
                                })

                            if cs.state:
                                if cs.state.waiting:
                                    reason = cs.state.waiting.reason
                                    if reason in ['ImagePullBackOff', 'ErrImagePull']:
                                        alerts.append({
                                            'severity': 'critical',
                                            'type': 'pod_image_pull_error',
                                            'message': f'Pod {pod_name} has {reason}',
                                            'resource': pod_name,
                                        })

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


class KubernetesBotLookupAPI(View):
    """API for cross-referencing bot database record with Kubernetes pod."""

    def get(self, request):
        from kubernetes import client

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
            if 'bot_' in pod_name:
                extracted_id = pod_name[pod_name.index('bot_'):]
                if '-' in extracted_id:
                    extracted_id = extracted_id.split('-')[0]
                bot = Bot.objects.filter(object_id=extracted_id).first()

        if bot:
            result['found'] = True

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
            v1 = _init_kubernetes_client()

            namespaces = [
                getattr(settings, 'BOT_POD_NAMESPACE', 'attendee'),
                getattr(settings, 'WEBPAGE_STREAMER_POD_NAMESPACE', 'attendee-webpage-streamer'),
            ]

            target_pod = None
            target_ns = None

            for ns in namespaces:
                try:
                    if pod_name:
                        try:
                            target_pod = v1.read_namespaced_pod(name=pod_name, namespace=ns)
                            target_ns = ns
                            break
                        except client.ApiException:
                            continue
                    elif bot_id:
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
                    for event in events.items[-10:]:
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
        from kubernetes import client

        try:
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

                        # Check conditions
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
        from kubernetes import client

        try:
            _init_kubernetes_client()
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
                        total_cpu += _parse_cpu(usage.get('cpu', '0'))
                        total_memory += _parse_memory(usage.get('memory', '0'))

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


class SystemPodsAPI(View):
    """API for system (non-bot) pods with resource usage."""

    # Known system pod name prefixes
    SYSTEM_POD_PREFIXES = [
        'attendee-api',
        'attendee-worker',
        'attendee-scheduler',
        'attendee-beat',
        'postgres',
        'redis',
        'webpage-streamer',
    ]

    def get(self, request):
        from kubernetes import client

        try:
            v1 = _init_kubernetes_client()

            namespace = getattr(settings, 'BOT_POD_NAMESPACE', 'attendee')

            pods = v1.list_namespaced_pod(namespace=namespace)
            system_pods = []

            # Try to get metrics if available
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
            except Exception:
                pass  # Metrics server may not be available

            for pod in pods.items:
                pod_name = pod.metadata.name

                # Skip bot pods (they start with 'bot-')
                if pod_name.startswith('bot-'):
                    continue

                # Check if it's a known system pod
                is_system = False
                service_name = pod_name
                for prefix in self.SYSTEM_POD_PREFIXES:
                    if pod_name.startswith(prefix):
                        is_system = True
                        service_name = prefix.replace('attendee-', '').capitalize()
                        break

                if not is_system:
                    continue

                # Get pod status
                phase = pod.status.phase or 'Unknown'
                ready = False
                for cs in (pod.status.container_statuses or []):
                    if cs.ready:
                        ready = True
                        break

                status = 'Ready' if ready and phase == 'Running' else phase

                # Get metrics if available
                metrics = metrics_by_pod.get(pod_name, {})

                # Format memory nicely
                memory_bytes = metrics.get('memory_bytes', 0)
                if memory_bytes >= 1024 * 1024 * 1024:
                    memory_str = f"{memory_bytes / (1024*1024*1024):.1f}Gi"
                elif memory_bytes >= 1024 * 1024:
                    memory_str = f"{memory_bytes / (1024*1024):.0f}Mi"
                else:
                    memory_str = f"{memory_bytes / 1024:.0f}Ki"

                system_pods.append({
                    'name': service_name,
                    'pod_name': pod_name,
                    'cpu_millicores': metrics.get('cpu_millicores'),
                    'memory_bytes': memory_bytes,
                    'memory_display': memory_str if memory_bytes > 0 else None,
                    'status': status,
                    'ready': ready,
                })

            # Sort by service name
            system_pods.sort(key=lambda x: x['name'])

            return JsonResponse({
                'pods': system_pods,
                'timestamp': timezone.now().isoformat(),
            })

        except Exception as e:
            logger.exception(f"Failed to get system pods: {e}")
            return JsonResponse({
                'error': str(e),
                'pods': [],
            }, status=500)
