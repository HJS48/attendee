/**
 * Transcript viewer route + token generation
 *
 * Add these to your Express app (server.js)
 */

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const TRANSCRIPT_TOKEN_SECRET = process.env.SESSION_SECRET;

// ============================================================
// TOKEN FUNCTIONS
// ============================================================

function createTranscriptToken(meetingId, email) {
  const payload = JSON.stringify({
    meetingId,
    email: email.toLowerCase(),
    exp: Date.now() + 30 * 24 * 60 * 60 * 1000  // 30 days
  });
  const signature = crypto.createHmac('sha256', TRANSCRIPT_TOKEN_SECRET).update(payload).digest('base64url');
  return Buffer.from(payload).toString('base64url') + '.' + signature;
}

function verifyTranscriptToken(token) {
  try {
    const [payloadB64, signature] = token.split('.');
    if (!payloadB64 || !signature) return null;

    const payload = Buffer.from(payloadB64, 'base64url').toString();
    const expectedSig = crypto.createHmac('sha256', TRANSCRIPT_TOKEN_SECRET).update(payload).digest('base64url');

    if (!crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expectedSig))) {
      return null;
    }

    const data = JSON.parse(payload);
    if (data.exp < Date.now()) return null; // Expired

    return data;
  } catch {
    return null;
  }
}

// ============================================================
// HELPERS
// ============================================================

