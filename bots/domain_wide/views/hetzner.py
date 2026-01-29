"""
Hetzner Cloud-specific monitoring APIs.

Provides visibility into:
- Cluster autoscaler status (scale-up/scale-down events)
- Node pool breakdown (master, static worker, bot pool)
- Hetzner Cloud API health
- Cost tracking (estimated node-hours)
"""
import logging
import os
from datetime import datetime, timedelta

from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone
from django.views import View

from .kubernetes import _init_kubernetes_client

logger = logging.getLogger(__name__)


# Node type detection patterns
NODE_PATTERNS = {
    'master': ['master', 'control-plane', 'k3s-master'],
    'static-worker': ['static', 'worker-static', 'k3s-static'],
    'bot-pool': ['bot', 'pool', 'autoscale', 'k3s-pool'],
}

# Hetzner server type pricing (EUR/hour, approximate)
HETZNER_PRICING = {
    'cpx11': 0.007,   # 2 vCPU, 2GB RAM
    'cpx21': 0.014,   # 3 vCPU, 4GB RAM
    'cpx31': 0.028,   # 4 vCPU, 8GB RAM
    'cpx41': 0.056,   # 8 vCPU, 16GB RAM
    'cpx51': 0.112,   # 16 vCPU, 32GB RAM
    'cx22': 0.007,    # 2 vCPU, 4GB RAM (shared)
    'cx32': 0.014,    # 4 vCPU, 8GB RAM (shared)
    'cx42': 0.028,    # 8 vCPU, 16GB RAM (shared)
    'cx52': 0.056,    # 16 vCPU, 32GB RAM (shared)
    'default': 0.028, # Default to cpx31 pricing
}


def _classify_node(node_name: str, labels: dict) -> str:
    """Classify a node as master, static-worker, or bot-pool."""
    node_name_lower = node_name.lower()

    # Check labels first (most reliable)
    node_role = labels.get('node-role.kubernetes.io/master', '')
    if node_role or 'master' in labels.get('node-role.kubernetes.io/control-plane', ''):
        return 'master'

    pool_label = labels.get('hcloud/node-group', '') or labels.get('pool', '')
    if pool_label:
        pool_lower = pool_label.lower()
        if any(p in pool_lower for p in NODE_PATTERNS['bot-pool']):
            return 'bot-pool'
        if any(p in pool_lower for p in NODE_PATTERNS['static-worker']):
            return 'static-worker'

    # Fallback to name pattern matching
    for node_type, patterns in NODE_PATTERNS.items():
        if any(p in node_name_lower for p in patterns):
            return node_type

    return 'unknown'


def _get_server_type_from_labels(labels: dict) -> str:
    """Extract Hetzner server type from node labels."""
    # Hetzner CCM adds instance type labels
    instance_type = labels.get('node.kubernetes.io/instance-type', '')
    if instance_type:
        return instance_type.lower()

    # Alternative label locations
    for key in ['beta.kubernetes.io/instance-type', 'hcloud/server-type']:
        if key in labels:
            return labels[key].lower()

    return 'unknown'


