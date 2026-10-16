#!/usr/bin/env bash
# Removes all lab scenario resources from kubesentinel-lab, without deleting the
# namespace itself or touching anything outside it.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# Namespaced resource kinds a scenario might create -- networkpolicy
# (Milestone 6), horizontalpodautoscaler (Milestone 5), and serviceaccount
# (Milestone 6) were each added only when a scenario first needed them;
# found live that they weren't here from the start, so an earlier
# `cleanup.sh` run silently left them behind despite being scenario-labeled.
kubectl -n kubesentinel-lab delete \
  deployment,pod,pvc,service,networkpolicy,horizontalpodautoscaler,serviceaccount \
  -l 'kubesentinel.io/scenario' --ignore-not-found

# ClusterRoleBinding/ClusterRole have no namespace at all -- a `-n
# kubesentinel-lab` delete above can never reach them, no matter the
# label, so they need their own explicit cluster-scoped delete.
kubectl delete clusterrolebinding,clusterrole -l 'kubesentinel.io/scenario' --ignore-not-found

echo "Cleaned up all scenario resources in kubesentinel-lab (and any scenario-labeled cluster-scoped objects)."
