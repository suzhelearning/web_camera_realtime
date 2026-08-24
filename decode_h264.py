#!/usr/bin/env python3
"""Decode captured Annex-B H.264 and dump JPEG frames + stream info."""
import sys

import av


def main(path, out_prefix, interval_s=2.0, max_frames=12):
    container = av.open(path)
    stream = container.streams.video[0]
    fps = float(stream.average_rate or 30.0)
    step = max(1, int(round(fps * interval_s)))
    print(f"[info] {stream.codec_context.width}x{stream.codec_context.height} "
          f"fps={fps:.1f} step={step}", flush=True)
    got = 0
    for i, frame in enumerate(container.decode(stream)):
        if got >= max_frames:
            break
        if i % step == 0:
            img = frame.to_image()
            out = f"{out_prefix}_{got:02d}.jpg"
            img.save(out, quality=92)
            print(f"[save] frame#{i} -> {out}", flush=True)
            got += 1
    print(f"[done] {got} jpg", flush=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2],
         float(sys.argv[3]) if len(sys.argv) > 3 else 2.0)
