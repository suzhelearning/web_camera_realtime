#!/usr/bin/env bash
# 3588 数据发送端: 采集 USB 相机 → 单画面 H.264 → TCP 推流 (13579)
# 前台运行: 实时显示相机帧率与推流统计, Ctrl+C 退出并清理
# 用法: ./start.sh   (清理残留实例与打开相机需要 sudo, 当前机 sudo -n 免密)
set -euo pipefail
cd "$(dirname "$0")"

# 清除残留实例 (含 root 启动的旧进程), 避免 Address already in use
sudo -n pkill -f "[s]tream_to_pico" 2>/dev/null || true
sleep 1

exec sudo -n .pixi/envs/default/bin/python -u stream_to_pico.py \
    --device /dev/video0 \
    --encoder libx264 \
    --width 1024 \
    --height 768 \
    --fps 30 \
    --view-mode mono \
    --bitrate 4M
