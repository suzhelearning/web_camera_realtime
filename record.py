"""
本地摄像头采集录制：直接打开 RealSense + USB 摄像头，按键控制录制，保存为 MP4。
无需 ZMQ 跨机通信，单脚本即可完成采集。

快捷键:
  s: 开始录制
  d: 停止录制并保存
  x: 丢弃当前录制
  q: 退出

用法:
  列出所有设备：
    python record.py --list

  基础用法（RealSense + USB 混合）：
    python record.py \
      --serial-names 352122274322:right_hand 409122273200:left_hand \
      --usb-cameras /dev/video0:fisheye

  只用 USB 摄像头：
    python record.py --usb-cameras /dev/video0:cam0 /dev/video2:cam1

  关闭可视化（后台录制）：
    python record.py --usb-cameras /dev/video0:cam0 --no-display

  每个摄像头独立设置：
    python record.py \
      --serial-names \
        352122274322:right_hand:exposure=300:gain=16 \
      --usb-cameras \
        /dev/video0:fisheye:auto_exposure=0:exposure=200:rotate=90

  可用的 per-camera 参数：
    auto_exposure=0/1    自动曝光开关
    exposure=<值>        手动曝光
    gain=<值>            增益/ISO
    auto_wb=0/1          自动白平衡开关
    wb_temperature=<值>  白平衡色温(K)
    rotate=0/90/180/270  旋转角度（仅 USB 摄像头）
"""
import numpy as np
import cv2
import time
import argparse
import sys
import threading
import queue
import os
import subprocess
from datetime import datetime

try:
    import pyrealsense2 as rs
    HAS_REALSENSE = True
except ImportError:
    HAS_REALSENSE = False

try:
    from turbojpeg import TurboJPEG, TJXOP_ROT90, TJXOP_ROT180, TJXOP_ROT270
    _tj = TurboJPEG()
    HAS_TURBOJPEG = True
except ImportError:
    HAS_TURBOJPEG = False


# ========== 设备列举 ==========

def list_realsense_devices():
    if not HAS_REALSENSE:
        print("pyrealsense2 未安装，跳过 RealSense 检测。")
        return []
    ctx = rs.context()
    devices = ctx.query_devices()
    if len(devices) == 0:
        print("未检测到任何 RealSense 设备。")
        return []
    print(f"检测到 {len(devices)} 个 RealSense 设备：")
    device_info = []
    for i, dev in enumerate(devices):
        serial = dev.get_info(rs.camera_info.serial_number)
        name = dev.get_info(rs.camera_info.name)
        fw = dev.get_info(rs.camera_info.firmware_version)
        print(f"  [RS-{i}] {name}  序列号: {serial}  固件: {fw}")
        device_info.append({"index": i, "serial": serial, "name": name})
    return device_info