class HetznerNodePoolsAPI(View):
    """API for Hetzner node pool breakdown and capacity."""

    def get(self, request):
        from kubernetes import client

        try:
            v1 = _init_kubernetes_client()
            nodes = v1.list_node()

            pools = {
                'master': {'count': 0, 'ready': 0, 'nodes': []},
                'static-worker': {'count': 0, 'ready': 0, 'nodes': []},
                'bot-pool': {'count': 0, 'ready': 0, 'nodes': []},
                'unknown': {'count': 0, 'ready': 0, 'nodes': []},
            }

            total_cpu_capacity = 0
            total_memory_capacity = 0

            for node in nodes.items:
                node_name = node.metadata.name
                labels = node.metadata.labels or {}

                # Classify node
                node_type = _classify_node(node_name, labels)

                # Check if ready
                is_ready = False
                for condition in (node.status.conditions or []):
                    if condition.type == 'Ready':
                        is_ready = condition.status == 'True'
                        break

                # Get capacity
                capacity = node.status.capacity or {}
                allocatable = node.status.allocatable or {}

                cpu = capacity.get('cpu', '0')
                memory = capacity.get('memory', '0')

                # Parse CPU to millicores
                if cpu.endswith('m'):
                    cpu_millicores = int(cpu[:-1])
                else:
                    cpu_millicores = int(cpu) * 1000

                # Parse memory to bytes
                memory_bytes = 0
                mem_str = memory
                multipliers = {'Ki': 1024, 'Mi': 1024**2, 'Gi': 1024**3}
                for suffix, mult in multipliers.items():
                    if mem_str.endswith(suffix):
                        memory_bytes = int(float(mem_str[:-len(suffix)]) * mult)
                        break

                total_cpu_capacity += cpu_millicores
                total_memory_capacity += memory_bytes

                # Get server type for cost estimation
                server_type = _get_server_type_from_labels(labels)

                # Calculate age
                age_hours = None
                if node.metadata.creation_timestamp:
                    age_hours = (timezone.now() - node.metadata.creation_timestamp).total_seconds() / 3600

                node_info = {
                    'name': node_name,
                    'ready': is_ready,
                    'server_type': server_type,
                    'cpu_cores': cpu_millicores // 1000,
                    'memory_gb': round(memory_bytes / (1024**3), 1),
                    'age_hours': round(age_hours, 1) if age_hours else None,
                }

                pools[node_type]['count'] += 1
                if is_ready:
                    pools[node_type]['ready'] += 1
                pools[node_type]['nodes'].append(node_info)

            # Summary
            total_nodes = sum(p['count'] for p in pools.values())
            total_ready = sum(p['ready'] for p in pools.values())

            return JsonResponse({
                'pools': pools,
                'summary': {
                    'total_nodes': total_nodes,
                    'total_ready': total_ready,
                    'total_cpu_cores': total_cpu_capacity // 1000,
                    'total_memory_gb': round(total_memory_capacity / (1024**3), 1),
                },
                'timestamp': timezone.now().isoformat(),
            })

        except Exception as e:
            logger.exception(f"Failed to get Hetzner node pools: {e}")
            return JsonResponse({
                'error': str(e),
                'pools': {},
                'summary': {},
            }, status=500)


class HetznerAutoscalerStatusAPI(View):
    """API for cluster autoscaler status and events."""

    def get(self, request):
        from kubernetes import client

        try:
            v1 = _init_kubernetes_client()

            # Get autoscaler-related events from kube-system namespace
            events = v1.list_namespaced_event(
                namespace='kube-system',
                limit=100,
            )

            autoscaler_events = []
            scale_up_count = 0
            scale_down_count = 0

            for event in events.items:
                # Filter to autoscaler events
                if not event.involved_object:
                    continue

                obj_name = event.involved_object.name or ''
                reason = event.reason or ''
                message = event.message or ''

                is_autoscaler = (
                    'autoscaler' in obj_name.lower() or
                    'scale' in reason.lower() or
                    'ScaleUp' in reason or
                    'ScaleDown' in reason or
                    'TriggeredScaleUp' in reason or
                    'cluster-autoscaler' in obj_name.lower()
                )

                if not is_autoscaler:
                    continue

                event_time = event.last_timestamp or event.event_time or event.metadata.creation_timestamp
                age_seconds = None
                if event_time:
                    age_seconds = int((timezone.now() - event_time).total_seconds())

                # Categorize event
                event_category = 'info'
                if 'ScaleUp' in reason or 'scale up' in message.lower():
                    event_category = 'scale-up'
                    scale_up_count += 1
                elif 'ScaleDown' in reason or 'scale down' in message.lower():
                    event_category = 'scale-down'
                    scale_down_count += 1
                elif event.type == 'Warning':
                    event_category = 'warning'

                autoscaler_events.append({
                    'timestamp': event_time.isoformat() if event_time else None,
                    'age_seconds': age_seconds,
                    'type': event.type,
                    'category': event_category,
                    'reason': reason,
                    'message': message[:200],
                    'object': f"{event.involved_object.kind}/{obj_name}",
                })

            # Sort by timestamp (most recent first)
            autoscaler_events.sort(key=lambda x: x['age_seconds'] or float('inf'))

            # Get current pending pods (waiting for capacity)
            namespace = getattr(settings, 'BOT_POD_NAMESPACE', 'attendee')
            pending_pods = []
            try:
                pods = v1.list_namespaced_pod(namespace=namespace)
                for pod in pods.items:
                    if pod.status.phase == 'Pending':
                        # Check if pending due to insufficient resources
                        conditions = pod.status.conditions or []
                        reason = None
                        for cond in conditions:
                            if cond.type == 'PodScheduled' and cond.status == 'False':
                                reason = cond.reason
                                break

                        age_seconds = None
                        if pod.metadata.creation_timestamp:
                            age_seconds = int((timezone.now() - pod.metadata.creation_timestamp).total_seconds())

                        pending_pods.append({
                            'name': pod.metadata.name,
                            'reason': reason,
                            'age_seconds': age_seconds,
                        })
            except Exception as e:
                logger.warning(f"Failed to get pending pods: {e}")

            return JsonResponse({
                'events': autoscaler_events[:50],  # Last 50 events
                'summary': {
                    'scale_up_events_recent': scale_up_count,
                    'scale_down_events_recent': scale_down_count,
                    'pending_pods': len(pending_pods),
                },
                'pending_pods': pending_pods,
                'timestamp': timezone.now().isoformat(),
            })

        except Exception as e:
            logger.exception(f"Failed to get autoscaler status: {e}")
            return JsonResponse({
                'error': str(e),
                'events': [],
                'summary': {},
                'pending_pods': [],
            }, status=500)


