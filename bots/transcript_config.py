"""
Shared configuration for transcript system.

Centralizes config functions used by transcript_views.py and send_transcript_email_task.py.
"""
import os


def get_internal_domain():
    """Get internal email domain for filtering participants."""
    return os.getenv('INTERNAL_EMAIL_DOMAIN', 'soarwithus.co')


def get_dev_bypass_email():
    """Get dev bypass email that can access any transcript."""
    return os.getenv('DEV_BYPASS_EMAIL', 'harryschmidt042@gmail.com')


def get_transcript_base_url():
    """Get base URL for transcript links."""
    return os.getenv('TRANSCRIPT_BASE_URL', 'https://wayfarrow.info')


def format_duration(seconds: int) -> str:
    """Format duration in seconds for display."""
    if not seconds:
        return 'Unknown duration'
    mins = seconds // 60
    hrs = mins // 60
    if hrs > 0:
        return f"{hrs}h {mins % 60}m"
    return f"{mins} minutes"
