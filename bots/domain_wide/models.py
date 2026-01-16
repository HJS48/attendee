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
