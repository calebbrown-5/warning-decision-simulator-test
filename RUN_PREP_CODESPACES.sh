#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
if [[ "${CODESPACES:-}" != "true" ]]; then
  echo "This launcher is for GitHub Codespaces. Local host can use: python scenario_backend.py --port 8765"
  exit 1
fi
if [[ ! -x .venv-wds/bin/python ]]; then
  echo "Missing .venv-wds. Run: bash SETUP_PREP_CODESPACES.sh"
  exit 1
fi
export PYTHONDONTWRITEBYTECODE=1
exec .venv-wds/bin/python scenario_backend.py --port 8765
