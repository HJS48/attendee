<!-- Parent: ../CLAUDE.md | Namespace: attendee | KUBECONFIG=~/projects/hetzner-migration/kubeconfig -->

# Operations

## Health Checks

```bash
kubectl get pods -n attendee                           # All pods running?
kubectl logs deployment/attendee-scheduler -n attendee --tail=20  # Scheduler activity
kubectl get configmap cluster-autoscaler-status -n kube-system -o yaml  # Autoscaler
```

## Database

```bash
# Connect to psql
kubectl exec -it deployment/postgres -n attendee -- psql -U attendee -d attendee

# Run migrations
kubectl exec deployment/attendee-api -n attendee -- python manage.py migrate

# Django shell
kubectl exec -it deployment/attendee-api -n attendee -- python manage.py shell
```

## Scaling

```bash
# Scale API replicas
kubectl scale deployment/attendee-api -n attendee --replicas=2

# View active bot pods
kubectl get pods -n attendee -l app=attendee-bot
```

## Troubleshooting

### Bots Not Joining

```bash
# 1. Check scheduler
kubectl logs deployment/attendee-scheduler -n attendee --tail=50

# 2. Check calendar sync
kubectl exec deployment/attendee-api -n attendee -- python manage.py shell -c \
  "from bots.models import Calendar; print([(c.id, c.last_sync) for c in Calendar.objects.all()])"

# 3. Check autoscaler
kubectl logs deployment/cluster-autoscaler -n kube-system --tail=30
```

### API Down

```bash
kubectl describe pod -n attendee -l app=attendee-api
kubectl logs deployment/attendee-api -n attendee --tail=50
```

### Certificate Issues

```bash
kubectl get certificate -n attendee
kubectl delete certificate wayfarrow-tls -n attendee  # Force renewal
```

### Database Issues

```bash
kubectl exec deployment/postgres -n attendee -- df -h /var/lib/postgresql/data
kubectl exec deployment/postgres -n attendee -- psql -U attendee -c "SELECT count(*) FROM pg_stat_activity;"
```
