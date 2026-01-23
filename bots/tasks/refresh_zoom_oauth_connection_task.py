import logging
import time

from django.db import transaction
from django.utils import timezone

from bots.models import ZoomOAuthConnection, ZoomOAuthConnectionStates
from bots.zoom_oauth_connections_utils import ZoomAPIAuthenticationError, ZoomAPIError, _get_access_token, _handle_zoom_api_authentication_error

logger = logging.getLogger(__name__)

from celery import shared_task


def refresh_zoom_oauth_connection_sync(zoom_oauth_connection_id, retry_count=0):
    """
    Synchronous version of refresh_zoom_oauth_connection for direct execution without Celery.
    Used in Kubernetes mode where Celery worker is not available.
    Implements retry logic with exponential backoff.
    """
    MAX_RETRIES = 6
    RETRY_BACKOFF_BASE = 2

    logger.info(f"Refreshing zoom oauth connection token for zoom oauth connection {zoom_oauth_connection_id} (sync)")
    zoom_oauth_connection = ZoomOAuthConnection.objects.get(id=zoom_oauth_connection_id)

    try:
        # Just get the access token which will refresh the refresh token
        access_token = _get_access_token(zoom_oauth_connection)

        if not access_token:
            raise ZoomAPIError("No access token returned from Zoom API")

        # Update zoom oauth connection sync success timestamp and window
        zoom_oauth_connection.last_attempted_token_refresh_at = timezone.now()
        zoom_oauth_connection.last_successful_token_refresh_at = zoom_oauth_connection.last_attempted_token_refresh_at
        zoom_oauth_connection.state = ZoomOAuthConnectionStates.CONNECTED
        zoom_oauth_connection.connection_failure_data = None
        zoom_oauth_connection.save()

        logger.info(f"Successfully refreshed zoom oauth connection token for zoom oauth connection {zoom_oauth_connection_id}")

    except ZoomAPIAuthenticationError as e:
        _handle_zoom_api_authentication_error(zoom_oauth_connection, e)

    except Exception as e:
        if retry_count < MAX_RETRIES:
            backoff_time = RETRY_BACKOFF_BASE ** (retry_count + 1)
            logger.info(f"Retrying refresh_zoom_oauth_connection {zoom_oauth_connection_id} (attempt {retry_count + 1}/{MAX_RETRIES}) in {backoff_time}s: {e}")
            time.sleep(backoff_time)
            return refresh_zoom_oauth_connection_sync(zoom_oauth_connection_id, retry_count + 1)
        else:
            logger.exception(f"Zoom OAuth connection token refresh failed with {type(e).__name__} for {zoom_oauth_connection_id}: {e}")
            zoom_oauth_connection.last_attempted_token_refresh_at = timezone.now()
            zoom_oauth_connection.save()
            raise


def enqueue_refresh_zoom_oauth_connection_task(zoom_oauth_connection: ZoomOAuthConnection):
    """Enqueue a refresh zoom oauth connection task for a zoom oauth connection."""
    from bots.task_executor import is_kubernetes_mode, task_executor

    with transaction.atomic():
        zoom_oauth_connection.token_refresh_task_enqueued_at = timezone.now()
        zoom_oauth_connection.token_refresh_task_requested_at = None
        zoom_oauth_connection.save()

        if is_kubernetes_mode():
            task_executor.submit(refresh_zoom_oauth_connection_sync, zoom_oauth_connection.id)
        else:
            refresh_zoom_oauth_connection.delay(zoom_oauth_connection.id)


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,  # Enable exponential backoff
    max_retries=6,
)
def refresh_zoom_oauth_connection(self, zoom_oauth_connection_id):
    """Celery task to refresh the token for a zoom oauth connection."""
    logger.info(f"Refreshing zoom oauth connection token for zoom oauth connection {zoom_oauth_connection_id}")
    zoom_oauth_connection = ZoomOAuthConnection.objects.get(id=zoom_oauth_connection_id)

    try:
        # Just get the access token which will refresh the refresh token
        access_token = _get_access_token(zoom_oauth_connection)

        if not access_token:
            raise ZoomAPIError("No access token returned from Zoom API")

        # Update zoom oauth connection sync success timestamp and window
        zoom_oauth_connection.last_attempted_token_refresh_at = timezone.now()
        zoom_oauth_connection.last_successful_token_refresh_at = zoom_oauth_connection.last_attempted_token_refresh_at
        zoom_oauth_connection.state = ZoomOAuthConnectionStates.CONNECTED
        zoom_oauth_connection.connection_failure_data = None
        zoom_oauth_connection.save()

        logger.info(f"Successfully refreshed zoom oauth connection token for zoom oauth connection {zoom_oauth_connection_id}")

    except ZoomAPIAuthenticationError as e:
        _handle_zoom_api_authentication_error(zoom_oauth_connection, e)

    except Exception as e:
        logger.exception(f"Zoom OAuth connection token refresh failed with {type(e).__name__} for {zoom_oauth_connection_id}: {e}")
        zoom_oauth_connection.last_attempted_token_refresh_at = timezone.now()
        zoom_oauth_connection.save()
        raise