function isValidUUID(str) {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(str);
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function getPublicVideoUrl(recordingUrl) {
  if (!recordingUrl) return '';
  // Transform private R2 URL to public URL
  const match = recordingUrl.match(/r2\.cloudflarestorage\.com\/[^/]+\/(.+)$/);
  if (match) {
    return `https://pub-b4590a75005946ca8c543dc5efb61b28.r2.dev/${match[1]}`;
  }
  return recordingUrl;
}

// ============================================================
// API: Generate tokens (called by Python worker)
// ============================================================

app.post('/api/transcript-tokens', async (req, res) => {
  const apiKey = req.headers['x-api-key'];
  if (apiKey !== process.env.SUPABASE_SERVICE_KEY) {
    return res.status(401).json({ error: 'Unauthorized' });
  }

  const { meeting_id, emails } = req.body;
  if (!meeting_id || !Array.isArray(emails)) {
    return res.status(400).json({ error: 'meeting_id and emails array required' });
  }

  const tokens = {};
  for (const email of emails) {
    tokens[email] = createTranscriptToken(meeting_id, email);
  }

  res.json({ tokens });
});

// ============================================================
// TRANSCRIPT VIEWER PAGE
// ============================================================

app.get('/transcripts/:meetingId', async (req, res) => {
  const meetingId = req.params.meetingId;

  if (!isValidUUID(meetingId)) {
    return res.status(400).send('Invalid meeting ID');
  }

  // Token-based auth
  const token = req.query.token;
  let userEmail = null;

  if (token) {
    const tokenData = verifyTranscriptToken(token);
    if (!tokenData) {
      return res.status(401).send('Invalid or expired access link');
    }
    if (tokenData.meetingId !== meetingId) {
      return res.status(403).send('Access link does not match this meeting');
    }
    userEmail = tokenData.email;
  } else {
    return res.status(401).send('Access denied - use the link from your email');
  }

  try {
    // Fetch meeting from your DB
    const meeting = await db.getMeeting(meetingId);
    if (!meeting) {
      return res.status(404).send('Meeting not found');
    }

    // Verify user is participant
    const attendees = await db.getAttendeeEmailsForMeeting(meeting.meeting_url);
    const isParticipant = attendees.some(a => a.email?.toLowerCase() === userEmail);
    const isOrganizer = meeting.organizer_email?.toLowerCase() === userEmail;

    if (!isParticipant && !isOrganizer) {
      return res.status(403).send('Access denied');
    }

    // Fetch insights
    const insights = await db.getMeetingInsights(meetingId);
    const summary = insights[0]?.summary || '';
    const actionItems = insights[0]?.action_items?.items || [];

    // Format helpers
    const formatTimestamp = (ms) => {
      const totalSeconds = Math.floor(ms / 1000);
      const minutes = Math.floor(totalSeconds / 60);
      const seconds = totalSeconds % 60;
      return `[${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}]`;
    };

    const formatDate = (dateStr) => {
      if (!dateStr) return 'Unknown date';
      const date = new Date(dateStr);
      return date.toLocaleDateString('en-US', {
        weekday: 'short', year: 'numeric', month: 'short', day: 'numeric',
        hour: '2-digit', minute: '2-digit'
      });
    };

    const formatDuration = (seconds) => {
      if (!seconds) return 'Unknown duration';
      const mins = Math.floor(seconds / 60);
      const hrs = Math.floor(mins / 60);
      return hrs > 0 ? `${hrs}h ${mins % 60}m` : `${mins} minutes`;
    };

    // Speaker colors
    const speakers = [...new Set((meeting.transcript || []).map(s => s.speaker_name))];
    const speakerColorMap = {};
    speakers.forEach((speaker, idx) => { speakerColorMap[speaker] = idx % 8; });

    // Render transcript HTML
    const transcriptHtml = (meeting.transcript || []).map((seg, idx) => {
      const colorClass = `speaker-color-${speakerColorMap[seg.speaker_name] || 0}`;
      return `
        <div class="transcript-segment" data-start="${seg.start_ms}" data-end="${seg.end_ms}" data-index="${idx}">
          <div class="segment-header">
            <span class="speaker-dot ${colorClass}"></span>
            <span class="speaker-name">${escapeHtml(seg.speaker_name)}</span>
            <span class="segment-time">${formatTimestamp(seg.start_ms)}</span>
          </div>
          <div class="segment-text">${escapeHtml(seg.text)}</div>
        </div>`;
    }).join('');

    // Render action items
    let actionItemsHtml = '';
    if (actionItems.length === 0) {
      actionItemsHtml = '<p class="no-action-items">No action items identified</p>';
    } else {
      const byAssignee = {};
      actionItems.forEach(item => {
        const name = item.assignee || 'Unassigned';
        if (!byAssignee[name]) byAssignee[name] = [];
        byAssignee[name].push(item);
      });

      actionItemsHtml = Object.entries(byAssignee).map(([name, items]) => {
        const itemsHtml = items.map(item => {
          const due = item.due ? escapeHtml(item.due) : 'Not specified';
          const timestamp = item.timestamp ? ` <a href="#" class="ai-timestamp" data-timestamp="${escapeHtml(item.timestamp)}">${escapeHtml(item.timestamp)}</a>` : '';
          return `<div class="ai-row"><span class="ai-check">☐</span><span class="ai-task">${escapeHtml(item.task)}${timestamp}</span><span class="ai-due"><span class="ai-due-label">Due:</span> ${due}</span></div>`;
        }).join('');
        return `<details class="ai-group" open><summary class="ai-assignee-header">${escapeHtml(name)} <span class="ai-count">${items.length}</span></summary><div class="ai-items">${itemsHtml}</div></details>`;
      }).join('');
    }

    // Participant names
    const participantNames = (meeting.participants || [])
      .map(p => p.name).slice(0, 5).join(', ') +
      (meeting.participants?.length > 5 ? ` +${meeting.participants.length - 5} more` : '');

    // Read template and replace placeholders
    const templatePath = path.join(__dirname, 'views', 'transcript.html');
    let template = fs.readFileSync(templatePath, 'utf8');

    template = template.replace(/\{\{MEETING_TITLE\}\}/g, escapeHtml(meeting.title || 'Untitled Meeting'));
    template = template.replace('{{MEETING_ID}}', escapeHtml(meetingId));
    template = template.replace('{{MEETING_DATE}}', formatDate(meeting.started_at));
    template = template.replace('{{MEETING_DURATION}}', formatDuration(meeting.duration_seconds));
    template = template.replace('{{PARTICIPANT_NAMES}}', escapeHtml(participantNames));
    template = template.replace('{{VIDEO_URL}}', getPublicVideoUrl(meeting.recording_url));
    template = template.replace('{{SUMMARY}}', escapeHtml(summary));
    template = template.replace('{{ACTION_ITEMS_HTML}}', actionItemsHtml);
    template = template.replace('{{TRANSCRIPT_HTML}}', transcriptHtml);
    template = template.replace('{{CSRF_TOKEN}}', '');

    res.send(template);

  } catch (error) {
    console.error('Error loading transcript:', error);
    res.status(500).send('Error loading transcript');
  }
});
