<!-- Parent: ../CLAUDE.md | Namespace: attendee | KUBECONFIG=~/projects/hetzner-migration/kubeconfig -->

# Maintenance

## Costs

| Resource | Cost |
|----------|------|
| Master (cpx22) | €9.29/mo |
| Static worker (cpx22) | €9.29/mo |
| Load Balancer | €6.41/mo |
| Volume (20GB) | €0.96/mo |
| **Base total** | **~€26/mo** |
| Bot nodes | €0.026/hr each |
| **Realistic total** | **€30-45/mo** |

## Hetzner Console

https://console.hetzner.cloud

## Rotate API Token

1. Hetzner Console → Security → API Tokens → Generate new
2. Update `cluster.yaml`
3. Update cluster-autoscaler secret if needed

## Rollback to Contabo

If Hetzner has issues:

```bash
# 1. Start Contabo scheduler
ssh deploy@84.247.128.112 "kubectl scale deployment attendee-scheduler -n attendee --replicas=1"

# 2. Update DNS in GoDaddy
#    wayfarrow.info → 84.247.128.112
#    api.wayfarrow.info → 84.247.128.112

# 3. Stop Hetzner scheduler
kubectl scale deployment/attendee-scheduler -n attendee --replicas=0
```

Contabo password: `Deploy2024!Meet#123`

## Maintenance Schedule

**Weekly:** Check autoscaler logs, review errors
**Monthly:** Check disk usage, review invoice
**Quarterly:** Rotate tokens, test rollback

## Resources

- Hetzner Status: https://status.hetzner.com
- k3s Docs: https://docs.k3s.io
