#!/usr/bin/env bash
# Installs metrics-server on the local kind cluster -- optional, needed only for
# the resource optimization engine's CPU/memory recommendations to have real
# usage data to work from (without it, the engine still runs correctly, just
# with every recommendation honestly staying at EvidenceQuality.INSUFFICIENT/
# SNAPSHOT_ONLY -- see docs/RESOURCE_OPTIMIZATION_DESIGN.md Section 9).
#
# Not part of create-cluster.sh: the troubleshooting engine and its lab
# scenarios need nothing here, so this stays a separate, explicit opt-in
# rather than something every lab setup pays for.
#
# kind's kubelet serves a self-signed cert metrics-server doesn't trust by
# default; --kubelet-insecure-tls is the standard, documented kind-specific
# workaround (not something to carry into a real cluster).
set -euo pipefail

kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml

kubectl -n kube-system patch deployment metrics-server --type=json \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'

echo "Waiting for metrics-server to roll out..."
kubectl -n kube-system rollout status deployment/metrics-server --timeout=90s

echo
echo "metrics-server installed. Verify with: kubectl top pods -n kubesentinel-lab"
