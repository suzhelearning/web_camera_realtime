#!/usr/bin/env python3
"""Pico 图像桥: 订阅 3588 数据发送端的流, 处理后连接 Pico XRoboToolkit 推流。

角色拓扑 (pc 分支):
  [3588 数据发送端]  stream_to_pico.py: 监听 13579, 收到 OPEN_CAMERA 后推流
        ↑ 订阅段: 本桥作为 XRoboToolkit 客户端连接 3588, 请求单眼 1024x768@30
  [本桥]  1. 订阅段: 生成 [4BE len][H.264] 帧流, 写 FIFO 由 PyAV 解码
         2. 处理段: mono-to-sbs 拼接 (单眼 1024x768 → 2048x768) 或 mono 直通,
            按客户端请求的 宽/高/帧率 用 libx264 重编码 (默认 4Mbps)
         3. 连接段: 监听 13579 充当 XRoboToolkit 服务端, Pico 连入发
            OPEN_CAMERA 后, 作为 TCP Client 连接 Pico 的 MediaDecoder 端口推流
  [Pico 设备]      XRoboToolkit: Listen 输入本桥 IP, 收流显示

用法:
  python stream_bridge_pico.py                       # 默认连 192.168.50.83:13579
  python stream_bridge_pico.py --source-host 192.168.50.21 --preview
"""
import argparse
import fcntl
import fractions
import os
import queue
import socket
import struct
import threading
import time

import av
import cv2
import numpy as np


# ========== 协议: 与 live_view.py / stream_to_pico.py 保持一致 ==========


def build_open_camera(port, ip, width, height, fps, bitrate, camera):
    """构造 OPEN_CAMERA 请求 (XRoboToolkit 客户端 -> 流服务器)."""
    payload = b"\xca\xfe" + bytes([1])
    payload += struct.pack("<iiiii", width, height, fps, bitrate, 0)
    payload += struct.pack("<i", 0)  # render_mode
    payload += struct.pack("<i", port)
    cam = camera.encode()
    if len(cam) > 255:
        cam = cam[:255]
    payload += bytes([len(cam)]) + cam
    ipb = ip.encode()
    payload += bytes([len(ipb)]) + ipb
    cmd = b"OPEN_CAMERA"
    msg = struct.pack(">i", 4 + 4 + len(cmd) + 4 + len(payload))
    msg += struct.pack("<i", len(cmd)) + cmd
    msg += struct.pack("<i", len(payload)) + payload
    return msg


def parse_network_protocol(buffer):
    """从字节缓冲解析出一个完整命令: [total_len BE][cmd_len LE][cmd]...

    返回 (command, payload, consumed_bytes); 数据不足返回 (None, None, 0).
    """
    if len(buffer) < 12:
        return None, None, 0
    total_len = struct.unpack_from(">i", buffer, 0)[0]
    cmd_len = struct.unpack_from("<i", buffer, 4)[0]
    if cmd_len < 0:
        return None, None, 0
    if len(buffer) < 8 + cmd_len + 4:
        return None, None, 0
    command = buffer[8:8 + cmd_len].decode("utf-8", "replace")
    data_len = struct.unpack_from("<i", buffer, 8 + cmd_len)[0]
    payload = buffer[12 + cmd_len:12 + cmd_len + data_len]
    consumed = 4 + 4 + cmd_len + 4 + data_len
    return command, payload, consumed


def parse_camera_request(data):
    """解析 CameraRequestData 二进制格式 (与 stream_to_pico.py 相同)."""
    if len(data) < 10:
        return None
    if data[0] != 0xCA or data[1] != 0xFE:
        return None
    offset = 2
    offset += 1  # version
    width = struct.unpack_from("<i", data, offset)[0]; offset += 4
    height = struct.unpack_from("<i", data, offset)[0]; offset += 4
    fps = struct.unpack_from("<i", data, offset)[0]; offset += 4
    bitrate = struct.unpack_from("<i", data, offset)[0]; offset += 4
    offset += 4  # enable_mv_hevc
    offset += 4  # render_mode
    port = struct.unpack_from("<i", data, offset)[0]; offset += 4
    cam_len = data[offset]; offset += 1
    camera = data[offset:offset + cam_len].decode("utf-8", "replace") \
        if cam_len > 0 else ""
    offset += cam_len
    ip_len = data[offset]; offset += 1
    ip = data[offset:offset + ip_len].decode("utf-8", "replace") \
        if ip_len > 0 else ""
    return {"width": width, "height": height, "fps": fps,
            "bitrate": bitrate, "port": port, "ip": ip, "camera": camera}


