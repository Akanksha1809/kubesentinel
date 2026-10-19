#!/usr/bin/env bash
# Deletes the optional multi-node kind cluster entirely. Destructive but
# scoped to the "kubesentinel-multi" kind cluster only -- never touches the
# primary single-node "kubesentinel" cluster or anything outside either.
set -euo pipefail

CLUSTER_NAME="kubesentinel-multi"

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  kind delete cluster --name "$CLUSTER_NAME"
  echo "Deleted kind cluster '$CLUSTER_NAME'."
else
  echo "kind cluster '$CLUSTER_NAME' does not exist, nothing to do."
fi
