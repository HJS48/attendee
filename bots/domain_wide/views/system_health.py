"""
System health and pipeline monitoring APIs.
"""
import json
import logging
import os
import time
from datetime import datetime, timedelta

from django.conf import settings
from django.db import connection
from django.db.models import Count, Max, Subquery
from django.http import JsonResponse
from django.utils import timezone
from django.views import View

from bots.models import (
    Bot, BotStates, Recording, RecordingStates, RecordingTranscriptionStates,
)

from .kubernetes import _init_kubernetes_client

logger = logging.getLogger(__name__)


class ActiveIssuesAPI(View):
    """API for detecting active issues that need attention."""

    def get(self, request):
        from bots.models import (
            AsyncTranscription, AsyncTranscriptionStates,
            Utterance, ZoomOAuthConnection, ZoomOAuthConnectionStates
        )
        from ..models import OAuthCredential

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
        from ..supabase_client import get_supabase_client

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
        ended_bot_ids = list(ended_bots.values_list('id', flat=True))

        recordings = Recording.objects.filter(bot_id__in=ended_bot_ids)

        # Recording states
        rec_complete = recordings.filter(state=RecordingStates.COMPLETE).count()
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
        supabase_meetings = {}
        result = None
        if client:
            try:
                bot_object_ids = list(ended_bots.values_list('object_id', flat=True))

                if bot_object_ids:
                    result = client.table('meetings').select(
                        'attendee_bot_id, transcript_status'
                    ).in_('attendee_bot_id', bot_object_ids).execute()

                    supabase_meetings = {m['attendee_bot_id']: m.get('transcript_status', 'pending')
                                         for m in (result.data or [])}

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

        # Identify stuck meetings
        stuck_count = 0
        if supabase_stats['available'] and result:
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
        force_mode = os.getenv('INFRASTRUCTURE_MODE')
        if force_mode in ('kubernetes', 'docker'):
            return force_mode
        if os.path.exists('/var/run/secrets/kubernetes.io/serviceaccount/token'):
            return 'kubernetes'
        if os.getenv('KUBERNETES_SERVICE_HOST'):
            return 'kubernetes'
        return 'docker'

    def get(self, request):
        from kubernetes import client

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
                docker_client = docker.from_env()
                for container in docker_client.containers.list():
                    name = container.name.lower()
                    if 'scheduler' in name and container.status == 'running':
                        health['scheduler']['pod_running'] = True
                        health['scheduler']['status'] = 'healthy'
                    elif 'worker' in name and container.status == 'running':
                        health['worker']['pod_running'] = True
                docker_client.close()
            except Exception as e:
                logger.warning(f"Docker health check failed: {e}")

        # Get issues count from ActiveIssuesAPI
        try:
            issues_response = ActiveIssuesAPI().get(request)
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
        for utterance in failed_utterances_24h[:500]:
            if utterance.failure_data:
                reason = utterance.failure_data.get('reason', 'unknown')
                failure_reasons[reason] = failure_reasons.get(reason, 0) + 1

        # Utterances processed today
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
        from ..models import PipelineActivity
        from ..supabase_client import get_supabase_client

        today = timezone.now().date()

        # Get bot_ids for bots that ended today
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
            today_activities_for_today_meetings = today_activities.filter(
                bot_id__in=today_bot_ids
            )

        # Helper to count unique meetings by final outcome
        def count_by_final_outcome(queryset):
            """Count unique meetings by their final (most recent) status."""
            latest_per_meeting = queryset.filter(
                meeting_id__isnull=False
            ).exclude(meeting_id='').values('meeting_id').annotate(
                latest_id=Max('id')
            ).values('latest_id')

            final_outcomes = queryset.filter(id__in=Subquery(latest_per_meeting))

            return {
                'success': final_outcomes.filter(status=PipelineActivity.Status.SUCCESS).count(),
                'failed': final_outcomes.filter(status=PipelineActivity.Status.FAILED).count(),
            }

        # Filter by event type
        insight_extraction = today_activities_for_today_meetings.filter(
            event_type=PipelineActivity.EventType.INSIGHT_EXTRACTION
        )
        emails = today_activities_for_today_meetings.filter(
            event_type=PipelineActivity.EventType.EMAIL_SENT
        )

        # Get recent email details
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
        from ..models import OAuthCredential

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
