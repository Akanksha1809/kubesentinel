#!/usr/bin/env bash
# One-command local setup: creates the venv, installs the package + dev deps, and
# copies .env.example to .env if one doesn't exist yet. Does not touch any cluster.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

PYTHON_BIN="${PYTHON_BIN:-python3.12}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Could not find $PYTHON_BIN. Install Python 3.11+ (e.g. 'brew install python@3.12') or set PYTHON_BIN." >&2
  exit 1
fi

if [ ! -d .venv ]; then
  echo "Creating virtualenv with $PYTHON_BIN..."
  "$PYTHON_BIN" -m venv .venv
fi

echo "Installing kubesentinel + dev dependencies..."
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e ".[dev]"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example (edit as needed)."
fi

echo
echo "Setup complete. Next steps:"
echo "  ./scripts/check-prereqs.sh   # verify docker/kind/kubectl"
echo "  ./lab/create-cluster.sh      # create the local kind cluster"
echo "  ./scripts/test.sh            # run the test suite"
echo "  ./scripts/demo.sh            # run a live demo scenario"
