"""
Models for domain-wide calendar integration.
Isolated from upstream Attendee models to allow clean git pulls.
"""
from django.db import models
from django.utils import timezone


class GoogleWatchChannel(models.Model):
    """
    Track Google Calendar push notification channels.

    Google Calendar API watch channels expire after 7 days max.
    We need to renew them before expiry to keep receiving push notifications.
    """
    user_email = models.EmailField(unique=True, db_index=True)
    channel_id = models.CharField(max_length=255, unique=True, db_index=True)
    resource_id = models.CharField(max_length=255)
    expiration = models.DateTimeField()
    calendar = models.ForeignKey(
        'bots.Calendar',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='watch_channels'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'domain_wide'
        verbose_name = 'Google Watch Channel'
        verbose_name_plural = 'Google Watch Channels'

    def __str__(self):
        return f"{self.user_email} ({self.channel_id[:8]}...)"

    @property
    def is_expired(self):
        return timezone.now() > self.expiration

    @property
    def expires_soon(self):
        """Returns True if expiring within 48 hours."""
        return timezone.now() + timezone.timedelta(hours=48) > self.expiration


class OAuthCredential(models.Model):
    """
    Store encrypted OAuth tokens for individual users.

    Used for users who connect their own calendar via OAuth
    (as opposed to domain-wide delegation via service account).
    """
    PROVIDER_CHOICES = [
        ('google', 'Google'),
        ('microsoft', 'Microsoft'),
    ]

    email = models.EmailField(db_index=True)
    provider = models.CharField(max_length=20, choices=PROVIDER_CHOICES)
    access_token_encrypted = models.TextField()
    refresh_token_encrypted = models.TextField()
    token_expiry = models.DateTimeField(null=True, blank=True)
    scopes = models.JSONField(default=list, blank=True)
    calendar = models.ForeignKey(
        'bots.Calendar',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='oauth_credentials'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'domain_wide'
        verbose_name = 'OAuth Credential'
        verbose_name_plural = 'OAuth Credentials'
        unique_together = [['email', 'provider']]

    def __str__(self):
        return f"{self.email} ({self.provider})"

    @property
    def is_token_expired(self):
        if not self.token_expiry:
            return True
        return timezone.now() > self.token_expiry


class PipelineActivity(models.Model):
    """
    Track pipeline events for dashboard visibility.
    Verifies transcript→sync→insights→email pipeline is working.
    """
    class EventType(models.TextChoices):
        SUPABASE_SYNC = 'supabase_sync', 'Supabase Sync'
        INSIGHT_EXTRACTION = 'insight_extraction', 'Insight Extraction'
        EMAIL_SENT = 'email_sent', 'Email Sent'
        MEETING_CREATED = 'meeting_created', 'Meeting Created'
        TRANSCRIPT_SYNCED = 'transcript_synced', 'Transcript Synced'

    class Status(models.TextChoices):
        SUCCESS = 'success', 'Success'
        FAILED = 'failed', 'Failed'

    event_type = models.CharField(max_length=32, choices=EventType.choices, db_index=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.SUCCESS)
    bot_id = models.CharField(max_length=64, blank=True, db_index=True)
    meeting_id = models.CharField(max_length=64, blank=True, db_index=True)
    meeting_title = models.CharField(max_length=255, blank=True)
    recipient = models.EmailField(blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        app_label = 'domain_wide'
        verbose_name = 'Pipeline Activity'
        verbose_name_plural = 'Pipeline Activities'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.get_event_type_display()} - {self.status} @ {self.created_at.strftime('%H:%M')}"

    @classmethod
    def log(cls, event_type, status='success', bot_id='', meeting_id='', meeting_title='', recipient='', error=''):
        """Log a pipeline event."""
        return cls.objects.create(
            event_type=event_type,
            status=status,
            bot_id=bot_id,
            meeting_id=meeting_id,
            meeting_title=meeting_title,
            recipient=recipient,
            error=error,
        )


class MeetingInsight(models.Model):
    """
    Stores Claude-extracted insights (summary + action items) in Postgres.
    Source of truth for insights — mirrored to Supabase best-effort.
    """
    recording = models.OneToOneField(
        'bots.Recording', on_delete=models.CASCADE, related_name='insight'
    )
    bot = models.ForeignKey(
        'bots.Bot', on_delete=models.CASCADE, related_name='insights'
    )
    supabase_meeting_id = models.CharField(
        max_length=64, blank=True, default='', db_index=True
    )
    summary = models.TextField(blank=True, default='')
    action_items = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'domain_wide'

    def __str__(self):
        return f"Insight for recording {self.recording_id}"
