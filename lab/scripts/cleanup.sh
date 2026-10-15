#!/usr/bin/env bash
# Removes all lab scenario resources from kubesentinel-lab, without deleting the
# namespace itself or touching anything outside it.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

kubectl -n kubesentinel-lab delete deployment,pod,pvc,service -l 'kubesentinel.io/scenario' --ignore-not-found
echo "Cleaned up all scenario resources in kubesentinel-lab."
