# Hetzner k3s Cluster - Attendee

Production Kubernetes cluster for **Attendee** deployed on Hetzner Cloud.

- **Domain:** wayfarrow.info
- **Namespace:** `attendee`
- **Location:** Hetzner Cloud, Nuremberg

## Kubeconfig

Kubeconfig is stored locally (not in repo). Set before running kubectl:
```bash
export KUBECONFIG=~/projects/hetzner-migration/kubeconfig
```

## Essential Commands

```bash
# Check everything is running
kubectl get pods -n attendee

# View logs
kubectl logs deployment/attendee-api -n attendee
kubectl logs deployment/attendee-scheduler -n attendee
kubectl logs deployment/attendee-worker -n attendee

# Restart a service
kubectl rollout restart deployment/attendee-api -n attendee
```

## Services

| Service | Purpose |
|---------|---------|
| attendee-api | Django API |
| attendee-scheduler | Creates bot pods before meetings |
| attendee-worker | Celery, transcription processing |
| postgres | Database |
| redis | Cache/queue |

## Bot Autoscaling

- Bot pods run on dedicated cpx32 nodes (0-20 nodes)
- 1 bot per node, 3.5 CPU request / 4 CPU limit
- Nodes auto-provision via Hetzner cluster autoscaler

## Docs

| Doc | Contents |
|-----|----------|
| [docs/architecture.md](docs/architecture.md) | Infrastructure diagram, service details |
| [docs/configuration.md](docs/configuration.md) | ConfigMap, secrets, autoscaler settings |
| [docs/operations.md](docs/operations.md) | Troubleshooting, database, scaling |
| [docs/github.md](docs/github.md) | Image updates, deployments |
| [docs/maintenance.md](docs/maintenance.md) | Costs, rollback, contacts |

## Manifests

K8s manifests in `manifests/` - apply with:
```bash
kubectl apply -f manifests/
```

## Sensitive Files (NOT in repo)

Keep these locally in `~/projects/hetzner-migration/`:
- `kubeconfig` - cluster credentials
- `cluster.yaml` - Hetzner cluster definition (contains API token)
- `cluster-autoscaler.yaml` - autoscaler config (contains k3s token)
