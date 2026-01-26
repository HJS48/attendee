"""
URL routing for transcript viewer and related API endpoints.
"""
from django.urls import path

from bots.transcript_views import transcript_view
from bots.transcript_api_views import notify_meeting

app_name = 'transcripts'

urlpatterns = [
    path('<str:meeting_id>/', transcript_view, name='transcript_view'),
]

# Internal API endpoints (added to main urls.py separately)
internal_api_urlpatterns = [
    path('internal/notify-meeting/', notify_meeting, name='notify_meeting'),
]
