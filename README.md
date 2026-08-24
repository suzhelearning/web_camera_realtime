# Web Camera Realtime

USB 摄像头实时串流工具集，支持录制、WebRTC 浏览器串流、Pico VR 直连串流。

## 快速开始

### 依赖安装

```bash
pip install aiortc aiohttp opencv-python numpy av
```

### Pico VR 直连串流 (推荐)

直接将摄像头视频流推送到 Pico XRoboToolkit 内显示，无需浏览器，不占用 XR session。

```bash
python stream_to_pico.py --device /dev/video0 --encoder libx264 --width 1280 --height 960
```

使用步骤:
1. PC 端运行上述命令，终端会显示本机 IP
2. Pico 上打开 XRoboToolkit
3. 选择视频源 "ZED Mini"
4. 点击 "Listen"，输入 PC 的 IP 地址
5. 视频自动显示在 VR 中

### WebRTC 浏览器串流

```bash
# 基础版
python stream.py --usb-camera /dev/video0

# RK3588 硬件加速版
python stream_rk3588.py --usb-camera /dev/video0 --width 1280 --height 960
```

启动后在浏览器访问 `http://<设备IP>:8080`。

### 录制

```bash
# 列出设备
python record.py --list

# 录制
python record.py --usb-cameras /dev/video0:fisheye
```

录制快捷键: `s` 开始 / `d` 停止保存 / `x` 丢弃 / `q` 退出

## 常用命令

```bash
# Pico VR 直连 (推荐)
python stream_to_pico.py --device /dev/video0 --encoder libx264 --width 1280 --height 960

# Pico VR 直连 (RK3588 硬件编码)
python stream_to_pico.py --device /dev/video0 --encoder h264_rkmpp --width 1280 --height 960

# Pico VR 直连 (更高码率)
python stream_to_pico.py --device /dev/video0 --encoder libx264 --width 1280 --height 960 --bitrate 8M

# WebRTC 浏览器串流
python stream_rk3588.py --usb-camera /dev/video0 --width 1280 --height 960

# WebRTC VR 模式 (需要 HTTPS)
python stream_vr.py --usb-camera /dev/video0 --width 1280 --height 960
```

## 文件说明

| 文件 | 说明 |
|------|------|
| `stream_to_pico.py` | Pico VR 直连串流 (推荐，兼容 XRoboToolkit MediaDecoder) |
| `stream.py` | WebRTC 串流基础版 |
| `stream_rk3588.py` | WebRTC 串流 RK3588 硬件加速版 |
| `stream_vr.py` | WebRTC VR 模式 (WebXR 球面渲染) |
| `stream_hand.py` | WebRTC 串流 + BrainCo 手控制 |
| `record.py` | 本地摄像头录制工具 |
| `brainco_vr_trigger_listener.py` | BrainCo 灵巧手 trigger 监听 |

## 架构

### Pico VR 直连模式 (stream_to_pico.py)

```
PC/RK3588                              Pico VR
┌──────────────────┐    TCP 13579     ┌──────────────────┐
│ stream_to_pico   │◄────────────────│ XRoboToolkit     │
│                  │  OPEN_CAMERA     │                  │
│ USB Camera       │                  │ MediaDecoder     │
│   ↓              │   TCP 12345     │   ↓              │
│ H.264 Encode     │────────────────►│ HW Decode        │
│ [4B len][data]   │  H.264 stream   │   ↓              │
│                  │                  │ VR Display       │
└──────────────────┘                  └──────────────────┘
```

协议格式: 每个 H.264 packet 前加 4 字节大端长度头 `[size(4B BE)][h264 data]`
