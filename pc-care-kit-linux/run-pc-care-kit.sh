#!/usr/bin/env bash
# Starts PC Care Kit on Linux. Keep this file next to pc_care_kit.py.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Use the system Python, which is where apt installs Tk.
for PY in /usr/bin/python3 /usr/bin/python3.[0-9]* python3; do
  if "$PY" -c "import tkinter" >/dev/null 2>&1; then
    exec "$PY" "$HERE/pc_care_kit.py" "$@"
  fi
done
echo "Missing parts. Run:  bash \"$HERE/install-linux.sh\""
command -v notify-send >/dev/null && notify-send "PC Care Kit" "Run install-linux.sh first."
exit 1
