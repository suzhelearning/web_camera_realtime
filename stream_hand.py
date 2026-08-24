"""
WebRTC 摄像头串流 + BrainCo 手控制合并版。

视频串流到 VR 浏览器，同时在网页端用 Gamepad API 捕获手柄 trigger，
通过 WebSocket 发回服务端控制 BrainCo 灵巧手。

用法:
  python stream_hand.py --usb-camera /dev/video0 --width 1280 --height 960

  # 指定 brainco 参数
  python stream_hand.py --usb-camera /dev/video0 \\
    --brainco-ctrl ./bin/brainco_hand_ctrl \\
    --network-interface eth0 \\
    --threshold 0.5

  # 自定义手势
  python stream_hand.py --usb-camera /dev/video0 \\
    --left-initial 0,0,0,0,0,0 --left-closed 1,1,1,1,1,1 \\
    --right-initial 0,0,0,0,0,0 --right-closed 1,1,1,1,1,1
"""

import argparse
import asyncio
import json
import time
import threading
import fractions
import subprocess
import signal
import sys
import os
from pathlib import Path

import cv2
import numpy as np
import av
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from av import VideoFrame


# ========== 编码器检测与 Monkey-Patch ==========

HW_ENCODERS = ["h264_rkmpp", "h264_v4l2m2m"]
SW_ENCODER = "libx264"


def detect_best_encoder(preferred=None):
    if preferred:
        try:
            codec = av.CodecContext.create(preferred, "w")
            codec.width = 64
            codec.height = 64
            codec.pix_fmt = "yuv420p"
            codec.time_base = fractions.Fraction(1, 30)
            codec.open()
            codec.close()
            print(f"[encoder] 使用指定编码器: {preferred}")
            return preferred
        except Exception as e:
            print(f"[encoder] 指定编码器 {preferred} 不可用: {e}")

    for enc in HW_ENCODERS:
        try:
            codec = av.CodecContext.create(enc, "w")
            codec.width = 64
            codec.height = 64
            codec.pix_fmt = "yuv420p"
            codec.time_base = fractions.Fraction(1, 30)
            codec.open()
            codec.close()
            print(f"[encoder] 检测到硬件编码器: {enc}")
            return enc
        except Exception:
            continue

    print(f"[encoder] 未检测到硬件编码器，使用软件编码: {SW_ENCODER}")
    return SW_ENCODER


def patch_aiortc_encoder(encoder_name, bitrate):
    from aiortc.codecs.h264 import H264Encoder

    def patched_encode_frame(self, frame, force_keyframe):
        if self.codec and (
            frame.width != self.codec.width
            or frame.height != self.codec.height
            or abs(self.target_bitrate - self.codec.bit_rate) / self.codec.bit_rate > 0.1
        ):
            self.buffer_data = b""
            self.buffer_pts = None
            self.codec = None

        if force_keyframe:
            frame.pict_type = av.video.frame.PictureType.I
        else:
            frame.pict_type = av.video.frame.PictureType.NONE

        if self.codec is None:
            self.codec = av.CodecContext.create(encoder_name, "w")
            self.codec.width = frame.width
            self.codec.height = frame.height
            self.codec.bit_rate = bitrate
            self.codec.pix_fmt = "yuv420p"
            self.codec.framerate = fractions.Fraction(30, 1)
            self.codec.time_base = fractions.Fraction(1, 30)

            if encoder_name == SW_ENCODER:
                self.codec.options = {
                    "preset": "ultrafast",
                    "tune": "zerolatency",
                    "level": "31",
                    "rc-lookahead": "0",
                    "refs": "1",
                    "bframes": "0",
                    "nal-hrd": "cbr",
                    "sc_threshold": "0",
                }
                self.codec.profile = "Baseline"
            elif encoder_name == "h264_rkmpp":
                self.codec.options = {
                    "profile": "baseline",
                    "level": "31",
                    "rc_mode": "CBR",
                }
            elif encoder_name == "h264_v4l2m2m":
                self.codec.options = {
                    "num_output_buffers": "2",
                    "num_capture_buffers": "2",
                }
                self.codec.profile = "Baseline"
            else:
                self.codec.options = {"tune": "zerolatency"}
                self.codec.profile = "Baseline"

            self.codec.gop_size = 30
            self.codec.max_b_frames = 0
            print(f"[encoder] 初始化: {encoder_name} {frame.width}x{frame.height} "
                  f"bitrate={bitrate//1000}kbps")

        data_to_send = b""
        for package in self.codec.encode(frame):
            data_to_send += bytes(package)

        if data_to_send:
            yield from self._split_bitstream(data_to_send)

    H264Encoder._encode_frame = patched_encode_frame
    print(f"[encoder] aiortc H264 编码器已替换为: {encoder_name}")


