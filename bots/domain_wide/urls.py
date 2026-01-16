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
