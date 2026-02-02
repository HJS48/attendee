"""
URL configuration for attendee project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

import os

from django.conf import settings
from django.contrib import admin
from django.http import HttpResponse
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

from accounts import views


def health_check(request):
    return HttpResponse(status=200)


urlpatterns = [
    path("health/", health_check, name="health-check"),
]

if not os.environ.get("DISABLE_ADMIN"):
    urlpatterns.append(path("admin/", admin.site.urls))

# Internal API endpoints (service-to-service, X-Api-Key auth) - must come before bots_api_urls catch-all
from bots.transcript_api_views import notify_meeting
from bots.domain_wide.views import TranscriptView
internal_api_urlpatterns = [
    path('internal/notify-meeting/', notify_meeting, name='notify_meeting'),
]

urlpatterns += [
    path("accounts/", include("allauth.urls")),
    path("accounts/", include("allauth.socialaccount.urls")),
    path("external_webhooks/", include("bots.external_webhooks_urls")),
    path("bot_sso/", include("bots.bot_sso_urls", namespace="bot_sso")),
    path("", views.home, name="home"),
    path("projects/", include("bots.projects_urls", namespace="projects")),
    path("api/v1/", include("bots.calendars_api_urls")),
    path("api/v1/", include("bots.zoom_oauth_connections_api_urls")),
    path("api/v1/", include("bots.app_session_api_urls")),
    # Internal transcript notification endpoint (before catch-all)
    path("api/v1/", include((internal_api_urlpatterns, "internal_api"))),
    path("api/v1/", include("bots.bots_api_urls")),
    # Domain-wide health dashboard (no auth required)
    path("dashboard/", include("bots.domain_wide.urls", namespace="domain_wide")),
    # Transcript viewer (token-based auth, no login required)
    path("transcripts/<str:meeting_id>/", TranscriptView.as_view(), name="transcript-view"),
]

if settings.DEBUG:
    # API docs routes - only available in development
    urlpatterns += [
        path("schema/", SpectacularAPIView.as_view(), name="schema"),
        path(
            "schema/swagger-ui/",
            SpectacularSwaggerView.as_view(url_name="schema"),
            name="swagger-ui",
        ),
        path(
            "schema/redoc/",
            SpectacularRedocView.as_view(url_name="schema"),
            name="redoc",
        ),
    ]
