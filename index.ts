import { serve } from "https://deno.land/std@0.168.0/http/server.ts"
import { createClient } from "https://esm.sh/@supabase/supabase-js@2"
import Anthropic from "https://esm.sh/@anthropic-ai/sdk@0.24.0"

const corsHeaders = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': 'authorization, x-client-info, apikey, content-type',
}

serve(async (req) => {
    // Handle CORS preflight
    if (req.method === 'OPTIONS') {
        return new Response('ok', { headers: corsHeaders })
    }

    try {
        const { meeting_id, insight_type } = await req.json()

        if (!meeting_id || !insight_type) {
            return new Response(
                JSON.stringify({ error: 'meeting_id and insight_type are required' }),
                { status: 400, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
            )
        }

        // Initialize clients
        const supabaseUrl = Deno.env.get('SUPABASE_URL')!
        const supabaseKey = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!
        const anthropicKey = Deno.env.get('ANTHROPIC_API_KEY')!

        const supabase = createClient(supabaseUrl, supabaseKey)
        const anthropic = new Anthropic({ apiKey: anthropicKey })

        // Fetch meeting transcript
        const { data: meeting, error: meetingError } = await supabase
            .from('meetings')
            .select('title, transcript, participants')
            .eq('id', meeting_id)
            .single()

        if (meetingError || !meeting) {
            return new Response(
                JSON.stringify({ error: 'Meeting not found', details: meetingError?.message }),
                { status: 404, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
            )
        }

        if (!meeting.transcript || !Array.isArray(meeting.transcript) || meeting.transcript.length === 0) {
            return new Response(
                JSON.stringify({ error: 'No transcript found for this meeting' }),
                { status: 404, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
            )
        }

        // Format transcript for LLM with timestamps
        const transcriptText = meeting.transcript
            .map((s: { speaker_name: string; text: string; start_ms?: number }) => {
                const timestamp = s.start_ms != null
                    ? `[${String(Math.floor(s.start_ms / 60000)).padStart(2, '0')}:${String(Math.floor((s.start_ms % 60000) / 1000)).padStart(2, '0')}] `
                    : ''
                return `${timestamp}${s.speaker_name}: ${s.text}`
            })
            .join('\n')

        // Get extraction prompts based on insight type
        const { system, user } = getPromptsForInsightType(insight_type, transcriptText, meeting)

        // Call Claude
        const response = await anthropic.messages.create({
            model: 'claude-sonnet-4-5-20250929',
            max_tokens: 4096,
            system,
            messages: [{ role: 'user', content: user }]
        })

        // Parse response
        const responseText = response.content[0].type === 'text' ? response.content[0].text : ''

        // Extract JSON from response (handle markdown code blocks)
        let jsonStr = responseText
        const jsonMatch = responseText.match(/```(?:json)?\s*([\s\S]*?)```/)
        if (jsonMatch) {
            jsonStr = jsonMatch[1].trim()
        }

        let content
        try {
            content = JSON.parse(jsonStr)
        } catch (parseError) {
            console.error('Failed to parse LLM response:', responseText)
            return new Response(
                JSON.stringify({ error: 'Failed to parse LLM response', raw: responseText }),
                { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
            )
        }

        // Upsert result to database
        // For action_items type, we now get both summary and items
        const upsertData: Record<string, unknown> = {
            meeting_id,
        }

        if (insight_type === 'action_items') {
            // New format: { summary: "...", items: [...] }
            upsertData.summary = content.summary || null
            upsertData.action_items = { items: content.items || [], extracted_at: new Date().toISOString() }
        } else {
            upsertData.action_items = { ...content, extracted_at: new Date().toISOString() }
        }

        const { error: upsertError } = await supabase
            .from('meeting_insights')
            .upsert(upsertData, { onConflict: 'meeting_id' })

        if (upsertError) {
            console.error('Failed to save insights:', upsertError)
            return new Response(
                JSON.stringify({ error: 'Failed to save insights', details: upsertError.message }),
                { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
            )
        }

        // Notify attendee API to send transcript emails
        if (insight_type === 'action_items') {
            try {
                const notifyResponse = await fetch('https://wayfarrow.info/api/v1/internal/notify-meeting', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-Api-Key': supabaseKey
                    },
                    body: JSON.stringify({
                        meeting_id,
                        summary: content.summary || '',
                        action_items: content.items || []
                    })
                })

                if (!notifyResponse.ok) {
                    console.error('Failed to notify attendee API:', await notifyResponse.text())
                } else {
                    console.log('Attendee API notified successfully')
                }
            } catch (notifyError) {
                // Don't fail the whole request if notification fails
                console.error('Error notifying attendee API:', notifyError)
            }
        }

        return new Response(
            JSON.stringify({ success: true, content }),
            { headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
        )

    } catch (error) {
        console.error('Error:', error)
        return new Response(
            JSON.stringify({ error: error.message || 'Unknown error' }),
            { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
        )
    }
})

function getPromptsForInsightType(type: string, transcript: string, meeting: { title: string; participants: Array<{ name: string }> | null }): { system: string; user: string } {
    const participants = meeting.participants?.map((p) => p.name).join(', ') || 'Unknown participants'

    switch (type) {
        case 'action_items':
            return {
                system: `You are an expert meeting analyst, like Fireflies.ai's note-taker. Your job is to:
1. Write a brief summary of the meeting (3-5 sentences max)
2. Extract EVERY action item from the transcript

Your accuracy directly impacts project success. Missing an action item means:
- Delayed deliverables
- Broken commitments
- Lost revenue
- Damaged client relationships

Be THOROUGH with action items. Err on the side of including items rather than missing them. A good 30-60 minute meeting typically produces 8-20 action items.

Action items include:
- Explicit commitments: "I'll send that over", "I will follow up"
- Soft commitments: "Let me look into that", "I can check on that"
- Requests accepted: "Can you...?" followed by agreement
- Implied tasks: "We need to...", "We should..."
- Follow-ups: "Let me know if...", "Keep me posted"
- Scheduled activities: meetings to book, calls to arrange
- Things to share: documents, reports, updates to send
- Things to review: items to check, verify, or look into

CRITICAL RULES:
1. The assignee is the person SPEAKING when they make the commitment.
   - "Sam: Let me check on that" → Assignee is Sam
   - "Sam: Can you send the report?" "Luke: Sure" → Assignee is Luke

2. EVERY action item MUST include the exact [MM:SS] timestamp from the transcript where it was discussed. This is REQUIRED - never omit timestamps.`,

                user: `Analyze this meeting transcript and provide:
1. A brief summary (3-5 sentences) explaining what the meeting was about
2. All action items extracted from the meeting

Each line in the transcript starts with a timestamp like [MM:SS].

Meeting: ${meeting.title || 'Untitled Meeting'}
Participants: ${participants}

Transcript:
${transcript}

Return ONLY valid JSON with this structure:
{
    "summary": "3-5 sentence summary of what the meeting was about - the key topics discussed and outcomes",
    "items": [
        {
            "task": "What needs to be done",
            "assignee": "Person who committed to doing it",
            "due": "Deadline mentioned or null",
            "context": "Why this came up",
            "timestamp": "[MM:SS] - REQUIRED: exact timestamp from transcript"
        }
    ]
}`
            }

        case 'summary':
            return {
                system: 'You are an expert meeting analyst. Provide concise, accurate meeting summaries.',
                user: `Summarize this meeting transcript concisely.

Meeting: ${meeting.title || 'Untitled Meeting'}
Participants: ${participants}

Transcript:
${transcript}

Return ONLY valid JSON:
{
    "summary": "2-3 sentence summary of what was discussed",
    "key_points": ["Point 1", "Point 2", "Point 3"],
    "decisions": ["Any decisions that were made"]
}`
            }

        case 'key_decisions':
            return {
                system: 'You are an expert meeting analyst. Extract all key decisions made during meetings.',
                user: `Extract key decisions made in this meeting.

Meeting: ${meeting.title || 'Untitled Meeting'}
Participants: ${participants}

Transcript:
${transcript}

Return ONLY valid JSON:
{
    "decisions": [
        {
            "decision": "What was decided",
            "context": "Why or how it was decided",
            "stakeholders": ["People involved in the decision"]
        }
    ]
}`
            }

        default:
            throw new Error(`Unknown insight type: ${type}`)
    }
}
