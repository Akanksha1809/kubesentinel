#!/usr/bin/env bash
# Checks that the local tools KubeSentinel needs are installed, without installing
# or modifying anything. Exits non-zero if something required is missing.
set -uo pipefail

ok=true

check() {
  local name="$1"
  local cmd="$2"
  local version_args="$3"
  local hint="$4"
  if command -v "$cmd" >/dev/null 2>&1; then
    local version
    version=$("$cmd" $version_args 2>&1 | head -1)
    echo "[OK]   $name: $version"
  else
    echo "[MISSING] $name: $hint"
    ok=false
  fi
}

check "Docker"       docker     "--version" "install Docker Desktop or Colima: https://docs.docker.com/get-docker/"
check "kind"         kind       "--version" "brew install kind"
check "kubectl"      kubectl    "version --client" "brew install kubectl"
check "Python 3.11+" python3.12 "--version" "brew install python@3.12 (3.11+ required; system python3 may be too old)"

echo
if command -v docker >/dev/null 2>&1; then
  if docker info >/dev/null 2>&1; then
    echo "[OK]   Docker daemon is reachable"
  else
    echo "[FAIL] Docker daemon is not reachable (is Docker Desktop/Colima running?)"
    ok=false
  fi
fi

echo
if $ok; then
  echo "All prerequisites satisfied."
  exit 0
else
  echo "One or more prerequisites are missing. Install them and re-run this script."
  exit 1
fi
