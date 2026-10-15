#!/usr/bin/env bash
# Applies one failure scenario's manifests into kubesentinel-lab.
# Usage: run-scenario.sh <scenario-name> -- see `manifests/scenario-*` for the full list.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

scenario="${1:-}"
dir="manifests/scenario-${scenario}"

if [[ -z "$scenario" || ! -d "$dir" ]]; then
  echo "Usage: $0 <scenario-name>"
  echo "Available scenarios:"
  ls -d manifests/scenario-* | sed 's#manifests/scenario-#  - #'
  exit 1
fi

kubectl apply -f "$dir/"
echo
echo "Applied scenario '$scenario'. Watch it with:"
echo "  kubectl -n kubesentinel-lab get pods -w"
