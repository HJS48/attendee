# Integration Plan: Meeting Insights & Transcript Viewer

## Summary

Add AI-powered meeting insights (summary + action items) and a transcript viewer for meeting participants. Emails sent to internal participants (`@soarwithus.co`) with personalized links.

---

## Architecture

```
Supabase (already in place)
    │
    ├─ meetings table ← transcript written here
    │
    ▼
Edge Function (index.ts) ← triggered by new meeting row
    │
    ├─ Calls Claude API → extracts summary + action items
    ├─ Writes to meeting_insights table
    │
    ▼
POST https://wayfarrow.info/api/v1/internal/notify-meeting
    │
    ├─ Celery task: send_transcript_email_task
    ├─ Filters internal participants (@soarwithus.co)
    ├─ Generates personalized tokens
    ├─ Sends emails via Resend
    │
    ▼
Recipient clicks: https://wayfarrow.info/transcripts/<meeting_id>?token=<token>
    │
    ▼
Django view: transcript_view
    │
    ├─ Verifies token
    ├─ Fetches meeting data from Supabase
    ├─ Renders transcript.html
```

---

## Components to Build

### 1. Django: Transcript Viewer

**File:** `attendee/bots/transcript_views.py`

```python
# Token functions
- create_transcript_token(meeting_id, email) → signed JWT-like token (30 day expiry)
- verify_transcript_token(token) → {meeting_id, email} or None

# Views
- transcript_view(request, meeting_id) → renders transcript.html
  - Validates token from ?token= query param
  - Fetches meeting from Supabase (or cache)
  - Checks: is user a participant OR is dev bypass email?
  - Renders template with video, transcript, action items
```

**File:** `attendee/bots/transcript_urls.py`

```python
urlpatterns = [
    path('transcripts/<str:meeting_id>/', transcript_view, name='transcript_view'),
]
```

**Wire into:** `attendee/attendee/urls.py`

---

### 2. Django: Internal Notification Endpoint

**File:** `attendee/bots/transcript_api_views.py`

```python
# POST /api/v1/internal/notify-meeting
# Called by Supabase Edge Function after insight extraction

@api_view(['POST'])
def notify_meeting(request):
    """
    Expects: {
        "meeting_id": "uuid",
        "summary": "...",
        "action_items": [...]
    }
    Auth: X-Api-Key header (SUPABASE_SERVICE_KEY)
    """
    # Queue Celery task
    send_transcript_email_task.delay(
        meeting_id=data['meeting_id'],
        summary=data['summary'],
        action_items=data['action_items']
    )
    return Response({'status': 'queued'})
```

---

### 3. Celery Task: Send Transcript Emails

**File:** `attendee/bots/tasks/send_transcript_email_task.py`

```python
@shared_task
def send_transcript_email_task(meeting_id, summary, action_items):
    """
    1. Fetch meeting from Supabase (title, participants, duration, etc.)
    2. Filter participants:
       - Include if email ends with @soarwithus.co
       - Always include harryschmidt042@gmail.com (dev bypass)
    3. For each recipient:
       - Generate personalized token
       - Build HTML email (summary, action items, transcript link)
       - Send via Resend SMTP
    """
```

**Register in:** `attendee/bots/tasks/__init__.py`

---

### 4. Templates & Static Files

**Move files:**
```
attendee/templates/transcript.html  (convert {{VAR}} → {{ var }})
attendee/static/css/transcript.css
attendee/static/js/transcript.js
```

**Template changes:**
- Add `{% load static %}`
- Fix static file paths: `href="{% static 'css/transcript.css' %}"`
- Convert mustache `{{VAR}}` to Django `{{ var }}`

---

### 5. Supabase Edge Function Update

**File:** `index.ts` (already deployed)

**Add at end of successful extraction:**
```typescript
// After writing to meeting_insights, notify attendee API
await fetch('https://wayfarrow.info/api/v1/internal/notify-meeting', {
    method: 'POST',
    headers: {
        'Content-Type': 'application/json',
        'X-Api-Key': Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')
    },
    body: JSON.stringify({
        meeting_id,
        summary: content.summary,
        action_items: content.items
    })
});
```

---

### 6. Configuration

**Add to ConfigMap** (`attendee/k8s/configmap.yaml`):
```yaml
INTERNAL_EMAIL_DOMAIN: "soarwithus.co"
DEV_BYPASS_EMAIL: "harryschmidt042@gmail.com"
TRANSCRIPT_BASE_URL: "https://wayfarrow.info"
```

**Already in secrets:**
- `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`
- `SUPABASE_SERVICE_KEY` (for API auth)

**Add to secrets** (if not present):
- `TRANSCRIPT_TOKEN_SECRET` (use existing `SESSION_SECRET` or generate new)

---

## File Structure (Final)

```
attendee/bots/
├── transcript_views.py      # NEW - viewer + token logic
├── transcript_api_views.py  # NEW - internal notification endpoint
├── transcript_urls.py       # NEW - URL routing
├── tasks/
│   ├── __init__.py          # EDIT - register new task
│   └── send_transcript_email_task.py  # NEW
├── templates/
│   └── transcript.html      # NEW (converted from mustache)
└── static/
    ├── css/
    │   └── transcript.css   # NEW
    └── js/
        └── transcript.js    # NEW

attendee/attendee/
└── urls.py                  # EDIT - include transcript_urls
```

---

## Implementation Order

| Step | Task | Files |
|------|------|-------|
| 1 | Create transcript token utils | `transcript_views.py` |
| 2 | Create transcript viewer view | `transcript_views.py` |
| 3 | Convert & move template | `templates/transcript.html` |
| 4 | Move static files | `static/css/`, `static/js/` |
| 5 | Create URL routing | `transcript_urls.py`, `urls.py` |
| 6 | Create email task | `tasks/send_transcript_email_task.py` |
| 7 | Create notification endpoint | `transcript_api_views.py` |
| 8 | Add config to ConfigMap | `k8s/configmap.yaml` |
| 9 | Update edge function | `index.ts` (Supabase) |
| 10 | Deploy & test | `deploy.sh apply` |

---

## Dev Bypass Behavior

`harryschmidt042@gmail.com`:
- Always receives transcript emails (regardless of domain)
- Can view any transcript (token still required, but participant check skipped)
- Controlled by `DEV_BYPASS_EMAIL` env var (set to empty string to disable)

---

## Testing Checklist

- [ ] Token generation creates valid signed tokens
- [ ] Token verification rejects expired/tampered tokens
- [ ] Transcript view renders with video, transcript, action items
- [ ] Transcript view rejects invalid tokens
- [ ] Dev bypass email can view any transcript
- [ ] Email task filters to internal domain only
- [ ] Dev bypass email always receives emails
- [ ] Emails contain correct personalized links
- [ ] Edge function successfully calls notification endpoint
- [ ] Video playback syncs with transcript highlighting
- [ ] Copy buttons work (summary, transcript, action items)

---

## Rollback

If issues arise:
1. Remove URL route from `urls.py`
2. Edge function: comment out the fetch to notification endpoint
3. Emails stop, but existing functionality unaffected
