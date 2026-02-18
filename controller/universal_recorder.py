#!/usr/bin/env python3
"""
Universal Recorder - Record Android interactions in two modes.

Mode 1: RAW (--mode raw)
    Records real finger touches via getevent in real-time.
    Like using your actual finger - captures every down/move/up.
    Optionally auto-annotates each gesture with the element it hit.

Mode 2: ANNOTATED (--mode annotated)
    Interactive mode where you perform actions on device,
    then the recorder captures the UI state and maps your touch
    to the exact element. Produces more stable/portable recordings.

Both modes save to the same Universal Recording Format (.urf.json)
so they can be mixed, edited, and replayed together.

Usage:
    # Record raw touches (like screen recording with fingers)
    universal-recorder.py --mode raw -p com.example.app -o /work/my-recording.urf.json

    # Record with element annotation (interactive)
    universal-recorder.py --mode annotated -p com.example.app -o /work/my-recording.urf.json

    # Record raw + auto-annotate elements on each gesture
    universal-recorder.py --mode raw --annotate -p com.example.app
"""

from __future__ import annotations

import argparse
import re
import select
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from universal_format import (
    Frame, RecordingMeta, RecordingSettings, UniversalRecording, KEY_NAMES,
)
from element_interactor import (
    AdbError, ElementInfo, adb_cmd, run_adb, get_ui_xml,
    parse_all_elements, get_current_app,
)


EVENT_PATTERN = re.compile(
    r"\[\s*(\d+\.\d+)\]\s+(\S+):\s+(\S+)\s+(\S+)\s+([0-9a-fA-F]+)"
)
POSITION_CODES = {"ABS_MT_POSITION_X", "ABS_MT_POSITION_Y"}
MULTITOUCH_CODES = {"ABS_MT_SLOT", "ABS_MT_TRACKING_ID"}

# Common Android key codes mapping (from getevent to Android keycode)
# Reference: https://source.android.com/devices/input/keyboard-devices
KEY_CODE_MAP = {
    "KEY_BACK": 4,           # KEYCODE_BACK
    "KEY_HOME": 3,           # KEYCODE_HOME
    "KEY_MENU": 82,          # KEYCODE_MENU
    "KEY_POWER": 26,         # KEYCODE_POWER
    "KEY_VOLUMEUP": 24,      # KEYCODE_VOLUME_UP
    "KEY_VOLUMEDOWN": 25,    # KEYCODE_VOLUME_DOWN
    "KEY_CAMERA": 27,        # KEYCODE_CAMERA
    "KEY_SEARCH": 84,        # KEYCODE_SEARCH
}