# ========== 摄像头采集 ==========

class CameraCapture:
    def __init__(self, device, width, height, fps):
        self.device = device
        self.width = width
        self.height = height
        self.fps = fps
        self.frame = None
        self.frame_time = 0.0
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.actual_fps = 0.0
        self._frame_seq = 0

    def start(self):
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def get_frame(self):
        with self.lock:
            return self.frame, self.frame_time, self._frame_seq

    def _capture_loop(self):
        if self.device.startswith("/dev/"):
            cap = cv2.VideoCapture(self.device, cv2.CAP_V4L2)
        else:
            cap = cv2.VideoCapture(int(self.device), cv2.CAP_V4L2)

        if not cap.isOpened():
            print(f"[camera] 打开 {self.device} 失败")
            return

        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 2)

        actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        actual_fps = cap.get(cv2.CAP_PROP_FPS)
        print(f"[camera] 已打开: {self.device}  {actual_w}x{actual_h} @ {actual_fps} fps")

        if actual_w != self.width or actual_h != self.height:
            print(f"[camera] 警告: 请求 {self.width}x{self.height} 但实际 {actual_w}x{actual_h}")
            self.width = actual_w
            self.height = actual_h

        frame_count = 0
        fps_timer = time.time()

        while not self._stop.is_set():
            if not cap.grab():
                time.sleep(0.001)
                continue
            ret, frame = cap.retrieve()
            if not ret or frame is None:
                continue

            now = time.time()
            with self.lock:
                self.frame = frame
                self.frame_time = now
                self._frame_seq += 1

            frame_count += 1
            elapsed = now - fps_timer
            if elapsed >= 2.0:
                self.actual_fps = frame_count / elapsed
                print(f"[camera] FPS: {self.actual_fps:.1f}")
                frame_count = 0
                fps_timer = now

        cap.release()
        print("[camera] 已停止")


# ========== WebRTC 视频轨道 ==========

class CameraStreamTrack(VideoStreamTrack):
    kind = "video"

    def __init__(self, camera: CameraCapture):
        super().__init__()
        self.camera = camera

    async def recv(self):
        pts, time_base = await self.next_timestamp()
        frame, _, _ = self.camera.get_frame()
        if frame is None:
            frame = np.zeros((self.camera.height, self.camera.width, 3), dtype=np.uint8)
        video_frame = VideoFrame.from_ndarray(frame, format="bgr24")
        video_frame.pts = pts
        video_frame.time_base = time_base
        return video_frame


# ========== BrainCo 手控制 ==========

FINGER_COUNT = 6


def parse_vector(text):
    parts = [item.strip() for item in text.split(",")]
    if len(parts) != FINGER_COUNT:
        raise argparse.ArgumentTypeError(f"expected {FINGER_COUNT} comma-separated values")
    return [float(item) for item in parts]


def parse_mask(text):
    parts = [item.strip() for item in text.split(",")]
    if len(parts) != FINGER_COUNT:
        raise argparse.ArgumentTypeError(f"expected {FINGER_COUNT} comma-separated values")
    return [int(item) for item in parts]


def apply_mask(open_pose, close_pose, mask):
    return [close_pose[i] if mask[i] else open_pose[i] for i in range(FINGER_COUNT)]


