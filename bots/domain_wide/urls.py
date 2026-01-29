from django.urls import path
from . import views

app_name = 'domain_wide'

urlpatterns = [
    # Health Dashboard
    path('', views.HealthDashboardView.as_view(), name='dashboard'),
    path('api/summary/', views.HealthSummaryAPI.as_view(), name='api-summary'),
    path('api/failures/', views.RecentFailuresAPI.as_view(), name='api-failures'),
    path('api/pipeline/', views.PipelineStatusAPI.as_view(), name='api-pipeline'),
    path('api/events/', views.EventsBotListAPI.as_view(), name='api-events'),

    # Debugging APIs
    path('api/calendar-sync/', views.CalendarSyncHealthAPI.as_view(), name='api-calendar-sync'),
    path('api/infrastructure/', views.InfrastructureStatusAPI.as_view(), name='api-infrastructure'),
    path('api/logs/stream', views.LogStreamView.as_view(), name='api-logs-stream'),

    # System Health APIs (new)
    path('api/system-health/', views.SystemHealthAPI.as_view(), name='api-system-health'),
    path('api/active-issues/', views.ActiveIssuesAPI.as_view(), name='api-active-issues'),
    path('api/processing-pipeline/', views.ProcessingPipelineAPI.as_view(), name='api-processing-pipeline'),
    path('api/pipeline-activity/', views.PipelineActivityAPI.as_view(), name='api-pipeline-activity'),
    path('api/external-integrations/', views.ExternalIntegrationsAPI.as_view(), name='api-external-integrations'),
    path('api/meeting-sync-status/', views.MeetingSyncStatusAPI.as_view(), name='api-meeting-sync-status'),

    # Kubernetes APIs
    path('api/k8s/pods/', views.KubernetesPodsAPI.as_view(), name='api-k8s-pods'),
    path('api/k8s/alerts/', views.KubernetesAlertsAPI.as_view(), name='api-k8s-alerts'),
    path('api/k8s/bot-lookup/', views.KubernetesBotLookupAPI.as_view(), name='api-k8s-bot-lookup'),
    path('api/k8s/nodes/', views.KubernetesNodesAPI.as_view(), name='api-k8s-nodes'),
    path('api/k8s/events/', views.KubernetesEventsAPI.as_view(), name='api-k8s-events'),
    path('api/k8s/deployments/', views.KubernetesDeploymentsAPI.as_view(), name='api-k8s-deployments'),
    path('api/k8s/metrics/', views.KubernetesResourceMetricsAPI.as_view(), name='api-k8s-metrics'),

    # Resource Monitoring APIs
    path('api/resources/summary/', views.ResourceSummaryAPI.as_view(), name='api-resource-summary'),
    path('api/resources/bot/', views.BotResourcesAPI.as_view(), name='api-bot-resources'),

    # Hetzner Cloud APIs
    path('api/hetzner/node-pools/', views.HetznerNodePoolsAPI.as_view(), name='api-hetzner-node-pools'),
    path('api/hetzner/autoscaler/', views.HetznerAutoscalerStatusAPI.as_view(), name='api-hetzner-autoscaler'),
    path('api/hetzner/costs/', views.HetznerCostEstimateAPI.as_view(), name='api-hetzner-costs'),
    path('api/hetzner/health/', views.HetznerCloudHealthAPI.as_view(), name='api-hetzner-health'),

    # Webhooks
    path('webhook/google-calendar/', views.GoogleCalendarWebhook.as_view(), name='google-calendar-webhook'),

    # Google OAuth
    path('auth/google/', views.GoogleOAuthStart.as_view(), name='google-oauth-start'),
    path('auth/google/callback/', views.GoogleOAuthCallback.as_view(), name='google-oauth-callback'),

    # Microsoft OAuth
    path('auth/microsoft/', views.MicrosoftOAuthStart.as_view(), name='microsoft-oauth-start'),
    path('auth/microsoft/callback/', views.MicrosoftOAuthCallback.as_view(), name='microsoft-oauth-callback'),

    # Transcript Viewer
    path('transcripts/<str:meeting_id>/', views.TranscriptView.as_view(), name='transcript-view'),
]