def list_usb_cameras():
    print("\nV4L2 视频设备：")
    try:
        result = subprocess.run(
            ["v4l2-ctl", "--list-devices"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            print(result.stdout)
        else:
            _list_dev_video()
    except FileNotFoundError:
        _list_dev_video()
    except Exception as e:
        print(f"  列出设备失败: {e}")


def _list_dev_video():
    import glob
    videos = sorted(glob.glob("/dev/video*"))
    if videos:
        for v in videos:
            print(f"  {v}")
    else:
        print("  未检测到视频设备")


# ========== 参数解析与配置 ==========

def parse_cam_params(parts):
    params = {}
    for part in parts:
        if '=' not in part:
            continue
        key, val = part.split('=', 1)
        key, val = key.strip(), val.strip()
        if key in ('auto_exposure', 'auto_wb'):
            params[key] = val in ('1', 'true', 'True', 'on')
        elif key in ('exposure', 'gain', 'wb_temperature'):
            params[key] = int(val)
        elif key == 'rotate':
            r = int(val)
            if r not in (0, 90, 180, 270):
                print(f"  警告: rotate 只支持 0/90/180/270，收到 {r}，忽略")
            else:
                params[key] = r
        elif key == 'fourcc':
            params[key] = val.upper()
        else:
            print(f"  警告: 未知参数 {key}={val}，忽略")
    return params


def merge_settings(global_settings, per_cam_params):
    merged = dict(global_settings)
    merged.update(per_cam_params)
    return merged


def configure_rs_sensor(sensor, tag, settings):
    auto_exposure = settings.get('auto_exposure', True)
    exposure = settings.get('exposure')
    gain = settings.get('gain')
    auto_wb = settings.get('auto_wb', True)
    wb_temperature = settings.get('wb_temperature')

    try:
        if sensor.supports(rs.option.enable_auto_exposure):
            sensor.set_option(rs.option.enable_auto_exposure, 1 if auto_exposure else 0)
            print(f"{tag} 自动曝光: {'开' if auto_exposure else '关'}")
        if not auto_exposure and exposure is not None:
            if sensor.supports(rs.option.exposure):
                opt_range = sensor.get_option_range(rs.option.exposure)
                exposure = max(opt_range.min, min(opt_range.max, exposure))
                sensor.set_option(rs.option.exposure, exposure)
                print(f"{tag} 手动曝光: {exposure}")
        if gain is not None:
            if sensor.supports(rs.option.gain):
                opt_range = sensor.get_option_range(rs.option.gain)
                gain = max(opt_range.min, min(opt_range.max, gain))
                sensor.set_option(rs.option.gain, gain)
                print(f"{tag} 增益(ISO): {gain}")
        if sensor.supports(rs.option.enable_auto_white_balance):
            sensor.set_option(rs.option.enable_auto_white_balance, 1 if auto_wb else 0)
            print(f"{tag} 自动白平衡: {'开' if auto_wb else '关'}")
        if not auto_wb and wb_temperature is not None:
            if sensor.supports(rs.option.white_balance):
                opt_range = sensor.get_option_range(rs.option.white_balance)
                wb_temperature = max(opt_range.min, min(opt_range.max, wb_temperature))
                sensor.set_option(rs.option.white_balance, wb_temperature)
                print(f"{tag} 白平衡色温: {wb_temperature}K")
    except Exception as e:
        print(f"{tag} 设置传感器参数时出错: {e}")


def configure_usb_camera(cap, tag, settings):
    auto_exposure = settings.get('auto_exposure', True)
    exposure = settings.get('exposure')
    gain = settings.get('gain')
    auto_wb = settings.get('auto_wb', True)
    wb_temperature = settings.get('wb_temperature')

    try:
        if auto_exposure:
            cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 3)
            print(f"{tag} 自动曝光: 开")
        else:
            cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1)
            print(f"{tag} 自动曝光: 关")
            if exposure is not None:
                cap.set(cv2.CAP_PROP_EXPOSURE, exposure)
                actual = cap.get(cv2.CAP_PROP_EXPOSURE)
                print(f"{tag} 手动曝光: {exposure} (实际: {actual})")
        if gain is not None:
            cap.set(cv2.CAP_PROP_GAIN, gain)
            actual = cap.get(cv2.CAP_PROP_GAIN)
            print(f"{tag} 增益: {gain} (实际: {actual})")
        if auto_wb:
            cap.set(cv2.CAP_PROP_AUTO_WB, 1)
            print(f"{tag} 自动白平衡: 开")
        else:
            cap.set(cv2.CAP_PROP_AUTO_WB, 0)
            print(f"{tag} 自动白平衡: 关")
            if wb_temperature is not None:
                cap.set(cv2.CAP_PROP_WB_TEMPERATURE, wb_temperature)
                actual = cap.get(cv2.CAP_PROP_WB_TEMPERATURE)
                print(f"{tag} 白平衡色温: {wb_temperature} (实际: {actual})")
    except Exception as e:
        print(f"{tag} 设置 USB 摄像头参数时出错: {e}")


# ========== 摄像头采集线程 ==========

