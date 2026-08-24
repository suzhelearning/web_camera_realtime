#!/usr/bin/env bash
set -euo pipefail

SSH_SOURCE="${SSH_CLIENT:-}"
TARGET_IP="${1:-${SSH_SOURCE%% *}}"
BASE_PORT="${BASE_PORT:-5000}"
PORT_STEP="${PORT_STEP:-2}"
MAX_CAMERAS="${MAX_CAMERAS:-8}"
ROTATIONS_CSV="${ROTATIONS:-none,clockwise,counterclockwise}"
EXPOSURE_AUTOS_CSV="${EXPOSURE_AUTOS:-1,1,1}"
EXPOSURES_CSV="${EXPOSURES:-80,40,40}"
GAINS_CSV="${GAINS:-32,24,24}"

if [[ -z "${TARGET_IP}" ]]; then
  echo "ERROR: target IP is empty." >&2
  echo "Usage: $0 <LOCAL_PC_IP>" >&2
  exit 1
fi

if [[ -n "${DEVICES:-}" ]]; then
  IFS=',' read -r -a DEVICES_ARR <<< "${DEVICES}"
else
  mapfile -t DEVICES_ARR < <(python3 - <<'PY'
import ctypes
import fcntl
import glob
import os

VIDIOC_QUERYCAP = 0x80685600
V4L2_CAP_VIDEO_CAPTURE = 0x00000001
V4L2_CAP_DEVICE_CAPS = 0x80000000

class v4l2_capability(ctypes.Structure):
    _fields_ = [
        ("driver", ctypes.c_char * 16),
        ("card", ctypes.c_char * 32),
        ("bus_info", ctypes.c_char * 32),
        ("version", ctypes.c_uint32),
        ("capabilities", ctypes.c_uint32),
        ("device_caps", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 3),
    ]

for dev in sorted(glob.glob('/dev/video*'), key=lambda p: int(''.join(ch for ch in os.path.basename(p) if ch.isdigit()) or 0)):
    try:
        fd = os.open(dev, os.O_RDWR | os.O_NONBLOCK)
    except OSError:
        continue
    try:
        cap = v4l2_capability()
        fcntl.ioctl(fd, VIDIOC_QUERYCAP, cap)
        caps = cap.device_caps if (cap.capabilities & V4L2_CAP_DEVICE_CAPS) else cap.capabilities
        if caps & V4L2_CAP_VIDEO_CAPTURE:
            print(dev)
    except OSError:
        pass
    finally:
        os.close(fd)
PY
)
fi

IFS=',' read -r -a ROTATIONS_ARR <<< "${ROTATIONS_CSV}"
IFS=',' read -r -a EXPOSURE_AUTOS_ARR <<< "${EXPOSURE_AUTOS_CSV}"
IFS=',' read -r -a EXPOSURES_ARR <<< "${EXPOSURES_CSV}"
IFS=',' read -r -a GAINS_ARR <<< "${GAINS_CSV}"
PIDS=()
cleanup() {
  for pid in "${PIDS[@]:-}"; do
    kill "${pid}" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

idx=0
for dev in "${DEVICES_ARR[@]}"; do
  dev="${dev//[[:space:]]/}"
  [[ -z "${dev}" ]] && continue
  if (( idx >= MAX_CAMERAS )); then
    break
  fi
  if [[ ! -e "${dev}" ]]; then
    echo "Skipping ${dev}: does not exist" >&2
    continue
  fi
  port=$((BASE_PORT + idx * PORT_STEP))
  label="CAM${idx}"
  rotate="${ROTATIONS_ARR[$idx]:-none}"
  rotate="${rotate//[[:space:]]/}"
  exposure_auto="${EXPOSURE_AUTOS_ARR[$idx]:-1}"
  exposure_auto="${exposure_auto//[[:space:]]/}"
  exposure="${EXPOSURES_ARR[$idx]:-80}"
  exposure="${exposure//[[:space:]]/}"
  gain="${GAINS_ARR[$idx]:-32}"
  gain="${gain//[[:space:]]/}"
  echo "Starting ${label}: ${dev} -> ${TARGET_IP}:${port} rotate=${rotate} exposure=${exposure} gain=${gain}"
  DEVICE="${dev}" PORT="${port}" LABEL="${label}" ROTATE="${rotate}" EXPOSURE_AUTO="${exposure_auto}" EXPOSURE_ABSOLUTE="${exposure}" GAIN="${gain}" \
    /home/unitree/project/web_camera_realtime/start_wifi_camera_stream.sh "${TARGET_IP}" &
  PIDS+=("$!")
  idx=$((idx + 1))
  sleep 0.5
done

if [[ ${#PIDS[@]} -eq 0 ]]; then
  echo "ERROR: no capture-capable camera devices found." >&2
  echo "Override manually with DEVICES=/dev/video0,/dev/video2 $0 ${TARGET_IP}" >&2
  exit 1
fi

echo "Started ${#PIDS[@]} camera stream(s). Ports: ${BASE_PORT}, step ${PORT_STEP}. Press Ctrl-C to stop all."
wait
