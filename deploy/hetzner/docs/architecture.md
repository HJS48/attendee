<!-- Parent: ../CLAUDE.md | Namespace: attendee | KUBECONFIG=~/projects/hetzner-migration/kubeconfig -->

# Architecture

## Diagram

```
Internet → Load Balancer (91.98.2.212) → Traefik → Services

┌─────────────────┐  ┌─────────────────┐  ┌─────────────────────────┐
│  Master Node    │  │  Static Worker  │  │  Bot Pool (0-20 nodes)  │
│  cpx22          │  │  cpx22          │  │  cpx32 each             │
│                 │  │                 │  │                         │
│  • k3s control  │  │  • attendee-api │  │  • 1 bot pod per node   │
│  • autoscaler   │  │  • scheduler    │  │  • scales automatically │
│  • CCM          │  │  • worker       │  │  • scales to 0 at night │
│                 │  │  • postgres     │  │                         │
│                 │  │  • redis        │  │                         │
│                 │  │  • traefik      │  │                         │
└─────────────────┘  └─────────────────┘  └─────────────────────────┘
```

## Services

| Service | Purpose | Port |
|---------|---------|------|
| attendee-api | Django REST API, webhooks | 8000 |
| attendee-scheduler | Creates bot pods 5min before meetings | - |
| attendee-worker | Celery, processes transcriptions | - |
| postgres | Database | 5432 |
| redis | Cache, Celery broker | 6379 |
| traefik | Ingress, TLS termination | 80, 443 |

## Bot Lifecycle

1. Google Calendar webhook → Bot record created (state=SCHEDULED)
2. Scheduler polls every 60s, finds bots due in 5 minutes
3. Scheduler creates bot pod → triggers autoscaler if needed
4. Autoscaler provisions Hetzner node (15-30 seconds)
5. Bot joins meeting, records audio
6. Bot uploads to Cloudflare R2, queues transcription tasks
7. Worker processes via Deepgram, saves to PostgreSQL
8. Cleanup cronjob removes completed pods

## External Services

| Service | Purpose |
|---------|---------|
| Deepgram | Transcription API |
| Supabase | Client data mirror |
| Cloudflare R2 | Audio storage |
| Google Calendar | Meeting sync |
