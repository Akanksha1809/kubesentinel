#!/usr/bin/env bash
# Creates the optional multi-node kind cluster (kind-config-multi-node.yaml)
# and the kubesentinel-lab namespace in it -- a separate cluster from the
# primary single-node one create-cluster.sh manages, never touching it.
# Safe to re-run: kind no-ops if the cluster already exists, kubectl apply
# is idempotent.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

CLUSTER_NAME="kubesentinel-multi"

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  echo "kind cluster '$CLUSTER_NAME' already exists, skipping creation."
else
  echo "Creating kind cluster '$CLUSTER_NAME' (1 control-plane + 2 workers)..."
  kind create cluster --config kind-config-multi-node.yaml
fi

kubectl config use-context "kind-$CLUSTER_NAME"
kubectl apply -f manifests/namespace.yaml

echo
echo "Multi-node cluster ready. Context: kind-$CLUSTER_NAME"
kubectl get nodes
echo
echo "This context is now current. Switch back to the primary single-node"
echo "cluster with: kubectl config use-context kind-kubesentinel"
echo "Tear this one down with: ./lab/delete-multi-node-cluster.sh"
