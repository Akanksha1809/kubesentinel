#!/usr/bin/env bash
# Runs a live demo: ensures the kind cluster + lab namespace exist, starts the API
# server (so its own watch loops are already running), then applies the requested
# failure scenario (default: all of them) against that already-watching server. Open
# a second terminal to poll it while this runs. Ctrl+C stops the server; it does not
# delete the cluster or the scenario pods.
#
# Server-then-scenarios, not the other way around: a scenario that crashes and
# recovers in under a second (transient-recovery) would otherwise already be healthy
# by the time a server started afterward took its first look, so its own documented
# "opens an incident, then resolves it with a recorded recovery_duration_seconds"
# behavior would never actually happen -- found live, not theoretical.
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

export KUBESENTINEL_WATCH_NAMESPACES=kubesentinel-lab
export KUBESENTINEL_DB_PATH="${KUBESENTINEL_DB_PATH:-./kubesentinel.db}"

echo "Starting the API server on http://127.0.0.1:8000 ..."
.venv/bin/uvicorn kubesentinel.api.app:app --host 127.0.0.1 --port 8000 --app-dir src &
SERVER_PID=$!
# Ctrl+C (or any script exit) stops the server the same way `exec`-ing it used to --
# it's a background job now, so that has to be forwarded explicitly.
trap 'kill "$SERVER_PID" 2>/dev/null' EXIT INT TERM

for _ in $(seq 1 30); do
  if curl -sf http://127.0.0.1:8000/metrics >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done

if [[ "$SCENARIO" == "all" ]]; then
  for s in crashloop oom imagepull pvc-binding service-selector-mismatch \
           pending-resources readiness-probe-failure failed-rollout checkout-demo; do
    ./lab/scripts/run-scenario.sh "$s"
  done
  kubectl apply -f lab/manifests/scenario-transient-recovery/pod.yaml
else
  ./lab/scripts/run-scenario.sh "$SCENARIO"
fi

echo
echo "Scenario(s) applied against the already-running server."
echo "In another terminal, try:"
echo "  curl -s http://127.0.0.1:8000/incidents | python3 -m json.tool"
echo "  .venv/bin/kubesentinel incidents"
echo "  .venv/bin/kubesentinel evidence <incident-id>"
echo "  curl -s http://127.0.0.1:8000/metrics | grep kubesentinel_"
echo
echo "Press Ctrl+C to stop the server."
echo

wait "$SERVER_PID" || true