def realsense_capture_thread(serial, cam_name, width, height, fps, cam_settings,
                              frame_dict, lock, stop_event):
    """RealSense 摄像头采集线程，帧写入共享字典"""
    tag = f"[{cam_name}]"
    if cam_settings is None:
        cam_settings = {}

    pipeline = rs.pipeline()
    config = rs.config()
    config.enable_device(serial)
    config.enable_stream(rs.stream.color, width, height, rs.format.bgr8, fps)

    max_retries = 5
    retry_count = 0
    while retry_count < max_retries and not stop_event.is_set():
        try:
            profile = pipeline.start(config)
            break
        except RuntimeError as e:
            retry_count += 1
            print(f"{tag} 启动失败 ({retry_count}/{max_retries}): {e}")
            if retry_count < max_retries:
                time.sleep(3)
    else:
        print(f"{tag} 无法启动，放弃。")
        return

    device = profile.get_device()
    print(f"{tag} 已连接: {device.get_info(rs.camera_info.name)} ({serial})")

    color_sensor = None
    for sensor in device.query_sensors():
        for p in sensor.get_stream_profiles():
            if p.stream_type() == rs.stream.color:
                color_sensor = sensor
                break
        if color_sensor:
            break
    if color_sensor:
        configure_rs_sensor(color_sensor, tag, cam_settings)

    time.sleep(0.5)
    frame_count = 0
    fps_timer = time.time()

    try:
        while not stop_event.is_set():
            try:
                frames = pipeline.wait_for_frames(timeout_ms=1000)
            except RuntimeError:
                continue
            color_frame = frames.get_color_frame()
            if not color_frame:
                continue
            frame_bgr = np.asanyarray(color_frame.get_data())
            with lock:
                frame_dict[cam_name] = frame_bgr

            frame_count += 1
            elapsed = time.time() - fps_timer
            if elapsed >= 2.0:
                print(f"{tag} FPS: {frame_count / elapsed:.1f}")
                frame_count = 0
                fps_timer = time.time()
    finally:
        pipeline.stop()
        print(f"{tag} 已停止。")


def usb_capture_thread(device_path, cam_name, width, height, fps, cam_settings,
                        frame_dict, lock, stop_event):
    """USB 摄像头采集线程，帧写入共享字典"""
    tag = f"[{cam_name}]"
    if cam_settings is None:
        cam_settings = {}

    if device_path.startswith("/dev/"):
        cap = cv2.VideoCapture(device_path, cv2.CAP_V4L2)
    else:
        cap = cv2.VideoCapture(int(device_path), cv2.CAP_V4L2)

    if not cap.isOpened():
        print(f"{tag} 打开 {device_path} 失败")
        return

    fourcc_str = cam_settings.get('fourcc', 'MJPG')
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc_str))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_FPS, fps)

    actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    actual_fps = cap.get(cv2.CAP_PROP_FPS)

    # 旋转
    rotate_deg = cam_settings.get('rotate', 0)
    cv_rotate_map = {
        90: cv2.ROTATE_90_CLOCKWISE,
        180: cv2.ROTATE_180,
        270: cv2.ROTATE_90_COUNTERCLOCKWISE,
    }
    cv_rotate_code = cv_rotate_map.get(rotate_deg)

    rotate_info = f"  旋转: {rotate_deg}°" if rotate_deg else ""
    print(f"{tag} 已打开: {device_path}  分辨率: {actual_w}x{actual_h}  帧率: {actual_fps}{rotate_info}")

    # 减小 V4L2 缓冲区，降低延迟
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    configure_usb_camera(cap, tag, cam_settings)

    frame_count = 0
    fps_timer = time.time()
    fail_count = 0

    try:
        while not stop_event.is_set():
            # grab+retrieve 比 read 更适合多线程：grab 只抓取不解码，减少 GIL 竞争
            if not cap.grab():
                fail_count += 1
                if fail_count >= 30:
                    print(f"{tag} 连续读帧失败，退出。")
                    break
                time.sleep(0.01)
                continue
            ret, frame = cap.retrieve()
            if not ret or frame is None:
                continue
            fail_count = 0

            if cv_rotate_code is not None:
                frame = cv2.rotate(frame, cv_rotate_code)

            with lock:
                frame_dict[cam_name] = frame

            frame_count += 1
            elapsed = time.time() - fps_timer
            if elapsed >= 2.0:
                print(f"{tag} FPS: {frame_count / elapsed:.1f}")
                frame_count = 0
                fps_timer = time.time()
    finally:
        cap.release()
        print(f"{tag} 已停止。")


# ========== 录制器 ==========

