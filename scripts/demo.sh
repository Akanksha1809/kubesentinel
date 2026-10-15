#!/usr/bin/env bash
# Runs a live demo: ensures the kind cluster + lab namespace exist, applies the
# requested failure scenario (default: all of them), then starts the API server in
# the foreground against that cluster. Open a second terminal to poll it while this
# runs. Ctrl+C stops the server; it does not delete the cluster or the scenario pods.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

SCENARIO="${1:-all}"

if [ ! -x .venv/bin/uvicorn ]; then
  echo "venv not found or incomplete. Run ./scripts/setup.sh first." >&2
  exit 1
fi

if ! kind get clusters 2>/dev/null | grep -qx kubesentinel; then
  ./lab/create-cluster.sh
else
  kubectl config use-context kind-kubesentinel >/dev/null
fi

if [[ "$SCENARIO" == "all" ]]; then
  for s in crashloop oom imagepull pvc-binding service-selector-mismatch \
           pending-resources readiness-probe-failure failed-rollout; do
    ./lab/scripts/run-scenario.sh "$s"
  done
  kubectl apply -f lab/manifests/scenario-transient-recovery/pod.yaml
else
  ./lab/scripts/run-scenario.sh "$SCENARIO"
fi

echo
echo "Scenario(s) applied. Starting the API server on http://127.0.0.1:8000"
echo "In another terminal, try:"
echo "  curl -s http://127.0.0.1:8000/incidents | python3 -m json.tool"
echo "  .venv/bin/kubesentinel incidents"
echo "  .venv/bin/kubesentinel evidence <incident-id>"
echo "  curl -s http://127.0.0.1:8000/metrics | grep kubesentinel_"
echo
echo "Press Ctrl+C to stop the server."
echo

export KUBESENTINEL_WATCH_NAMESPACES=kubesentinel-lab
export KUBESENTINEL_DB_PATH="${KUBESENTINEL_DB_PATH:-./kubesentinel.db}"
exec .venv/bin/uvicorn kubesentinel.api.app:app --host 127.0.0.1 --port 8000 --app-dir src
