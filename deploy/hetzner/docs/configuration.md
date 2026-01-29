<!-- Parent: ../README.md | Namespace: attendee | KUBECONFIG=~/projects/attendee/deploy/hetzner/kubeconfig -->

# Configuration

## ConfigMap (env)

```bash
# View
kubectl get configmap env -n attendee -o yaml

# Edit
kubectl edit configmap env -n attendee

# Restart services after edit
kubectl rollout restart deployment/attendee-api deployment/attendee-scheduler deployment/attendee-worker -n attendee
```

### Key Values

| Key | Value | Description |
|-----|-------|-------------|
| BOT_CPU_REQUEST | 3500m | CPU request per bot (must be < cpx32 allocatable ~3.8) |
| BOT_CPU_LIMIT | 4000m | CPU limit per bot (allows burst) |
| BOT_MEMORY_REQUEST | 2Gi | Memory per bot |
| BOT_MEMORY_LIMIT | 8Gi | Max memory |
| BOT_POD_IMAGE_PULL_POLICY | Always | Must be Always for autoscaled nodes (default Never breaks new nodes) |
| MAX_CONCURRENT_BOTS | 20 | Scheduler limit |
| BOT_POD_NODE_SELECTOR | {"workload":"bots"} | Target bot pool |
| DATABASE_URL | postgres://... | PostgreSQL connection |
| REDIS_URL | redis://... | Redis connection |

## Secrets (app-secrets)

Contains API keys for external services.

```bash
# List secret keys
kubectl get secret app-secrets -n attendee -o jsonpath='{.data}' | jq 'keys'

# Decode a value
kubectl get secret app-secrets -n attendee -o jsonpath='{.data.DEEPGRAM_API_KEY}' | base64 -d
```

### Secret Keys

- DEEPGRAM_API_KEY, ANTHROPIC_API_KEY
- SUPABASE_URL, SUPABASE_SERVICE_KEY
- R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY
- GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
- DJANGO_SECRET_KEY, DATABASE_URL

## Autoscaler Config

```bash
# Check current max nodes
kubectl get deployment cluster-autoscaler -n kube-system -o jsonpath='{.spec.template.spec.containers[0].command}' | grep nodes

# Edit (change 0:20 to new min:max)
kubectl edit deployment cluster-autoscaler -n kube-system
```

### cloudInit Labels/Taints (CRITICAL)

The autoscaler creates bot nodes via Hetzner API using a cloudInit script. This script MUST include node labels and taints for pods to schedule correctly.

**Verify cloudInit has labels/taints:**
```bash
kubectl get deploy cluster-autoscaler -n kube-system -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="HCLOUD_CLUSTER_CONFIG")].value}' | base64 -d | jq -r '.nodeConfigs.bots.cloudInit' | base64 -d
```

**Expected output should include:**
```
--node-label=workload=bots --node-taint=workload=bots:NoSchedule
```

If missing, bot nodes will be created without labels and pods won't schedule (causes runaway scaling).

## RBAC

The `attendee-bot-creator` service account needs:
1. **Role `bot-pod-manager`** (namespace-scoped): Create/manage bot pods
2. **ClusterRole `attendee-health-reader`** (cluster-scoped): Read namespaces/nodes for health dashboard

```bash
# Verify RBAC
kubectl get clusterrolebinding attendee-health-reader-binding
kubectl get rolebinding attendee-bot-creator-binding -n attendee
```

If health dashboard shows scheduler as orange/unknown, the ClusterRole may be missing:
```bash
kubectl apply -f manifests/rbac-health-reader.yaml
```