def recv_exact(conn, n):
    data = b""
    while len(data) < n:
        chunk = conn.recv(n - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def parse_bitrate(text):
    text = text.upper().strip()
    if text.endswith("M"):
        return int(float(text[:-1]) * 1_000_000)
    if text.endswith("K"):
        return int(float(text[:-1]) * 1_000)
    return int(text)


# ========== 订阅段: 连接 3588 数据发送端, 接收 [4BE len][H.264] ==========


class Subscriber:
    """作为 XRoboToolkit 客户端连接 source 服务器并收流写 FIFO."""

    def __init__(self, args, ip):
        self.args = args
        self.ip = ip  # 本机回连 IP (写进 OPEN_CAMERA)
        self.stop = threading.Event()

    def run(self):
        args = self.args
        while not self.stop.is_set():
            try:
                self._once()
            except Exception as exc:  # 断流/网络抖动后重连
                print(f"[sub] 连接中断: {exc}, 4s 后重连", flush=True)
                self.stop.wait(4)

    def _once(self):
        args = self.args
        # 1. 监听本机回连端口
        ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        ls.bind(("0.0.0.0", args.subscribe_port))
        ls.listen(1)
        ls.settimeout(1.0)
        try:
            # 2. 发 OPEN_CAMERA, 请求单眼画面
            s = socket.create_connection(
                (args.source_host, args.source_port), timeout=5)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.sendall(build_open_camera(
                args.subscribe_port, self.ip,
                args.subscribe_width, args.subscribe_height,
                args.subscribe_fps, parse_bitrate(args.bitrate),
                args.camera))
            print(f"[sub] OPEN_CAMERA {args.source_host}:{args.source_port} "
                  f"请求 {args.subscribe_width}x{args.subscribe_height}"
                  f"@{args.subscribe_fps}fps, 本机回连 {self.ip}:"
                  f"{args.subscribe_port}", flush=True)
            s.close()

            # 3. 等待流服务器回连推流 (超时则继续等, 不视为断连)
            conn = None
            while not self.stop.is_set():
                try:
                    conn, addr = ls.accept()
                    break
                except socket.timeout:
                    continue
            if conn is None:
                return
            print(f"[sub] 收到推流连接: {addr}", flush=True)
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # 首次使用才创建 FIFO (重连时复用, 解码端按 EOF 循环重开)
            if not os.path.exists(args.fifo):
                os.mkfifo(args.fifo)
            writer = os.fdopen(os.open(args.fifo, os.O_WRONLY), "wb", 0)
            try:
                fcntl.fcntl(writer.fileno(), fcntl.F_SETPIPE_SZ, 16384)
            except OSError as exc:
                print(f"[warn] 设置 FIFO 缓冲失败: {exc}", flush=True)
            try:
                n = 0
                while not self.stop.is_set():
                    hdr = recv_exact(conn, 4)
                    if hdr is None:
                        break
                    ln = struct.unpack(">I", hdr)[0]
                    pkt = recv_exact(conn, ln)
                    if pkt is None:
                        break
                    writer.write(pkt)
                    n += 1
                    if n % 300 == 0:
                        print(f"[sub] 已收 {n} 帧", flush=True)
            finally:
                writer.close()
                conn.close()
                s.close()
                print(f"[sub] 收流断开, 共 {n} 帧", flush=True)
        finally:
            ls.close()


# ========== 解码段: FIFO -> H.264 帧队列 ==========


class Decoder:
    """PyAV 解码 FIFO 中 H.264, 帧进有界队列, 队列满丢最旧 (保实时)."""

    def __init__(self, args):
        self.args = args
        self.stop = threading.Event()
        self.frames = queue.Queue(maxsize=4)

    def run(self):
        args = self.args
        while not self.stop.is_set():
            while not self.stop.is_set() and not os.path.exists(args.fifo):
                self.stop.wait(0.2)
            if self.stop.is_set():
                break
            try:
                container = av.open(args.fifo, "r", format="h264")
            except Exception as exc:
                print(f"[proc] 打开 FIFO 失败: {exc}", flush=True)
                self.stop.wait(1)
                continue
            try:
                for frame in container.decode(video=0):
                    if self.stop.is_set():
                        break
                    img = frame.to_ndarray(format="bgr24")
                    if self.frames.full():
                        try:
                            self.frames.get_nowait()  # 丢最旧帧追实时
                        except queue.Empty:
                            pass
                    self.frames.put(img)
            except (av.error.EOFError, StopIteration):
                pass  # 流断开: 回到外层等待订阅端重连
            except Exception as exc:
                print(f"[proc] 解码结束: {exc}", flush=True)
            finally:
                container.close()
            self.stop.wait(1)


# ========== 连接段: 监听 13579 等 Pico XRoboToolkit 连入 ==========


class Bridge:
    """Pico 服务端 + 处理重编码 + 推流."""

    def __init__(self, args, decoder):
        self.args = args
        self.decoder = decoder
        self.stop = threading.Event()
        self.request = None          # Pico 请求 (width/height/fps/ip/port)
        self.request_lock = threading.Lock()
        self.push_sock = None        # 到 Pico 的推流 socket
        self.push_lock = threading.Lock()
        self.latest = None           # 最近处理帧 (--preview 用)

    # ---- Pico 服务端 ----
    def serve(self):
        args = self.args
        ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        ls.bind(("0.0.0.0", args.server_port))
        ls.listen(2)
        print(f"[pico] 监听 {args.server_port}, 等待 Pico XRoboToolkit 连接...",
              flush=True)
        while not self.stop.is_set():
            try:
                conn, addr = ls.accept()
            except OSError:
                break
            print(f"[pico] Pico 已连接: {addr}", flush=True)
            self._handle_client(conn, addr[0])
            print(f"[pico] 连接断开: {addr}", flush=True)
        ls.close()

    def _handle_client(self, conn, addr_ip):
        conn.settimeout(1.0)
        buf = b""
        try:
            while not self.stop.is_set():
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buf += chunk
                while True:
                    cmd, payload, consumed = parse_network_protocol(buf)
                    if cmd is None:
                        break
                    buf = buf[consumed:]
                    self._handle_command(conn, cmd, payload, addr_ip)
        except (ConnectionResetError, OSError):
            pass
        finally:
            with self.request_lock:
                self.request = None
            with self.push_lock:
                self.push_sock = None   # 客户端断开, 推流端下次重建
            conn.close()

    def _handle_command(self, conn, cmd, payload, addr_ip):
        if cmd == "OPEN_CAMERA" and payload:
            req = parse_camera_request(payload)
            if not req:
                print(f"[pico] 无法解析 OPEN_CAMERA payload", flush=True)
                return
            req.setdefault("ip", addr_ip)
            if not req["ip"]:
                req["ip"] = addr_ip
            print(f"[pico] OPEN_CAMERA: {req['width']}x{req['height']}"
                  f"@{req['fps']}fps -> {req['ip']}:{req['port']}", flush=True)
            with self.request_lock:
                self.request = req
            # (重)建立到 Pico 的推流连接
            self._connect_push(req)
        elif cmd == "CLOSE_CAMERA":
            print(f"[pico] CLOSE_CAMERA", flush=True)
            with self.request_lock:
                self.request = None
            with self.push_lock:
                self.push_sock = None

    def _connect_push(self, req):
        try:
            s = socket.create_connection((req["ip"], req["port"]), timeout=5)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as exc:
            print(f"[pico] 连 Pico {req['ip']}:{req['port']} 失败: {exc}",
                  flush=True)
            return
        with self.push_lock:
            self.push_sock = s
        print(f"[pico] 已连接推流: {req['ip']}:{req['port']}", flush=True)

    # ---- 处理 + 推流 ----
    def process(self):
        args = self.args
        codec = None
        codec_key = None
        pts = 0
        pushed = 0
        t0 = time.time()

        def make_codec(req):
            c = av.CodecContext.create(args.encoder, "w")
            c.width = req["width"]
            c.height = req["height"]
            c.pix_fmt = "yuv420p"
            c.bit_rate = parse_bitrate(args.bitrate)
            c.framerate = fractions.Fraction(max(1, req["fps"]), 1)
            c.time_base = fractions.Fraction(1, max(1, req["fps"]))
            c.gop_size = max(1, req["fps"])
            c.max_b_frames = 0
            if args.encoder == "libx264":
                c.options = {
                    "preset": "ultrafast",
                    "tune": "zerolatency",
                    "profile": "baseline",
                    "repeat-headers": "1",
                    "threads": "8",
                }
            c.open()
            return c

        while not self.stop.is_set():
            try:
                bgr = self.decoder.frames.get(timeout=0.2)
            except queue.Empty:
                continue
            with self.request_lock:
                req = self.request
            if req is None:
                continue

            out_w, out_h, fps = req["width"], req["height"], max(1, req["fps"])
            if codec_key != (out_w, out_h, fps):
                codec = make_codec(req)
                codec_key = (out_w, out_h, fps)
                pts = 0
                print(f"[proc] 编码 {out_w}x{out_h}@{fps}fps "
                      f"bitrate={args.bitrate}", flush=True)

            # mono-to-sbs / mono 变换
            if args.view_mode == "mono":
                if bgr.shape[1] != out_w or bgr.shape[0] != out_h:
                    bgr = cv2.resize(bgr, (out_w, out_h),
                                     interpolation=cv2.INTER_LINEAR)
            else:
                half_w = out_w // 2
                if bgr.shape[1] != half_w or bgr.shape[0] != out_h:
                    single = cv2.resize(bgr, (half_w, out_h),
                                        interpolation=cv2.INTER_LINEAR)
                else:
                    single = bgr
                stereo = np.empty((out_h, half_w * 2, 3), dtype=single.dtype)
                stereo[:, :half_w] = single
                stereo[:, half_w:] = single
                bgr = stereo

            self.latest = bgr
            frame = av.VideoFrame.from_ndarray(bgr, format="bgr24")
            frame.pts = pts
            frame.time_base = fractions.Fraction(1, fps)
            pts += 1

            for pkt in codec.encode(frame):
                data = bytes(pkt)
                with self.push_lock:
                    sock = self.push_sock
                if sock is None:
                    continue
                try:
                    sock.sendall(struct.pack(">I", len(data)) + data)
                except OSError:
                    print(f"[proc] 推流连接断开", flush=True)
                    with self.push_lock:
                        self.push_sock = None
                    continue
                pushed += 1

            now = time.time()
            if now - t0 >= 2.0:
                print(f"[proc] 推流 {pushed / (now - t0):.1f} fps / 4e6",
                      flush=True)
                pushed = 0
                t0 = now


# ========== 主流程 ==========


def detect_local_ip(host):
    tmp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tmp.connect((host, 80))
    ip = tmp.getsockname()[0]
    tmp.close()
    return ip


def main():
    ap = argparse.ArgumentParser(
        description="Pico 图像桥: 订阅 3588 流 -> SBS 处理 -> 连接 Pico 推流")
    ap.add_argument("--source-host", default="192.168.50.21",
                    help="数据发送端 (3588) 地址, 默认 192.168.50.83")
    ap.add_argument("--source-port", type=int, default=13579,
                    help="数据发送端 OPEN_CAMERA 端口, 默认 13579")
    ap.add_argument("--subscribe-port", type=int, default=13582,
                    help="本机接收 3588 回推流的端口, 默认 13582")
    ap.add_argument("--subscribe-width", type=int, default=1024,
                    help="单眼订阅宽度, 默认 1024")
    ap.add_argument("--subscribe-height", type=int, default=768,
                    help="单眼订阅高度, 默认 768")
    ap.add_argument("--subscribe-fps", type=int, default=30,
                    help="订阅帧率, 默认 30")
    ap.add_argument("--server-port", type=int, default=13579,
                    help="等待 Pico XRoboToolkit 连接的端口, 默认 13579")
    ap.add_argument("--bitrate", default="4M", help="推流码率, 默认 4M")
    ap.add_argument("--encoder", default="libx264")
    ap.add_argument("--view-mode", default="stereo", choices=["stereo", "mono"],
                    help="stereo=左右拼接(默认), mono=单目直通")
    ap.add_argument("--camera", default="ZED Mini")
    ap.add_argument("--fifo", default="/tmp/pc_sub.h264")
    ap.add_argument("--ip", default=None, help="本机局域网 IP (默认自动探测)")
    ap.add_argument("--preview", action="store_true", help="本地预览窗口")
    args = ap.parse_args()

    ip = args.ip or detect_local_ip(args.source_host)
    print(f"[pc] 本机 IP: {ip}", flush=True)

    stop = threading.Event()

    sub = Subscriber(args, ip)
    dec = Decoder(args)
    bridge = Bridge(args, dec)

    threads = [
        threading.Thread(target=sub.run, daemon=True),
        threading.Thread(target=dec.run, daemon=True),
        threading.Thread(target=bridge.process, daemon=True),
        threading.Thread(target=bridge.serve, daemon=True),
    ]
    for t in threads:
        t.start()

    try:
        while not stop.is_set():
            if args.preview:
                img = bridge.latest
                if img is not None:
                    cv2.imshow("PC Bridge (SBS)", img)
                    if cv2.waitKey(1) & 0xFF in (27, ord("q")):
                        stop.set()
            stop.wait(0.01)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if args.preview:
            cv2.destroyAllWindows()
        print("\n[pc] 已退出", flush=True)


if __name__ == "__main__":
    main()
