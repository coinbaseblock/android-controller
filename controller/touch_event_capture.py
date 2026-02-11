#!/usr/bin/env python3
"""
Capture touch events from an Android device via `adb shell getevent -lt` and
save them to JSON or CSV files for later analysis.

The script listens for `ABS_MT_POSITION_X`, `ABS_MT_POSITION_Y`, and
`SYN_REPORT` events to produce simplified touch actions (`down`, `move`, `up`).
It is designed to run inside the Docker `controller` container where `/work`
is mounted to the host, allowing the output file to be accessible directly on
the host machine.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional

EVENT_PATTERN = re.compile(
    r"\[\s*(\d+\.\d+)\]\s+(\S+):\s+(\S+)\s+(\S+)\s+([0-9a-fA-F]+)"
)
POSITION_CODES = {"ABS_MT_POSITION_X", "ABS_MT_POSITION_Y"}
DEFAULT_OUTPUT = "/work/touch-events.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Capture touch events via 'adb shell getevent -lt' and save them "
            "as simplified coordinates."
        )
    )
    parser.add_argument(
        "-o",
        "--output",
        default=DEFAULT_OUTPUT,
        help="Output file path (default: %(default)s)",
    )
    parser.add_argument(
        "-f",
        "--format",
        choices=["json", "csv"],
        help="Force output format (otherwise inferred from file extension)",
    )
    parser.add_argument(
        "-d",
        "--device",
        help="Target input device path (e.g., /dev/input/event2)",
    )
    parser.add_argument(
        "-s",
        "--serial",
        help="ADB serial to target a specific device",
    )
    return parser.parse_args()


def infer_format(output_path: Path, explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    suffix = output_path.suffix.lower()
    return "csv" if suffix == ".csv" else "json"


def build_adb_command(args: argparse.Namespace) -> List[str]:
    cmd = ["adb"]
    if args.serial:
        cmd.extend(["-s", args.serial])
    cmd.extend(["shell", "getevent", "-lt"])
    if args.device:
        cmd.append(args.device)
    return cmd


def parse_stream(lines: Iterable[str], device_filter: Optional[str]) -> List[Dict[str, object]]:
    events: List[Dict[str, object]] = []
    last_x: Optional[int] = None
    last_y: Optional[int] = None
    pending_update = False
    active = False

    for line in lines:
        match = EVENT_PATTERN.match(line.strip())
        if not match:
            continue

        timestamp_raw, device, ev_type, code, value_hex = match.groups()
        if device_filter and device != device_filter:
            continue

        if ev_type == "EV_ABS" and code in POSITION_CODES:
            value = int(value_hex, 16)
            if code == "ABS_MT_POSITION_X":
                last_x = value
            else:
                last_y = value
            pending_update = True
            continue

        if ev_type == "EV_SYN" and code == "SYN_REPORT":
            timestamp = float(timestamp_raw)
            if pending_update and last_x is not None and last_y is not None:
                action = "down" if not active else "move"
                active = True
                events.append(
                    {"timestamp": timestamp, "x": last_x, "y": last_y, "action": action}
                )
            elif active:
                events.append(
                    {"timestamp": timestamp, "x": last_x, "y": last_y, "action": "up"}
                )
                active = False
            pending_update = False

    return events


def write_output(events: List[Dict[str, object]], output_path: Path, fmt: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        output_path.write_text(json.dumps(events, indent=2), encoding="utf-8")
        return

    with output_path.open("w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=["timestamp", "x", "y", "action"])
        writer.writeheader()
        writer.writerows(events)


def main() -> int:
    args = parse_args()
    output_path = Path(args.output).expanduser()
    output_format = infer_format(output_path, args.format)
    adb_cmd = build_adb_command(args)
    events: List[Dict[str, object]] = []

    print(f"Running: {' '.join(adb_cmd)}", file=sys.stderr)
    print("Press Ctrl+C to stop capturing and write the output file.", file=sys.stderr)

    try:
        with subprocess.Popen(
            adb_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        ) as proc:
            if not proc.stdout:
                raise RuntimeError("Failed to open adb stdout stream")

            try:
                events = parse_stream(proc.stdout, args.device)
            except KeyboardInterrupt:
                print("\nStopping capture...", file=sys.stderr)
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()

            if proc.stderr:
                stderr_output = proc.stderr.read().strip()
                if stderr_output:
                    print(stderr_output, file=sys.stderr)
    except FileNotFoundError:
        print("adb not found. Ensure Android platform tools are installed in the container.", file=sys.stderr)
        return 1

    write_output(events, output_path, output_format)
    print(f"Saved {len(events)} events to {output_path} ({output_format.upper()}).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())