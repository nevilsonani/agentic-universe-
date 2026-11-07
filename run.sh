#!/usr/bin/env bash
# Starts the controller against a running simulator and blocks until the scenario finishes.
#   ./run.sh http://127.0.0.1:8700
# No Docker needed: Python 3.10+ with the packages in requirements.txt (numpy, pyyaml, httpx; fastapi/uvicorn
# are only for the simulator). State goes to $CTL_DB (default runs/controller-<port>/state.db). Re-running
# with the same $CTL_DB resumes after a crash; point it at a fresh path for a fresh world.
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
cd "$(dirname "$0")"
PY="${PYTHON:-$(command -v python3 || command -v python)}"
PORT="$(echo "$SIM_URL" | sed -E 's#.*:([0-9]+).*#\1#')"
DB="${CTL_DB:-runs/controller-${PORT}/state.db}"
VIEW="${CTL_VIEW:-$(dirname "$DB")/supervisor.html}"
mkdir -p "$(dirname "$DB")"
export PYTHONUTF8=1
exec "$PY" -m controller run --url "$SIM_URL" --db "$DB" --view "$VIEW"
