#!/usr/bin/env bash
set -euo pipefail

SSH_SOURCE="${SSH_CLIENT:-}"
TARGET_IP="${1:-${SSH_SOURCE%% *}}"
PORT="${PORT:-5000}"
DEVICE="${DEVICE:-/dev/video0}"
WIDTH="${WIDTH:-1280}"
HEIGHT="${HEIGHT:-1024}"
FPS="${FPS:-60}"
BITRATE="${BITRATE:-1000000}"
FORMAT="${FORMAT:-mjpeg}"
IFRAME_INTERVAL="${IFRAME_INTERVAL:-30}"
# UVC exposure_auto values: 1=manual, 3=auto/aperture-priority on this camera.
EXPOSURE_AUTO="${EXPOSURE_AUTO:-1}"
EXPOSURE_ABSOLUTE="${EXPOSURE_ABSOLUTE:-80}"
GAIN="${GAIN:-32}"
LABEL="${LABEL:-ROBOT}"
ROTATE="${ROTATE:-none}"

if [[ -z "${TARGET_IP}" ]]; then
  echo "ERROR: target IP is empty." >&2
  echo "Usage: $0 <LOCAL_PC_IP>" >&2
  echo "Example: $0 192.168.50.223" >&2
  exit 1
fi

if ! command -v gst-launch-1.0 >/dev/null 2>&1; then
  echo "ERROR: gst-launch-1.0 is not installed on the robot." >&2
  exit 1
fi

if ! gst-inspect-1.0 nvv4l2h264enc >/dev/null 2>&1; then
  echo "ERROR: nvv4l2h264enc is not available. Jetson hardware H.264 encoder plugin is missing." >&2
  exit 1
fi

if [[ ! -e "${DEVICE}" ]]; then
  echo "ERROR: camera device ${DEVICE} does not exist." >&2
  echo "Available video devices:" >&2
  ls /dev/video* 2>/dev/null >&2 || true
  exit 1
fi

echo "Streaming ${DEVICE} to ${TARGET_IP}:${PORT} over RTP/UDP"
echo "Format=${FORMAT}, target/native=${WIDTH}x${HEIGHT}@${FPS}, bitrate=${BITRATE}, iframe=${IFRAME_INTERVAL}"
echo "Camera controls: exposure_auto=${EXPOSURE_AUTO}, exposure_absolute=${EXPOSURE_ABSOLUTE}, gain=${GAIN}"
echo "Video transform: rotate=${ROTATE}"

case "${FORMAT}" in
  auto)
    # Lets the UVC driver choose its native mode. On this robot it negotiates YUY2 1280x1024@5.
    gst-launch-1.0 -e \
      v4l2src device="${DEVICE}" io-mode=2 do-timestamp=true extra-controls="c,exposure_auto=${EXPOSURE_AUTO},exposure_absolute=${EXPOSURE_ABSOLUTE},gain=${GAIN}" \
      ! videoconvert \
      ! videoflip method="${ROTATE}" \
      ! clockoverlay text="${LABEL} " valignment=top halignment=left shaded-background=true font-desc="Sans, 22" time-format="%H:%M:%S.%N" \
      ! nvvidconv \
      ! 'video/x-raw(memory:NVMM),format=NV12' \
      ! nvv4l2h264enc insert-sps-pps=true iframeinterval="${IFRAME_INTERVAL}" idrinterval="${IFRAME_INTERVAL}" \
          bitrate="${BITRATE}" control-rate=1 preset-level=1 maxperf-enable=1 \
      ! h264parse config-interval=1 \
      ! rtph264pay pt=96 mtu=1200 \
      ! udpsink host="${TARGET_IP}" port="${PORT}" sync=false async=false
    ;;
  raw|yuy2)
    gst-launch-1.0 -e \
      v4l2src device="${DEVICE}" io-mode=2 do-timestamp=true extra-controls="c,exposure_auto=${EXPOSURE_AUTO},exposure_absolute=${EXPOSURE_ABSOLUTE},gain=${GAIN}" \
      ! "video/x-raw,format=YUY2,width=${WIDTH},height=${HEIGHT},framerate=${FPS}/1" \
      ! videoconvert \
      ! videoconvert \
      ! videoflip method="${ROTATE}" \
      ! clockoverlay text="${LABEL} " valignment=top halignment=left shaded-background=true font-desc="Sans, 22" time-format="%H:%M:%S.%N" \
      ! nvvidconv \
      ! 'video/x-raw(memory:NVMM),format=NV12' \
      ! nvv4l2h264enc insert-sps-pps=true iframeinterval="${IFRAME_INTERVAL}" idrinterval="${IFRAME_INTERVAL}" \
          bitrate="${BITRATE}" control-rate=1 preset-level=1 maxperf-enable=1 \
      ! h264parse config-interval=1 \
      ! rtph264pay pt=96 mtu=1200 \
      ! udpsink host="${TARGET_IP}" port="${PORT}" sync=false async=false
    ;;
  mjpeg)
    gst-launch-1.0 -e \
      v4l2src device="${DEVICE}" io-mode=2 do-timestamp=true extra-controls="c,exposure_auto=${EXPOSURE_AUTO},exposure_absolute=${EXPOSURE_ABSOLUTE},gain=${GAIN}" \
      ! "image/jpeg,width=${WIDTH},height=${HEIGHT},framerate=${FPS}/1" \
      ! jpegdec \
      ! videoconvert \
      ! videoflip method="${ROTATE}" \
      ! clockoverlay text="${LABEL} " valignment=top halignment=left shaded-background=true font-desc="Sans, 22" time-format="%H:%M:%S.%N" \
      ! nvvidconv \
      ! 'video/x-raw(memory:NVMM),format=NV12' \
      ! nvv4l2h264enc insert-sps-pps=true iframeinterval="${IFRAME_INTERVAL}" idrinterval="${IFRAME_INTERVAL}" \
          bitrate="${BITRATE}" control-rate=1 preset-level=1 maxperf-enable=1 \
      ! h264parse config-interval=1 \
      ! rtph264pay pt=96 mtu=1200 \
      ! udpsink host="${TARGET_IP}" port="${PORT}" sync=false async=false
    ;;
  *)
    echo "ERROR: FORMAT must be 'auto', 'raw', or 'mjpeg'. Current: ${FORMAT}" >&2
    exit 1
    ;;
esac
