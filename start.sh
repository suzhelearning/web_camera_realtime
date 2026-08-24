#!/usr/bin/env bash
# 启动 Pico 图像桥: 订阅 3588 数据发送端 (默认 192.168.50.83:13579) 的流,
# mono-to-sbs 处理后连接 Pico XRoboToolkit 推流 (监听 13579 等待其连入).
# 用法: ./start.sh [额外参数, 如 --source-host 192.168.50.83 --preview]
set -u
cd "$(dirname "$0")"

pkill -f stream_bridge_pico.py 2>/dev/null
sleep 1

exec pixi run python stream_bridge_pico.py "$@"
