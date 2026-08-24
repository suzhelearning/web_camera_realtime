#!/usr/bin/env python3
"""Simulate the Pico XRoboToolkit client for stream_to_pico.py.

1. Opens a MediaDecoder listener on --listen-port.
2. Connects to the stream server (--server:13579), sends OPEN_CAMERA.
3. Receives [4BE len][H.264 packet] frames, writes them to --out as raw Annex-B.
"""
import argparse
import socket
import struct
import threading
import time


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


def run(listen_port, server_host, server_port, ip, width, height, fps,
        bitrate, camera, out_path, duration):
    with open(out_path, "wb") as out:
        frames = [0]
        started = [time.time()]
        stop = [False]

        def media_server():
            ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            ls.bind(("0.0.0.0", listen_port))
            ls.listen(1)
            ls.settimeout(duration + 10)
            conn, addr = ls.accept()
            print(f"[media] 收到推流连接: {addr}", flush=True)
            conn.settimeout(2)
            while not stop[0]:
                try:
                    hdr = recv_exact(conn, 4)
                except socket.timeout:
                    continue
                if hdr is None:
                    break
                ln = struct.unpack(">I", hdr)[0]
                pkt = recv_exact(conn, ln)
                if pkt is None:
                    break
                out.write(pkt)
                frames[0] += 1
                if frames[0] % 100 == 0:
                    el = time.time() - started[0]
                    print(f"[media] 已收 {frames[0]} 帧, "
                          f"{frames[0] / el:.1f} fps", flush=True)
            conn.close()
            ls.close()
            print(f"[media] 收流结束, 共 {frames[0]} 帧", flush=True)

        t = threading.Thread(target=media_server, daemon=True)
        t.start()
        time.sleep(0.5)

        s = socket.create_connection((server_host, server_port), timeout=5)
        s.sendall(build_open_camera(listen_port, ip, width, height, fps,
                                    bitrate, camera))
        print(f"[ctl] OPEN_CAMERA 已发送 (listen {ip}:{listen_port}, "
              f"{width}x{height}@{fps}fps)", flush=True)
        time.sleep(duration)
        stop[0] = True
        s.close()
        t.join(timeout=5)
        el = time.time() - started[0]
        print(f"[done] 捕获 {frames[0]} 帧 / {el:.1f}s, 输出 {out_path}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen-port", type=int, default=13580)
    ap.add_argument("--server", default="127.0.0.1")
    ap.add_argument("--server-port", type=int, default=13579)
    ap.add_argument("--ip", default="127.0.0.1")
    ap.add_argument("--width", type=int, default=1024)
    ap.add_argument("--height", type=int, default=768)
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--bitrate", type=int, default=2000000)
    ap.add_argument("--camera", default="ZED Mini")
    ap.add_argument("--out", default="/tmp/sim_pico.h264")
    ap.add_argument("--duration", type=float, default=25.0)
    args = ap.parse_args()
    run(args.listen_port, args.server, args.server_port, args.ip,
        args.width, args.height, args.fps, args.bitrate, args.camera,
        args.out, args.duration)


if __name__ == "__main__":
    main()
