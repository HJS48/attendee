"""
Calendar sync health monitoring.
"""
import logging

from django.http import JsonResponse
from django.utils import timezone
from django.views import View

from bots.models import Calendar, CalendarStates

logger = logging.getLogger(__name__)


class CalendarSyncHealthAPI(View):
    """API for calendar sync health status."""

    def get(self, request):
        calendars = Calendar.objects.all().order_by('-last_successful_sync_at')

        calendars_data = []
        for cal in calendars:
            # Determine state
            if cal.state == CalendarStates.CONNECTED:
                state = 'connected'
            else:
                state = 'disconnected'

            # Calculate sync age
            sync_age_minutes = None
            if cal.last_successful_sync_at:
                delta = timezone.now() - cal.last_successful_sync_at
                sync_age_minutes = int(delta.total_seconds() / 60)

            # Extract error details
            error = None
            first_failure = None
            days_disconnected = None

            if cal.connection_failure_data:
                error = cal.connection_failure_data.get('error', str(cal.connection_failure_data))
                failure_time = cal.connection_failure_data.get('first_failure_at')
                if failure_time:
                    first_failure = failure_time
                    try:
                        from dateutil.parser import parse as parse_date
                        failure_dt = parse_date(failure_time)
                        days_disconnected = (timezone.now() - failure_dt).days
                    except Exception:
                        pass

            # Get calendar owner from deduplication_key
            owner = cal.deduplication_key
            if owner:
                owner = owner.replace('-google-sa', '').replace('-google-oauth', '').replace('-microsoft-oauth', '')

            cal_data = {
                'id': cal.object_id,
                'owner': owner,
                'state': state,
                'last_sync': cal.last_successful_sync_at.isoformat() if cal.last_successful_sync_at else None,
                'sync_age_minutes': sync_age_minutes,
            }

            if state == 'disconnected':
                cal_data['error'] = error
                cal_data['first_failure'] = first_failure
                cal_data['days_disconnected'] = days_disconnected

            calendars_data.append(cal_data)

        return JsonResponse({'calendars': calendars_data})
