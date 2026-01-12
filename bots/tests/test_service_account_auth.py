"""
Tests for service account authentication in calendar sync.
Tests Path B implementation: domain-wide mode using Attendee with SA credentials.
"""

from unittest.mock import patch, MagicMock

from django.test import TestCase

from accounts.models import Organization
from bots.calendars_api_utils import create_calendar
from bots.models import Calendar, CalendarPlatform, CalendarStates, Project
from bots.serializers import CreateCalendarSerializer


class TestCalendarAuthType(TestCase):
    """Test Calendar model auth_type field."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Organization")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)

    def test_calendar_default_auth_type_is_oauth(self):
        """New calendars default to oauth auth_type."""
        calendar = Calendar.objects.create(
            project=self.project,
            platform=CalendarPlatform.GOOGLE,
            client_id="test-client-id",
        )
        self.assertEqual(calendar.auth_type, "oauth")

    def test_calendar_stores_service_account_auth_type(self):
        """Calendar can store service_account auth_type."""
        calendar = Calendar.objects.create(
            project=self.project,
            platform=CalendarPlatform.GOOGLE,
            client_id="test-sa@project.iam.gserviceaccount.com",
            auth_type="service_account",
            deduplication_key="test-user@example.com",
        )
        self.assertEqual(calendar.auth_type, "service_account")

    def test_calendar_stores_sa_credentials(self):
        """Calendar can store and retrieve service account credentials."""
        calendar = Calendar.objects.create(
            project=self.project,
            platform=CalendarPlatform.GOOGLE,
            client_id="test-sa@project.iam.gserviceaccount.com",
            auth_type="service_account",
        )

        sa_credentials = {
            "auth_type": "service_account",
            "service_account_key": {
                "client_email": "test@project.iam.gserviceaccount.com",
                "private_key": "-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----\n",
            },
            "impersonate_email": "user@example.com",
        }
        calendar.set_credentials(sa_credentials)

        # Retrieve and verify
        retrieved = calendar.get_credentials()
        self.assertEqual(retrieved["auth_type"], "service_account")
        self.assertIn("service_account_key", retrieved)
        self.assertEqual(retrieved["impersonate_email"], "user@example.com")

    def test_oauth_calendar_still_works(self):
        """Existing OAuth calendars continue to work."""
        calendar = Calendar.objects.create(
            project=self.project,
            platform=CalendarPlatform.GOOGLE,
            client_id="oauth-client-id",
            auth_type="oauth",
        )
        calendar.set_credentials({
            "client_secret": "secret",
            "refresh_token": "refresh-token-123",
        })

        creds = calendar.get_credentials()
        self.assertEqual(creds["refresh_token"], "refresh-token-123")
        self.assertEqual(calendar.auth_type, "oauth")


class TestCreateCalendarSerializer(TestCase):
    """Test CreateCalendarSerializer with service account fields."""

    def test_oauth_calendar_requires_oauth_fields(self):
        """OAuth auth_type requires client_secret and refresh_token."""
        data = {
            "platform": "google",
            "client_id": "test-client-id",
            "auth_type": "oauth",
            # Missing client_secret and refresh_token
        }
        serializer = CreateCalendarSerializer(data=data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("client_secret is required", str(serializer.errors))

    def test_oauth_calendar_valid(self):
        """OAuth calendar with all required fields is valid."""
        data = {
            "platform": "google",
            "client_id": "test-client-id",
            "client_secret": "test-secret",
            "refresh_token": "test-refresh-token",
            "auth_type": "oauth",
        }
        serializer = CreateCalendarSerializer(data=data)
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_oauth_is_default_auth_type(self):
        """auth_type defaults to oauth when not specified."""
        data = {
            "platform": "google",
            "client_id": "test-client-id",
            "client_secret": "test-secret",
            "refresh_token": "test-refresh-token",
        }
        serializer = CreateCalendarSerializer(data=data)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data.get("auth_type", "oauth"), "oauth")

    def test_service_account_requires_sa_fields(self):
        """service_account auth_type requires service_account_key and impersonate_email."""
        data = {
            "platform": "google",
            "client_id": "test-sa@project.iam.gserviceaccount.com",
            "auth_type": "service_account",
            # Missing service_account_key and impersonate_email
        }
        serializer = CreateCalendarSerializer(data=data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("service_account_key is required", str(serializer.errors))

    def test_service_account_valid(self):
        """Service account calendar with all required fields is valid."""
        data = {
            "platform": "google",
            "client_id": "test-sa@project.iam.gserviceaccount.com",
            "auth_type": "service_account",
            "service_account_key": {
                "client_email": "test@project.iam.gserviceaccount.com",
                "private_key": "-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----\n",
            },
            "impersonate_email": "user@example.com",
        }
        serializer = CreateCalendarSerializer(data=data)
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_service_account_does_not_require_oauth_fields(self):
        """Service account auth_type does not require client_secret or refresh_token."""
        data = {
            "platform": "google",
            "client_id": "test-sa@project.iam.gserviceaccount.com",
            "auth_type": "service_account",
            "service_account_key": {"client_email": "test@project.iam.gserviceaccount.com", "private_key": "key"},
            "impersonate_email": "user@example.com",
            # No client_secret or refresh_token - should still be valid
        }
        serializer = CreateCalendarSerializer(data=data)
        self.assertTrue(serializer.is_valid(), serializer.errors)


class TestCreateCalendarWithServiceAccount(TestCase):
    """Test create_calendar function with service account credentials."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Organization")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)

    def test_create_sa_calendar_success(self):
        """Successfully create a calendar with service account auth."""
        calendar_data = {
            "platform": CalendarPlatform.GOOGLE,
            "client_id": "test-sa@project.iam.gserviceaccount.com",
            "auth_type": "service_account",
            "service_account_key": {
                "client_email": "test@project.iam.gserviceaccount.com",
                "private_key": "-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----\n",
            },
            "impersonate_email": "user@soarwithus.co",
            "deduplication_key": "user@soarwithus.co-google-domain",
            "metadata": {"auth_mode": "domain_wide"},
        }

        calendar, error = create_calendar(calendar_data, self.project)

        self.assertIsNotNone(calendar)
        self.assertIsNone(error)
        self.assertEqual(calendar.auth_type, "service_account")
        self.assertEqual(calendar.client_id, "test-sa@project.iam.gserviceaccount.com")

        # Verify credentials
        creds = calendar.get_credentials()
        self.assertEqual(creds["auth_type"], "service_account")
        self.assertEqual(creds["impersonate_email"], "user@soarwithus.co")
        self.assertIn("service_account_key", creds)

    def test_create_oauth_calendar_still_works(self):
        """OAuth calendar creation still works after adding SA support."""
        calendar_data = {
            "platform": CalendarPlatform.GOOGLE,
            "client_id": "oauth-client-id",
            "client_secret": "oauth-secret",
            "refresh_token": "oauth-refresh-token",
            # auth_type defaults to oauth
        }

        calendar, error = create_calendar(calendar_data, self.project)

        self.assertIsNotNone(calendar)
        self.assertIsNone(error)
        self.assertEqual(calendar.auth_type, "oauth")

        creds = calendar.get_credentials()
        self.assertEqual(creds["refresh_token"], "oauth-refresh-token")


