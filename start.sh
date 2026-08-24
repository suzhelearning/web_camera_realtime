#!/usr/bin/env bash
# 一键启动 Pico 图像链路 (pc 端):
#   1. stream_bridge_pico.py   订阅 3588 数据发送端(默认 .21:13579),
#      mono-to-sbs 处理后监听 13579 等待 Pico XRoboToolkit 连入
#   2. fake Pico 信号          sim_pico.py 冒充 XRoboToolkit 收流(默认模式)
#   3. --viewer 模式           改用 live_view.py 显示窗口查看流
# 用法: ./start.sh [--viewer] [stream_bridge_pico.py 额外参数透传]
set -u
cd "$(dirname "$0")"

MODE="fake"
EXTRA=()
for a in "$@"; do
  if [ "$a" = "--viewer" ]; then
    MODE="viewer"
  else
    EXTRA+=("$a")
  fi
done

pkill -f stream_bridge_pico.py 2>/dev/null
pkill -f sim_pico.py 2>/dev/null
pkill -f live_view.py 2>/dev/null
sleep 1

# 1. pc 桥 (订阅 3588 -> SBS 处理 -> 等 Pico 连入推流)
pixi run python stream_bridge_pico.py "${EXTRA[@]}" &
BRIDGE=$!
sleep 3

if [ "$MODE" = "viewer" ]; then
  # 2. 本机查看窗口: 模拟 XRoboToolkit 连本机桥 13579
  env DISPLAY=:1 pixi run python live_view.py \
      --server 127.0.0.1 --server-port 13579 \
      --width 2048 --height 768 --fps 30 &
  VIEWER=$!
  trap 'pkill -f stream_bridge_pico.py; pkill -f live_view.py' INT TERM
  wait
fi

# 2. fake Pico 信号 (收流保存 /tmp/fake_pico.h264)
pixi run python sim_pico.py --server 127.0.0.1 --server-port 13579 \
    --width 2048 --height 768 --fps 30 --ip 127.0.0.1 \
    --duration 86400 --out /tmp/fake_pico.h264 &
FAKE=$!
echo "[launch] pc 桥 + fake Pico 信号已启动 (Ctrl+C 退出; 收流: /tmp/fake_pico.h264)"

trap 'kill $BRIDGE $FAKE 2>/dev/null; pkill -f stream_bridge_pico.py; pkill -f sim_pico.py; wait 2>/dev/null' INT TERM
wait
echo "[launch] 已退出"
