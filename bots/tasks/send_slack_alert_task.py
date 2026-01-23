import logging
import os
import time

import requests
from celery import shared_task

logger = logging.getLogger(__name__)


def send_slack_alert_sync(message: str, retry_count=0):
    """
    Synchronous version of send_slack_alert for direct execution without Celery.
    Used in Kubernetes mode where Celery worker is not available.
    """
    MAX_RETRIES = 2
    RETRY_BACKOFF_BASE = 2

    slack_webhook_url = os.getenv("SLACK_WEBHOOK_URL")
    if not slack_webhook_url:
        logger.debug("SLACK_WEBHOOK_URL not configured, skipping Slack notification")
        return

    try:
        response = requests.post(
            slack_webhook_url,
            json={"text": message},
            timeout=5,
        )
        response.raise_for_status()
        logger.info(f"Slack webhook sent successfully: {message[:100]}")
    except Exception as e:
        if retry_count < MAX_RETRIES:
            backoff_time = RETRY_BACKOFF_BASE ** (retry_count + 1)
            logger.info(f"Retrying Slack alert (attempt {retry_count + 1}/{MAX_RETRIES}) in {backoff_time}s: {e}")
            time.sleep(backoff_time)
            return send_slack_alert_sync(message, retry_count + 1)
        else:
            logger.warning(f"Failed to send Slack webhook after {MAX_RETRIES} retries: {e}")


def enqueue_send_slack_alert_task(message: str):
    """Enqueue a send slack alert task."""
    from bots.task_executor import is_kubernetes_mode, task_executor

    if is_kubernetes_mode():
        task_executor.submit(send_slack_alert_sync, message)
    else:
        send_slack_alert.delay(message)


@shared_task(
    bind=True,
    max_retries=2,
)
def send_slack_alert(self, message: str):
    """
    Send a message to Slack via webhook.
    Only sends if SLACK_WEBHOOK_URL environment variable is defined.
    """
    slack_webhook_url = os.getenv("SLACK_WEBHOOK_URL")
    if not slack_webhook_url:
        logger.debug("SLACK_WEBHOOK_URL not configured, skipping Slack notification")
        return

    try:
        response = requests.post(
            slack_webhook_url,
            json={"text": message},
            timeout=5,
        )
        response.raise_for_status()
        logger.info(f"Slack webhook sent successfully: {message[:100]}")
    except Exception as e:
        logger.warning(f"Failed to send Slack webhook: {e}")
        # Don't retry on failure, just log and move on
