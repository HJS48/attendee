# Domain-Wide Calendar Integration - Migration from calendar-app

## Overview

In January 2026, we migrated functionality from a separate Node.js service (`calendar-app`) into the Django `attendee` project. All custom code lives in `bots/domain_wide/` to keep it isolated from upstream Attendee code (allowing clean `git pull` from upstream).

## What Was Migrated

| Feature | Old Location (calendar-app) | New Location (attendee) |
|---------|----------------------------|------------------------|
| Google Calendar push notifications | `/webhook/google-calendar` | `bots/domain_wide/views.py` → `GoogleCalendarWebhook` |
| Transcript viewer | `/transcripts/:meetingId` | `bots/domain_wide/views.py` → `TranscriptView` |
| Google OAuth | `/auth/google/*` | `bots/domain_wide/views.py` → `GoogleOAuthStart`, `GoogleOAuthCallback` |
| Microsoft OAuth | `/auth/microsoft/*` | `bots/domain_wide/views.py` → `MicrosoftOAuthStart`, `MicrosoftOAuthCallback` |
| Health dashboard | N/A (new) | `bots/domain_wide/views.py` → `HealthDashboardView` + APIs |

## File Structure

```
bots/domain_wide/
├── __init__.py
├── apps.py                 # Registers signals in ready()
├── config.py               # Pilot user configuration
├── models.py               # GoogleWatchChannel, OAuthCredential
├── views.py                # All views (dashboard, OAuth, transcript, webhooks)
├── urls.py                 # URL routing
├── signals.py              # Auto-bot creation, cleanup, Supabase sync
├── tasks.py                # Celery tasks (calendar sync, Supabase sync)
├── utils.py                # Token encryption, transcript access tokens
├── supabase_client.py      # Supabase connection for transcript viewer
├── management/
│   └── commands/
│       ├── ensure_bots_exist.py      # Create missing bots for events
│       ├── manage_watch_channels.py  # Google watch channel lifecycle
│       └── setup_pilot_calendars.py  # Initialize pilot user calendars
└── migrations/
    └── 0001_initial_models.py

bots/templates/domain_wide/
├── dashboard.html          # Health dashboard UI (auto-refreshes every 30s)
├── transcript.html         # Meeting transcript viewer
├── oauth_success.html      # OAuth completion page
└── error.html              # Error page
```

## URL Routes

All routes are under `/dashboard/` prefix (configured in `attendee/urls.py`):

```
/dashboard/                              # Health dashboard
/dashboard/api/summary/                  # All-time stats API
/dashboard/api/pipeline/?date=YYYY-MM-DD # Daily pipeline stats
/dashboard/api/events/?date=YYYY-MM-DD   # Events list for date
/dashboard/api/failures/?days=7          # Recent failures

/dashboard/auth/google/                  # Start Google OAuth
/dashboard/auth/google/callback/         # Google OAuth callback
/dashboard/auth/microsoft/               # Start Microsoft OAuth
/dashboard/auth/microsoft/callback/      # Microsoft OAuth callback

/dashboard/webhook/google-calendar/      # Google Calendar push notifications
/dashboard/transcripts/<meeting_id>/     # Transcript viewer
```

## Key Components

### 1. Auto-Bot Creation (signals.py)

When a `CalendarEvent` is created with a `meeting_url`:
1. Signal `auto_create_bot_for_event` fires
2. Checks for existing bot for same URL+date (deduplication)
3. Creates bot via `create_bot()` with `BotCreationSource.SCHEDULER`

When a `CalendarEvent` is deleted:
1. Signal `handle_event_deletion` fires
2. Tries to re-link bot to another attendee's event for same meeting
3. If no other event exists, marks bot as ENDED

### 2. Bot Lifecycle

```
CalendarEvent created → Bot created (SCHEDULED)
                              ↓
                    run_scheduler picks up bot
                              ↓
                    launch_scheduled_bot task
                              ↓
                    JOINING → JOINED_RECORDING → ENDED
                              ↓
                    sync_meeting_to_supabase signal fires
```

**Important:** `run_scheduler` only launches bots within ±5 minutes of `join_at`. Missed bots stay SCHEDULED forever.

### 3. Supabase Integration

- **Read:** Transcript viewer fetches meeting data from Supabase `meetings` table
- **Write:** When bot ends, `sync_meeting_to_supabase` task upserts to Supabase

Supabase is the "output hub" for downstream automations (MCP connectors, etc.).

### 4. Transcript Access Tokens

