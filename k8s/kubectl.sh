#!/bin/bash
# Wrapper script to ensure KUBECONFIG is set
export KUBECONFIG=/home/deploy/.kube/config
kubectl "$@"
