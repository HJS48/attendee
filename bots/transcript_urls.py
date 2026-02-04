"""
URL routing for transcript viewer.
"""
from django.urls import path

from bots.transcript_views import transcript_view

app_name = 'transcripts'

urlpatterns = [
    path('<str:meeting_id>/', transcript_view, name='transcript_view'),
]
