<!-- Parent: ../CLAUDE.md | Namespace: attendee | KUBECONFIG=~/projects/hetzner-migration/kubeconfig -->

# GitHub Integration

## Container Images

Images hosted on GitHub Container Registry:

```
ghcr.io/hjs48/attendee:latest
```

**Pull Secret:** `ghcr-secret` in attendee namespace

## Deploying Updates

### Option 1: Restart (pulls latest)

```bash
kubectl rollout restart deployment/attendee-api deployment/attendee-scheduler deployment/attendee-worker -n attendee
```

### Option 2: Force re-pull

```bash
kubectl delete pod -n attendee -l app=attendee-api
kubectl delete pod -n attendee -l app=attendee-scheduler
kubectl delete pod -n attendee -l app=attendee-worker
```

### Option 3: Specific tag

```bash
kubectl set image deployment/attendee-api attendee-api=ghcr.io/hjs48/attendee:v1.2.3 -n attendee
kubectl set image deployment/attendee-scheduler attendee-scheduler=ghcr.io/hjs48/attendee:v1.2.3 -n attendee
kubectl set image deployment/attendee-worker attendee-worker=ghcr.io/hjs48/attendee:v1.2.3 -n attendee
```

## Image Pull Secret

If token expires:

```bash
kubectl delete secret ghcr-secret -n attendee

kubectl create secret docker-registry ghcr-secret \
  --namespace attendee \
  --docker-server=ghcr.io \
  --docker-username=hjs48 \
  --docker-password=<NEW_GITHUB_PAT>
```

Needs PAT with `read:packages` scope: https://github.com/settings/tokens/new?scopes=read:packages
