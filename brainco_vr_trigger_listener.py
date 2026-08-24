#!/usr/bin/env python3

import argparse
import json
import shlex
import signal
import struct
import subprocess
import sys
from pathlib import Path

import zmq


HEADER_SIZE = 1280
TOPIC_TRIGGER = b"trigger"
FINGER_COUNT = 6


def parse_vector(text: str) -> list[float]:
    parts = [item.strip() for item in text.split(",")]
    if len(parts) != FINGER_COUNT:
        raise argparse.ArgumentTypeError(
            f"expected {FINGER_COUNT} comma-separated values, got: {text}"
        )
    try:
        values = [float(item) for item in parts]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    for value in values:
        if value < 0.0 or value > 1.0:
            raise argparse.ArgumentTypeError("finger values must be in [0, 1]")
    return values


def parse_mask(text: str) -> list[int]:
    parts = [item.strip() for item in text.split(",")]
    if len(parts) != FINGER_COUNT:
        raise argparse.ArgumentTypeError(
            f"expected {FINGER_COUNT} comma-separated values, got: {text}"
        )
    try:
        values = [int(item) for item in parts]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    for value in values:
        if value not in (0, 1):
            raise argparse.ArgumentTypeError("mask values must be 0 or 1")
    return values


def apply_mask(open_pose: list[float], close_pose: list[float], mask: list[int]) -> list[float]:
    return [close_pose[i] if mask[i] else open_pose[i] for i in range(FINGER_COUNT)]