class HandController:
    """处理 trigger 信号，控制 BrainCo 灵巧手"""

    def __init__(self, args):
        self.args = args
        self.left_closed = None
        self.right_closed = None

        self.left_open_pose = list(args.left_initial)
        self.right_open_pose = list(args.right_initial)
        self.left_closed_pose = apply_mask(
            self.left_open_pose, list(args.left_closed), list(args.left_close_mask)
        )
        self.right_closed_pose = apply_mask(
            self.right_open_pose, list(args.right_closed), list(args.right_close_mask)
        )

        self.brainco_ctrl = args.brainco_ctrl
        self.has_ctrl = self.brainco_ctrl.exists() if self.brainco_ctrl else False
        if not self.has_ctrl:
            print(f"[hand] 警告: brainco_hand_ctrl 未找到 ({self.brainco_ctrl})，仅打印日志")

    def send_pose(self, hand, pose, reason):
        msg = f"[hand] {hand} {reason}: {' '.join(f'{v:.2f}' for v in pose)}"
        if self.has_ctrl:
            cmd = [
                str(self.brainco_ctrl),
                "--hand", hand,
                "--net", self.args.network_interface,
                "set",
                *[f"{v:.4f}" for v in pose],
            ]
            try:
                subprocess.run(cmd, check=True, timeout=2)
                print(msg, flush=True)
            except Exception as e:
                print(f"[hand] 命令失败: {e}", file=sys.stderr, flush=True)
        else:
            print(msg, flush=True)

    def update(self, left_trigger, right_trigger):
        """根据 trigger 值更新手的状态"""
        threshold = self.args.threshold

        if left_trigger is not None:
            next_state = left_trigger >= threshold
            if self.left_closed is None or next_state != self.left_closed:
                pose = self.left_closed_pose if next_state else self.left_open_pose
                reason = "closed" if next_state else "opened"
                self.send_pose("left", pose, reason)
                self.left_closed = next_state

        if right_trigger is not None:
            next_state = right_trigger >= threshold
            if self.right_closed is None or next_state != self.right_closed:
                pose = self.right_closed_pose if next_state else self.right_open_pose
                reason = "closed" if next_state else "opened"
                self.send_pose("right", pose, reason)
                self.right_closed = next_state

    def initialize(self):
        startup = self.args.startup_pose
        left_pose = self.left_open_pose if startup != "closed" else self.left_closed_pose
        right_pose = self.right_open_pose if startup != "closed" else self.right_closed_pose
        self.send_pose("left", left_pose, "startup")
        self.send_pose("right", right_pose, "startup")
        self.left_closed = startup == "closed"
        self.right_closed = startup == "closed"
        print(f"[hand] threshold={self.args.threshold:.2f}")
        print(f"[hand] left  open/closed = {self.left_open_pose} / {self.left_closed_pose}")
        print(f"[hand] right open/closed = {self.right_open_pose} / {self.right_closed_pose}")


# ========== Web 前端 ==========

INDEX_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Camera + Hand Control</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #000; overflow: hidden; width: 100vw; height: 100vh; }
  video {
    width: 100vw; height: 100vh;
    object-fit: contain;
    display: block;
  }
  #stats {
    position: fixed; top: 10px; left: 10px;
    color: #0f0; font: 14px monospace;
    background: rgba(0,0,0,0.6);
    padding: 6px 10px; border-radius: 4px;
    z-index: 10; pointer-events: none;
    white-space: pre;
  }
</style>
</head>
<body>
<div id="stats">connecting...</div>
<video id="video" autoplay playsinline muted></video>
<script>
const video = document.getElementById('video');
const stats = document.getElementById('stats');

let fps = 0, frameCount = 0, lastFpsTime = performance.now();
let rtt = '-';
let ws = null;
let lastLeftTrigger = -1, lastRightTrigger = -1;

// ===== WebSocket 连接 =====
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(proto + '//' + location.host + '/ws');
  ws.onclose = () => { setTimeout(connectWS, 1000); };
  ws.onerror = () => { ws.close(); };
}
connectWS();

