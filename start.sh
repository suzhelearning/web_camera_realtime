/usr/local/bin/pixi  run python -u stream_to_pico.py \
    --device /dev/video0 \
    --encoder libx264 \
    --width 1024 \
    --height 768 \
    --fps 30 \
    --view-mode mono \
    --bitrate 4M
