#!/usr/bin/env python3
"""
Capture a UI hierarchy dump and a matching screenshot from an Android device via ADB.

- Runs `uiautomator dump` over `exec-out` to fetch the XML without temporary files.
- Runs `adb shell screencap -p /sdcard/screen.png && adb pull ...` to save the
  screenshot alongside the UI dump.
- Uses the same timestamp/stage prefix for both outputs so they can be correlated
  easily during analysis.
"""

from __future__ import annotations

import argparse
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List

DEFAULT_OUTPUT_DIR = Path("/work/ui-dumps")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Dump UI hierarchy and capture a screenshot with matching timestamp/stage"
        )
    )
    parser.add_argument(
        "-s", "--serial", help="ADB serial (or ip:port) if multiple devices are attached"
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory to save outputs (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "-g",
        "--stage",
        default="stage",
        help="Stage/name to include in filenames (default: %(default)s)",
    )
    parser.add_argument(
        "-t",
        "--timestamp",
        help=(
            "Optional timestamp override (format: YYYYmmdd-HHMMSS). If omitted, the "
            "current time is used."
        ),
    )
    return parser.parse_args()


def adb_prefix(serial: str | None) -> List[str]:
    return ["adb", "-s", serial] if serial else ["adb"]


def run_checked(cmd: List[str]) -> str:
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return result.stdout


def capture_ui_dump(prefix: List[str]) -> str:
    cmd = prefix + ["exec-out", "uiautomator", "dump", "/dev/tty"]
    return run_checked(cmd)


def capture_screenshot(prefix: List[str], destination: Path) -> None:
    remote_path = "/sdcard/screen.png"
    shell_cmd = prefix + ["shell", "screencap", "-p", remote_path]
    run_checked(shell_cmd)
    pull_cmd = prefix + ["pull", remote_path, str(destination)]
    run_checked(pull_cmd)


def main() -> int:
    args = parse_args()
    timestamp = args.timestamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"{timestamp}-{args.stage}" if args.stage else timestamp

    args.output_dir.mkdir(parents=True, exist_ok=True)
    ui_path = args.output_dir / f"{base}.xml"
    screenshot_path = args.output_dir / f"{base}.png"

    prefix = adb_prefix(args.serial)

    print(f"Capturing UI dump to {ui_path}...")
    ui_output = capture_ui_dump(prefix)
    ui_path.write_text(ui_output, encoding="utf-8")

    print(f"Capturing screenshot to {screenshot_path}...")
    capture_screenshot(prefix, screenshot_path)

    print("Done.")
    print(f"UI dump: {ui_path}")
    print(f"Screenshot: {screenshot_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())