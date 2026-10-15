#!/usr/bin/env bash
# Creates the local kind cluster and the kubesentinel-lab namespace.
# Safe to re-run: kind no-ops if the cluster already exists, kubectl apply is idempotent.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

CLUSTER_NAME="kubesentinel"

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  echo "kind cluster '$CLUSTER_NAME' already exists, skipping creation."
else
  echo "Creating kind cluster '$CLUSTER_NAME'..."
  kind create cluster --config kind-config.yaml
fi

kubectl config use-context "kind-$CLUSTER_NAME"
kubectl apply -f manifests/namespace.yaml

echo
echo "Cluster ready. Context: kind-$CLUSTER_NAME"
kubectl get nodes