Format: `base64(payload).signature`
- Payload: `{"meetingId": "...", "email": "...", "exp": timestamp_ms}`
- Signature: HMAC-SHA256 with `TRANSCRIPT_TOKEN_SECRET`
- Default expiry: 30 days

Generate token:
```python
from bots.domain_wide.utils import create_transcript_token
token = create_transcript_token(meeting_id, user_email)
url = f"https://yoursite.com/dashboard/transcripts/{meeting_id}/?token={token}"
```

### 5. OAuth Token Encryption

OAuth tokens are encrypted with Fernet before storage:
```python
from bots.domain_wide.utils import encrypt_token, decrypt_token
encrypted = encrypt_token(access_token)
decrypted = decrypt_token(encrypted)
```

Requires `CREDENTIALS_ENCRYPTION_KEY` env var.

## Environment Variables

Add these to Infisical (or .env for local):

```bash
# Google OAuth (for individual users connecting their calendars)
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_REDIRECT_URI=https://yoursite.com/dashboard/auth/google/callback/

# Microsoft OAuth
MICROSOFT_CLIENT_ID=
MICROSOFT_CLIENT_SECRET=
MICROSOFT_REDIRECT_URI=https://yoursite.com/dashboard/auth/microsoft/callback/
MICROSOFT_TENANT_ID=common

# Supabase (transcript viewer reads, bot-end writes)
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_SERVICE_KEY=

# Token encryption (generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
CREDENTIALS_ENCRYPTION_KEY=

# Transcript access tokens (falls back to WEBHOOK_SECRET if not set)
TRANSCRIPT_TOKEN_SECRET=

# Pilot users for domain-wide delegation (comma-separated emails)
PILOT_USERS=user1@company.com,user2@company.com
```

## Domain-Wide Delegation vs Individual OAuth

**Domain-wide delegation (current setup):**
- Uses Google Service Account with domain-wide delegation
- No OAuth needed - service account impersonates users
- Calendars created with deduplication_key: `{email}-google-sa`
- No watch channels needed (uses polling via `run_scheduler`)

**Individual OAuth (future users outside the domain):**
- User goes through OAuth flow
- Tokens stored in `OAuthCredential` model (encrypted)
- Calendars created with deduplication_key: `{email}-google-oauth`
- Watch channels track push notification subscriptions (expire every 7 days)

## Common Issues

### Bots stuck at "Scheduled"

1. Check if `run_scheduler` is running: `ps aux | grep run_scheduler`
2. Check if Celery worker is running: `ps aux | grep celery`
3. Bots with `join_at` more than 5 minutes in the past are skipped permanently
4. Fix: Update `join_at` to now, or mark as ENDED

### Dashboard not showing updates

- Dashboard auto-refreshes every 30 seconds (JS in dashboard.html line 507)
- If not updating, the issue is backend (bot states not changing), not frontend

### Calendar events not syncing

1. Check calendar state is CONNECTED
2. Check `run_scheduler` is running (triggers sync every 30 min)
3. Manual sync: `python manage.py shell` → `from bots.tasks.sync_calendar_task import sync_calendar; sync_calendar.delay(calendar_id)`

### Transcript viewer 404 or access denied

1. Check meeting exists in Supabase `meetings` table
2. Check token is valid and not expired
3. Check user email matches an attendee or organizer

## Upstream Isolation

**DO NOT MODIFY these files** (they come from upstream Attendee):
- `bots/models.py`
- `bots/views.py`
- `bots/urls.py`
- `bots/tasks/*.py`
- `attendee/settings/base.py`

**Safe to modify** (our custom code):
- `bots/domain_wide/**/*`
- `bots/templates/domain_wide/**/*`
- `attendee/settings/development.py` (INSTALLED_APPS only)
- `attendee/urls.py` (adding our include)

To pull upstream changes:
```bash
git fetch origin
git merge origin/main  # or rebase
# Resolve conflicts only in our domain_wide files
```

## Deployment Checklist

1. Push code to GitHub (HJS48 fork, not upstream)
2. On VPS: `git pull`
3. Run migrations: `python manage.py migrate domain_wide`
4. Add env vars to Infisical
5. Restart services: `sudo systemctl restart attendee celery`
6. Verify dashboard loads: `https://yoursite.com/dashboard/`
7. Check scheduler running: `ps aux | grep run_scheduler`

## Retiring calendar-app

Once everything is verified working:
1. Update nginx to remove calendar-app routes
2. Update Google Calendar webhook URL in GCP console to point to Attendee
3. Stop calendar-app service
4. Archive calendar-app repository
