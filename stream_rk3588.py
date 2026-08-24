"""
WebRTC 低延迟摄像头串流 — RK3588 硬件加速版。

相比 stream.py 的优化:
  1. 自动检测并使用硬件 H264 编码器 (h264_rkmpp > h264_v4l2m2m > libx264)
  2. 编码参数针对低延迟调优 (最小 GOP, 无 B 帧, CBR)
  3. 帧pipeline优化 (减少拷贝, 跳过过期帧)

用法:
  python stream_rk3588.py --usb-camera /dev/video0
  python stream_rk3588.py --usb-camera /dev/video0 --width 1280 --height 720
  python stream_rk3588.py --usb-camera /dev/video0 --encoder h264_rkmpp
  python stream_rk3588.py --usb-camera /dev/video0 --bitrate 2000000
"""

import argparse
import asyncio
import json
import time
import threading
import fractions

import cv2
import numpy as np
import av
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from av import VideoFrame


# ========== 硬件编码器检测与 Monkey-Patch ==========

HW_ENCODERS = ["h264_rkmpp", "h264_v4l2m2m"]
SW_ENCODER = "libx264"


def detect_best_encoder(preferred=None):
    """检测可用的最佳 H264 编码器"""
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
    """Monkey-patch aiortc 的 H264 编码器"""
    from aiortc.codecs.h264 import H264Encoder

    original_encode_frame = H264Encoder._encode_frame

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

            # 最小 GOP: 每秒一个关键帧
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


# ========== 摄像头采集 (低延迟优化) ==========

class CameraCapture:
    """后台线程持续采集，保留最新帧"""

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

        # 设置顺序很重要: 先 FOURCC，再分辨率，再帧率
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


# ========== WebRTC 视频轨道 (跳帧优化) ==========

class CameraStreamTrack(VideoStreamTrack):
    """从 CameraCapture 读取最新帧，跳过过期帧"""

    kind = "video"

    def __init__(self, camera: CameraCapture):
        super().__init__()
        self.camera = camera
        self._last_seq = -1

    async def recv(self):
        pts, time_base = await self.next_timestamp()

        frame, frame_time, seq = self.camera.get_frame()
        if frame is None:
            frame = np.zeros((self.camera.height, self.camera.width, 3), dtype=np.uint8)

        self._last_seq = seq

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
<title>Camera Stream (RK3588)</title>
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

  setInterval(async () => {
    let rtt = '-';
    try {
      const allStats = await pc.getStats();
      allStats.forEach(report => {
        if (report.type === 'candidate-pair' && report.state === 'succeeded'
            && report.currentRoundTripTime !== undefined) {
          rtt = (report.currentRoundTripTime * 1000).toFixed(0);
        }
      });
    } catch(e) {}
    stats.textContent = 'FPS: ' + fps + '  RTT: ' + rtt + 'ms';
  }, 500);

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


async def on_shutdown(app):
    coros = [pc.close() for pc in pcs]
    await asyncio.gather(*coros)
    pcs.clear()
    app["camera"].stop()


# ========== 主入口 ==========

def main():
    parser = argparse.ArgumentParser(
        description="WebRTC 摄像头低延迟串流 (RK3588 硬件加速版)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python stream_rk3588.py --usb-camera /dev/video0
  python stream_rk3588.py --usb-camera /dev/video0 --encoder h264_rkmpp
  python stream_rk3588.py --usb-camera /dev/video0 --width 1280 --height 720 --bitrate 3000000
        """
    )
    parser.add_argument("--usb-camera", type=str, default="/dev/video0",
                        help="摄像头设备路径 (默认 /dev/video0)")
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--host", type=str, default="192.168.50.97")
    parser.add_argument("--port", type=int, default=9080)
    parser.add_argument("--encoder", type=str, default=None,
                        help="指定编码器 (h264_rkmpp/h264_v4l2m2m/libx264)，默认自动检测")
    parser.add_argument("--bitrate", type=int, default=2_000_000,
                        help="目标码率 (默认 2Mbps)")
    args = parser.parse_args()

    # 检测并 patch 编码器
    encoder = detect_best_encoder(args.encoder)
    patch_aiortc_encoder(encoder, args.bitrate)

    # 启动摄像头
    camera = CameraCapture(args.usb_camera, args.width, args.height, args.fps)
    camera.start()
    print(f"[camera] 启动采集: {args.usb_camera} {args.width}x{args.height} @ {args.fps}fps")

    for _ in range(20):
        f, _, _ = camera.get_frame()
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
