#!/usr/bin/env python3
"""
Flow Recorder - Record user interactions and generate a reusable YAML flow.

Usage:
    flow-recorder.py --package com.example.app [--output flow.yaml] [-s DEVICE]

Workflow:
1. Recorder watches the device screen in real-time
2. User performs actions on the device manually
3. Recorder captures each UI state change and maps touch events to elements
4. On Ctrl+C, generates a YAML flow file that can be replayed

Recording modes:
- element: Map touches to nearest UI element (resource-id/text) - more stable
- coordinate: Record raw coordinates - works when elements have no IDs
- hybrid: Record element when possible, fall back to coordinates
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

from element_interactor import (
    adb_cmd,
    run_adb,
    get_ui_xml,
    parse_all_elements,
    find_element,
    get_current_app,
    ElementInfo,
    AdbError,
)

GETEVENT_PATTERN = re.compile(
    r"\[\s*([\d.]+)\]\s+(\S+)\s+(\S+)\s+([0-9a-fA-F]+)"
)


class RecordedAction:
    def __init__(self, action: str, **kwargs: Any):
        self.action = action
        self.timestamp = datetime.now().isoformat()
        self.props: Dict[str, Any] = kwargs

    def to_step_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"action": self.action}
        if "description" in self.props:
            d["description"] = self.props["description"]
        for k, v in self.props.items():
            if k != "description" and v is not None:
                d[k] = v
        return d


class FlowRecorder:
    def __init__(
        self,
        serial: Optional[str],
        package: str,
        mode: str = "hybrid",
        screenshot_dir: str = "/img/recordings",
    ):
        self.serial = serial
        self.package = package
        self.mode = mode
        self.screenshot_dir = Path(screenshot_dir)
        self.actions: List[RecordedAction] = []
        self._running = False
        self._touch_x: Optional[int] = None
        self._touch_y: Optional[int] = None
        self._last_ui_xml: str = ""
        self._last_elements: List[ElementInfo] = []
        self._screen_width = 1080
        self._screen_height = 2400

    def _get_screen_size(self) -> None:
        """Get device screen resolution."""
        try:
            prefix = adb_cmd(self.serial)
            output = run_adb(prefix + ["shell", "wm", "size"])
            match = re.search(r"(\d+)x(\d+)", output)
            if match:
                self._screen_width = int(match.group(1))
                self._screen_height = int(match.group(2))
                print(f"Screen: {self._screen_width}x{self._screen_height}")
        except AdbError:
            pass

    def _refresh_ui(self) -> None:
        """Refresh the UI hierarchy."""
        try:
            self._last_ui_xml = get_ui_xml(self.serial)
            self._last_elements = parse_all_elements(self._last_ui_xml)
        except (AdbError, Exception) as e:
            print(f"  [warn] UI refresh failed: {e}")

    def _find_nearest_element(self, x: int, y: int) -> Optional[ElementInfo]:
        """Find the most specific (smallest) clickable element containing the point."""
        candidates: List[ElementInfo] = []
        for el in self._last_elements:
            b = el.bounds
            if b[0] <= x <= b[2] and b[1] <= y <= b[3]:
                if el.clickable or el.resource_id or el.text:
                    candidates.append(el)

        if not candidates:
            return None

        # Pick the smallest area (most specific element)
        def area(el: ElementInfo) -> int:
            return (el.bounds[2] - el.bounds[0]) * (el.bounds[3] - el.bounds[1])

        candidates.sort(key=area)
        return candidates[0]

    def _record_touch(self, x: int, y: int) -> None:
        """Process a detected touch and record the action."""
        # Scale from getevent coordinates to screen coordinates
        # getevent often reports in device-specific ranges
        self._refresh_ui()

        element = self._find_nearest_element(x, y) if self.mode in ("element", "hybrid") else None

        if element and (element.resource_id or element.text):
            desc_parts = []
            if element.text:
                desc_parts.append(f"'{element.text}'")
            if element.resource_id:
                desc_parts.append(element.resource_id.split("/")[-1])
            desc = f"Tap {' '.join(desc_parts)}"

            action = RecordedAction(
                "tap_element",
                resource_id=element.resource_id or None,
                text=element.text or None,
                description=desc,
                timeout=10,
            )
            print(f"  [record] tap_element: {desc}")
        else:
            action = RecordedAction(
                "tap_coord",
                x=x,
                y=y,
                description=f"Tap at ({x}, {y})",
            )
            print(f"  [record] tap_coord: ({x}, {y})")

        self.actions.append(action)

    def start_interactive(self) -> None:
        """Interactive recording mode - user manually triggers captures."""
        self._get_screen_size()
        self._running = True

        print("\n=== Flow Recorder (Interactive Mode) ===")
        print(f"Package: {self.package}")
        print(f"Mode: {self.mode}")
        print()
        print("Commands:")
        print("  t          - Capture current screen & pick element to tap")
        print("  s [stage]  - Add screenshot step")
        print("  w [secs]   - Add wait step")
        print("  k [code]   - Add key event step (4=back, 3=home)")
        print("  o          - Add open_app step")
        print("  c          - Add close_app step")
        print("  l          - List recorded steps")
        print("  u          - Undo last step")
        print("  q / Ctrl+C - Stop and save")
        print()

        try:
            while self._running:
                try:
                    cmd = input("rec> ").strip()
                except EOFError:
                    break

                if not cmd:
                    continue

                parts = cmd.split(maxsplit=1)
                command = parts[0].lower()
                arg = parts[1] if len(parts) > 1 else ""

                if command == "q":
                    break
                elif command == "t":
                    self._interactive_tap()
                elif command == "s":
                    stage = arg or f"screen{len(self.actions) + 1}"
                    self.actions.append(RecordedAction("screenshot", stage=stage, send=True,
                                                       description=f"Screenshot: {stage}"))
                    print(f"  Added screenshot step: {stage}")
                elif command == "w":
                    secs = float(arg) if arg else 2.0
                    self.actions.append(RecordedAction("wait", duration=secs,
                                                       description=f"Wait {secs}s"))
                    print(f"  Added wait: {secs}s")
                elif command == "k":
                    code = int(arg) if arg else 4
                    key_names = {3: "HOME", 4: "BACK", 26: "POWER", 82: "MENU"}
                    name = key_names.get(code, str(code))
                    self.actions.append(RecordedAction("key_event", keycode=code,
                                                       description=f"Key: {name}"))
                    print(f"  Added key event: {name} ({code})")
                elif command == "o":
                    self.actions.append(RecordedAction("open_app",
                                                       description="Launch app"))
                    print("  Added open_app")
                elif command == "c":
                    self.actions.append(RecordedAction("close_app",
                                                       description="Close app"))
                    print("  Added close_app")
                elif command == "l":
                    self._list_steps()
                elif command == "u":
                    if self.actions:
                        removed = self.actions.pop()
                        print(f"  Removed: {removed.action} - {removed.props.get('description', '')}")
                    else:
                        print("  No steps to undo")
                else:
                    print(f"  Unknown command: {command}")

        except KeyboardInterrupt:
            print()

        self._running = False

    def _interactive_tap(self) -> None:
        """Capture current UI and let user pick an element."""
        print("  Refreshing UI...")
        self._refresh_ui()

        if not self._last_elements:
            print("  No elements found! Is the device connected?")
            return

        # Show clickable elements with text or resource-id
        clickable = [
            el for el in self._last_elements
            if (el.clickable or el.resource_id or el.text) and el.visible
        ]

        if not clickable:
            print("  No interactable elements found.")
            coord = input("  Enter coordinates (x,y) or skip: ").strip()
            if coord:
                parts = coord.split(",")
                if len(parts) == 2:
                    x, y = int(parts[0].strip()), int(parts[1].strip())
                    self._record_touch(x, y)
            return

        print(f"\n  Found {len(clickable)} interactable elements:")
        for i, el in enumerate(clickable[:30]):  # Limit display
            text_info = f" text='{el.text}'" if el.text else ""
            rid_info = f" id={el.resource_id.split('/')[-1]}" if el.resource_id else ""
            print(f"    [{i:2d}] {el.cls.split('.')[-1]}{rid_info}{text_info}  @{el.center}")

        if len(clickable) > 30:
            print(f"    ... and {len(clickable) - 30} more")

        choice = input("\n  Pick element # (or x,y coords, or skip): ").strip()
        if not choice:
            return

        if "," in choice:
            parts = choice.split(",")
            x, y = int(parts[0].strip()), int(parts[1].strip())
            self._record_touch(x, y)
        else:
            try:
                idx = int(choice)
                if 0 <= idx < len(clickable):
                    el = clickable[idx]
                    self._record_touch(el.center[0], el.center[1])
                else:
                    print("  Invalid index")
            except ValueError:
                print("  Invalid input")

    def _list_steps(self) -> None:
        if not self.actions:
            print("  No steps recorded yet.")
            return
        print(f"\n  Recorded {len(self.actions)} steps:")
        for i, a in enumerate(self.actions):
            desc = a.props.get("description", a.action)
            print(f"    {i + 1}. [{a.action}] {desc}")
        print()

    def generate_yaml(self, output_path: Path) -> None:
        """Generate a YAML flow file from recorded actions."""
        if yaml is None:
            # Fallback: write as JSON
            self._generate_json_fallback(output_path)
            return

        flow_data = {
            "flow": {
                "name": f"Recorded Flow - {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                "app_package": self.package,
                "app_activity": "",
                "device": self.serial or "",
                "loop": True,
                "loop_count": 0,
                "loop_delay": 5,
                "screenshot_dir": str(self.screenshot_dir),
                "send_to": {
                    "method": "cp",
                    "path": "/img/sent",
                },
            },
            "on_error": {
                "notify": True,
                "notify_method": "stdout",
                "screenshot_on_error": True,
                "max_retries": 3,
                "retry_delay": 2,
                "pause_on_error": False,
            },
            "steps": [a.to_step_dict() for a in self.actions],
        }

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            yaml.dump(flow_data, default_flow_style=False, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        print(f"\nFlow saved to: {output_path}")
        print(f"Steps recorded: {len(self.actions)}")
        print(f"\nRun with: automation-engine.py {output_path}")

    def _generate_json_fallback(self, output_path: Path) -> None:
        """Generate as JSON when PyYAML is not available."""
        json_path = output_path.with_suffix(".json")
        flow_data = {
            "flow": {
                "name": f"Recorded Flow - {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                "app_package": self.package,
                "loop": True,
                "loop_count": 0,
                "loop_delay": 5,
                "screenshot_dir": str(self.screenshot_dir),
            },
            "steps": [a.to_step_dict() for a in self.actions],
        }
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(flow_data, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nFlow saved (JSON fallback): {json_path}")
        print("Note: Install PyYAML for YAML output: pip install pyyaml")


# ---------- CLI ----------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record Android interactions into a reusable flow")
    parser.add_argument("--package", "-p", required=True, help="App package name")
    parser.add_argument("--output", "-o", type=Path, default=Path("/work/recorded-flow.yaml"),
                        help="Output flow file")
    parser.add_argument("-s", "--serial", help="ADB device serial")
    parser.add_argument("--mode", choices=["element", "coordinate", "hybrid"], default="hybrid",
                        help="Recording mode (default: hybrid)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    recorder = FlowRecorder(
        serial=args.serial,
        package=args.package,
        mode=args.mode,
    )
    recorder.start_interactive()

    if recorder.actions:
        recorder.generate_yaml(args.output)
    else:
        print("No actions recorded.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
