"""
WebRTC 低延迟摄像头串流 — VR WebXR 版。

在 VR 头显浏览器中打开，点击 "Enter VR" 进入沉浸模式，
视频画面固定在视野前方，跟随头部移动。

用法:
  python stream_vr.py --usb-camera /dev/video0 --width 1280 --height 960
"""

import argparse
import asyncio
import json
import time
import threading
import fractions
import ssl
import os

import cv2
import numpy as np
import av
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from av import VideoFrame


# ========== 复用 stream_rk3588 的编码器检测与 patch ==========

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


# ========== WebXR 前端 ==========

INDEX_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Camera VR</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #000; overflow: hidden; width: 100vw; height: 100vh; }
  canvas { display: block; width: 100vw; height: 100vh; }
  #enterVR {
    position: fixed; bottom: 30px; left: 50%; transform: translateX(-50%);
    padding: 14px 32px; font-size: 18px; font-weight: bold;
    background: #2196F3; color: #fff; border: none; border-radius: 8px;
    cursor: pointer; z-index: 100;
  }
  #enterVR:hover { background: #1976D2; }
  #enterVR:disabled { background: #666; cursor: not-allowed; }
  #stats {
    position: fixed; top: 10px; left: 10px;
    color: #0f0; font: 14px monospace;
    background: rgba(0,0,0,0.6);
    padding: 6px 10px; border-radius: 4px;
    z-index: 10; pointer-events: none;
  }
  video { display: none; }
</style>
</head>
<body>
<div id="stats">connecting...</div>
<button id="enterVR" disabled>Enter VR</button>
<canvas id="gl"></canvas>
<video id="video" autoplay playsinline muted></video>
<script>
const canvas = document.getElementById('gl');
const video = document.getElementById('video');
const statsEl = document.getElementById('stats');
const enterBtn = document.getElementById('enterVR');

// WebGL1, alpha:true 让透视背景透过来
const gl = canvas.getContext('webgl', { xrCompatible: true, alpha: true });

let xrSession = null;
let xrRefSpace = null;
let videoTexture = null;
let shaderProgram = null;
let posBuf = null, uvBuf = null, idxBuf = null;
let numIndices = 0;
let videoReady = false;
let fps = 0, frameCount = 0, lastFpsTime = performance.now();

// ===== WebGL1 shaders =====
const VS = `
attribute vec3 aPos;
attribute vec2 aUV;
uniform mat4 uProj;
uniform mat4 uView;
varying vec2 vUV;
void main() {
  // aPos 是以原点为中心的球面坐标
  // 在 viewer 空间中，原点就是头部，所以球面始终包裹头部
  gl_Position = uProj * uView * vec4(aPos, 1.0);
  vUV = aUV;
}`;

const FS = `
precision mediump float;
varying vec2 vUV;
uniform sampler2D uTex;
void main() {
  gl_FragColor = texture2D(uTex, vUV);
}`;

function compileShader(type, src) {
  const s = gl.createShader(type);
  gl.shaderSource(s, src);
  gl.compileShader(s);
  if (!gl.getShaderParameter(s, gl.COMPILE_STATUS))
    console.error('Shader error:', gl.getShaderInfoLog(s));
  return s;
}

// 生成球面片段网格
// hFov: 水平视场角(弧度), vFov: 垂直视场角(弧度), r: 半径
function buildSphereMesh(hFov, vFov, r, segsH, segsV) {
  const positions = [];
  const uvs = [];
  const indices = [];

  for (let j = 0; j <= segsV; j++) {
    // 垂直角度: 从上到下
    const v = j / segsV;
    const phi = (0.5 - v) * vFov; // 上正下负

    for (let i = 0; i <= segsH; i++) {
      // 水平角度: 从左到右 (面向 -Z 方向)
      const u = i / segsH;
      const theta = (u - 0.5) * hFov;

      // 球面坐标 -> 笛卡尔坐标 (面向 -Z)
      const x = r * Math.cos(phi) * Math.sin(theta);
      const y = r * Math.sin(phi);
      const z = -r * Math.cos(phi) * Math.cos(theta);

      positions.push(x, y, z);
      // UV: 水平翻转修正镜像
      uvs.push(1.0 - u, v);
    }
  }

  for (let j = 0; j < segsV; j++) {
    for (let i = 0; i < segsH; i++) {
      const a = j * (segsH + 1) + i;
      const b = a + 1;
      const c = a + (segsH + 1);
      const d = c + 1;
      indices.push(a, b, c);
      indices.push(b, d, c);
    }
  }

  return {
    positions: new Float32Array(positions),
    uvs: new Float32Array(uvs),
    indices: new Uint16Array(indices)
  };
}