// ===== Gamepad 轮询 =====
function pollGamepads() {
  const gamepads = navigator.getGamepads ? navigator.getGamepads() : [];
  let leftTrigger = null;
  let rightTrigger = null;

  for (const gp of gamepads) {
    if (!gp) continue;

    // Pico 手柄通常有多个 axes 和 buttons
    // 标准 XR gamepad mapping:
    //   buttons[0] = trigger
    //   buttons[1] = grip/squeeze
    // Pico 可能映射不同，尝试读取所有 gamepad

    // 尝试根据 hand/id 区分左右
    const id = (gp.id || '').toLowerCase();
    const hand = gp.hand || '';

    let trigger = 0;
    if (gp.buttons.length > 0) {
      trigger = gp.buttons[0].value;  // trigger 通常是 button[0]
    }

    if (hand === 'left' || id.includes('left')) {
      leftTrigger = trigger;
    } else if (hand === 'right' || id.includes('right')) {
      rightTrigger = trigger;
    } else {
      // 无法区分左右，按 gamepad index 分配
      if (gp.index === 0) leftTrigger = trigger;
      else if (gp.index === 1) rightTrigger = trigger;
    }
  }

  // 只在 trigger 值变化时发送 (减少流量)
  const changed = (leftTrigger !== null && Math.abs(leftTrigger - lastLeftTrigger) > 0.02)
               || (rightTrigger !== null && Math.abs(rightTrigger - lastRightTrigger) > 0.02);

  if (changed && ws && ws.readyState === WebSocket.OPEN) {
    const msg = {};
    if (leftTrigger !== null) { msg.left_trigger = leftTrigger; lastLeftTrigger = leftTrigger; }
    if (rightTrigger !== null) { msg.right_trigger = rightTrigger; lastRightTrigger = rightTrigger; }
    ws.send(JSON.stringify(msg));
  }

  requestAnimationFrame(pollGamepads);
}
requestAnimationFrame(pollGamepads);

// ===== FPS 计算 =====
function onFrame() {
  frameCount++;
  const now = performance.now();
  if (now - lastFpsTime >= 1000) {
    fps = (frameCount / (now - lastFpsTime) * 1000).toFixed(1);
    frameCount = 0;
    lastFpsTime = now;
  }
  requestAnimationFrame(onFrame);
}

// ===== WebRTC =====
async function start() {
  const pc = new RTCPeerConnection({ sdpSemantics: 'unified-plan', iceServers: [] });

  pc.ontrack = (evt) => {
    video.srcObject = evt.streams[0];
    requestAnimationFrame(onFrame);
  };

  setInterval(async () => {
    try {
      const s = await pc.getStats();
      s.forEach(r => {
        if (r.type === 'candidate-pair' && r.state === 'succeeded'
            && r.currentRoundTripTime !== undefined)
          rtt = (r.currentRoundTripTime * 1000).toFixed(0);
      });
    } catch(e) {}

    // 显示 trigger 状态
    const gps = navigator.getGamepads ? navigator.getGamepads() : [];
    let lt = '-', rt = '-';
    for (const gp of gps) {
      if (!gp) continue;
      const hand = gp.hand || '';
      const val = gp.buttons.length > 0 ? gp.buttons[0].value.toFixed(2) : '-';
      if (hand === 'left') lt = val;
      else if (hand === 'right') rt = val;
      else if (gp.index === 0) lt = val;
      else if (gp.index === 1) rt = val;
    }
    stats.textContent = 'FPS: ' + fps + '  RTT: ' + rtt + 'ms\\nL: ' + lt + '  R: ' + rt;
  }, 200);

  pc.addTransceiver('video', { direction: 'recvonly' });
  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);

  const resp = await fetch('/offer', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sdp: pc.localDescription.sdp, type: pc.localDescription.type })
  });
  const answer = await resp.json();
  await pc.setRemoteDescription(new RTCSessionDescription(answer));
}