class Listener:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.running = True
        self.left_closed = None
        self.right_closed = None

        self.left_open_pose = list(args.left_initial)
        self.right_open_pose = list(args.right_initial)
        self.left_closed_pose = apply_mask(
            self.left_open_pose, list(args.left_closed), list(args.left_close_mask)
        )
        self.right_closed_pose = apply_mask(
            self.right_open_pose, list(args.right_closed), list(args.right_close_mask)
        )

        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.SUB)
        self.socket.setsockopt(zmq.SUBSCRIBE, TOPIC_TRIGGER)
        self.socket.setsockopt(zmq.CONFLATE, 1)
        self.socket.setsockopt(zmq.RCVHWM, 1)
        self.socket.setsockopt(zmq.RCVTIMEO, 1000)
        self.socket.connect(f"tcp://{args.zmq_host}:{args.zmq_port}")

    def close(self) -> None:
        self.running = False
        self.socket.close(linger=0)
        self.context.term()

    def parse_pose_message(self, message: bytes) -> tuple[float | None, float | None]:
        if not message.startswith(TOPIC_TRIGGER):
            return None, None

        header_start = len(TOPIC_TRIGGER)
        header_end = header_start + HEADER_SIZE
        if len(message) < header_end:
            return None, None

        header_bytes = message[header_start:header_end]
        payload = message[header_end:]
        header_json = header_bytes.rstrip(b"\x00")
        if not header_json:
            return None, None

        header = json.loads(header_json.decode("utf-8"))
        offset = 0
        left_trigger = None
        right_trigger = None

        for field in header.get("fields", []):
            name = field.get("name")
            dtype = field.get("dtype")
            shape = field.get("shape", [1])
            count = 1
            for dim in shape:
                count *= int(dim)

            if dtype == "f32":
                byte_count = 4 * count
                if offset + byte_count > len(payload):
                    break
                if name == "left_trigger" and count >= 1:
                    left_trigger = struct.unpack_from("<f", payload, offset)[0]
                elif name == "right_trigger" and count >= 1:
                    right_trigger = struct.unpack_from("<f", payload, offset)[0]
            elif dtype in ("f64", "i64"):
                byte_count = 8 * count
            elif dtype in ("i32", "u32"):
                byte_count = 4 * count
            elif dtype in ("u8", "bool"):
                byte_count = count
            else:
                return None, None

            offset += byte_count

        return left_trigger, right_trigger

    def send_pose(self, hand: str, pose: list[float], reason: str) -> None:
        cmd = [
            str(self.args.brainco_ctrl),
            "--hand",
            hand,
            "--net",
            self.args.network_interface,
            "set",
            *[f"{value:.4f}" for value in pose],
        ]
        subprocess.run(cmd, check=True)
        print(
            f"[listener] {hand} {reason}: {' '.join(f'{value:.2f}' for value in pose)}",
            flush=True,
        )

    def update_hand(
        self,
        hand: str,
        trigger_value: float | None,
        current_state: bool | None,
        open_pose: list[float],
        closed_pose: list[float],
    ) -> bool | None:
        if trigger_value is None:
            return current_state

        next_state = trigger_value >= self.args.threshold
        if current_state is None or next_state != current_state:
            pose = closed_pose if next_state else open_pose
            reason = "closed" if next_state else "opened"
            self.send_pose(hand, pose, reason)
            return next_state
        return current_state

    def initialize_pose(self) -> None:
        startup_pose = self.args.startup_pose
        left_pose = self.left_open_pose if startup_pose != "closed" else self.left_closed_pose
        right_pose = self.right_open_pose if startup_pose != "closed" else self.right_closed_pose
        self.send_pose("left", left_pose, "startup")
        self.send_pose("right", right_pose, "startup")
        self.left_closed = startup_pose == "closed"
        self.right_closed = startup_pose == "closed"

    def run(self) -> int:
        self.initialize_pose()
        print(
            "[listener] listening on "
            f"tcp://{self.args.zmq_host}:{self.args.zmq_port}, "
            f"threshold={self.args.threshold:.2f}",
            flush=True,
        )
        print(
            "[listener] left open/closed = "
            f"{self.left_open_pose} / {self.left_closed_pose}",
            flush=True,
        )
        print(
            "[listener] right open/closed = "
            f"{self.right_open_pose} / {self.right_closed_pose}",
            flush=True,
        )

        while self.running:
            try:
                message = self.socket.recv()
            except zmq.Again:
                continue
            except KeyboardInterrupt:
                break

            try:
                left_trigger, right_trigger = self.parse_pose_message(message)
                self.left_closed = self.update_hand(
                    "left",
                    left_trigger,
                    self.left_closed,
                    self.left_open_pose,
                    self.left_closed_pose,
                )
                self.right_closed = self.update_hand(
                    "right",
                    right_trigger,
                    self.right_closed,
                    self.right_open_pose,
                    self.right_closed_pose,
                )
            except subprocess.CalledProcessError as exc:
                print(f"[listener] command failed: {exc}", file=sys.stderr, flush=True)
                return exc.returncode or 1
            except Exception as exc:
                print(f"[listener] decode error: {exc}", file=sys.stderr, flush=True)

        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Listen to Pico trigger values over ZMQ and drive BrainCo hand poses."
    )
    parser.add_argument("--zmq-host", default="127.0.0.1", help="Pico ZMQ publisher host")
    parser.add_argument(
        "--zmq-port",
        type=int,
        default=6556,
        help="Dedicated Pico trigger ZMQ publisher port",
    )
    parser.add_argument(
        "--network-interface",
        default="eth0",
        help="DDS network interface passed to brainco_hand_ctrl",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.5,
        help="Trigger threshold above which the hand switches to closed pose",
    )
    parser.add_argument(
        "--startup-pose",
        choices=("initial", "closed"),
        default="initial",
        help="Pose applied once at startup before listening",
    )
    parser.add_argument(
        "--left-initial",
        type=parse_vector,
        default=parse_vector("0,0,0,0,0,0"),
        help="Initial/open pose for left hand, comma-separated 6 values in [0,1]",
    )
    parser.add_argument(
        "--right-initial",
        type=parse_vector,
        default=parse_vector("0,0,0,0,0,0"),
        help="Initial/open pose for right hand, comma-separated 6 values in [0,1]",
    )
    parser.add_argument(
        "--left-closed",
        type=parse_vector,
        default=parse_vector("1,1,1,1,1,1"),
        help="Closed target pose for left hand before mask is applied",
    )
    parser.add_argument(
        "--right-closed",
        type=parse_vector,
        default=parse_vector("1,1,1,1,1,1"),
        help="Closed target pose for right hand before mask is applied",
    )
    parser.add_argument(
        "--left-close-mask",
        type=parse_mask,
        default=parse_mask("1,1,1,1,1,1"),
        help="Left-hand closure mask, 1 means this finger follows closed pose",
    )
    parser.add_argument(
        "--right-close-mask",
        type=parse_mask,
        default=parse_mask("1,1,1,1,1,1"),
        help="Right-hand closure mask, 1 means this finger follows closed pose",
    )
    parser.add_argument(
        "--brainco-ctrl",
        type=Path,
        default=Path(__file__).resolve().parent / "bin" / "brainco_hand_ctrl",
        help="Path to brainco_hand_ctrl binary",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    args.brainco_ctrl = args.brainco_ctrl.resolve()
    if not args.brainco_ctrl.exists():
        print(f"brainco_hand_ctrl not found: {args.brainco_ctrl}", file=sys.stderr)
        return 1

    listener = Listener(args)

    def handle_signal(_signum, _frame) -> None:
        listener.close()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        return listener.run()
    finally:
        listener.close()


if __name__ == "__main__":
    sys.exit(main())