function initGL() {
  const vs = compileShader(gl.VERTEX_SHADER, VS);
  const fs = compileShader(gl.FRAGMENT_SHADER, FS);
  shaderProgram = gl.createProgram();
  gl.attachShader(shaderProgram, vs);
  gl.attachShader(shaderProgram, fs);
  gl.bindAttribLocation(shaderProgram, 0, 'aPos');
  gl.bindAttribLocation(shaderProgram, 1, 'aUV');
  gl.linkProgram(shaderProgram);
  if (!gl.getProgramParameter(shaderProgram, gl.LINK_STATUS))
    console.error('Program error:', gl.getProgramInfoLog(shaderProgram));

  // 球面参数:
  // 半径 3m, 水平 70°, 垂直 50° (适中大小，正前方居中)
  const hFovDeg = 70, vFovDeg = 50;
  const mesh = buildSphereMesh(
    hFovDeg * Math.PI / 180,
    vFovDeg * Math.PI / 180,
    3.0,  // 半径
    32,   // 水平细分
    24    // 垂直细分
  );
  numIndices = mesh.indices.length;

  posBuf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, posBuf);
  gl.bufferData(gl.ARRAY_BUFFER, mesh.positions, gl.STATIC_DRAW);

  uvBuf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, uvBuf);
  gl.bufferData(gl.ARRAY_BUFFER, mesh.uvs, gl.STATIC_DRAW);

  idxBuf = gl.createBuffer();
  gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, idxBuf);
  gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, mesh.indices, gl.STATIC_DRAW);

  videoTexture = gl.createTexture();
  gl.bindTexture(gl.TEXTURE_2D, videoTexture);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 1, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE,
    new Uint8Array([0,0,0,255]));
}

function updateTexture() {
  if (!videoReady || video.readyState < 2) return;
  try {
    gl.bindTexture(gl.TEXTURE_2D, videoTexture);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, video);
  } catch(e) {}
}

// ===== 矩阵运算 =====
function mat4Mul(a, b) {
  const r = new Float32Array(16);
  for (let i = 0; i < 4; i++)
    for (let j = 0; j < 4; j++)
      r[i*4+j] = a[i*4]*b[j] + a[i*4+1]*b[4+j] + a[i*4+2]*b[8+j] + a[i*4+3]*b[12+j];
  return r;
}

function mat4Id() {
  const m = new Float32Array(16);
  m[0]=m[5]=m[10]=m[15]=1;
  return m;
}

function mat4Persp(fov, aspect, near, far) {
  const f = 1/Math.tan(fov/2), nf = 1/(near-far);
  const m = new Float32Array(16);
  m[0]=f/aspect; m[5]=f; m[10]=(far+near)*nf; m[11]=-1; m[14]=2*far*near*nf;
  return m;
}

// ===== 渲染 =====
function drawSphere(proj, view) {
  updateTexture();
  gl.useProgram(shaderProgram);

  gl.uniformMatrix4fv(gl.getUniformLocation(shaderProgram, 'uProj'), false, proj);
  gl.uniformMatrix4fv(gl.getUniformLocation(shaderProgram, 'uView'), false, view);

  gl.activeTexture(gl.TEXTURE0);
  gl.bindTexture(gl.TEXTURE_2D, videoTexture);
  gl.uniform1i(gl.getUniformLocation(shaderProgram, 'uTex'), 0);

  gl.bindBuffer(gl.ARRAY_BUFFER, posBuf);
  gl.enableVertexAttribArray(0);
  gl.vertexAttribPointer(0, 3, gl.FLOAT, false, 0, 0);

  gl.bindBuffer(gl.ARRAY_BUFFER, uvBuf);
  gl.enableVertexAttribArray(1);
  gl.vertexAttribPointer(1, 2, gl.FLOAT, false, 0, 0);

  gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, idxBuf);
  gl.drawElements(gl.TRIANGLES, numIndices, gl.UNSIGNED_SHORT, 0);
}

// 非 VR 桌面渲染
function renderFlat() {
  if (xrSession) return;
  canvas.width = canvas.clientWidth;
  canvas.height = canvas.clientHeight;
  gl.viewport(0, 0, canvas.width, canvas.height);
  gl.clearColor(0,0,0,1);
  gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
  gl.enable(gl.DEPTH_TEST);
  drawSphere(mat4Persp(Math.PI/3, canvas.width/canvas.height, 0.1, 100), mat4Id());
  countFps();
  requestAnimationFrame(renderFlat);
}

function countFps() {
  frameCount++;
  const now = performance.now();
  if (now - lastFpsTime >= 1000) {
    fps = (frameCount / (now - lastFpsTime) * 1000).toFixed(1);
    frameCount = 0;
    lastFpsTime = now;
  }
}