start().catch(e => { stats.textContent = 'Error: ' + e.message; });
</script>
</body>
</html>
"""


# ========== 信令服务 ==========

pcs = set()


async def index(request):
    return web.Response(content_type="text/html", text=INDEX_HTML)


async def offer(request):
    params = await request.json()
    offer_sdp = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    pc = RTCPeerConnection()
    pcs.add(pc)

    @pc.on("connectionstatechange")
    async def on_state():
        print(f"[webrtc] state: {pc.connectionState}")
        if pc.connectionState in ("failed", "closed"):
            await pc.close()
            pcs.discard(pc)

    camera = request.app["camera"]
    track = CameraStreamTrack(camera)
    pc.addTrack(track)

    await pc.setRemoteDescription(offer_sdp)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    return web.json_response({
        "sdp": pc.localDescription.sdp,
        "type": pc.localDescription.type
    })


async def websocket_handler(request):
    """WebSocket: 接收浏览器端的 trigger 数据，控制 BrainCo 手"""
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    hand_ctrl = request.app["hand_controller"]
    print("[ws] 客户端已连接")

    async for msg in ws:
        if msg.type == web.WSMsgType.TEXT:
            try:
                data = json.loads(msg.data)
                left_trigger = data.get("left_trigger")
                right_trigger = data.get("right_trigger")
                hand_ctrl.update(left_trigger, right_trigger)
            except Exception as e:
                print(f"[ws] 解析错误: {e}", file=sys.stderr)
        elif msg.type == web.WSMsgType.ERROR:
            print(f"[ws] 错误: {ws.exception()}")

    print("[ws] 客户端断开")
    return ws


async def on_shutdown(app):
    coros = [pc.close() for pc in pcs]
    await asyncio.gather(*coros)
    pcs.clear()
    app["camera"].stop()


# ========== 主入口 ==========

def main():
    parser = argparse.ArgumentParser(
        description="WebRTC 摄像头串流 + BrainCo 手控制",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # 视频参数
    parser.add_argument("--usb-camera", type=str, default="/dev/video0")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=960)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--encoder", type=str, default=None)
    parser.add_argument("--bitrate", type=int, default=2_000_000)

    # BrainCo 手控制参数 (与 brainco_vr_trigger_listener.py 一致)
    parser.add_argument("--network-interface", default="eth0",
                        help="DDS network interface for brainco_hand_ctrl")
    parser.add_argument("--threshold", type=float, default=0.5,
                        help="Trigger threshold for hand close/open")
    parser.add_argument("--startup-pose", choices=("initial", "closed"), default="initial")
    parser.add_argument("--left-initial", type=parse_vector, default=parse_vector("0,0,0,0,0,0"))
    parser.add_argument("--right-initial", type=parse_vector, default=parse_vector("0,0,0,0,0,0"))
    parser.add_argument("--left-closed", type=parse_vector, default=parse_vector("1,1,1,1,1,1"))
    parser.add_argument("--right-closed", type=parse_vector, default=parse_vector("1,1,1,1,1,1"))
    parser.add_argument("--left-close-mask", type=parse_mask, default=parse_mask("1,1,1,1,1,1"))
    parser.add_argument("--right-close-mask", type=parse_mask, default=parse_mask("1,1,1,1,1,1"))
    parser.add_argument("--brainco-ctrl", type=Path,
                        default=Path(__file__).resolve().parent / "bin" / "brainco_hand_ctrl")

    args = parser.parse_args()

    # 编码器
    encoder = detect_best_encoder(args.encoder)
    patch_aiortc_encoder(encoder, args.bitrate)

    # 摄像头
    camera = CameraCapture(args.usb_camera, args.width, args.height, args.fps)
    camera.start()
    print(f"[camera] 启动采集: {args.usb_camera} {args.width}x{args.height} @ {args.fps}fps")

    for _ in range(20):
        f, _, _ = camera.get_frame()
        if f is not None:
            break
        time.sleep(0.05)

    # 手控制
    hand_ctrl = HandController(args)
    hand_ctrl.initialize()

    # HTTPS 支持 (可选)
    import ssl
    ssl_ctx = None
    script_dir = os.path.dirname(os.path.abspath(__file__))
    cert_path = os.path.join(script_dir, "cert.pem")
    key_path = os.path.join(script_dir, "key.pem")
    if os.path.exists(cert_path) and os.path.exists(key_path):
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.load_cert_chain(cert_path, key_path)
        proto = "https"
    else:
        proto = "http"

    app = web.Application()
    app["camera"] = camera
    app["hand_controller"] = hand_ctrl
    app.on_shutdown.append(on_shutdown)
    app.router.add_get("/", index)
    app.router.add_post("/offer", offer)
    app.router.add_get("/ws", websocket_handler)

    print(f"[server] {proto}://{args.host}:{args.port}")
    web.run_app(app, host=args.host, port=args.port, ssl_context=ssl_ctx, print=None)


if __name__ == "__main__":
    main()
