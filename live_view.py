#!/usr/bin/env python3
"""Live OpenCV window showing the remote Pico camera stream.

Simulates the Pico XRoboToolkit client: OPEN_CAMERA to the stream server,
receives [4BE len][H.264] frames on a local listener, decodes via PyAV
from a FIFO and shows them in a window in real time. The terminal shows a
live status line: playback fps, incoming fps, network jitter, backlog,
bitrate.
"""
import argparse
import fcntl
import os
import socket
import struct
import threading
import time

import av
import cv2


def build_open_camera(port, ip, width, height, fps, bitrate, camera):
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


def recv_exact(conn, n):
    data = b""
    while len(data) < n:
        chunk = conn.recv(n - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="192.168.50.21")
    ap.add_argument("--server-port", type=int, default=13579)
    ap.add_argument("--listen-port", type=int, default=13581)
    ap.add_argument("--ip", default=None, help="本机局域网 IP，服务器回连用")
    ap.add_argument("--width", type=int, default=1024)
    ap.add_argument("--height", type=int, default=768)
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--fifo", default="/tmp/live_cam.h264")
    ap.add_argument("--max-backlog", type=int, default=8,
                    help="积压帧超过此值丢弃已解码帧, 突发时追回实时")
    ap.add_argument("--pipe-size", type=int, default=16384,
                    help="FIFO 管道缓冲字节数, 越小端到端延迟越低")
    ap.add_argument("--title", default="Pico Camera Stream")
    args = ap.parse_args()

    ip = args.ip
    if not ip:
        tmp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        tmp.connect((args.server, 80))
        ip = tmp.getsockname()[0]
        tmp.close()
        print(f"[ctl] 本机 IP: {ip}", flush=True)

    if os.path.exists(args.fifo):
        os.unlink(args.fifo)
    os.mkfifo(args.fifo)

    stats = {"frames": 0, "bytes": 0, "t0": time.time(), "jitter": 0.0}
    jstate = {"last_ts": None, "last_delta": 0.0}

    def receiver():
        ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        ls.bind(("0.0.0.0", args.listen_port))
        ls.listen(1)
        conn, addr = ls.accept()
        print(f"[media] 推流连接来自: {addr}", flush=True)
        writer = os.fdopen(os.open(args.fifo, os.O_WRONLY), "wb", 0)
        try:
            fcntl.fcntl(writer.fileno(), fcntl.F_SETPIPE_SZ, args.pipe_size)
        except OSError as exc:
            print(f"[warn] 设置 FIFO 缓冲 {args.pipe_size}B 失败: {exc}",
                  flush=True)
        try:
            while True:
                hdr = recv_exact(conn, 4)
                if hdr is None:
                    break
                ln = struct.unpack(">I", hdr)[0]
                pkt = recv_exact(conn, ln)
                if pkt is None:
                    break
                writer.write(pkt)
                stats["frames"] += 1
                stats["bytes"] += len(pkt)
                now = time.time()
                if jstate["last_ts"] is not None:
                    delta = now - jstate["last_ts"]
                    dev = abs(delta - jstate["last_delta"])
                    stats["jitter"] += (dev - stats["jitter"]) / 16.0
                    jstate["last_delta"] = delta
                jstate["last_ts"] = now
        finally:
            writer.close()
            conn.close()
            ls.close()
            print("\n[media] 收流结束", flush=True)

    threading.Thread(target=receiver, daemon=True).start()
    time.sleep(0.5)

    s = socket.create_connection((args.server, args.server_port), timeout=5)
    s.sendall(build_open_camera(args.listen_port, ip,
                                args.width, args.height, args.fps, 2000000,
                                "ZED Mini"))
    print(f"[ctl] OPEN_CAMERA -> {args.server}:{args.server_port}", flush=True)

    container = av.open(args.fifo, "r", format="h264")
    if not container.streams.video:
        print("[err] 无法打开视频流", flush=True)
        return 1

    window = args.title
    last = time.time()
    consumed = 0
    shown = 0
    dropped = 0
    prev_in = 0
    prev_shown = 0

    def show_stats(now):
        nonlocal last, prev_in, prev_shown
        if now - last < 2:
            return
        dt = now - last
        in_rate = (stats["frames"] - prev_in) / dt
        out_rate = (shown - prev_shown) / dt
        prev_in = stats["frames"]
        prev_shown = shown
        backlog = stats["frames"] - consumed
        bitrate = stats["bytes"] * 8 / (now - stats["t0"]) / 1e6
        line = (f"[view] 播放 {out_rate:5.1f}fps | 入流 {in_rate:5.1f}fps | "
                f"抖动 {stats['jitter'] * 1000:5.1f}ms | 积压 {backlog:4d}帧 | "
                f"码率 {bitrate:5.2f}Mbps | 已收 {stats['frames']}帧")
        if dropped:
            line += f" | 丢弃 {dropped}帧"
        # 换行滚动输出 (不清屏), 便于回顾历史统计
        print(line, flush=True)
        last = now

    try:
        for frame in container.decode(video=0):
            now = time.time()
            consumed += 1
            if stats["frames"] - consumed > args.max_backlog:
                dropped += 1
                continue  # 积压超限: 丢弃本帧不显示, 追回实时
            shown += 1
            cv2.imshow(window, frame.to_ndarray(format="bgr24"))
            key = cv2.waitKey(1) & 0xFF
            if key in (27, ord("q")):
                break
            show_stats(now)
    except (av.error.EOFError, StopIteration):
        show_stats(time.time())
        print("\n[media] 视频流已结束", flush=True)

    print(flush=True)
    container.close()
    cv2.destroyAllWindows()
    s.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[view] 已退出 (Ctrl+C)", flush=True)
