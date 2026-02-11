#!/usr/bin/env python3
"""
Replay recorded touch coordinates or element-based actions on an Android device via ADB.

Features
- Load touch logs produced by ``touch-event-capture.py`` (JSON/CSV) or element
  action logs containing ``resource_id`` / ``text`` keys with timestamps.
- For coordinate logs, collapse raw ``down``/``move``/``up`` events into tap or
  swipe gestures and send ``adb shell input tap|swipe`` accordingly.
- For element logs, map ``resource-id`` or ``text`` to the element center using
  the latest UI dump (JSON) and tap the resolved point.
- Respect real-world timing between steps with an optional speed multiplier or
  fixed delay override.
- Optionally capture a UI dump and/or screenshot after each step for validation.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

DEFAULT_UI_SOURCE = Path("/work/ui-dumps")
DEFAULT_VERIFY_DIR = Path("/work/replay-verification")


@dataclass
class ReplayStep:
    kind: str  # "tap" | "swipe"
    start_x: int
    start_y: int
    end_x: int
    end_y: int
    start_ts: float
    end_ts: float
    label: str


class ReplayError(RuntimeError):
    pass


# -------------------- Log loading --------------------

def load_log_entries(path: Path) -> List[Dict[str, object]]:
    if path.suffix.lower() == ".csv":
        return list(_load_csv(path))
    return json.loads(path.read_text(encoding="utf-8"))


def _load_csv(path: Path) -> Iterable[Dict[str, object]]:
    with path.open(newline="", encoding="utf-8") as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            yield {k: _coerce_value(v) for k, v in row.items()}


def _coerce_value(value: str) -> object:
    try:
        return float(value)
    except ValueError:
        return value


# -------------------- UI dump helpers --------------------

def resolve_ui_source(path: Optional[Path]) -> Optional[Path]:
    if path is None:
        return None
    if path.is_file():
        return path
    if not path.exists():
        return None

    json_files = sorted(path.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return json_files[0] if json_files else None


def load_ui_dump(path: Path) -> Dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if "nodes" not in payload or "lookup" not in payload:
        raise ReplayError("UI dump missing required fields 'nodes' or 'lookup'.")
    return payload


def find_element_center(payload: Dict[str, object], resource_id: str | None, text: str | None) -> tuple[int, int]:
    lookup: Dict[str, Dict[str, List[int]]] = payload.get("lookup", {})  # type: ignore[assignment]
    nodes: Sequence[Dict[str, object]] = payload.get("nodes", [])  # type: ignore[assignment]

    target_idx: Optional[int] = None
    if resource_id:
        indices = lookup.get("by_resource_id", {}).get(resource_id)
        if indices:
            target_idx = indices[0]
    if target_idx is None and text:
        indices = lookup.get("by_text", {}).get(text)
        if indices:
            target_idx = indices[0]

    if target_idx is None:
        raise ReplayError(
            f"Element not found in UI dump (resource-id='{resource_id}', text='{text}')"
        )

    node = nodes[target_idx]
    center = node.get("center") or {}
    x = center.get("x")  # type: ignore[assignment]
    y = center.get("y")  # type: ignore[assignment]
    if x is None or y is None:
        raise ReplayError("Element center is missing; ensure bounds are present in UI dump.")
    return int(x), int(y)


# -------------------- Touch collapsing --------------------

def collapse_touch_events(events: List[Dict[str, object]]) -> List[ReplayStep]:
    gestures: List[ReplayStep] = []
    buffer: List[Dict[str, object]] = []

    def flush_buffer() -> None:
        if not buffer:
            return
        start = buffer[0]
        end = buffer[-1]
        start_ts = float(start.get("timestamp", 0.0))
        end_ts = float(end.get("timestamp", start_ts))
        start_x = int(start["x"])
        start_y = int(start["y"])
        end_x = int(end.get("x", start_x))
        end_y = int(end.get("y", start_y))

        kind = "tap" if len(buffer) == 1 else "swipe"
        gestures.append(
            ReplayStep(
                kind=kind,
                start_x=start_x,
                start_y=start_y,
                end_x=end_x,
                end_y=end_y,
                start_ts=start_ts,
                end_ts=end_ts,
                label=f"touch-{len(gestures)+1}",
            )
        )
        buffer.clear()

    for event in sorted(events, key=lambda e: float(e.get("timestamp", 0.0))):
        action = str(event.get("action", "")).lower()
        if action == "down" and buffer:
            flush_buffer()
        buffer.append(event)
        if action == "up":
            flush_buffer()

    flush_buffer()
    return gestures


# -------------------- Element steps --------------------

def build_element_steps(entries: List[Dict[str, object]], ui_payload: Dict[str, object]) -> List[ReplayStep]:
    steps: List[ReplayStep] = []
    for idx, entry in enumerate(sorted(entries, key=lambda e: float(e.get("timestamp", 0.0)))):
        resource_id = entry.get("resource_id") or entry.get("resource-id")
        text = entry.get("text")
        timestamp = float(entry.get("timestamp", idx))
        x, y = find_element_center(ui_payload, str(resource_id) if resource_id else None, str(text) if text else None)
        steps.append(
            ReplayStep(
                kind="tap",
                start_x=x,
                start_y=y,
                end_x=x,
                end_y=y,
                start_ts=timestamp,
                end_ts=timestamp,
                label=f"element-{len(steps)+1}",
            )
        )
    return steps


# -------------------- ADB helpers --------------------

def adb_prefix(serial: Optional[str]) -> List[str]:
    return ["adb", "-s", serial] if serial else ["adb"]


def run_adb(cmd: List[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise ReplayError(result.stderr.strip() or "adb command failed")


def send_gesture(prefix: List[str], step: ReplayStep, speed: float) -> None:
    if step.kind == "tap":
        cmd = prefix + ["shell", "input", "tap", str(step.start_x), str(step.start_y)]
    else:
        duration_ms = max(1, int((step.end_ts - step.start_ts) * 1000 / max(speed, 0.0001)))
        cmd = prefix + [
            "shell",
            "input",
            "swipe",
            str(step.start_x),
            str(step.start_y),
            str(step.end_x),
            str(step.end_y),
            str(duration_ms),
        ]
    print(f"→ {step.label}: {' '.join(cmd)}")
    run_adb(cmd)


def capture_verification(prefix: List[str], mode: str, output_dir: Path, step_idx: int) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    base_name = f"step{step_idx:03d}-{timestamp}"

    if mode in {"ui", "both"}:
        remote_xml = "/sdcard/replay_window_dump.xml"
        run_adb(prefix + ["shell", "uiautomator", "dump", remote_xml])
        run_adb(prefix + ["pull", remote_xml, str(output_dir / f"{base_name}.xml")])

    if mode in {"screenshot", "both"}:
        remote_png = "/sdcard/replay_screen.png"
        run_adb(prefix + ["shell", "screencap", "-p", remote_png])
        run_adb(prefix + ["pull", remote_png, str(output_dir / f"{base_name}.png")])


# -------------------- Timing helpers --------------------

def compute_delay(prev_end: Optional[float], next_start: float, speed: float, fixed: Optional[float]) -> float:
    if fixed is not None:
        return max(0.0, fixed)
    if prev_end is None:
        return 0.0
    raw_delay = max(0.0, next_start - prev_end)
    return raw_delay / max(speed, 0.0001)


# -------------------- CLI --------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay touch or element logs via ADB")
    parser.add_argument("log", type=Path, help="Path to touch or element log (JSON/CSV)")
    parser.add_argument(
        "--ui-source",
        type=Path,
        default=DEFAULT_UI_SOURCE,
        help=(
            "UI dump source (file or directory). Required for element logs. "
            "Default: /work/ui-dumps (picks latest JSON in the directory)."
        ),
    )
    parser.add_argument("-s", "--serial", help="ADB serial/ip:port")
    parser.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="Speed multiplier (2.0 = 2x faster timing between steps)",
    )
    parser.add_argument(
        "--fixed-delay",
        type=float,
        help="Override delay between steps with a fixed number of seconds",
    )
    parser.add_argument(
        "--verify",
        choices=["none", "ui", "screenshot", "both"],
        default="none",
        help="Optional validation after each step",
    )
    parser.add_argument(
        "--verify-dir",
        type=Path,
        default=DEFAULT_VERIFY_DIR,
        help="Where to store validation outputs (when --verify is enabled)",
    )
    return parser.parse_args()


# -------------------- Main --------------------

def main() -> int:
    args = parse_args()
    log_entries = load_log_entries(args.log)

    has_touch = any("x" in e and "y" in e for e in log_entries)
    has_element = any(e.get("resource_id") or e.get("resource-id") or e.get("text") for e in log_entries)

    if has_touch and has_element:
        raise ReplayError("Mixed touch and element entries are not supported in a single log.")

    ui_payload: Optional[Dict[str, object]] = None
    if has_element:
        ui_path = resolve_ui_source(args.ui_source)
        if not ui_path:
            raise ReplayError("UI dump not found. Provide --ui-source pointing to a JSON file or directory.")
        ui_payload = load_ui_dump(ui_path)

    if has_touch:
        steps = collapse_touch_events(log_entries)
    else:
        if ui_payload is None:
            raise ReplayError("Element logs require a UI dump to resolve coordinates.")
        steps = build_element_steps(log_entries, ui_payload)

    if not steps:
        print("No replayable steps found in the log.")
        return 0

    prefix = adb_prefix(args.serial)
    prev_end = None

    print(f"Loaded {len(steps)} steps. Starting replay (speed={args.speed}, fixed_delay={args.fixed_delay}).")

    for idx, step in enumerate(steps, start=1):
        delay = compute_delay(prev_end, step.start_ts, args.speed, args.fixed_delay)
        if delay > 0:
            print(f"Waiting {delay:.3f}s before step {idx} ({step.label})...")
            time.sleep(delay)

        send_gesture(prefix, step, args.speed)

        if args.verify != "none":
            capture_verification(prefix, args.verify, args.verify_dir, idx)

        prev_end = step.end_ts

    print("Replay finished.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReplayError as exc:
        print(f"Replay failed: {exc}", file=sys.stderr)
        raise SystemExit(1)