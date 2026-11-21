#!/bin/sh
set -eu
umask 077
cd "$(dirname "$0")"
exec python3 server.py "${PRINTPILOT_CONFIG:-$HOME/.config/printpilot/server.json}"
