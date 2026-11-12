#!/usr/bin/env bash
# Starts Puppet Feeds Local on Linux and opens it in your browser. Close the terminal (or use "Close app") to stop.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$HERE/news_briefing.py" "$@"
