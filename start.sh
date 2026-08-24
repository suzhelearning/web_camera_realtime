#!/usr/bin/env bash
# 启动 Pico 摄像头实时查看器 (pixi 环境, 前台运行, Ctrl+C 退出)
# 用法: ./start.sh [盒子IP]    默认 192.168.50.21 (无线)
set -u
cd "$(dirname "$0")"

SERVER_IP="${1:-192.168.50.21}"

pkill -f live_view.py 2>/dev/null
sleep 1

exec env DISPLAY=:1 pixi run python live_view.py --server "$SERVER_IP"