class TestGoogleCalendarSyncHandlerAuth(TestCase):
    """Test GoogleCalendarSyncHandler authentication methods."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Organization")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)

    @patch("bots.tasks.sync_calendar_task.service_account.Credentials")
    @patch("bots.tasks.sync_calendar_task.GoogleAuthRequest")
    def test_get_access_token_uses_sa_auth_for_service_account(self, mock_request, mock_sa_creds):
        """Sync handler uses SA auth when auth_type is service_account."""
        from bots.tasks.sync_calendar_task import GoogleCalendarSyncHandler

        # Create SA calendar
        calendar = Calendar.objects.create(
            project=self.project,
            platform=CalendarPlatform.GOOGLE,
            client_id="test-sa@project.iam.gserviceaccount.com",
            auth_type="service_account",
            state=CalendarStates.CONNECTED,
        )
        calendar.set_credentials({
            "auth_type": "service_account",
            "service_account_key": {
                "client_email": "test@project.iam.gserviceaccount.com",
                "private_key": "-----BEGIN PRIVATE KEY-----\nMIItest\n-----END PRIVATE KEY-----\n",
            },
            "impersonate_email": "user@example.com",
        })

        # Mock the SA credentials
        mock_creds_instance = MagicMock()
        mock_creds_instance.token = "test-sa-token-123"
        mock_sa_creds.from_service_account_info.return_value = mock_creds_instance

        # Create handler and get token
        handler = GoogleCalendarSyncHandler(calendar.id)
        token = handler._get_access_token()

        # Verify SA auth was used
        self.assertEqual(token, "test-sa-token-123")
        mock_sa_creds.from_service_account_info.assert_called_once()

        # Verify correct parameters
        call_args = mock_sa_creds.from_service_account_info.call_args
        self.assertIn("client_email", call_args[0][0])
        self.assertEqual(call_args[1]["subject"], "user@example.com")

    @patch("bots.tasks.sync_calendar_task.requests.post")
    def test_get_access_token_uses_oauth_for_oauth_calendar(self, mock_post):
        """Sync handler uses OAuth refresh token for oauth auth_type."""
        from bots.tasks.sync_calendar_task import GoogleCalendarSyncHandler

        # Create OAuth calendar
        calendar = Calendar.objects.create(
            project=self.project,
            platform=CalendarPlatform.GOOGLE,
            client_id="oauth-client-id",
            auth_type="oauth",
            state=CalendarStates.CONNECTED,
        )
        calendar.set_credentials({
            "client_secret": "oauth-secret",
            "refresh_token": "oauth-refresh-token",
        })

        # Mock the OAuth token response
        mock_response = MagicMock()
        mock_response.json.return_value = {"access_token": "oauth-access-token-123"}
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        # Create handler and get token
        handler = GoogleCalendarSyncHandler(calendar.id)
        token = handler._get_access_token()

        # Verify OAuth was used
        self.assertEqual(token, "oauth-access-token-123")
        mock_post.assert_called_once()

        # Verify OAuth endpoint was called
        call_args = mock_post.call_args
        self.assertEqual(call_args[0][0], "https://oauth2.googleapis.com/token")
