"""Tests for domain_wide module."""
import pytest
from unittest.mock import patch, MagicMock
from datetime import timedelta
from django.utils import timezone


class TestAutoCreateBotSignal:
    """Test auto_create_bot_for_event signal handler."""

    @pytest.fixture
    def mock_calendar_event(self):
        """Create a mock CalendarEvent."""
        event = MagicMock()
        event.object_id = "test-event-123"
        event.meeting_url = "https://meet.google.com/abc-defg-hij"
        event.start_time = timezone.now() + timedelta(hours=1)
        event.calendar.project = MagicMock()
        return event

    @patch('bots.domain_wide.signals.Bot')
    @patch('bots.domain_wide.signals.create_bot')
    def test_creates_bot_for_new_event_with_meeting_url(
        self, mock_create_bot, mock_bot_model, mock_calendar_event
    ):
        """Should create bot when new event with meeting_url is saved."""
        from bots.domain_wide.signals import auto_create_bot_for_event

        # Setup
        mock_bot_model.objects.filter.return_value.exclude.return_value.exists.return_value = False
        mock_create_bot.return_value = (MagicMock(), None)

        # Execute
        auto_create_bot_for_event(
            sender=None,
            instance=mock_calendar_event,
            created=True
        )

        # Verify
        mock_create_bot.assert_called_once()
        call_args = mock_create_bot.call_args
        assert call_args[1]['data']['meeting_url'] == mock_calendar_event.meeting_url

    @patch('bots.domain_wide.signals.create_bot')
    def test_skips_existing_event(self, mock_create_bot, mock_calendar_event):
        """Should not create bot when event is not new (created=False)."""
        from bots.domain_wide.signals import auto_create_bot_for_event

        auto_create_bot_for_event(
            sender=None,
            instance=mock_calendar_event,
            created=False
        )

        mock_create_bot.assert_not_called()

    @patch('bots.domain_wide.signals.create_bot')
    def test_skips_event_without_meeting_url(self, mock_create_bot, mock_calendar_event):
        """Should not create bot when event has no meeting_url."""
        from bots.domain_wide.signals import auto_create_bot_for_event

        mock_calendar_event.meeting_url = None

        auto_create_bot_for_event(
            sender=None,
            instance=mock_calendar_event,
            created=True
        )

        mock_create_bot.assert_not_called()

    @patch('bots.domain_wide.signals.Bot')
    @patch('bots.domain_wide.signals.create_bot')
    def test_skips_past_event(self, mock_create_bot, mock_bot_model, mock_calendar_event):
        """Should not create bot for event > 2 hours in the past."""
        from bots.domain_wide.signals import auto_create_bot_for_event

        mock_calendar_event.start_time = timezone.now() - timedelta(hours=3)

        auto_create_bot_for_event(
            sender=None,
            instance=mock_calendar_event,
            created=True
        )

        mock_create_bot.assert_not_called()

    @patch('bots.domain_wide.signals.Bot')
    @patch('bots.domain_wide.signals.create_bot')
    def test_skips_if_bot_already_exists(self, mock_create_bot, mock_bot_model, mock_calendar_event):
        """Should not create bot if one already exists for the event."""
        from bots.domain_wide.signals import auto_create_bot_for_event

        # Bot already exists
        mock_bot_model.objects.filter.return_value.exclude.return_value.exists.return_value = True

        auto_create_bot_for_event(
            sender=None,
            instance=mock_calendar_event,
            created=True
        )

        mock_create_bot.assert_not_called()


class TestConfig:
    """Test config module."""

    @patch.dict('os.environ', {'DOMAIN_USERS': 'user1@example.com, user2@example.com'})
    def test_get_domain_users(self):
        from bots.domain_wide.config import get_domain_users
        users = get_domain_users()
        assert users == ['user1@example.com', 'user2@example.com']

    @patch.dict('os.environ', {'DOMAIN_USERS': ''})
    def test_get_domain_users_empty(self):
        from bots.domain_wide.config import get_domain_users
        users = get_domain_users()
        assert users == []


class TestViews:
    """Test health dashboard views."""

    @patch('bots.domain_wide.views.Bot')
    @patch('bots.domain_wide.views.BotEvent')
    @patch('bots.domain_wide.views.CalendarEvent')
    @patch('bots.domain_wide.views.Calendar')
    def test_health_summary_api(self, mock_cal, mock_event, mock_bot_event, mock_bot):
        """Test HealthSummaryAPI returns expected structure."""
        from bots.domain_wide.views import HealthSummaryAPI
        from django.test import RequestFactory

        # Setup mocks
        mock_bot.objects.values_list.return_value.annotate.return_value = [(9, 10), (7, 5)]
        mock_bot_event.objects.filter.return_value.values.return_value.annotate.return_value.order_by.return_value = []
        mock_event.objects.filter.return_value.exclude.return_value.count.return_value = 5
        mock_bot.objects.filter.return_value.count.return_value = 4
        mock_cal.objects.values_list.return_value.annotate.return_value = []

        request = RequestFactory().get('/dashboard/api/summary/')
        view = HealthSummaryAPI()
        response = view.get(request)

        assert response.status_code == 200
        data = response.json() if hasattr(response, 'json') else {}
        # Basic structure check
        assert 'success_rate' in str(response.content) or 'bot_states' in str(response.content)
