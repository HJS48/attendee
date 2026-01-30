"""
Domain-wide views package.

Re-exports all views for backwards compatibility with existing imports.
"""

from .dashboard import (
    HealthDashboardView,
    HealthSummaryAPI,
    RecentFailuresAPI,
    PipelineStatusAPI,
    EventsBotListAPI,
)

from .webhooks import GoogleCalendarWebhook

from .transcripts import TranscriptView

from .oauth import (
    GoogleOAuthStart,
    GoogleOAuthCallback,
    MicrosoftOAuthStart,
    MicrosoftOAuthCallback,
)

from .calendar_health import CalendarSyncHealthAPI

from .kubernetes import (
    _init_kubernetes_client,
    InfrastructureStatusAPI,
    KubernetesPodsAPI,
    KubernetesAlertsAPI,
    KubernetesBotLookupAPI,
    KubernetesNodesAPI,
    KubernetesEventsAPI,
    KubernetesDeploymentsAPI,
    KubernetesResourceMetricsAPI,
    SystemPodsAPI,
)

from .logs import LogStreamView

from .system_health import (
    ActiveIssuesAPI,
    MeetingSyncStatusAPI,
    SystemHealthAPI,
    ProcessingPipelineAPI,
    PipelineActivityAPI,
    ExternalIntegrationsAPI,
)

from .resources import (
    ResourceSummaryAPI,
    BotResourcesAPI,
)

from .active_bots import (
    ActiveBotPodsAPI,
    ActiveBotsAPI,
    CompletedBotsAPI,
    CalendarEventDetailAPI,
    BotPoolStatusAPI,
)

from .hetzner import (
    HetznerNodePoolsAPI,
    HetznerAutoscalerStatusAPI,
    HetznerCostEstimateAPI,
    HetznerCloudHealthAPI,
)

__all__ = [
    # Dashboard
    'HealthDashboardView',
    'HealthSummaryAPI',
    'RecentFailuresAPI',
    'PipelineStatusAPI',
    'EventsBotListAPI',
    # Webhooks
    'GoogleCalendarWebhook',
    # Transcripts
    'TranscriptView',
    # OAuth
    'GoogleOAuthStart',
    'GoogleOAuthCallback',
    'MicrosoftOAuthStart',
    'MicrosoftOAuthCallback',
    # Calendar Health
    'CalendarSyncHealthAPI',
    # Kubernetes
    '_init_kubernetes_client',
    'InfrastructureStatusAPI',
    'KubernetesPodsAPI',
    'KubernetesAlertsAPI',
    'KubernetesBotLookupAPI',
    'KubernetesNodesAPI',
    'KubernetesEventsAPI',
    'KubernetesDeploymentsAPI',
    'KubernetesResourceMetricsAPI',
    'SystemPodsAPI',
    # Logs
    'LogStreamView',
    # System Health
    'ActiveIssuesAPI',
    'MeetingSyncStatusAPI',
    'SystemHealthAPI',
    'ProcessingPipelineAPI',
    'PipelineActivityAPI',
    'ExternalIntegrationsAPI',
    # Resources
    'ResourceSummaryAPI',
    'BotResourcesAPI',
    # Active Bots/Pods
    'ActiveBotPodsAPI',
    'ActiveBotsAPI',
    'CompletedBotsAPI',
    'CalendarEventDetailAPI',
    'BotPoolStatusAPI',
    # Hetzner
    'HetznerNodePoolsAPI',
    'HetznerAutoscalerStatusAPI',
    'HetznerCostEstimateAPI',
    'HetznerCloudHealthAPI',
]