class UniversalRecorder:
    def __init__(
        self,
        serial: Optional[str],
        package: str,
        mode: str = "raw",
        auto_annotate: bool = False,
        output: Path = Path("/work/recording.urf.json"),
    ):
        self.serial = serial
        self.package = package
        self.mode = mode
        self.auto_annotate = auto_annotate
        self.output = output

        self.recording = UniversalRecording()
        self.recording.meta.app_package = package
        self.recording.meta.device = serial or ""
        self.recording.meta.created = datetime.now().isoformat()

        self._running = False
        self._start_time: float = 0
        self._screen_w = 1080
        self._screen_h = 2400
        self._input_max_x = 0
        self._input_max_y = 0
        self._record_lock = threading.Lock()
        self._app_monitor_stop = threading.Event()
        self._app_monitor_thread: Optional[threading.Thread] = None
        self._last_app: Dict[str, str] = {"package": "", "activity": ""}

        # For raw recording
        self._last_x: Optional[int] = None
        self._last_y: Optional[int] = None
        self._touch_active = False
        self._gesture_buffer: List[Frame] = []
        self._pending_key_code: Optional[str] = None  # Track key down events

        # Multi-touch tracking
        self._current_slot: int = 0
        self._active_slots: Dict[int, Dict[str, Any]] = {}  # slot -> {x, y, tracking_id}
        self._gesture_start_slot_count: int = 0  # Number of fingers when gesture started

    # ---------- Setup ----------

    def _detect_screen_size(self) -> None:
        try:
            prefix = adb_cmd(self.serial)
            output = run_adb(prefix + ["shell", "wm", "size"])
            match = re.search(r"(\d+)x(\d+)", output)
            if match:
                self._screen_w = int(match.group(1))
                self._screen_h = int(match.group(2))
        except AdbError:
            pass
        self.recording.meta.screen_width = self._screen_w
        self.recording.meta.screen_height = self._screen_h
        print(f"Screen: {self._screen_w}x{self._screen_h}")

    def _detect_input_range(self) -> None:
        """Detect the input device coordinate range for scaling."""
        try:
            prefix = adb_cmd(self.serial)
            output = run_adb(prefix + ["shell", "getevent", "-p"])
            # Parse ABS_MT_POSITION_X/Y max values
            lines = output.splitlines()
            for i, line in enumerate(lines):
                if "ABS_MT_POSITION_X" in line and i + 1 < len(lines):
                    m = re.search(r"max\s+(\d+)", lines[i + 1])
                    if m:
                        self._input_max_x = int(m.group(1))
                elif "ABS_MT_POSITION_Y" in line and i + 1 < len(lines):
                    m = re.search(r"max\s+(\d+)", lines[i + 1])
                    if m:
                        self._input_max_y = int(m.group(1))
        except AdbError:
            pass

        if self._input_max_x == 0:
            self._input_max_x = self._screen_w
        if self._input_max_y == 0:
            self._input_max_y = self._screen_h

        print(f"Input range: X=0..{self._input_max_x}, Y=0..{self._input_max_y}")

    def _scale_coord(self, raw_x: int, raw_y: int) -> tuple[int, int]:
        """Scale getevent coordinates to screen coordinates."""
        if self._input_max_x == self._screen_w:
            return raw_x, raw_y
        x = int(raw_x * self._screen_w / self._input_max_x)
        y = int(raw_y * self._screen_h / self._input_max_y)
        return x, y

    def _annotate_point(self, x: int, y: int) -> Optional[Dict[str, Any]]:
        """Get element info at a screen coordinate."""
        try:
            xml = get_ui_xml(self.serial)
            elements = parse_all_elements(xml)
            # Find the smallest clickable element containing this point
            candidates = []
            for el in elements:
                b = el.bounds
                if b[0] <= x <= b[2] and b[1] <= y <= b[3]:
                    if el.resource_id or el.text or el.clickable:
                        candidates.append(el)
            if not candidates:
                return None
            # Smallest area = most specific
            candidates.sort(key=lambda e: (e.bounds[2]-e.bounds[0]) * (e.bounds[3]-e.bounds[1]))
            el = candidates[0]
            return {
                "resource_id": el.resource_id,
                "text": el.text,
                "class": el.cls,
                "bounds": list(el.bounds),
            }
        except Exception:
            return None

    def _get_all_text_at_point(self, x: int, y: int) -> Set[str]:
        """Get all text content from elements at a screen coordinate (including children)."""
        try:
            xml = get_ui_xml(self.serial)
            root = ET.fromstring(xml)

            def parse_bounds(bounds_str: str) -> tuple:
                match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_str)
                if not match:
                    return (0, 0, 0, 0)
                return tuple(int(x) for x in match.groups())

            def contains_point(bounds: tuple, px: int, py: int) -> bool:
                return bounds[0] <= px <= bounds[2] and bounds[1] <= py <= bounds[3]

            def collect_text(node: ET.Element, texts: Set[str]) -> None:
                """Recursively collect all text from nodes."""
                if node.tag == "node":
                    text = node.attrib.get("text", "").strip()
                    if text:
                        texts.add(text)
                for child in node:
                    collect_text(child, texts)

            # Find all elements containing the point
            texts: Set[str] = set()

            def walk(node: ET.Element, inside_target: bool = False) -> None:
                if node.tag == "node":
                    bounds_str = node.attrib.get("bounds", "")
                    bounds = parse_bounds(bounds_str)
                    if contains_point(bounds, x, y):
                        # Collect text from this node and all its children
                        collect_text(node, texts)
                        inside_target = True

                # Continue walking even if we found a match (to find overlapping elements)
                if not inside_target:
                    for child in node:
                        walk(child, inside_target)

            walk(root)
            return texts
        except Exception:
            return set()

    def _display_all_elements_categorized(self) -> None:
        """Display all UI elements on screen, categorized by element type."""
        try:
            print("\n" + "="*80)
            print("🔍 ALL SCREEN ELEMENTS (Categorized by Type)")
            print("="*80)

            xml = get_ui_xml(self.serial)
            elements = parse_all_elements(xml)

            if not elements:
                print("⚠️  No elements found on screen")
                print("="*80 + "\n")
                return

            # Categorize elements by class type
            categories: Dict[str, List[ElementInfo]] = {}
            for el in elements:
                # Skip elements without useful information
                if not el.text and not el.resource_id and not el.clickable:
                    continue

                # Simplify class name (remove package prefix)
                class_name = el.cls.split(".")[-1] if el.cls else "Unknown"
                if class_name not in categories:
                    categories[class_name] = []
                categories[class_name].append(el)

            if not categories:
                print("⚠️  No meaningful elements found (no text, resource_id, or clickable elements)")
                print("="*80 + "\n")
                return

            # Display each category
            total_elements = 0
            for class_name in sorted(categories.keys()):
                element_list = categories[class_name]
                print(f"\n📌 {class_name} ({len(element_list)} elements)")
                print("-" * 80)

                for idx, el in enumerate(element_list, 1):
                    total_elements += 1

                    # Build element description
                    parts = []
                    if el.text:
                        parts.append(f'text="{el.text}"')
                    if el.resource_id:
                        # Show only the last part of resource_id for readability
                        short_id = el.resource_id.split("/")[-1] if "/" in el.resource_id else el.resource_id
                        parts.append(f'id="{short_id}"')
                    if el.clickable:
                        parts.append("clickable")
                    if not el.enabled:
                        parts.append("disabled")

                    # Format bounds
                    bounds_str = f"[{el.bounds[0]},{el.bounds[1]}]-[{el.bounds[2]},{el.bounds[3]}]"

                    # Print element info
                    info = ", ".join(parts) if parts else "no text/id"
                    print(f"  {idx:2d}. {info}")
                    print(f"      bounds: {bounds_str} | center: ({el.center[0]}, {el.center[1]})")

            print("\n" + "="*80)
            print(f"✅ Total: {total_elements} elements across {len(categories)} categories")
            print("="*80 + "\n")

        except Exception as e:
            print(f"\n⚠️  Error displaying elements: {e}\n")

    def _display_elements_as_grid(self) -> None:
        """Display all UI elements on screen in a visual grid frame format."""
        try:
            print("\n" + "="*100)
            print("╔" + "═"*98 + "╗")
            print("║" + " "*34 + "📱 SCREEN TEXT ELEMENTS (GRID VIEW)" + " "*32 + "║")
            print("╚" + "═"*98 + "╝")

            xml = get_ui_xml(self.serial)
            elements = parse_all_elements(xml)

            if not elements:
                print("⚠️  No elements found on screen")
                print("="*100 + "\n")
                return

            # Filter elements with text content
            text_elements = [el for el in elements if el.text and el.text.strip()]

            if not text_elements:
                print("⚠️  No elements with text content found")
                print("="*100 + "\n")
                return

            # Sort by vertical position, then horizontal
            text_elements.sort(key=lambda e: (e.center[1], e.center[0]))

            # Display in grid format
            for idx, el in enumerate(text_elements, 1):
                x1, y1, x2, y2 = el.bounds
                width = x2 - x1
                height = y2 - y1

                # Create element box
                print("┌" + "─" * 94 + "┐")
                print(f"│ [{idx:2d}] {el.text[:90]:90s} │")
                print(f"│      ID: {el.resource_id[:86]:86s} │")
                print(f"│      Bounds: [{x1:4d}, {y1:4d}] - [{x2:4d}, {y2:4d}] | Size: {width:4d}x{height:4d} px │")
                print(f"│      Class: {el.cls.split('.')[-1]:90s} │")
                print("└" + "─" * 94 + "┘")

            print("\n" + "="*100)
            print(f"✅ Total: {len(text_elements)} text elements displayed")
            print("="*100 + "\n")

        except Exception as e:
            print(f"\n⚠️  Error displaying grid: {e}\n")

    def _ms_since_start(self) -> int:
        return int((time.time() - self._start_time) * 1000)

    def _add_frame(self, frame: Frame) -> Frame:
        with self._record_lock:
            return self.recording.add_frame(frame)

    def _start_app_monitor(self) -> None:
        if self._app_monitor_thread:
            return
        try:
            self._last_app = get_current_app(self.serial)
        except AdbError:
            self._last_app = {"package": "", "activity": ""}
        self._app_monitor_stop.clear()
        self._app_monitor_thread = threading.Thread(target=self._monitor_app_changes, daemon=True)
        self._app_monitor_thread.start()

    def _stop_app_monitor(self) -> None:
        if not self._app_monitor_thread:
            return
        self._app_monitor_stop.set()
        self._app_monitor_thread.join(timeout=2)
        self._app_monitor_thread = None

    def _monitor_app_changes(self) -> None:
        while not self._app_monitor_stop.is_set():
            try:
                current = get_current_app(self.serial)
            except AdbError:
                current = {"package": "", "activity": ""}

            if (
                current.get("package") != self._last_app.get("package")
                or current.get("activity") != self._last_app.get("activity")
            ):
                self._record_app_transition(self._last_app, current)
                self._last_app = current

            self._app_monitor_stop.wait(0.5)

    def _record_app_transition(self, previous: Dict[str, str], current: Dict[str, str]) -> None:
        ts = self._ms_since_start()
        prev_pkg = previous.get("package", "")
        prev_act = previous.get("activity", "")
        curr_pkg = current.get("package", "")
        curr_act = current.get("activity", "")

        if prev_pkg:
            close_frame = Frame(
                t=ts,
                type="app",
                action="close",
                package=prev_pkg,
                activity=prev_act,
            )
            self._add_frame(close_frame)

        if curr_pkg:
            open_frame = Frame(
                t=ts,
                type="app",
                action="open",
                package=curr_pkg,
                activity=curr_act,
            )
            self._add_frame(open_frame)

    # ---------- Raw Recording ----------

    def record_raw(self) -> None:
        """Record raw touches from getevent stream."""
        self.recording.meta.recording_mode = "raw" if not self.auto_annotate else "mixed"
        self._detect_screen_size()
        self._detect_input_range()

        prefix = adb_cmd(self.serial)
        cmd = prefix + ["shell", "getevent", "-lt"]

        print(f"\nRecording RAW touches...")
        print(f"{'(with element annotation) ' if self.auto_annotate else ''}Press Ctrl+C to stop.\n")

        self._start_time = time.time()
        self._running = True
        self._start_app_monitor()

        try:
            with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, bufsize=1) as proc:
                if not proc.stdout:
                    raise RuntimeError("Failed to open getevent stream")
                try:
                    self._process_getevent_stream(proc.stdout)
                except KeyboardInterrupt:
                    print("\nStopping...")
                finally:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
        except FileNotFoundError:
            print("adb not found.", file=sys.stderr)
            return

        self._flush_gesture_buffer()
        self._stop_app_monitor()
        self._finalize()

    def _process_getevent_stream(self, stream) -> None:
        pending_update = False

        for line in stream:
            if not self._running:
                break

            match = EVENT_PATTERN.match(line.strip())
            if not match:
                continue

            timestamp_raw, device, ev_type, code, value_hex = match.groups()

            # Handle multi-touch slot and tracking ID
            if ev_type == "EV_ABS" and code in MULTITOUCH_CODES:
                value = int(value_hex, 16)
                if code == "ABS_MT_SLOT":
                    self._current_slot = value
                elif code == "ABS_MT_TRACKING_ID":
                    if value == 0xFFFFFFFF:  # -1 in signed 32-bit, means finger lifted
                        if self._current_slot in self._active_slots:
                            del self._active_slots[self._current_slot]
                    else:
                        if self._current_slot not in self._active_slots:
                            self._active_slots[self._current_slot] = {}
                        self._active_slots[self._current_slot]["tracking_id"] = value
                continue

            # Handle touch events
            if ev_type == "EV_ABS" and code in POSITION_CODES:
                value = int(value_hex, 16)
                if code == "ABS_MT_POSITION_X":
                    self._last_x = value
                    if self._current_slot not in self._active_slots:
                        self._active_slots[self._current_slot] = {}
                    self._active_slots[self._current_slot]["x"] = value
                else:
                    self._last_y = value
                    if self._current_slot not in self._active_slots:
                        self._active_slots[self._current_slot] = {}
                    self._active_slots[self._current_slot]["y"] = value
                pending_update = True
                continue

            # Handle key events (hardware buttons)
            if ev_type == "EV_KEY" and code in KEY_CODE_MAP:
                value = int(value_hex, 16)
                if value == 1:  # Key down
                    self._pending_key_code = code
                elif value == 0 and self._pending_key_code == code:  # Key up (complete press)
                    keycode = KEY_CODE_MAP[code]
                    key_name = KEY_NAMES.get(keycode, code)
                    frame = Frame(
                        id=self.recording.next_id(),
                        t=self._ms_since_start(),
                        type="key",
                        keycode=keycode,
                        key_name=key_name,
                    )
                    self._add_frame(frame)
                    unix_ts = int(time.time())
                    print(f"\r  Key: {key_name} ({keycode})  [ts:{unix_ts}]")
                    self._pending_key_code = None
                continue

            if ev_type == "EV_SYN" and code == "SYN_REPORT":
                # Detect finger lift: touch was active but no more active slots
                finger_lifted = self._touch_active and len(self._active_slots) == 0

                if pending_update and self._last_x is not None and self._last_y is not None:
                    sx, sy = self._scale_coord(self._last_x, self._last_y)

                    if not self._touch_active:
                        action = "down"
                        self._touch_active = True
                        self._gesture_start_slot_count = len(self._active_slots)
                    else:
                        action = "move"

                    frame = Frame(
                        id=self.recording.next_id(),
                        t=self._ms_since_start(),
                        type="touch",
                        action=action,
                        x=sx, y=sy,
                    )
                    self._add_frame(frame)
                    self._gesture_buffer.append(frame)

                    if action == "down":
                        unix_ts = int(time.time())
                        sys.stdout.write(f"\r  Touch DOWN ({sx}, {sy})  [ts:{unix_ts}]")
                        sys.stdout.flush()

                if finger_lifted:
                    # Use the last known coordinates for the up event
                    sx = self._last_x or 0
                    sy = self._last_y or 0
                    sx, sy = self._scale_coord(sx, sy)

                    frame = Frame(
                        id=self.recording.next_id(),
                        t=self._ms_since_start(),
                        type="touch",
                        action="up",
                        x=sx, y=sy,
                    )
                    self._add_frame(frame)
                    self._gesture_buffer.append(frame)
                    self._touch_active = False

                    unix_ts = int(time.time())
                    sys.stdout.write(f"\r  Touch UP   ({sx}, {sy})  [ts:{unix_ts}]")
                    sys.stdout.flush()

                    # Flush gesture and optionally annotate
                    self._flush_gesture_buffer()

                pending_update = False

    def _flush_gesture_buffer(self) -> None:
        if not self._gesture_buffer:
            return

        start = self._gesture_buffer[0]
        end = self._gesture_buffer[-1]

        # Use Euclidean distance for more accurate gesture classification
        # Manhattan distance misclassifies diagonal movements
        dx = end.x - start.x
        dy = end.y - start.y
        dist = (dx * dx + dy * dy) ** 0.5

        # Also compute max displacement from start across all frames
        # This catches swipes that return near the starting point
        max_dist = 0.0
        for f in self._gesture_buffer:
            fdx = f.x - start.x
            fdy = f.y - start.y
            d = (fdx * fdx + fdy * fdy) ** 0.5
            if d > max_dist:
                max_dist = d

        dur = end.t - start.t

        # Use the greater of endpoint distance and max displacement
        effective_dist = max(dist, max_dist)

        # Determine gesture type
        # Check for two-finger tap: started with 2+ fingers and small movement
        is_two_finger_tap = self._gesture_start_slot_count >= 2 and effective_dist < 30 and dur < 500

        if is_two_finger_tap:
            gesture_type = "two_finger_tap"
        elif effective_dist < 30 and dur < 500:
            gesture_type = "tap"
        elif effective_dist < 30:
            gesture_type = "long_press"
        else:
            gesture_type = "swipe"

        # Auto-annotate if enabled
        element_info = None
        unix_ts = int(time.time())

        if self.auto_annotate:
            if gesture_type in ("tap", "long_press", "two_finger_tap"):
                element_info = self._annotate_point(start.x, start.y) if gesture_type != "two_finger_tap" else None

                if gesture_type == "two_finger_tap":
                    print(f"  → {gesture_type} at ({start.x}, {start.y})  [ts:{unix_ts}]")
                    # Display all elements in grid format for two-finger tap
                    self._display_elements_as_grid()
                elif gesture_type == "long_press":
                    if element_info:
                        rid = element_info.get("resource_id", "")
                        txt = element_info.get("text", "")
                        label = txt or (rid.split("/")[-1] if rid else "")
                        print(f"  → {gesture_type} on [{label}] at ({start.x}, {start.y})  [ts:{unix_ts}]")
                    else:
                        print(f"  → {gesture_type} at ({start.x}, {start.y}) (no element)  [ts:{unix_ts}]")
                    # Display all elements on screen, categorized by type
                    self._display_all_elements_categorized()
                else:  # tap
                    if element_info:
                        rid = element_info.get("resource_id", "")
                        txt = element_info.get("text", "")
                        label = txt or (rid.split("/")[-1] if rid else "")
                        print(f"  → {gesture_type} on [{label}]  [ts:{unix_ts}]")
                    else:
                        print(f"  → {gesture_type} at ({start.x}, {start.y}) (no element)  [ts:{unix_ts}]")
            elif gesture_type == "swipe":
                print(f"  → swipe ({start.x},{start.y}) → ({end.x},{end.y}) {dur}ms  [ts:{unix_ts}]")

        # For swipe, use the actual last touch position for end coordinates
        # For taps, x2/y2 match start
        if gesture_type == "swipe":
            # Find the frame with max distance from start for the "true" endpoint
            # In case the swipe path curves, use the last frame's position
            swipe_end_x = end.x
            swipe_end_y = end.y
        else:
            swipe_end_x = start.x
            swipe_end_y = start.y

        # Add gesture summary frame
        gesture_frame = Frame(
            id=self.recording.next_id(),
            t=start.t,
            type="gesture",
            action=gesture_type,
            x=start.x, y=start.y,
            x2=swipe_end_x, y2=swipe_end_y,
            duration_ms=dur,
            element=element_info,
        )
        self._add_frame(gesture_frame)
        self._gesture_buffer.clear()
        self._gesture_start_slot_count = 0

    # ---------- Annotated (Interactive) Recording ----------

    def record_annotated(self) -> None:
        """Interactive recording - user acts on device, recorder captures elements."""
        self.recording.meta.recording_mode = "annotated"
        self._detect_screen_size()

        print("\n=== Universal Recorder (Annotated Mode) ===")
        print(f"Package: {self.package}")
        print()
        print("ทำ action บนมือถือ แล้วใช้คำสั่งที่นี่:")
        print()
        print("  t              - จับหน้าจอ แล้วเลือก element ที่จะกด")
        print("  tap X Y        - เพิ่ม tap ที่พิกัดตรงๆ")
        print("  swipe X1 Y1 X2 Y2 [ms]  - เพิ่ม swipe")
        print("  e              - แสดง elements ทั้งหมดบนหน้าจอ (แยกตามประเภท)")
        print("  s [stage]      - เพิ่ม screenshot step (+ส่ง)")
        print("  w [secs]       - เพิ่ม wait")
        print("  k [keycode]    - เพิ่ม key event (4=BACK 3=HOME)")
        print("  o              - เพิ่ม open app")
        print("  c              - เพิ่ม close app")
        print("  sh <command>   - เพิ่ม shell command")
        print("  m [note]       - เพิ่ม marker / bookmark")
        print("  l              - ดูรายการ frames ทั้งหมด")
        print("  u              - ลบ frame ล่าสุด")
        print("  q / Ctrl+C     - หยุดและบันทึก")
        print()

        self._start_time = time.time()
        self._running = True
        self._start_app_monitor()

        try:
            while self._running:
                try:
                    raw_input = input("rec> ").strip()
                except EOFError:
                    break

                if not raw_input:
                    continue

                parts = raw_input.split()
                cmd = parts[0].lower()
                args = parts[1:]

                if cmd == "q":
                    break
                elif cmd == "t":
                    self._interactive_pick_element()
                elif cmd == "tap" and len(args) >= 2:
                    self._add_tap_coord(int(args[0]), int(args[1]))
                elif cmd == "swipe" and len(args) >= 4:
                    dur = int(args[4]) if len(args) > 4 else 300
                    self._add_swipe(int(args[0]), int(args[1]), int(args[2]), int(args[3]), dur)
                elif cmd == "e":
                    # Show all elements on screen, categorized by type
                    self._display_all_elements_categorized()
                elif cmd == "s":
                    stage = args[0] if args else f"screen{self.recording.next_id()}"
                    self._add_screenshot(stage)
                elif cmd == "w":
                    secs = float(args[0]) if args else 2.0
                    self._add_wait(int(secs * 1000))
                elif cmd == "k":
                    code = int(args[0]) if args else 4
                    self._add_key(code)
                elif cmd == "o":
                    self._add_app_action("open")
                elif cmd == "c":
                    self._add_app_action("close")
                elif cmd == "sh" and args:
                    self._add_shell(" ".join(args))
                elif cmd == "m":
                    note = " ".join(args) if args else ""
                    self._add_marker(note)
                elif cmd == "l":
                    self._list_frames()
                elif cmd == "u":
                    self._undo()
                else:
                    print(f"  คำสั่งไม่รู้จัก: {cmd}")

        except KeyboardInterrupt:
            print()

        self._running = False
        self._stop_app_monitor()
        self._finalize()

    def _interactive_pick_element(self) -> None:
        print("  กำลังดึง UI...")
        try:
            xml = get_ui_xml(self.serial)
            elements = parse_all_elements(xml)
        except AdbError as e:
            print(f"  ดึง UI ไม่ได้: {e}")
            return

        clickable = [
            el for el in elements
            if (el.clickable or el.resource_id or el.text) and el.visible
        ]

        if not clickable:
            print("  ไม่เจอ element ที่กดได้")
            coord = input("  ใส่พิกัด x,y หรือ Enter ข้าม: ").strip()
            if coord and "," in coord:
                x, y = coord.split(",", 1)
                self._add_tap_coord(int(x.strip()), int(y.strip()))
            return

        print(f"\n  พบ {len(clickable)} elements:")
        for i, el in enumerate(clickable[:40]):
            txt = f" '{el.text}'" if el.text else ""
            rid = f" {el.resource_id.split('/')[-1]}" if el.resource_id else ""
            cls = el.cls.split('.')[-1]
            print(f"    [{i:2d}] {cls}{rid}{txt}  @{el.center}")

        if len(clickable) > 40:
            print(f"    ... อีก {len(clickable) - 40} items")

        choice = input("\n  เลือก # (หรือ x,y หรือ Enter ข้าม): ").strip()
        if not choice:
            return

        if "," in choice:
            x, y = choice.split(",", 1)
            self._add_tap_coord(int(x.strip()), int(y.strip()))
        else:
            try:
                idx = int(choice)
                if 0 <= idx < len(clickable):
                    el = clickable[idx]
                    # Add as element frame with full info
                    f = Frame(
                        t=self._ms_since_start(),
                        type="element",
                        action="tap",
                        resource_id=el.resource_id,
                        text=el.text,
                        x=el.center[0], y=el.center[1],
                        timeout=10,
                        element={
                            "resource_id": el.resource_id,
                            "text": el.text,
                            "class": el.cls,
                            "bounds": list(el.bounds),
                        },
                    )
                    self._add_frame(f)
                    label = el.text or el.resource_id.split("/")[-1]
                    print(f"  + element tap: {label} @{el.center}")
                else:
                    print("  index ไม่ถูก")
            except ValueError:
                print("  input ไม่ถูก")

    def _add_tap_coord(self, x: int, y: int) -> None:
        f = Frame(t=self._ms_since_start(), type="gesture", action="tap", x=x, y=y, duration_ms=50)
        self._add_frame(f)
        print(f"  + tap ({x}, {y})")

    def _add_swipe(self, x1: int, y1: int, x2: int, y2: int, dur: int) -> None:
        f = Frame(t=self._ms_since_start(), type="gesture", action="swipe",
                  x=x1, y=y1, x2=x2, y2=y2, duration_ms=dur)
        self._add_frame(f)
        print(f"  + swipe ({x1},{y1})→({x2},{y2}) {dur}ms")

    def _add_screenshot(self, stage: str) -> None:
        f = Frame(t=self._ms_since_start(), type="screenshot", stage=stage, send=True)
        self._add_frame(f)
        print(f"  + screenshot: {stage}")

    def _add_wait(self, ms: int) -> None:
        f = Frame(t=self._ms_since_start(), type="wait", duration_ms=ms)
        self._add_frame(f)
        print(f"  + wait {ms}ms")

    def _add_key(self, keycode: int) -> None:
        name = KEY_NAMES.get(keycode, str(keycode))
        f = Frame(t=self._ms_since_start(), type="key", keycode=keycode, key_name=name)
        self._add_frame(f)
        print(f"  + key: {name} ({keycode})")

    def _add_app_action(self, action: str) -> None:
        f = Frame(t=self._ms_since_start(), type="app", action=action, package=self.package)
        self._add_frame(f)
        print(f"  + app {action}: {self.package}")

    def _add_shell(self, command: str) -> None:
        f = Frame(t=self._ms_since_start(), type="shell", command=command)
        self._add_frame(f)
        print(f"  + shell: {command}")

    def _add_marker(self, note: str) -> None:
        f = Frame(t=self._ms_since_start(), type="marker", note=note)
        self._add_frame(f)
        print(f"  + marker: {note or '(no note)'}")

    def _list_frames(self) -> None:
        frames = self.recording.frames
        if not frames:
            print("  ยังไม่มี frames")
            return

        # Show collapsed view (skip raw touch move events)
        display = []
        for f in frames:
            if f.type == "touch" and f.action == "move":
                continue
            display.append(f)

        print(f"\n  {len(frames)} frames ({len(display)} displayed):")
        for f in display[-30:]:
            status = "" if f.enabled else " [DISABLED]"
            t_sec = f.t / 1000.0
            if f.type == "touch":
                print(f"    #{f.id:3d} [{t_sec:7.1f}s] touch {f.action} ({f.x},{f.y}){status}")
            elif f.type == "gesture":
                el = ""
                if f.element and (f.element.get("text") or f.element.get("resource_id")):
                    el = f" [{f.element.get('text') or f.element.get('resource_id','').split('/')[-1]}]"
                print(f"    #{f.id:3d} [{t_sec:7.1f}s] {f.action} ({f.x},{f.y}){el}{status}")
            elif f.type == "element":
                label = f.text or f.resource_id.split("/")[-1]
                print(f"    #{f.id:3d} [{t_sec:7.1f}s] element tap: {label}{status}")
            elif f.type == "app":
                print(f"    #{f.id:3d} [{t_sec:7.1f}s] app {f.action}: {f.package}{status}")
            elif f.type == "key":
                print(f"    #{f.id:3d} [{t_sec:7.1f}s] key: {f.key_name}{status}")
            elif f.type == "screenshot":
                print(f"    #{f.id:3d} [{t_sec:7.1f}s] screenshot: {f.stage}{status}")
            elif f.type == "wait":
                print(f"    #{f.id:3d} [{t_sec:7.1f}s] wait {f.duration_ms}ms{status}")
            elif f.type == "shell":
                print(f"    #{f.id:3d} [{t_sec:7.1f}s] shell: {f.command}{status}")
            elif f.type == "marker":
                print(f"    #{f.id:3d} [{t_sec:7.1f}s] marker: {f.note}{status}")
        print()

    def _undo(self) -> None:
        if self.recording.frames:
            removed = self.recording.frames.pop()
            print(f"  ลบ: #{removed.id} {removed.type} {removed.action}")
        else:
            print("  ไม่มีอะไรให้ลบ")

    def _finalize(self) -> None:
        self.recording.meta.total_duration_ms = self.recording.total_duration()
        if self.recording.frames:
            self.recording.save(self.output)
            total = len(self.recording.frames)
            gestures = len([f for f in self.recording.frames if f.type in ("gesture", "element")])
            touches = len([f for f in self.recording.frames if f.type == "touch"])
            keys = len([f for f in self.recording.frames if f.type == "key"])
            others = total - gestures - touches - keys
            dur_s = self.recording.meta.total_duration_ms / 1000
            print(f"\nบันทึกแล้ว: {self.output}")
            print(f"  Frames: {total} (gestures={gestures}, raw_touches={touches}, keys={keys}, other={others})")
            print(f"  Duration: {dur_s:.1f}s")
            print(f"\nReplay:  universal-player.py {self.output}")
            print(f"Edit:    flow-editor.py {self.output}")
        else:
            print("\nไม่มี frames - ไม่ได้บันทึก")


# ---------- CLI ----------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Universal Recorder - Record Android interactions (raw touches or annotated elements)"
    )
    parser.add_argument("--mode", "-m", choices=["raw", "annotated"], default="raw",
                        help="Recording mode (default: raw)")
    parser.add_argument("--package", "-p", required=True, help="App package name")
    parser.add_argument("--output", "-o", type=Path, default=Path("/work/recording.urf.json"),
                        help="Output file path")
    parser.add_argument("-s", "--serial", help="ADB device serial")
    parser.add_argument("--annotate", "-a", action="store_true",
                        help="Auto-annotate raw touches with element info")
    parser.add_argument("--name", "-n", default="", help="Recording name")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    recorder = UniversalRecorder(
        serial=args.serial,
        package=args.package,
        mode=args.mode,
        auto_annotate=args.annotate,
        output=args.output,
    )

    if args.name:
        recorder.recording.meta.name = args.name

    if args.mode == "raw":
        recorder.record_raw()
    else:
        recorder.record_annotated()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
