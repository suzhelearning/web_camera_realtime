"""
USB 摄像头 → H.264 → TCP 推流到 Pico XRoboToolkit。

兼容 Pico MediaDecoder 的协议: 每个 H.264 帧前加 4 字节大端长度头。
在 PC 端监听 TCP 13579，等待 Pico 连接并发送 OPEN_CAMERA 请求，
然后作为 TCP Client 连接 Pico 的 MediaDecoder 端口推送视频。

用法:
  python stream_to_pico.py --device /dev/video0
  python stream_to_pico.py --device /dev/video0 --encoder libx264 --bitrate 4M
"""

import argparse
import os
import socket
import struct
import subprocess
import signal
import sys
import time
import threading


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ========== 协议解析 ==========

def parse_network_protocol(data):
    """解析: [total_len(4 BE)][cmd_len(4 LE)][cmd][data_len(4 LE)][data]"""
    if len(data) < 12:
        return None, None
    offset = 0
    total_len = struct.unpack_from('>i', data, offset)[0]
    offset += 4
    cmd_len = struct.unpack_from('<i', data, offset)[0]
    offset += 4
    if cmd_len < 0 or offset + cmd_len > len(data):
        return None, None
    command = data[offset:offset + cmd_len].decode('utf-8')
    offset += cmd_len
    if offset + 4 > len(data):
        return command, None
    data_len = struct.unpack_from('<i', data, offset)[0]
    offset += 4
    payload = data[offset:offset + data_len] if data_len > 0 else b''
    return command, payload


def parse_camera_request(data):
    """解析 CameraRequestData 二进制格式"""
    if len(data) < 10:
        return None
    offset = 0
    if data[0] != 0xCA or data[1] != 0xFE:
        return None
    offset += 2
    version = data[offset]; offset += 1
    width = struct.unpack_from('<i', data, offset)[0]; offset += 4
    height = struct.unpack_from('<i', data, offset)[0]; offset += 4
    fps = struct.unpack_from('<i', data, offset)[0]; offset += 4
    bitrate = struct.unpack_from('<i', data, offset)[0]; offset += 4
    enable_mv_hevc = struct.unpack_from('<i', data, offset)[0]; offset += 4
    render_mode = struct.unpack_from('<i', data, offset)[0]; offset += 4
    port = struct.unpack_from('<i', data, offset)[0]; offset += 4
    cam_len = data[offset]; offset += 1
    camera = data[offset:offset + cam_len].decode('utf-8') if cam_len > 0 else ''
    offset += cam_len
    ip_len = data[offset]; offset += 1
    ip = data[offset:offset + ip_len].decode('utf-8') if ip_len > 0 else ''
    return {
        'width': width, 'height': height, 'fps': fps, 'bitrate': bitrate,
        'enable_mv_hevc': enable_mv_hevc, 'render_mode': render_mode,
        'camera': camera, 'ip': ip, 'port': port,
    }


# ========== H.264 帧分割与带长度头的 TCP 发送 ==========