class HetznerCostEstimateAPI(View):
    """API for estimated Hetzner cloud costs based on node hours."""

    def get(self, request):
        from kubernetes import client

        # Time range for cost calculation
        hours_param = request.GET.get('hours', '24')
        try:
            hours = min(int(hours_param), 720)  # Max 30 days
        except ValueError:
            hours = 24

        try:
            v1 = _init_kubernetes_client()
            nodes = v1.list_node()

            node_costs = []
            total_cost = 0.0
            cost_by_pool = {
                'master': 0.0,
                'static-worker': 0.0,
                'bot-pool': 0.0,
                'unknown': 0.0,
            }

            for node in nodes.items:
                node_name = node.metadata.name
                labels = node.metadata.labels or {}

                # Get node type and server type
                node_type = _classify_node(node_name, labels)
                server_type = _get_server_type_from_labels(labels)

                # Get hourly rate
                hourly_rate = HETZNER_PRICING.get(server_type, HETZNER_PRICING['default'])

                # Calculate node age (capped at requested hours)
                node_hours = hours
                if node.metadata.creation_timestamp:
                    age_hours = (timezone.now() - node.metadata.creation_timestamp).total_seconds() / 3600
                    node_hours = min(age_hours, hours)

                # Calculate cost
                node_cost = hourly_rate * node_hours
                total_cost += node_cost
                cost_by_pool[node_type] += node_cost

                node_costs.append({
                    'name': node_name,
                    'type': node_type,
                    'server_type': server_type,
                    'hourly_rate_eur': hourly_rate,
                    'hours': round(node_hours, 1),
                    'cost_eur': round(node_cost, 2),
                })

            # Project monthly cost based on current usage
            monthly_projection = (total_cost / hours) * 720 if hours > 0 else 0

            return JsonResponse({
                'time_range_hours': hours,
                'nodes': node_costs,
                'summary': {
                    'total_cost_eur': round(total_cost, 2),
                    'monthly_projection_eur': round(monthly_projection, 2),
                    'cost_by_pool': {k: round(v, 2) for k, v in cost_by_pool.items()},
                    'node_count': len(node_costs),
                },
                'pricing_note': 'Estimates based on Hetzner Cloud list prices. Actual costs may vary.',
                'timestamp': timezone.now().isoformat(),
            })

        except Exception as e:
            logger.exception(f"Failed to calculate Hetzner costs: {e}")
            return JsonResponse({
                'error': str(e),
                'time_range_hours': hours,
                'nodes': [],
                'summary': {},
            }, status=500)


