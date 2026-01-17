import os

from .base import *

# Add domain-wide calendar integration module
INSTALLED_APPS = INSTALLED_APPS + [
    'bots.domain_wide',
]

DEBUG = True
SITE_DOMAIN = "localhost:8000"
ALLOWED_HOSTS = ["tendee-stripe-hooks.ngrok.io", "localhost", "wayfarrow.info"]
CSRF_TRUSTED_ORIGINS = ["https://wayfarrow.info"]

# Calendar sync settings
# Polling interval for calendar syncs (in minutes) - used as backup when push notifications are enabled
CALENDAR_SYNC_INTERVAL_MINUTES = 5

# Webhook URL for Google Calendar push notifications
GOOGLE_CALENDAR_WEBHOOK_URL = "https://wayfarrow.info/dashboard/webhook/google-calendar/"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "attendee_development",
        "USER": "attendee_development_user",
        "PASSWORD": "attendee_development_user",
        "HOST": os.getenv("POSTGRES_HOST", "localhost"),
        "PORT": "5432",
    }
}

# Log more stuff in development
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "xmlschema": {"level": "WARNING", "handlers": ["console"], "propagate": False},
        # Uncomment to log database queries
        # "django.db.backends": {
        #    "handlers": ["console"],
        #    "level": "DEBUG",
        #    "propagate": False,
        # },
    },
}