def stream_h264_to_pico(
    device,
    pico_ip,
    pico_port,
    cap_w,
    cap_h,
    out_w,
    out_h,
    fps,
    bitrate_str,
    encoder_name,
    save_dir=None,
    save_fps=30.0,
    save_format="jpg",
    save_jpg_prefix="stream_high",
    save_jpg_quality=95,
    save_mp4_name="observation_high.mp4",
    view_mode="stereo",
):
    """用 OpenCV 采集 + PyAV 编码，每个编码 packet 加 4 字节大端长度头通过 TCP 发给 Pico"""
    import cv2
    import av
    import fractions
    import numpy as np

    # 连接 Pico 的 MediaDecoder TCP Server
    print(f"[tcp] 连接 {pico_ip}:{pico_port}...")
    tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    tcp_sock.settimeout(5)
    try:
        tcp_sock.connect((pico_ip, pico_port))
    except Exception as e:
        print(f"[tcp] 连接失败: {e}")
        return None, None
    print(f"[tcp] 已连接")

    # 解析码率
    br = bitrate_str.upper()
    if br.endswith('M'):
        bitrate = int(float(br[:-1]) * 1_000_000)
    elif br.endswith('K'):
        bitrate = int(float(br[:-1]) * 1_000)
    else:
        bitrate = int(br)

    # 打开摄像头
    cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
    if not cap.isOpened():
        print(f"[camera] 打开 {device} 失败")
        tcp_sock.close()
        return None, None
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, cap_w)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cap_h)
    cap.set(cv2.CAP_PROP_FPS, fps)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 2)
    actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"[camera] 已打开: {device} {actual_w}x{actual_h}")

    # 初始化编码器
    codec = av.CodecContext.create(encoder_name, 'w')
    codec.width = out_w
    codec.height = out_h
    codec.pix_fmt = 'yuv420p'
    codec.bit_rate = bitrate
    codec.framerate = fractions.Fraction(fps, 1)
    codec.time_base = fractions.Fraction(1, fps)
    codec.gop_size = fps  # 1 秒一个关键帧
    codec.max_b_frames = 0
    if encoder_name == 'libx264':
        codec.options = {
            'preset': 'ultrafast',
            'tune': 'zerolatency',
            'profile': 'baseline',
            'repeat-headers': '1',
            'threads': '8',
        }
    codec.open()
    print(f"[encoder] {encoder_name} {out_w}x{out_h} @ {fps}fps bitrate={bitrate//1000}kbps")
    print(f"[video] view_mode={view_mode}")

    save_enabled = bool(save_dir) and save_fps > 0 and save_format != "none"
    save_jpg_enabled = save_enabled and save_format in ("jpg", "both")
    save_mp4_enabled = save_enabled and save_format in ("mp4", "both")
    save_interval = 1.0 / save_fps if save_fps > 0 else 0.0
    mp4_writer_high = None
    mp4_path_high = None
    jpg_session_stamp = None
    save_run_dir = save_dir
    if save_enabled:
        os.makedirs(save_dir, exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        subdir_base = f"save_{timestamp}_{time.time_ns() % 1_000_000:06d}"
        attempt = 0
        while True:
            subdir_name = subdir_base if attempt == 0 else f"{subdir_base}_{attempt:02d}"
            candidate_dir = os.path.join(save_dir, subdir_name)
            try:
                os.makedirs(candidate_dir, exist_ok=False)
                save_run_dir = candidate_dir
                break
            except FileExistsError:
                attempt += 1

        jpg_session_stamp = timestamp

        if save_jpg_enabled:
            print(
                f"[save] 保存推流图像(JPG): dir={save_run_dir} "
                f"prefix={save_jpg_prefix}_{jpg_session_stamp}_*.jpg "
                f"fps={save_fps:.2f} quality={save_jpg_quality}"
            )

        if save_mp4_enabled:
            save_mp4_stem, save_mp4_ext = os.path.splitext(save_mp4_name)
            if not save_mp4_ext:
                save_mp4_ext = ".mp4"
            timestamped_mp4_name = f"{save_mp4_stem}_{timestamp}{save_mp4_ext}"
            mp4_path_high = os.path.join(save_run_dir, timestamped_mp4_name)
            mp4_parent = os.path.dirname(mp4_path_high)
            if mp4_parent:
                os.makedirs(mp4_parent, exist_ok=True)
            print(
                f"[save] 保存推流图像(MP4): high={mp4_path_high} "
                f"fps={save_fps:.2f}"
            )

    def ensure_mp4_writer(writer, path, width, height):
        if writer is not None:
            return writer
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        created = cv2.VideoWriter(path, fourcc, float(save_fps), (width, height))
        if not created.isOpened():
            raise RuntimeError(f"cannot open mp4 writer: {path}")
        return created

    stop_event = threading.Event()

    def capture_and_send():
        nonlocal mp4_writer_high
        frame_count = 0
        saved_count = 0
        saved_seq = 0
        t0 = time.time()
        pts = 0
        next_save_ts = time.monotonic()
        stream_interval = 1.0 / fps
        next_stream_ts = time.monotonic()

        try:
            while not stop_event.is_set():
                ret, bgr = cap.read()
                if not ret:
                    time.sleep(0.001)
                    continue

                now = time.monotonic()
                if now < next_stream_ts:
                    continue
                next_stream_ts = max(next_stream_ts + stream_interval, now)

                if view_mode == "mono":
                    # 单目模式: 输出一张完整大画面，不做左右拼接。
                    bgr = cv2.resize(bgr, (out_w, out_h), interpolation=cv2.INTER_LINEAR)
                else:
                    # Side-by-Side 模式: 左右眼各显示同一画面。
                    half_w = out_w // 2
                    single_eye = cv2.resize(bgr, (half_w, out_h), interpolation=cv2.INTER_LINEAR)
                    stereo = np.empty((out_h, half_w * 2, 3), dtype=single_eye.dtype)
                    stereo[:, :half_w] = single_eye
                    stereo[:, half_w:] = single_eye
                    bgr = stereo

                if save_enabled and save_interval > 0:
                    now = time.monotonic()
                    if now >= next_save_ts:
                        h, w, c = bgr.shape
                        if c != 3:
                            raise RuntimeError(f"unexpected channel count: {c}")

                        if save_jpg_enabled:
                            jpg_name = f"{save_jpg_prefix}_{jpg_session_stamp}_{saved_seq:08d}.jpg"
                            jpg_path = os.path.join(save_run_dir, jpg_name)
                            ok = cv2.imwrite(
                                jpg_path,
                                bgr,
                                [cv2.IMWRITE_JPEG_QUALITY, int(save_jpg_quality)],
                            )
                            if not ok:
                                raise RuntimeError(f"failed to write jpg: {jpg_path}")

                        if save_mp4_enabled:
                            if mp4_writer_high is None:
                                mp4_writer_high = ensure_mp4_writer(mp4_writer_high, mp4_path_high, w, h)
                            mp4_writer_high.write(bgr)

                        saved_count += 1
                        saved_seq += 1
                        # 采用时间推进，避免高负载下持续补写堆积
                        next_save_ts = now + save_interval

                # BGR -> YUV via av
                frame = av.VideoFrame.from_ndarray(bgr, format='bgr24')
                frame.pts = pts
                frame.time_base = fractions.Fraction(1, fps)
                pts += 1

                # 编码
                packets = codec.encode(frame)
                for pkt in packets:
                    pkt_bytes = bytes(pkt)
                    # [4 字节大端长度][H.264 packet 数据]
                    header = struct.pack('>I', len(pkt_bytes))
                    try:
                        tcp_sock.sendall(header + pkt_bytes)
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        print(f"[tcp] 连接断开")
                        stop_event.set()
                        return

                    frame_count += 1

                elapsed = time.time() - t0
                if elapsed >= 2.0:
                    if save_enabled and elapsed > 0:
                        print(
                            f"[stream] {frame_count / elapsed:.1f} frames/s, "
                            f"saved {saved_count / elapsed:.1f} fps",
                            flush=True,
                        )
                    else:
                        print(f"[stream] {frame_count / elapsed:.1f} frames/s", flush=True)
                    frame_count = 0
                    saved_count = 0
                    t0 = time.time()

        except Exception as e:
            print(f"[stream] 错误: {e}")
        finally:
            if mp4_writer_high is not None:
                mp4_writer_high.release()
            cap.release()
            tcp_sock.close()
            print(f"[stream] 已停止")

    send_thread = threading.Thread(target=capture_and_send, daemon=True)
    send_thread.start()

    return stop_event, send_thread


# ========== TCP 命令服务 ==========

def run_server(args):
    local_ip = get_local_ip()
    tcp_port = 13579

    print(f"[server] 本机 IP: {local_ip}")
    print(f"[server] 监听 TCP {tcp_port}，等待 Pico XRoboToolkit 连接...")
    print(f"[server] Pico 上输入 IP: {local_ip}")
    print()

    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind(('0.0.0.0', tcp_port))
    server_sock.listen(2)

    stream_stop = None
    send_thread = None

    def cleanup(sig=None, frame=None):
        nonlocal stream_stop
        if stream_stop:
            stream_stop.set()
            stream_stop = None
        server_sock.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    while True:
        print(f"[server] 等待连接...")
        try:
            conn, addr = server_sock.accept()
        except OSError:
            break
        print(f"[server] Pico 已连接: {addr}")

        try:
            while True:
                data = conn.recv(4096)
                if not data:
                    print(f"[server] 连接断开")
                    break

                command, payload = parse_network_protocol(data)
                print(f"[server] 命令: {command}")

                if command == "OPEN_CAMERA" and payload:
                    req = parse_camera_request(payload)
                    if req:
                        print(f"[server] 请求: {req['width']}x{req['height']}@{req['fps']}fps "
                              f"→ {req['ip']}:{req['port']}")

                        # 停掉旧的
                        if stream_stop:
                            stream_stop.set()
                            if send_thread:
                                send_thread.join(timeout=3)
                            stream_stop = None

                        pico_ip = req['ip'] or addr[0]
                        pico_port = req['port']
                        out_w = req['width']
                        out_h = req['height']
                        fps = args.fps if args.fps > 0 else req['fps']
                        cap_w = args.width if args.width > 0 else 1280
                        cap_h = args.height if args.height > 0 else 960

                        print(f"[server] 采集 {cap_w}x{cap_h} → 缩放 {out_w}x{out_h} "
                              f"→ {pico_ip}:{pico_port} @{fps}fps")

                        stream_stop, send_thread = stream_h264_to_pico(
                            args.device, pico_ip, pico_port,
                            cap_w, cap_h, out_w, out_h,
                            fps, args.bitrate, args.encoder,
                            args.save_dir,
                            args.save_fps,
                            args.save_format,
                            args.save_jpg_prefix,
                            args.save_jpg_quality,
                            args.save_mp4_name,
                            args.view_mode,
                        )

                elif command == "CLOSE_CAMERA":
                    if stream_stop:
                        print(f"[server] 停止推流")
                        stream_stop.set()
                        if send_thread:
                            send_thread.join(timeout=3)
                        stream_stop = None

        except ConnectionResetError:
            print(f"[server] 连接被重置")
        except Exception as e:
            print(f"[server] 错误: {e}")
        finally:
            conn.close()
            if stream_stop:
                stream_stop.set()
                stream_stop = None
            print()


def main():
    parser = argparse.ArgumentParser(
        description="USB 摄像头 H.264 推流到 Pico XRoboToolkit",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
运行后在 Pico XRoboToolkit 中:
  1. 选择视频源 (如 ZED Mini)
  2. 输入本机 IP 地址
  3. 视频会自动显示在 VR 中
        """
    )
    parser.add_argument("--device", type=str, default="/dev/video0")
    parser.add_argument("--width", type=int, default=0, help="采集宽 (0=默认1280)")
    parser.add_argument("--height", type=int, default=0, help="采集高 (0=默认960)")
    parser.add_argument("--fps", type=int, default=0, help="帧率 (0=用Pico请求值)")
    parser.add_argument("--bitrate", type=str, default="4M")
    parser.add_argument("--encoder", type=str, default="libx264")
    parser.add_argument("--save-dir", type=str, default=None, help="图像保存目录 (默认当前工作目录)")
    parser.add_argument("--save-fps", type=float, default=30.0, help="图像保存帧率")
    parser.add_argument("--save-format", type=str, default="jpg", choices=["jpg", "mp4", "both", "none"], help="保存格式: jpg/mp4/both/none")
    parser.add_argument("--save-jpg-prefix", type=str, default="stream_high", help="JPG 文件名前缀")
    parser.add_argument("--save-jpg-quality", type=int, default=95, help="JPG 质量 (1-100)")
    parser.add_argument("--save-mp4-name", type=str, default="observation_high.mp4", help="high 视角 MP4 文件名(会自动追加时间戳)")
    parser.add_argument("--view-mode", type=str, default="stereo", choices=["stereo", "mono"], help="输出模式: stereo=左右拼接, mono=单目全画面")
    args = parser.parse_args()
    if args.save_jpg_quality < 1 or args.save_jpg_quality > 100:
        parser.error("--save-jpg-quality 必须在 1-100 之间")

    run_server(args)


if __name__ == "__main__":
    main()
