#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
if [[ "${CODESPACES:-}" != "true" ]]; then
  echo "This setup is intended for GitHub Codespaces."
  exit 1
fi
python3 -m venv .venv-wds
.venv-wds/bin/python -m pip install --upgrade pip
.venv-wds/bin/python -m pip install -r WDS_PREP_REQUIREMENTS.txt
.venv-wds/bin/python -c 'import numpy, pyart, netCDF4, shapely; print("PASS: radar decoder and geography dependencies imported")'
echo 'SETUP PASSED. Make port 8765 PRIVATE before running: bash RUN_PREP_CODESPACES.sh'
