#!/usr/bin/env bash
# 启动 Pico 摄像头实时查看器 (main 分支): 直连 3588 数据发送端查看相机流
# 用法: ./start.sh [盒子IP]    默认 192.168.50.21 (无线)
set -u
cd "$(dirname "$0")"

SERVER_IP="${1:-192.168.50.21}"

pkill -f live_view.py 2>/dev/null
sleep 1

env DISPLAY=:1 pixi run python live_view.py --server "$SERVER_IP" &
VIEWER=$!
trap 'kill $VIEWER 2>/dev/null; pkill -f live_view.py 2>/dev/null' INT TERM
wait
