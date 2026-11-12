#!/usr/bin/env bash
# Starts the Cold Key Generator. Run it from a terminal on the offline computer:  bash start.sh
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# -I: ignore any Python settings or packages from the environment, -B: write no cache files
exec python3 -I -B "$HERE/coldkeys.py" "$@"
