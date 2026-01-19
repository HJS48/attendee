#!/bin/bash
# Deployment script for Attendee Kubernetes resources
# Usage: ./deploy.sh [apply|status|logs|delete]

set -e

export KUBECONFIG=/home/deploy/.kube/config
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

case "${1:-status}" in
  apply)
    echo "Applying Kubernetes resources..."
    kubectl apply -k "$SCRIPT_DIR"
    echo ""
    echo "Waiting for deployments to be ready..."
    kubectl rollout status deployment/attendee-api -n attendee --timeout=120s || true
    kubectl rollout status deployment/attendee-scheduler -n attendee --timeout=120s || true
    echo ""
    echo "Current status:"
    kubectl get pods -n attendee
    ;;

  status)
    echo "=== Nodes ==="
    kubectl get nodes
    echo ""
    echo "=== Namespaces ==="
    kubectl get ns | grep -E "attendee|NAME"
    echo ""
    echo "=== Pods in attendee namespace ==="
    kubectl get pods -n attendee -o wide
    echo ""
    echo "=== Services ==="
    kubectl get svc -n attendee
    echo ""
    echo "=== Recent Events ==="
    kubectl get events -n attendee --sort-by='.lastTimestamp' | tail -10
    ;;

  logs)
    COMPONENT="${2:-api}"
    echo "Logs for attendee-$COMPONENT..."
    kubectl logs -n attendee -l app=attendee-$COMPONENT --tail=100 -f
    ;;

  delete)
    echo "Deleting deployments (keeping namespace and secrets)..."
    kubectl delete deployment attendee-api attendee-scheduler -n attendee --ignore-not-found
    ;;

  delete-all)
    echo "WARNING: This will delete all Attendee Kubernetes resources!"
    read -p "Are you sure? (yes/no): " confirm
    if [ "$confirm" = "yes" ]; then
      kubectl delete -k "$SCRIPT_DIR" --ignore-not-found
    else
      echo "Cancelled"
    fi
    ;;

  *)
    echo "Usage: $0 [apply|status|logs|delete|delete-all]"
    echo ""
    echo "Commands:"
    echo "  apply      - Deploy/update all resources"
    echo "  status     - Show current state of all resources"
    echo "  logs [api|scheduler] - Stream logs from a component"
    echo "  delete     - Remove deployments (keep secrets)"
    echo "  delete-all - Remove all resources including namespace"
    exit 1
    ;;
esac