class MP4Recorder:
    """流式 MP4 录制器：后台线程逐帧写入，不阻塞主线程。"""

    def __init__(self, session_dir, fps):
        self.session_dir = session_dir
        self.fps = fps
        self.q = queue.Queue(maxsize=300)
        self.stop_event = threading.Event()
        self.thread = None
        self.writers = {}
        self.frame_counts = {}

    def start(self):
        os.makedirs(self.session_dir, exist_ok=True)
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._writer_loop, daemon=True)
        self.thread.start()

    def put_frame(self, cam_name, frame_bgr):
        try:
            self.q.put_nowait((cam_name, frame_bgr))
        except queue.Full:
            pass

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=10)
        return self.frame_counts.copy()

    def discard(self):
        self.stop_event.set()
        while not self.q.empty():
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        if self.thread:
            self.thread.join(timeout=5)
        import shutil
        if os.path.exists(self.session_dir):
            shutil.rmtree(self.session_dir)
            print(f"    已删除: {self.session_dir}")

    def _writer_loop(self):
        try:
            while not self.stop_event.is_set() or not self.q.empty():
                try:
                    cam_name, frame_bgr = self.q.get(timeout=0.1)
                except queue.Empty:
                    continue

                h, w = frame_bgr.shape[:2]
                if cam_name not in self.writers:
                    video_path = os.path.join(self.session_dir, f'{cam_name}.mp4')
                    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
                    self.writers[cam_name] = cv2.VideoWriter(
                        video_path, fourcc, self.fps, (w, h))
                    self.frame_counts[cam_name] = 0
                self.writers[cam_name].write(frame_bgr)
                self.frame_counts[cam_name] += 1
        finally:
            for writer in self.writers.values():
                writer.release()
            total = sum(self.frame_counts.values())
            print(f"    写入完成: 共 {total} 帧")
            for cam_name in self.writers:
                print(f"    视频: {self.session_dir}/{cam_name}.mp4")


# ========== 主函数 ==========

