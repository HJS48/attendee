"""
Log streaming for Docker and Kubernetes.
"""
import json as json_module
import logging
import os
import re

from django.conf import settings
from django.http import StreamingHttpResponse
from django.views import View

from .kubernetes import _init_kubernetes_client

logger = logging.getLogger(__name__)


class LogStreamView(View):
    """Server-Sent Events stream for live logs (Docker or Kubernetes)."""

    def _detect_mode(self):
        """Detect if running in Kubernetes or Docker mode."""
        force_mode = os.getenv('INFRASTRUCTURE_MODE')
        if force_mode in ('kubernetes', 'docker'):
            return force_mode
        if os.path.exists('/var/run/secrets/kubernetes.io/serviceaccount/token'):
            return 'kubernetes'
        if os.getenv('KUBERNETES_SERVICE_HOST'):
            return 'kubernetes'
        return 'docker'

    def get(self, request):
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
            log_level = 'INFO'

            # Skip diagnostic messages that contain 'error' in field names but aren't errors
            if 'PerParticipantNonStreamingAudioInputManager diagnostic' in line:
                log_level = 'DEBUG'
            elif 'diagnostic info:' in line.lower():
                log_level = 'DEBUG'
            else:
                # Look for log level indicators at word boundaries
                for lvl in ['ERROR', 'WARNING', 'CRITICAL', 'DEBUG', 'INFO']:
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
            k8s_patterns = {
                'scheduler': 'attendee-scheduler',
                'app': 'attendee-api',
                'worker': 'attendee-worker',
            }

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
                                short_pod = pod_name.replace('bot-pod-', '').replace('bot-', '')[:20]
                                data = json_module.dumps({
                                    'time': timestamp,
                                    'level': log_level,
                                    'message': f"[{short_pod}] {message}",
                                    'pod': pod_name,
                                })
                                yield f"data: {data}\n\n"
                            except queue.Empty:
                                if not any(t.is_alive() for t in threads):
                                    break
                                continue

                        stop_event.set()
                        return

                    # Single pod streaming
                    target_pod = None
                    for pod in pods.items:
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
