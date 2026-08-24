"""
WebRTC 低延迟摄像头串流服务。

在边缘设备上运行，通过局域网在 VR 浏览器中访问实时视频流。

用法:
  python stream.py --usb-camera /dev/video0
  python stream.py --usb-camera /dev/video0 --width 1280 --height 720
  python stream.py --usb-camera /dev/video0 --port 8080
"""

import argparse
import asyncio
import json
import time
import threading
import fractions

import cv2
import numpy as np
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from av import VideoFrame

# ========== 摄像头采集 ==========

class CameraCapture:
    """后台线程持续采集摄像头帧"""

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

    def start(self):
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def get_frame(self):
        with self.lock:
            return self.frame, self.frame_time

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

            with self.lock:
                self.frame = frame
                self.frame_time = time.time()

            frame_count += 1
            elapsed = time.time() - fps_timer
            if elapsed >= 2.0:
                self.actual_fps = frame_count / elapsed
                frame_count = 0
                fps_timer = time.time()

        cap.release()
        print("[camera] 已停止")


# ========== WebRTC 视频轨道 ==========

class CameraStreamTrack(VideoStreamTrack):
    """从 CameraCapture 读取帧，推送到 WebRTC"""

    kind = "video"

    def __init__(self, camera: CameraCapture):
        super().__init__()
        self.camera = camera

    async def recv(self):
        pts, time_base = await self.next_timestamp()

        frame, _ = self.camera.get_frame()
        if frame is None:
            frame = np.zeros((self.camera.height, self.camera.width, 3), dtype=np.uint8)

        # 直接用 bgr24 格式，省掉一次颜色转换
        video_frame = VideoFrame.from_ndarray(frame, format="bgr24")
        video_frame.pts = pts
        video_frame.time_base = time_base

        return video_frame


# ========== Web 前端 ==========

INDEX_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Camera Stream</title>
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

// FPS 计算
function onFrame() {
  frameCount++;
  const now = performance.now();
  const elapsed = now - lastFpsTime;
  if (elapsed >= 1000) {
    fps = (frameCount / elapsed * 1000).toFixed(1);
    frameCount = 0;
    lastFpsTime = now;
  }
  requestAnimationFrame(onFrame);
}

async function start() {
  const pc = new RTCPeerConnection({
    sdpSemantics: 'unified-plan',
    iceServers: []
  });

  pc.ontrack = (evt) => {
    video.srcObject = evt.streams[0];
    requestAnimationFrame(onFrame);
  };

  // 定时获取延迟统计
  setInterval(async () => {
    if (!pc.getReceivers) return;
    const receivers = pc.getReceivers();
    let rtt = '-';
    for (const r of receivers) {
      if (r.track && r.track.kind === 'video') {
        try {
          const s = await r.getStats();
          s.forEach(report => {
            if (report.type === 'candidate-pair' && report.currentRoundTripTime !== undefined) {
              rtt = (report.currentRoundTripTime * 1000).toFixed(0);
            }
          });
        } catch(e) {}
      }
    }
    // 也从 pc 级别获取
    try {
      const allStats = await pc.getStats();
      allStats.forEach(report => {
        if (report.type === 'candidate-pair' && report.state === 'succeeded' && report.currentRoundTripTime !== undefined) {
          rtt = (report.currentRoundTripTime * 1000).toFixed(0);
        }
      });
    } catch(e) {}
    stats.textContent = `FPS: ${fps}  RTT: ${rtt}ms`;
  }, 500);

  pc.addTransceiver('video', { direction: 'recvonly' });

  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);

  // 不等 ICE 收集完成，直接发 offer（局域网不需要 STUN/TURN）
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
        if pc.connectionState == "failed" or pc.connectionState == "closed":
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


async def on_shutdown(app):
    coros = [pc.close() for pc in pcs]
    await asyncio.gather(*coros)
    pcs.clear()
    app["camera"].stop()


# ========== 主入口 ==========

def main():
    parser = argparse.ArgumentParser(description="WebRTC 摄像头低延迟串流")
    parser.add_argument("--usb-camera", type=str, default="/dev/video0",
                        help="USB 摄像头设备路径 (默认 /dev/video0)")
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    camera = CameraCapture(args.usb_camera, args.width, args.height, args.fps)
    camera.start()
    print(f"[camera] 启动采集: {args.usb_camera} {args.width}x{args.height} @ {args.fps}fps")

    # 等摄像头首帧就绪
    import time
    for _ in range(20):
        f, _ = camera.get_frame()
        if f is not None:
            break
        time.sleep(0.05)

    app = web.Application()
    app["camera"] = camera
    app.on_shutdown.append(on_shutdown)
    app.router.add_get("/", index)
    app.router.add_post("/offer", offer)

    print(f"[server] http://{args.host}:{args.port}")
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
