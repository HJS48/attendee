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