class HetznerCloudHealthAPI(View):
    """API for Hetzner Cloud API and CCM health status."""

    def get(self, request):
        from kubernetes import client
        import time

        health = {
            'hetzner_api': {'status': 'unknown', 'latency_ms': None},
            'cloud_controller': {'status': 'unknown', 'running': False},
            'autoscaler': {'status': 'unknown', 'running': False},
            'load_balancer': {'status': 'unknown'},
        }

        try:
            v1 = _init_kubernetes_client()

            # Check CCM (Cloud Controller Manager) pod status
            try:
                ccm_pods = v1.list_namespaced_pod(
                    namespace='kube-system',
                    label_selector='app.kubernetes.io/name=hcloud-cloud-controller-manager'
                )
                for pod in ccm_pods.items:
                    if pod.status.phase == 'Running':
                        health['cloud_controller']['running'] = True
                        health['cloud_controller']['status'] = 'healthy'
                        break

                if not health['cloud_controller']['running']:
                    # Try alternative label
                    ccm_pods = v1.list_namespaced_pod(
                        namespace='kube-system',
                        label_selector='app=hcloud-cloud-controller-manager'
                    )
                    for pod in ccm_pods.items:
                        if pod.status.phase == 'Running':
                            health['cloud_controller']['running'] = True
                            health['cloud_controller']['status'] = 'healthy'
                            break
            except Exception as e:
                logger.warning(f"Failed to check CCM status: {e}")
                health['cloud_controller']['status'] = 'error'
                health['cloud_controller']['error'] = str(e)

            # Check autoscaler pod status
            try:
                # Check for cluster-autoscaler or hcloud-autoscaler
                for label in ['app=cluster-autoscaler', 'app.kubernetes.io/name=cluster-autoscaler', 'app=hcloud-autoscaler']:
                    autoscaler_pods = v1.list_namespaced_pod(
                        namespace='kube-system',
                        label_selector=label
                    )
                    for pod in autoscaler_pods.items:
                        if pod.status.phase == 'Running':
                            health['autoscaler']['running'] = True
                            health['autoscaler']['status'] = 'healthy'
                            break
                    if health['autoscaler']['running']:
                        break
            except Exception as e:
                logger.warning(f"Failed to check autoscaler status: {e}")
                health['autoscaler']['status'] = 'error'
                health['autoscaler']['error'] = str(e)

            # Check Hetzner API connectivity via CCM events
            # If CCM is running and no recent errors, API is likely healthy
            if health['cloud_controller']['running']:
                try:
                    events = v1.list_namespaced_event(
                        namespace='kube-system',
                        field_selector='involvedObject.name=hcloud-cloud-controller-manager',
                        limit=10
                    )

                    recent_errors = 0
                    for event in events.items:
                        if event.type == 'Warning':
                            event_time = event.last_timestamp or event.event_time
                            if event_time:
                                age = (timezone.now() - event_time).total_seconds()
                                if age < 3600:  # Last hour
                                    recent_errors += 1

                    if recent_errors == 0:
                        health['hetzner_api']['status'] = 'healthy'
                    else:
                        health['hetzner_api']['status'] = 'degraded'
                        health['hetzner_api']['recent_errors'] = recent_errors
                except Exception as e:
                    logger.warning(f"Failed to check Hetzner API health: {e}")

            # Check Load Balancer service status
            try:
                services = v1.list_service_for_all_namespaces(
                    field_selector='spec.type=LoadBalancer'
                )
                lb_count = 0
                lb_ready = 0
                for svc in services.items:
                    lb_count += 1
                    ingress = svc.status.load_balancer.ingress
                    if ingress and len(ingress) > 0:
                        lb_ready += 1

                health['load_balancer']['total'] = lb_count
                health['load_balancer']['ready'] = lb_ready
                health['load_balancer']['status'] = 'healthy' if lb_ready == lb_count else 'degraded'
            except Exception as e:
                logger.warning(f"Failed to check LB status: {e}")
                health['load_balancer']['status'] = 'error'

            return JsonResponse({
                'health': health,
                'timestamp': timezone.now().isoformat(),
            })

        except Exception as e:
            logger.exception(f"Failed to get Hetzner cloud health: {e}")
            return JsonResponse({
                'error': str(e),
                'health': health,
            }, status=500)
