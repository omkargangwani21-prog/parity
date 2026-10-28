#!/usr/bin/env bash
# Usage: ./run.sh            start the web UI on http://localhost:8000
#        ./run.sh bench DIR  run the CLI on a folder of .cob files
set -e
cd "$(dirname "$0")"
if [ "$1" = "bench" ]; then shift; exec python backend/parity.py run "$@"; fi
exec python backend/server.py
