#!/usr/bin/env bash
# Deletes the local kind cluster entirely. This is destructive but scoped to the kind
# VM/containers only -- it never touches anything outside the "kubesentinel" kind cluster.
set -euo pipefail

CLUSTER_NAME="kubesentinel"

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  kind delete cluster --name "$CLUSTER_NAME"
  echo "Deleted kind cluster '$CLUSTER_NAME'."
else
  echo "kind cluster '$CLUSTER_NAME' does not exist, nothing to do."
fi