def main():
    parser = argparse.ArgumentParser(
        description="本地摄像头采集录制（RealSense + USB），保存为 MP4",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python record.py --list
  python record.py --usb-cameras /dev/video0:cam0 /dev/video2:cam1
  python record.py --serial-names 352122274322:right_hand --usb-cameras /dev/video0:fisheye
  python record.py --usb-cameras /dev/video0:cam0 --no-display
  python record.py --usb-cameras /dev/video0:cam0:rotate=90:auto_exposure=0:exposure=200
        """
    )
    parser.add_argument("--list", action="store_true", help="列出所有设备后退出")
    # RealSense
    parser.add_argument("--serial-names", type=str, nargs="*", default=None,
                        help="序列号:名字[:key=val...]，如 352122274322:right_hand:exposure=300")
    # USB
    parser.add_argument("--usb-cameras", type=str, nargs="*", default=None,
                        help="设备路径:名字[:key=val...]，如 /dev/video0:fisheye:rotate=90")
    # 通用
    parser.add_argument("--width", type=int, default=640, help="图像宽度 (默认 640)")
    parser.add_argument("--height", type=int, default=480, help="图像高度 (默认 480)")
    parser.add_argument("--fps", type=int, default=30, help="帧率 (默认 30)")
    parser.add_argument("--save-dir", type=str, default="./recordings", help="保存目录 (默认 ./recordings)")
    parser.add_argument("--no-display", action="store_true", help="关闭可视化窗口")
    # 全局曝光设置
    parser.add_argument("--auto-exposure", action="store_true", default=True)
    parser.add_argument("--no-auto-exposure", dest="auto_exposure", action="store_false")
    parser.add_argument("--exposure", type=int, default=None, help="全局手动曝光值")
    parser.add_argument("--gain", type=int, default=None, help="全局增益/ISO")
    parser.add_argument("--auto-wb", action="store_true", default=True)
    parser.add_argument("--no-auto-wb", dest="auto_wb", action="store_false")
    parser.add_argument("--wb-temperature", type=int, default=None, help="全局白平衡色温 (K)")
    args = parser.parse_args()

    # 列出设备
    rs_devices = list_realsense_devices()
    if args.list:
        list_usb_cameras()
        sys.exit(0)

    global_settings = {
        'auto_exposure': args.auto_exposure,
        'exposure': args.exposure,
        'gain': args.gain,
        'auto_wb': args.auto_wb,
        'wb_temperature': args.wb_temperature,
    }

    frame_dict = {}  # cam_name -> latest BGR frame
    lock = threading.Lock()
    stop_event = threading.Event()
    threads = []

    # ===== RealSense =====
    if args.serial_names and HAS_REALSENSE:
        serial_to_info = {}
        for sn in args.serial_names:
            parts = sn.split(':')
            if len(parts) < 2:
                print(f"错误: --serial-names 格式应为 序列号:名字[:key=val...]，收到: {sn}")
                sys.exit(1)
            serial_to_info[parts[0]] = (parts[1], parse_cam_params(parts[2:]))

        for dev in rs_devices:
            if dev['serial'] in serial_to_info:
                name, per_cam_params = serial_to_info[dev['serial']]
                settings = merge_settings(global_settings, per_cam_params)
                print(f"  RealSense: {name} ({dev['serial']})")
                t = threading.Thread(
                    target=realsense_capture_thread,
                    args=(dev['serial'], name, args.width, args.height, args.fps,
                          settings, frame_dict, lock, stop_event),
                    daemon=True
                )
                t.start()
                threads.append(t)

    # ===== USB =====
    if args.usb_cameras:
        for usb_spec in args.usb_cameras:
            parts = usb_spec.split(':')
            if len(parts) < 2:
                print(f"错误: --usb-cameras 格式应为 设备路径:名字[:key=val...]，收到: {usb_spec}")
                sys.exit(1)
            dev_path = parts[0]
            cam_name = parts[1]
            per_cam_params = parse_cam_params(parts[2:])
            usb_defaults = {
                'auto_exposure': True, 'exposure': None, 'gain': None,
                'auto_wb': True, 'wb_temperature': None,
            }
            usb_settings = merge_settings(usb_defaults, per_cam_params)
            print(f"  USB: {cam_name} ({dev_path})")
            t = threading.Thread(
                target=usb_capture_thread,
                args=(dev_path, cam_name, args.width, args.height, args.fps,
                      usb_settings, frame_dict, lock, stop_event),
                daemon=True
            )
            t.start()
            threads.append(t)

    if not threads:
        print("错误: 没有指定任何摄像头")
        sys.exit(1)

    show_display = not args.no_display
    print(f"\n快捷键: 's'=开始录制  'd'=停止并保存(MP4)  'x'=丢弃录制  'q'=退出")
    if not show_display:
        print("可视化已关闭，使用终端按键控制 (需要终端焦点)")
    print()

    recorder = None

    # 无显示模式下用非阻塞终端输入
    if not show_display:
        import select
        import tty
        import termios
        old_settings = termios.tcgetattr(sys.stdin)
        tty.setcbreak(sys.stdin.fileno())

    try:
        while True:
            with lock:
                cam_frames = dict(frame_dict)

            if show_display:
                for cam_name, frame in cam_frames.items():
                    display = frame.copy()
                    if recorder is not None:
                        count = recorder.frame_counts.get(cam_name, 0)
                        cv2.circle(display, (30, 30), 12, (0, 0, 255), -1)
                        cv2.putText(display, f"REC {count}",
                                    (50, 38), cv2.FONT_HERSHEY_SIMPLEX,
                                    0.7, (0, 0, 255), 2)
                    cv2.imshow(cam_name, display)

            # 录制：送帧到写入队列
            if recorder is not None:
                for cam_name, frame in cam_frames.items():
                    recorder.put_frame(cam_name, frame)

            # 获取按键
            if show_display:
                key = cv2.waitKey(30) & 0xFF
            else:
                # 非阻塞读取终端输入
                key = 0
                if select.select([sys.stdin], [], [], 0.03)[0]:
                    key = ord(sys.stdin.read(1))

            if key == ord('s') and recorder is None:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                session_dir = os.path.join(args.save_dir, timestamp)
                recorder = MP4Recorder(session_dir, args.fps)
                recorder.start()
                print(f"\n>>> 开始录制 -> {session_dir}")

            elif key == ord('d') and recorder is not None:
                print(f"\n>>> 停止录制，等待写入完成...")
                counts = recorder.stop()
                total = sum(counts.values())
                print(f"    总计: {total} 帧 ({len(counts)} 个摄像头)")
                for cn, fc in counts.items():
                    print(f"    {cn}: {fc} 帧")
                recorder = None

            elif key == ord('x') and recorder is not None:
                print(f"\n>>> 丢弃当前录制...")
                recorder.discard()
                recorder = None
                print("    已丢弃，可按 's' 重新开始")

            elif key == ord('q'):
                if recorder is not None:
                    print(f"\n>>> 停止录制，等待写入完成...")
                    recorder.stop()
                break

    except KeyboardInterrupt:
        if recorder is not None:
            print(f"\n>>> 停止录制，等待写入完成...")
            recorder.stop()
        print("\n停止采集。")
    finally:
        stop_event.set()
        if show_display:
            cv2.destroyAllWindows()
        else:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
        print("资源已释放。")


if __name__ == "__main__":
    main()