// ===== WebXR =====
function onXRFrame(t, frame) {
  const session = frame.session;
  session.requestAnimationFrame(onXRFrame);

  const glLayer = session.renderState.baseLayer;
  gl.bindFramebuffer(gl.FRAMEBUFFER, glLayer.framebuffer);
  gl.clearColor(0,0,0,0);
  gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
  gl.enable(gl.DEPTH_TEST);

  const pose = frame.getViewerPose(xrRefSpace);
  if (!pose) return;

  for (const view of pose.views) {
    const vp = glLayer.getViewport(view);
    gl.viewport(vp.x, vp.y, vp.width, vp.height);
    // viewer 空间: view.transform.inverse 只有眼间距偏移
    // 球面以原点(头部)为中心，所以始终包裹头部
    drawSphere(view.projectionMatrix, view.transform.inverse.matrix);
  }
  countFps();
}

async function enterVR() {
  if (!navigator.xr) { alert('WebXR not supported'); return; }

  try { await video.play(); } catch(e) {}
  await gl.makeXRCompatible();

  let mode = 'immersive-ar';
  let supported = await navigator.xr.isSessionSupported('immersive-ar');
  if (!supported) {
    mode = 'immersive-vr';
    supported = await navigator.xr.isSessionSupported('immersive-vr');
  }
  if (!supported) { alert('VR/AR not supported'); return; }

  xrSession = await navigator.xr.requestSession(mode, {
    optionalFeatures: ['local', 'viewer', 'hand-tracking']
  });

  const glLayer = new XRWebGLLayer(xrSession, gl, { alpha: true });
  await xrSession.updateRenderState({ baseLayer: glLayer });

  // viewer 空间: 球面始终以头部为中心，走动/旋转都不会丢失画面
  xrRefSpace = await xrSession.requestReferenceSpace('viewer');

  xrSession.requestAnimationFrame(onXRFrame);
  enterBtn.style.display = 'none';
  statsEl.style.display = 'none';

  xrSession.addEventListener('end', () => {
    xrSession = null;
    enterBtn.style.display = '';
    statsEl.style.display = '';
    requestAnimationFrame(renderFlat);
  });
}

// ===== WebRTC =====
async function startWebRTC() {
  const pc = new RTCPeerConnection({ sdpSemantics: 'unified-plan', iceServers: [] });

  pc.ontrack = (evt) => {
    video.srcObject = evt.streams[0];
    video.onloadeddata = () => {
      videoReady = true;
      video.play().catch(()=>{});
    };
  };

  setInterval(async () => {
    let rtt = '-';
    try {
      const s = await pc.getStats();
      s.forEach(r => {
        if (r.type === 'candidate-pair' && r.state === 'succeeded'
            && r.currentRoundTripTime !== undefined)
          rtt = (r.currentRoundTripTime * 1000).toFixed(0);
      });
    } catch(e) {}
    if (!xrSession) statsEl.textContent = 'FPS: ' + fps + '  RTT: ' + rtt + 'ms';
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

// ===== 启动 =====
initGL();
startWebRTC().catch(e => { statsEl.textContent = 'Error: ' + e.message; });
requestAnimationFrame(renderFlat);

if (navigator.xr) {
  Promise.all([
    navigator.xr.isSessionSupported('immersive-ar'),
    navigator.xr.isSessionSupported('immersive-vr')
  ]).then(([ar, vr]) => {
    if (ar) {
      enterBtn.disabled = false;
      enterBtn.textContent = 'Enter AR (Passthrough)';
    } else if (vr) {
      enterBtn.disabled = false;
      enterBtn.textContent = 'Enter VR';
    } else {
      enterBtn.textContent = 'VR/AR not supported';
    }
  });
} else {
  enterBtn.textContent = 'WebXR not available';
}
enterBtn.onclick = enterVR;
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
        description="WebRTC 摄像头低延迟串流 (VR WebXR 版)")
    parser.add_argument("--usb-camera", type=str, default="/dev/video0")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=960)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--encoder", type=str, default=None)
    parser.add_argument("--bitrate", type=int, default=2_000_000)
    args = parser.parse_args()

    encoder = detect_best_encoder(args.encoder)
    patch_aiortc_encoder(encoder, args.bitrate)

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

    # HTTPS (WebXR 需要安全上下文)
    ssl_ctx = None
    script_dir = os.path.dirname(os.path.abspath(__file__))
    cert_path = os.path.join(script_dir, "cert.pem")
    key_path = os.path.join(script_dir, "key.pem")
    if os.path.exists(cert_path) and os.path.exists(key_path):
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.load_cert_chain(cert_path, key_path)
        print(f"[server] https://{args.host}:{args.port}  (自签名证书)")
    else:
        print(f"[server] http://{args.host}:{args.port}")
        print(f"[server] 警告: 未找到 cert.pem/key.pem，WebXR 在非 localhost 下不可用")

    web.run_app(app, host=args.host, port=args.port, ssl_context=ssl_ctx, print=None)


if __name__ == "__main__":
    main()
