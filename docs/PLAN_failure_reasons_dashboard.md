# Plan: Enhanced Failure Reasons in Dashboard

## Overview

Improve the "Recent Failures (Last 7 Days)" section of the health dashboard to show detailed failure reasons using event subtypes.

## Current State

The Recent Failures table shows:
- Bot ID
- Reason (event_sub_type_name) - already partially there
- Meeting URL
- Time

But it only shows `COULD_NOT_JOIN` failures from `BotEvent`, not distinguishing between different failure types clearly.

## Proposed Changes

### 1. Add Failure Summary Counts (above the table)

Show a breakdown of failure reasons in a stats row:

```html
<div class="card">
  <div class="card-header">
    <h2>Recent Failures (Last 7 Days)</h2>
    <span class="count-badge" id="failure-count">0 failures</span>
  </div>

  <!-- NEW: Failure breakdown -->
  <div class="pipeline-grid" style="grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); margin-bottom: 16px;">
    <div class="pipeline-col">
      <h3>Request Denied</h3>
      <div class="metric-row">
        <span class="metric-value metric-error">5</span>
      </div>
    </div>
    <div class="pipeline-col">
      <h3>Waiting Room Timeout</h3>
      <div class="metric-row">
        <span class="metric-value metric-error">2</span>
      </div>
    </div>
    <div class="pipeline-col">
      <h3>Meeting Not Found</h3>
      <div class="metric-row">
        <span class="metric-value metric-error">1</span>
      </div>
    </div>
    <!-- etc -->
  </div>

  <table>...</table>
</div>
```

### 2. Enhance the Failures Table

Add columns to distinguish:
- **Event Type**: `Could Not Join` vs `Fatal Error` (was bot ever admitted?)
- **Reason**: The specific event_sub_type

| Bot ID | Event Type | Reason | Meeting URL | Time |
|--------|------------|--------|-------------|------|
| bot_xxx | Could Not Join | Request Denied | meet.google.com/... | 10:32 |
| bot_yyy | Fatal Error | Heartbeat Timeout | zoom.us/... | 09:15 |

### 3. API Changes

**File:** `bots/domain_wide/views.py`

Update `RecentFailuresAPI` to return:

```python
def get(self, request):
    # ... existing query ...

    # Add summary counts by subtype
    from collections import Counter
    subtype_counts = Counter()

    failures_list = []
    for f in failures:
        event_type_label = 'Could Not Join' if f.event_type == BotEventTypes.COULD_NOT_JOIN else 'Fatal Error'
        subtype_label = BotEventSubTypes(f.event_sub_type).label if f.event_sub_type else 'Unknown'

        # Simplify the subtype label (remove prefix)
        reason = subtype_label.replace('Bot could not join meeting - ', '').replace('Fatal error - ', '')

        subtype_counts[reason] += 1

        failures_list.append({
            'bot_id': str(f.bot.object_id),
            'meeting_url': f.bot.meeting_url,
            'event_type': f.event_type,
            'event_type_label': event_type_label,
            'event_sub_type': f.event_sub_type,
            'reason': reason,
            'timestamp': f.created_at.isoformat(),
        })

    return JsonResponse({
        'failures': failures_list,
        'summary': dict(subtype_counts),
        'total': len(failures_list),
    })
```

### 4. Frontend Changes

**File:** `bots/templates/domain_wide/dashboard.html`

Update `loadFailures()` function:

```javascript
async function loadFailures() {
    const data = await fetch('/dashboard/api/failures/').then(r => r.json());

    // Update total count
    document.getElementById('failure-count').textContent = `${data.total} failures`;

    // Render summary counts
    const summaryDiv = document.getElementById('failure-summary');
    summaryDiv.innerHTML = Object.entries(data.summary)
        .sort((a, b) => b[1] - a[1])
        .map(([reason, count]) => `
            <div class="pipeline-col">
                <h3>${escapeHtml(reason)}</h3>
                <div class="metric-row">
                    <span class="metric-value metric-error">${count}</span>
                </div>
            </div>
        `).join('');

    // Render table with new columns
    failuresBody.innerHTML = data.failures.map(f => `
        <tr>
            <td><code>${f.bot_id.substring(0, 8)}...</code></td>
            <td><span class="badge ${f.event_type_label === 'Fatal Error' ? 'badge-error' : 'badge-warning'}">${f.event_type_label}</span></td>
            <td>${escapeHtml(f.reason)}</td>
            <td class="url-cell">${f.meeting_url || '-'}</td>
            <td>${new Date(f.timestamp).toLocaleString()}</td>
        </tr>
    `).join('');
}
```

## Files to Modify

| File | Changes |
|------|---------|
| `bots/domain_wide/views.py` | Update `RecentFailuresAPI` to return summary + enhanced failure details |
| `bots/templates/domain_wide/dashboard.html` | Add failure summary section, update table columns, update JS |

## Implementation Order

1. Update `RecentFailuresAPI` to return new data structure
2. Add failure summary HTML section
3. Update table headers (add Event Type column)
4. Update `loadFailures()` JS function

## Verification

1. Trigger a "request denied" failure - verify it shows as "Could Not Join" with reason "Request Denied"
2. Check summary counts match table rows
3. Verify existing functionality still works
