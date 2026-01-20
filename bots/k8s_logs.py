"""
Kubernetes Log Streaming Utility

Provides classes and functions for accessing and streaming logs from
Kubernetes pods, particularly bot pods in the attendee namespace.

Usage:
    from bots.k8s_logs import K8sLogStreamer

    streamer = K8sLogStreamer()

    # List all bot pods
    pods = streamer.list_bot_pods()

    # Get logs from a specific pod
    logs = streamer.get_pod_logs('bot-abc123')

    # Stream logs from a pod
    for line in streamer.stream_pod_logs('bot-abc123'):
        print(line)
"""

import logging
from typing import Generator, List, Optional

from django.conf import settings

logger = logging.getLogger(__name__)


def _init_kubernetes_client():
    """Initialize and return Kubernetes CoreV1Api client."""
    from kubernetes import client, config

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()

    return client.CoreV1Api()


class K8sLogStreamer:
    """
    Kubernetes log streaming utility for accessing pod logs.

    Provides methods to:
    - List bot pods in the attendee namespace
    - Get logs from specific pods
    - Stream logs in real-time
    """

    def __init__(self, namespace: Optional[str] = None):
        """
        Initialize the log streamer.

        Args:
            namespace: Kubernetes namespace. Defaults to BOT_POD_NAMESPACE setting.
        """
        self.namespace = namespace or getattr(settings, 'BOT_POD_NAMESPACE', 'attendee')
        self._v1 = None

    @property
    def v1(self):
        """Lazy-load Kubernetes client."""
        if self._v1 is None:
            self._v1 = _init_kubernetes_client()
        return self._v1

    def list_bot_pods(self, include_terminated: bool = False) -> List[dict]:
        """
        List all bot pods in the namespace.

        Args:
            include_terminated: If True, include pods that are not Running.

        Returns:
            List of pod info dictionaries with keys: name, status, created_at, bot_id
        """
        try:
            pods = self.v1.list_namespaced_pod(namespace=self.namespace)

            bot_pods = []
            for pod in pods.items:
                # Filter to only bot pods (name starts with 'bot-')
                if not pod.metadata.name.startswith('bot-'):
                    continue

                status = pod.status.phase
                if not include_terminated and status not in ['Running', 'Pending']:
                    continue

                # Extract bot ID from pod name (format: bot-{object_id})
                bot_id = pod.metadata.name.replace('bot-', '', 1) if pod.metadata.name.startswith('bot-') else None

                bot_pods.append({
                    'name': pod.metadata.name,
                    'status': status,
                    'created_at': pod.metadata.creation_timestamp.isoformat() if pod.metadata.creation_timestamp else None,
                    'bot_id': bot_id,
                    'node': pod.spec.node_name,
                    'containers': [c.name for c in pod.spec.containers] if pod.spec.containers else [],
                })

            # Sort by creation time, newest first
            bot_pods.sort(key=lambda x: x['created_at'] or '', reverse=True)
            return bot_pods

        except Exception as e:
            logger.error(f"Failed to list bot pods: {e}")
            return []

    def get_pod_logs(
        self,
        pod_name: str,
        container: Optional[str] = None,
        tail_lines: int = 100,
        timestamps: bool = True
    ) -> str:
        """
        Get logs from a specific pod.

        Args:
            pod_name: Name of the pod.
            container: Container name (optional, uses first container if not specified).
            tail_lines: Number of lines to return from the end.
            timestamps: Include timestamps in log lines.

        Returns:
            Log content as a string.
        """
        try:
            logs = self.v1.read_namespaced_pod_log(
                name=pod_name,
                namespace=self.namespace,
                container=container,
                tail_lines=tail_lines,
                timestamps=timestamps,
            )
            return logs
        except Exception as e:
            logger.error(f"Failed to get logs for pod {pod_name}: {e}")
            return f"Error getting logs: {e}"

    def stream_pod_logs(
        self,
        pod_name: str,
        container: Optional[str] = None,
        tail_lines: int = 100,
        timestamps: bool = True,
        timeout: int = 300
    ) -> Generator[str, None, None]:
        """
        Stream logs from a pod in real-time.

        Args:
            pod_name: Name of the pod.
            container: Container name (optional).
            tail_lines: Initial number of lines to return.
            timestamps: Include timestamps.
            timeout: Stream timeout in seconds.

        Yields:
            Log lines as strings.
        """
        from kubernetes import watch

        try:
            w = watch.Watch()
            for line in w.stream(
                self.v1.read_namespaced_pod_log,
                name=pod_name,
                namespace=self.namespace,
                container=container,
                follow=True,
                tail_lines=tail_lines,
                timestamps=timestamps,
                _request_timeout=timeout
            ):
                yield line

        except Exception as e:
            logger.error(f"Log stream error for pod {pod_name}: {e}")
            yield f"Error streaming logs: {e}"

    def get_pod_info(self, pod_name: str) -> Optional[dict]:
        """
        Get detailed information about a specific pod.

        Args:
            pod_name: Name of the pod.

        Returns:
            Pod info dictionary or None if not found.
        """
        try:
            pod = self.v1.read_namespaced_pod(name=pod_name, namespace=self.namespace)

            container_statuses = []
            if pod.status.container_statuses:
                for cs in pod.status.container_statuses:
                    state = 'unknown'
                    if cs.state.running:
                        state = 'running'
                    elif cs.state.waiting:
                        state = f'waiting: {cs.state.waiting.reason}'
                    elif cs.state.terminated:
                        state = f'terminated: {cs.state.terminated.reason}'

                    container_statuses.append({
                        'name': cs.name,
                        'state': state,
                        'ready': cs.ready,
                        'restart_count': cs.restart_count,
                    })

            return {
                'name': pod.metadata.name,
                'namespace': pod.metadata.namespace,
                'status': pod.status.phase,
                'created_at': pod.metadata.creation_timestamp.isoformat() if pod.metadata.creation_timestamp else None,
                'node': pod.spec.node_name,
                'ip': pod.status.pod_ip,
                'containers': container_statuses,
                'labels': dict(pod.metadata.labels) if pod.metadata.labels else {},
            }

        except Exception as e:
            logger.error(f"Failed to get pod info for {pod_name}: {e}")
            return None


def list_running_bot_pods() -> List[dict]:
    """Convenience function to list running bot pods."""
    return K8sLogStreamer().list_bot_pods(include_terminated=False)


def get_bot_pod_logs(pod_name: str, tail_lines: int = 100) -> str:
    """Convenience function to get bot pod logs."""
    return K8sLogStreamer().get_pod_logs(pod_name, tail_lines=tail_lines)
