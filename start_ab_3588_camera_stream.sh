#!/usr/bin/env bash
set -euo pipefail

# Copy this file into /home/current/web_camera_realtime on the RK3588.
# It is an independent wrapper around that checkout's existing start.sh and
# deliberately does not modify the original camera scripts.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCHER="$ROOT/start.sh"

if [[ ! -f "$LAUNCHER" ]]; then
  echo "ERROR: existing 3588 camera launcher is missing: $LAUNCHER" >&2
  exit 1
fi

echo "Starting the existing RK3588 camera stream:"
echo "  $LAUNCHER"

if [[ "${DRY_RUN:-0}" == "1" ]]; then
  echo "DRY RUN: no camera process was started."
  exit 0
fi

exec bash "$LAUNCHER"
