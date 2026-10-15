#!/usr/bin/env bash
# Runs the test suite. Unit tests always run (no cluster needed). Integration/e2e
# tests (Milestone 9) additionally require a live kind cluster and are opted into
# with --all -- each one skips (not fails) if the cluster, lab namespace, or a
# specific scenario it needs isn't present, so --all stays runnable without one.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [ ! -x .venv/bin/pytest ]; then
  echo "venv not found or incomplete. Run ./scripts/setup.sh first." >&2
  exit 1
fi

echo "--- ruff ---"
.venv/bin/ruff check src tests

echo "--- mypy ---"
.venv/bin/mypy src/kubesentinel

if [[ "${1:-}" == "--all" ]]; then
  echo "--- pytest (unit + integration + e2e) ---"
  .venv/bin/pytest tests -q
else
  echo "--- pytest (unit only; pass --all to include integration/e2e against a live kind cluster) ---"
  .venv/bin/pytest tests/unit -q
fi
