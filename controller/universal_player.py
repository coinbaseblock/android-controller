#!/usr/bin/env python3
"""
Universal Player - Replay recordings from the Universal Recording Format.

Plays back BOTH raw touches AND scripted actions from the same file.
Supports mixed recordings seamlessly.

Replay modes:
  - raw:    Replay raw touch events exactly as recorded (sendevent)
  - smart:  Use gesture summaries + element lookups (more reliable)
  - auto:   Use gestures when available, fall back to raw touches

Features:
  - Speed control (--speed 0.5 = half speed, 2.0 = double)
  - Time offset adjustments per-frame
  - Loop support (from recording settings or --loop flag)
  - Skip disabled frames
  - Obstruction detection and auto-dismiss
  - Error handling with retry/skip/pause/stop

Usage:
    # Play at normal speed
    universal-player.py /work/recording.urf.json

    # Play 2x speed with loop
    universal-player.py /work/recording.urf.json --speed 2.0 --loop

    # Play only gesture/element frames (skip raw touches)
    universal-player.py /work/recording.urf.json --replay-mode smart

    # Dry run
    universal-player.py /work/recording.urf.json --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from universal_format import Frame, UniversalRecording
from element_interactor import (
    AdbError, ElementNotFoundError, ElementObstructedError,
    adb_cmd, run_adb,
    launch_app, force_stop_app, take_screenshot,
    tap_element, send_tap, send_swipe, send_key_event,
    wait_for_element, get_ui_xml, parse_all_elements,
)

# Optional debug server
try:
    from debug_server import DebugServer
except ImportError:
    DebugServer = None  # type: ignore[assignment,misc]

# Playback logger
try:
    from playback_logger import PlaybackLogger, build_ctrl_details
except ImportError:
    PlaybackLogger = None  # type: ignore[assignment,misc]
    build_ctrl_details = None  # type: ignore[assignment]


class PlaybackState:
    """State shared with debug server."""
    def __init__(self) -> None:
        self.status: str = "idle"
        self.current_loop: int = 0
        self.current_step: int = 0
        self.current_step_desc: str = ""
        self.total_loops: int = 0
        self.total_steps: int = 0
        self.last_screenshot: str = ""
        self.last_error: str = ""
        self.last_error_screenshot: str = ""
        self.history: List[Dict[str, Any]] = []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "current_loop": self.current_loop,
            "current_step": self.current_step,
            "current_step_desc": self.current_step_desc,
            "total_loops": self.total_loops,
            "total_steps": self.total_steps,
            "last_screenshot": self.last_screenshot,
            "last_error": self.last_error,
            "last_error_screenshot": self.last_error_screenshot,
            "history_count": len(self.history),
            "recent_history": self.history[-20:],
        }


class UniversalPlayer:
    def __init__(
        self,
        recording: UniversalRecording,
        recording_path: Optional[Path] = None,
        replay_mode: str = "auto",
        speed: float = 1.0,
        loop: Optional[bool] = None,
        loop_count: Optional[int] = None,
        dry_run: bool = False,
        debug_port: int = 0,
        loop_offset: int = 0,
    ):
        self.recording = recording
        self.recording_path = recording_path
        self.replay_mode = replay_mode
        self.speed = speed
        self.dry_run = dry_run
        self.loop_offset = loop_offset
        self.state = PlaybackState()

        # Loop settings: CLI overrides > recording settings
        settings = recording.settings
        self.loop = loop if loop is not None else settings.loop
        self.loop_count = loop_count if loop_count is not None else settings.loop_count
        self.loop_delay = settings.loop_delay
        self.on_error = settings.on_error
        self.max_retries = settings.max_retries
        self.retry_delay = settings.retry_delay

        self._serial = recording.meta.device or None
        self._running = False
        self._debug_server: Any = None

        # Buffer for raw touch sequences (down -> move -> ... -> up)
        self._touch_buffer: List[Frame] = []

        # Prepare frames based on replay mode
        self._frames = self._prepare_frames()
        self.state.total_steps = len(self._frames)

        # JSON playback logger
        self._logger: Any = None
        if recording_path and PlaybackLogger is not None:
            meta = recording.meta
            self._logger = PlaybackLogger(
                recording_path=recording_path,
                recording_name=meta.name,
                app_package=meta.app_package,
                device=meta.device,
                screen_width=meta.screen_width,
                screen_height=meta.screen_height,
                replay_mode=replay_mode,
                speed=speed,
                total_frames=len(self._frames),
            )

        if debug_port > 0 and DebugServer is not None:
            self._debug_server = DebugServer(
                self.state, port=debug_port, playback_logger=self._logger,
            )

    @property
    def serial(self) -> Optional[str]:
        return self._serial if self._serial else None

    def _prepare_frames(self) -> List[Frame]:
        """Select frames based on replay mode."""
        all_frames = self.recording.get_enabled_frames()

        if self.replay_mode == "raw":
            # Only raw touches (skip gesture summaries to avoid duplicates)
            return [f for f in all_frames if f.type != "gesture"]

        elif self.replay_mode == "smart":
            # Only gestures + elements + control frames (no raw touches)
            return [f for f in all_frames if f.type != "touch"]

        else:  # auto
            # Prefer gesture frames over raw touches
            has_gestures = any(f.type == "gesture" for f in all_frames)
            if has_gestures:
                return [f for f in all_frames if f.type != "touch"]
            else:
                return all_frames

    def log(self, msg: str, level: str = "INFO") -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        icons = {"INFO": " ", "OK": "+", "WARN": "!", "ERROR": "X", "STEP": ">"}
        icon = icons.get(level, " ")
        line = f"[{ts}] {icon} {msg}"
        print(line, flush=True)
        self.state.history.append({"ts": ts, "level": level, "msg": msg})

    def play(self) -> None:
        """Start playback."""
        meta = self.recording.meta
        self.log(f"Recording: {meta.name}")
        self.log(f"Mode: {self.replay_mode}, Speed: {self.speed}x, Loop: {self.loop}")
        self.log(f"Frames: {len(self._frames)}, Duration: {meta.total_duration_ms/1000:.1f}s")

        if self._debug_server:
            self._debug_server.start()
            self.log(f"Debug server: port {self._debug_server.port}")

        if self._logger:
            self.log(f"JSON log: {self._logger.log_path}")
            self.log(f"Log viewer: {self._logger.viewer_path}")

        self._running = True
        loop_idx = 0

        try:
            while self._running:
                loop_idx += 1
                effective_loop = loop_idx + self.loop_offset
                self.state.current_loop = effective_loop
                self.state.status = "running"

                if self.loop_count > 0 and loop_idx > self.loop_count:
                    break

                self.log(f"=== Loop {effective_loop} ===")
                success = self._play_one_loop(effective_loop)

                if not self.loop:
                    break

                if self._running and self.loop_delay > 0:
                    self.log(f"Waiting {self.loop_delay}s before next loop...")
                    time.sleep(self.loop_delay)

        except KeyboardInterrupt:
            self.log("Stopped by user.", "WARN")
        finally:
            self._running = False
            self.state.status = "idle"
            if self._debug_server:
                self._debug_server.stop()
            self.log("Player stopped.")

    def _play_one_loop(self, loop_idx: int) -> bool:
        if self._logger:
            self._logger.start_loop(loop_idx)

        # Record the wall-clock moment this loop started.  All frame delays are
        # calculated as absolute offsets from this point so that execution time
        # of each frame does not accumulate into the next frame's delay.
        #
        # Subtract the first frame's timestamp so playback starts immediately
        # instead of waiting through the initial offset (caused by subprocess
        # startup, ADB latency, or time before the user's first action).
        # New recordings are already normalized to t=0, but older recordings
        # may still carry an initial offset.
        initial_offset = (self._frames[0].t / 1000.0) / self.speed if self._frames else 0
        # Use perf_counter for high-resolution monotonic timing to avoid
        # drift from system clock adjustments (NTP, suspend, etc.).
        playback_start = time.perf_counter() - initial_offset
        paused_total = 0.0  # seconds spent in pause state (excluded from playback clock)

        for i, frame in enumerate(self._frames):
            if not self._running:
                return False

            # Wait for paused state; track how long we were paused so the
            # playback clock is not thrown off by user-initiated pauses.
            if self.state.status == "paused":
                pause_start = time.perf_counter()
                while self.state.status == "paused":
                    time.sleep(0.5)
                paused_total += time.perf_counter() - pause_start

            self.state.current_step = i + 1
            self.state.current_step_desc = self._describe_frame(frame)

            # Timing: sleep until the wall-clock moment this frame should fire.
            # Using an absolute target avoids drift caused by frame execution time.
            # perf_counter provides sub-millisecond precision on most platforms.
            if self.speed > 0:
                target_wall = playback_start + paused_total + (frame.t / 1000.0) / self.speed
                now = time.perf_counter()
                if target_wall > now + 0.001:
                    time.sleep(target_wall - now)

            # Execute
            frame_start = time.perf_counter()
            success = self._execute_frame(frame, i, loop_idx)
            frame_exec_ms = int((time.perf_counter() - frame_start) * 1000)

            # Log this ctrl to JSON logger
            if self._logger and build_ctrl_details is not None:
                self._logger.log_ctrl(
                    frame_id=frame.id,
                    index=i,
                    frame_type=frame.type,
                    action=frame.action,
                    description=self._describe_frame(frame),
                    result="success" if success else "error",
                    error=self.state.last_error if not success else None,
                    exec_ms=frame_exec_ms,
                    details=build_ctrl_details(frame),
                )

            if not success:
                if self.on_error == "skip":
                    self.log(f"  Skipping failed frame #{frame.id}", "WARN")
                elif self.on_error == "pause":
                    self.state.status = "paused"
                    self.log("Paused due to error. Resume from debug dashboard.", "WARN")
                elif self.on_error == "stop":
                    self.log("Stopping due to error.", "ERROR")
                    self._running = False
                    if self._logger:
                        self._logger.end_loop(False)
                    return False
                # "retry" is handled inside _execute_frame

        # Flush any remaining touch buffer at end of loop
        self._flush_touch_buffer()
        if self._logger:
            self._logger.end_loop(True)
        return True

    def _execute_frame(self, frame: Frame, idx: int, loop_idx: int) -> bool:
        """Execute a single frame. Returns True on success."""
        retries = 0

        while retries <= self.max_retries:
            try:
                if self.dry_run:
                    self.log(f"  [dry] {self._describe_frame(frame)}")
                    return True

                if frame.type == "touch":
                    self._exec_touch(frame)
                elif frame.type == "gesture":
                    self._exec_gesture(frame)
                elif frame.type == "element":
                    self._exec_element(frame)
                elif frame.type == "app":
                    self._exec_app(frame)
                elif frame.type == "key":
                    self._exec_key(frame)
                elif frame.type == "screenshot":
                    self._exec_screenshot(frame, idx, loop_idx)
                elif frame.type == "wait":
                    self._exec_wait(frame)
                elif frame.type == "shell":
                    self._exec_shell(frame)
                elif frame.type == "extract":
                    self._exec_extract(frame, idx, loop_idx)
                elif frame.type == "marker":
                    self.log(f"  Marker: {frame.note}")

                return True

            except ElementObstructedError as e:
                self.log(f"  OBSTRUCTED: {e}", "ERROR")
                self._capture_error_screenshot(idx, loop_idx)
            except ElementNotFoundError as e:
                self.log(f"  NOT FOUND: {e}", "ERROR")
                self._capture_error_screenshot(idx, loop_idx)
            except AdbError as e:
                self.log(f"  ADB ERROR: {e}", "ERROR")
            except Exception as e:
                self.log(f"  ERROR: {e}", "ERROR")

            if self.on_error != "retry":
                return False

            retries += 1
            if retries <= self.max_retries:
                self.log(f"  Retry {retries}/{self.max_retries}...", "WARN")
                time.sleep(self.retry_delay)

        return False

    # ---------- Frame executors ----------

    def _exec_touch(self, frame: Frame) -> None:
        """Execute raw touch by buffering down->move->up into proper gestures."""
        if frame.action == "down":
            # Flush any previous incomplete buffer
            self._flush_touch_buffer()
            self._touch_buffer = [frame]
        elif frame.action == "move":
            self._touch_buffer.append(frame)
        elif frame.action == "up":
            self._touch_buffer.append(frame)
            self._flush_touch_buffer()

    def _flush_touch_buffer(self) -> None:
        """Convert buffered raw touches into a tap or swipe and execute."""
        if not self._touch_buffer:
            return

        start = self._touch_buffer[0]
        end = self._touch_buffer[-1]

        # Use Euclidean distance for accurate gesture detection
        dx = end.x - start.x
        dy = end.y - start.y
        dist = (dx * dx + dy * dy) ** 0.5
        dur = end.t - start.t

        if dist < 30 and dur < 500:
            # Tap - use centroid for better accuracy
            n = len(self._touch_buffer)
            cx = sum(f.x for f in self._touch_buffer) // n
            cy = sum(f.y for f in self._touch_buffer) // n
            self.log(f"  Touch tap ({cx}, {cy})", "STEP")
            send_tap(self.serial, cx, cy)
        elif dist < 30 and dur >= 500:
            # Long press - use centroid
            n = len(self._touch_buffer)
            cx = sum(f.x for f in self._touch_buffer) // n
            cy = sum(f.y for f in self._touch_buffer) // n
            actual_dur = max(500, int(dur / self.speed))
            self.log(f"  Touch long-press ({cx}, {cy}) {dur}ms", "STEP")
            send_swipe(self.serial, cx, cy, cx, cy, actual_dur)
        else:
            # Swipe - replay with intermediate segments for better path accuracy
            move_frames = [f for f in self._touch_buffer if f.action == "move"]
            if len(move_frames) > 4:
                # Use segmented swipe for curved paths
                self.log(f"  Touch swipe ({start.x},{start.y})->({end.x},{end.y}) {dur}ms [{len(move_frames)} pts]", "STEP")
                self._replay_segmented_swipe(start.x, start.y, move_frames, dur)
            else:
                actual_dur = max(1, int(dur / self.speed)) if dur > 0 else 300
                self.log(f"  Touch swipe ({start.x},{start.y})->({end.x},{end.y}) {dur}ms", "STEP")
                send_swipe(self.serial, start.x, start.y, end.x, end.y, actual_dur)

        self._touch_buffer.clear()

    def _replay_segmented_swipe(
        self, start_x: int, start_y: int, move_frames: List[Frame], total_dur: int
    ) -> None:
        """Replay a swipe using multiple segments for better path accuracy."""
        # Sample ~5 segments from the move frames
        step = max(1, len(move_frames) // 5)
        points = [(start_x, start_y)]
        for i in range(0, len(move_frames), step):
            points.append((move_frames[i].x, move_frames[i].y))
        # Always include end
        last = move_frames[-1]
        if points[-1] != (last.x, last.y):
            points.append((last.x, last.y))

        # Execute each segment
        seg_dur = max(1, int((total_dur / max(1, len(points) - 1)) / self.speed))
        for i in range(len(points) - 1):
            x1, y1 = points[i]
            x2, y2 = points[i + 1]
            send_swipe(self.serial, x1, y1, x2, y2, seg_dur)

    def _exec_gesture(self, frame: Frame) -> None:
        """Execute a gesture (tap/swipe/long_press/two_finger_tap)."""
        if frame.action == "tap":
            # If we have element info and it has resource_id/text, try element-based tap
            if frame.element and (frame.element.get("resource_id") or frame.element.get("text")):
                try:
                    el_info = frame.element
                    tap_element(
                        self.serial,
                        resource_id=el_info.get("resource_id") or None,
                        text=el_info.get("text") or None,
                        timeout=5,
                    )
                    label = el_info.get("text") or el_info.get("resource_id", "").split("/")[-1]
                    self.log(f"  Tap element [{label}]", "STEP")
                    return
                except (ElementNotFoundError, ElementObstructedError):
                    # Fall back to coordinates
                    pass

            self.log(f"  Tap ({frame.x}, {frame.y})", "STEP")
            send_tap(self.serial, frame.x, frame.y)

        elif frame.action == "swipe":
            if frame.waypoints and len(frame.waypoints) > 1:
                # Use waypoints for accurate path replay
                self.log(f"  Swipe ({frame.x},{frame.y})→({frame.x2},{frame.y2}) {frame.duration_ms}ms [{len(frame.waypoints)} pts]", "STEP")
                points = [(frame.x, frame.y)] + [(wp[0], wp[1]) for wp in frame.waypoints]
                # Ensure final endpoint is included
                if points[-1] != (frame.x2, frame.y2):
                    points.append((frame.x2, frame.y2))
                total_dur = max(1, int(frame.duration_ms / self.speed))
                seg_dur = max(1, total_dur // max(1, len(points) - 1))
                for i in range(len(points) - 1):
                    x1, y1 = points[i]
                    x2, y2 = points[i + 1]
                    send_swipe(self.serial, x1, y1, x2, y2, seg_dur)
            else:
                self.log(f"  Swipe ({frame.x},{frame.y})→({frame.x2},{frame.y2}) {frame.duration_ms}ms", "STEP")
                dur = max(1, int(frame.duration_ms / self.speed))
                send_swipe(self.serial, frame.x, frame.y, frame.x2, frame.y2, dur)

        elif frame.action == "long_press":
            self.log(f"  Long press ({frame.x}, {frame.y}) {frame.duration_ms}ms", "STEP")
            dur = max(500, int(frame.duration_ms / self.speed))
            send_swipe(self.serial, frame.x, frame.y, frame.x, frame.y, dur)

        elif frame.action == "two_finger_tap":
            self.log(f"  Two-finger tap ({frame.x}, {frame.y})", "STEP")
            # For playback, execute as a regular tap
            send_tap(self.serial, frame.x, frame.y)

    def _exec_element(self, frame: Frame) -> None:
        """Execute element-based tap with smart lookup."""
        rid = frame.resource_id or None
        txt = frame.text or None
        label = txt or (rid.split("/")[-1] if rid else "?")
        self.log(f"  Element tap: {label}", "STEP")

        el, obstruction = tap_element(
            self.serial,
            resource_id=rid,
            text=txt,
            timeout=frame.timeout,
        )

    def _exec_app(self, frame: Frame) -> None:
        if frame.action == "open":
            self.log(f"  Open app: {frame.package}", "STEP")
            launch_app(self.serial, frame.package, frame.activity)
            time.sleep(2)
        elif frame.action == "close":
            self.log(f"  Close app: {frame.package}", "STEP")
            force_stop_app(self.serial, frame.package)
            time.sleep(1)

    def _exec_key(self, frame: Frame) -> None:
        name = frame.key_name or str(frame.keycode)
        self.log(f"  Key: {name}", "STEP")
        send_key_event(self.serial, frame.keycode)

    def _exec_screenshot(self, frame: Frame, idx: int, loop_idx: int) -> None:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        stage = frame.stage or f"frame{idx}"
        filename = f"loop{loop_idx:04d}-{ts}-{stage}.png"
        out_dir = Path(self.recording.settings.screenshot_dir)
        out_path = out_dir / filename

        self.log(f"  Screenshot: {out_path}", "STEP")
        take_screenshot(self.serial, out_path)
        self.state.last_screenshot = str(out_path)

        if frame.send:
            self._send_file(out_path)

    def _exec_wait(self, frame: Frame) -> None:
        wait_s = (frame.duration_ms / 1000.0) / self.speed
        self.log(f"  Wait {frame.duration_ms}ms (actual: {wait_s:.1f}s)", "STEP")
        time.sleep(wait_s)

    def _exec_shell(self, frame: Frame) -> None:
        self.log(f"  Shell: {frame.command}", "STEP")
        prefix = adb_cmd(self.serial)
        run_adb(prefix + ["shell"] + frame.command.split())

    def _exec_extract(self, frame: Frame, idx: int, loop_idx: int) -> None:
        """Extract UI data from current screen and save to log.

        Each log entry includes coordinates (bounds, center) for every element
        so that the data can be mapped back to the exact screen region during
        analysis.  For Ctrl-capture frames the recorded ``extract_fields``
        already carry the original coordinates; we re-read the current screen
        and match them so the log contains both the *recorded* coordinates and
        the *current* (replay-time) coordinates for comparison.
        """
        label = frame.extract_label or f"extract_{idx}"
        self.log(f"  Extract: {label}", "STEP")

        # Get current UI hierarchy
        xml_str = get_ui_xml(self.serial)
        elements = parse_all_elements(xml_str)

        result: Dict[str, Any] = {
            "timestamp": datetime.now().astimezone().isoformat(),
            "loop": loop_idx,
            "step": idx,
            "label": label,
        }

        # Extract specific fields if configured
        if frame.extract_fields:
            fields_data: Dict[str, Any] = {}
            for field_def in frame.extract_fields:
                fname = field_def.get("name", "")
                rid = field_def.get("resource_id", "")
                text_match = field_def.get("text_match", "")

                # Carry over the recorded coordinates so the log shows both
                # the original (record-time) and current (replay-time) values.
                recorded_coords: Optional[Dict[str, Any]] = None
                if field_def.get("bounds") or field_def.get("center"):
                    recorded_coords = {}
                    if field_def.get("bounds"):
                        recorded_coords["bounds"] = field_def["bounds"]
                    if field_def.get("center"):
                        recorded_coords["center"] = field_def["center"]
                    if field_def.get("text"):
                        recorded_coords["text"] = field_def["text"]

                matched_values = []
                for el in elements:
                    # Match by resource_id
                    if rid and hasattr(el, "resource_id") and rid in getattr(el, "resource_id", ""):
                        matched_values.append({
                            "text": getattr(el, "text", ""),
                            "resource_id": getattr(el, "resource_id", ""),
                            "class": getattr(el, "cls", ""),
                            "bounds": list(getattr(el, "bounds", ())),
                            "center": list(getattr(el, "center", ())),
                        })
                    # Match by exact text from recording (for Ctrl captures)
                    elif not rid and not text_match and field_def.get("text"):
                        el_text = getattr(el, "text", "")
                        if el_text and el_text.strip() == field_def["text"]:
                            matched_values.append({
                                "text": el_text.strip(),
                                "resource_id": getattr(el, "resource_id", ""),
                                "class": getattr(el, "cls", ""),
                                "bounds": list(getattr(el, "bounds", ())),
                                "center": list(getattr(el, "center", ())),
                            })
                    # Match by text pattern (regex)
                    elif text_match and hasattr(el, "text"):
                        el_text = getattr(el, "text", "")
                        if el_text and re.search(text_match, el_text):
                            matched_values.append({
                                "text": el_text,
                                "resource_id": getattr(el, "resource_id", ""),
                                "class": getattr(el, "cls", ""),
                                "bounds": list(getattr(el, "bounds", ())),
                                "center": list(getattr(el, "center", ())),
                            })

                entry: Dict[str, Any]
                if len(matched_values) == 1:
                    entry = matched_values[0]
                elif matched_values:
                    entry = {"matches": matched_values}
                else:
                    entry = {"matches": None}

                # Attach the recorded coordinates so the reader can compare
                if recorded_coords:
                    entry["recorded"] = recorded_coords

                fields_data[fname] = entry

            result["fields"] = fields_data

        # Capture all visible text if requested — include coordinates
        if frame.extract_all_text:
            all_texts = []
            for el in elements:
                text = getattr(el, "text", "")
                if text and text.strip():
                    all_texts.append({
                        "text": text.strip(),
                        "resource_id": getattr(el, "resource_id", ""),
                        "class": getattr(el, "cls", ""),
                        "bounds": list(getattr(el, "bounds", ())),
                        "center": list(getattr(el, "center", ())),
                    })
            result["all_text"] = all_texts

        # Take screenshot if requested
        if frame.extract_screenshot:
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            filename = f"extract-loop{loop_idx:04d}-{ts}-{label}.png"
            out_dir = Path(self.recording.settings.screenshot_dir)
            out_path = out_dir / filename
            try:
                take_screenshot(self.serial, out_path)
                result["screenshot"] = str(out_path)
            except OSError as e:
                if e.errno == 28:  # ENOSPC - No space left on device
                    self._free_disk_space(out_dir)
                    try:
                        take_screenshot(self.serial, out_path)
                        result["screenshot"] = str(out_path)
                    except Exception:
                        result["screenshot"] = None
                else:
                    result["screenshot"] = None
            except Exception:
                result["screenshot"] = None

        # Write to extraction log (JSONL format - one JSON per line, append-friendly)
        # Use ONE log file per recording (per station), not per extract label.
        # e.g. station-01.urf.json -> station-01-extract.jsonl
        # All Ctrl captures within that station append to the same file.
        log_dir = Path(self.recording.settings.screenshot_dir).parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        if self.recording_path:
            stem = Path(self.recording_path).stem.replace(".urf", "")
            log_file = log_dir / f"{stem}-extract.jsonl"
        else:
            log_file = log_dir / f"extract-{label}.jsonl"
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(result, ensure_ascii=False) + "\n")
        except OSError as e:
            if e.errno == 28:  # ENOSPC
                captures_dir = Path(self.recording.settings.screenshot_dir)
                self._free_disk_space(captures_dir)
                try:
                    with open(log_file, "a", encoding="utf-8") as f:
                        f.write(json.dumps(result, ensure_ascii=False) + "\n")
                except OSError:
                    self.log(f"  WARNING: Could not write extract log (disk full)", "WARN")
            else:
                raise

        field_count = len(result.get("fields", {}))
        text_count = len(result.get("all_text", []))
        self.log(f"  Extracted {field_count} fields, {text_count} texts -> {log_file}", "OK")

    # ---------- Helpers ----------

    @staticmethod
    def _get_overflow_root() -> Optional[Path]:
        """Return the overflow root directory, or None if not configured."""
        config_path = Path("/work/.overflow_config")
        if config_path.exists():
            try:
                cfg = json.loads(config_path.read_text(encoding="utf-8"))
                p = Path(cfg.get("path", ""))
                if p.exists():
                    return p
            except Exception:
                pass
        p = Path("/overflow")
        if p.exists():
            return p
        return None

    def _free_disk_space(self, captures_dir: Path) -> int:
        """Emergency disk space recovery when ENOSPC occurs during extract.

        Primary: move captures to overflow/HDD (preserves all data).
        Fallback: delete oldest captures only if overflow unavailable.
        Returns bytes freed from local disk.
        """
        freed = 0
        overflow = self._get_overflow_root()

        # Collect all capture images
        candidates = []
        for d in [captures_dir, Path("/img/sent"), Path("/img/cache"), Path("/img/temp")]:
            if not d.exists():
                continue
            for f in d.iterdir():
                if f.is_file() and f.suffix.lower() in (".png", ".jpg", ".jpeg"):
                    try:
                        candidates.append((f.stat().st_mtime, f.stat().st_size, f))
                    except OSError:
                        pass
        candidates.sort(key=lambda x: x[0])  # oldest first

        if overflow is not None:
            # Move ALL captures to overflow/HDD
            dst_dir = overflow / "captures"
            dst_dir.mkdir(exist_ok=True)
            moved = 0
            for _, sz, f in candidates:
                try:
                    shutil.move(str(f), str(dst_dir / f.name))
                    freed += sz
                    moved += 1
                except Exception:
                    pass
            if freed > 0:
                self.log(f"  Moved {moved} captures to overflow ({freed / (1024*1024):.1f} MB)", "WARN")

            # Check if move actually freed space (overflow may be on same fs)
            try:
                u = shutil.disk_usage(str(captures_dir))
                still_full = (u.used / u.total * 100) >= 95 if u.total else True
            except OSError:
                still_full = True
            if still_full:
                # Overflow didn't help — delete oldest captures as fallback
                remaining = [(m, s, f) for m, s, f in candidates if f.exists()]
                delete_count = max(len(remaining) // 2, 1)
                del_freed = 0
                for _, sz, f in remaining[:delete_count]:
                    try:
                        f.unlink()
                        del_freed += sz
                    except OSError:
                        pass
                if del_freed > 0:
                    freed += del_freed
                    self.log(f"  Overflow didn't help — deleted {delete_count} old captures ({del_freed / (1024*1024):.1f} MB)", "WARN")

                # Also delete regenerable HTML log viewers
                for log_dir in [Path("/work/logs"), Path("/img/logs")]:
                    if not log_dir.exists():
                        continue
                    for f in log_dir.iterdir():
                        if f.is_file() and f.suffix in (".html", ".gz"):
                            try:
                                freed += f.stat().st_size
                                f.unlink()
                            except Exception:
                                pass
        else:
            # No overflow — delete oldest half as last resort
            delete_count = max(len(candidates) // 2, 1)
            for _, sz, f in candidates[:delete_count]:
                try:
                    f.unlink()
                    freed += sz
                except OSError:
                    pass
            if freed > 0:
                self.log(f"  Freed {freed / (1024*1024):.1f} MB (deleted {delete_count} old captures)", "WARN")
        return freed

    def _describe_frame(self, frame: Frame) -> str:
        if frame.type == "touch":
            return f"touch {frame.action} ({frame.x},{frame.y})"
        elif frame.type == "gesture":
            el = ""
            if frame.element:
                el = f" [{frame.element.get('text') or frame.element.get('resource_id','').split('/')[-1]}]"
            return f"{frame.action}{el} ({frame.x},{frame.y})"
        elif frame.type == "element":
            return f"element: {frame.text or frame.resource_id}"
        elif frame.type == "app":
            return f"app {frame.action}: {frame.package}"
        elif frame.type == "key":
            return f"key: {frame.key_name or frame.keycode}"
        elif frame.type == "screenshot":
            return f"screenshot: {frame.stage}"
        elif frame.type == "wait":
            return f"wait {frame.duration_ms}ms"
        elif frame.type == "shell":
            return f"shell: {frame.command}"
        elif frame.type == "extract":
            return f"extract: {frame.extract_label or 'data'}"
        elif frame.type == "marker":
            return f"marker: {frame.note}"
        return f"{frame.type}: {frame.action}"

    def _send_file(self, path: Path) -> None:
        cfg = self.recording.settings.send_to
        method = cfg.get("method", "cp")
        if method == "cp":
            dest = Path(cfg.get("path", "/img/sent"))
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(path), str(dest / path.name))
            self.log(f"  Sent (cp): {dest / path.name}")
        elif method == "curl":
            url = cfg.get("url", "")
            if url:
                headers_cfg = cfg.get("headers", {})
                cmd = ["curl", "-s", "-X", "POST", "-F", f"file=@{path}"]
                for k, v in headers_cfg.items():
                    cmd += ["-H", f"{k}: {v}"]
                cmd.append(url)
                subprocess.run(cmd, capture_output=True, timeout=30)
                self.log(f"  Sent (curl): {url}")
        elif method == "adb-push":
            prefix = adb_cmd(self.serial)
            run_adb(prefix + ["push", str(path), cfg.get("path", "/sdcard/")])

    def _capture_error_screenshot(self, idx: int, loop_idx: int) -> None:
        try:
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            out_dir = Path(self.recording.settings.screenshot_dir)
            path = out_dir / f"error-loop{loop_idx:04d}-frame{idx:03d}-{ts}.png"
            take_screenshot(self.serial, path)
            self.state.last_error_screenshot = str(path)
        except Exception:
            pass


# ---------- CLI ----------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Universal Player - Replay Android recordings (raw + scripted)"
    )
    parser.add_argument("recording", type=Path, help="Path to .urf.json file")
    parser.add_argument("--replay-mode", choices=["raw", "smart", "auto"], default="auto",
                        help="How to replay: raw touches, smart gestures, or auto (default: auto)")
    parser.add_argument("--speed", type=float, default=0,
                        help="Speed multiplier (0 = use recording setting)")
    parser.add_argument("--loop", action="store_true", default=None,
                        help="Enable looping (overrides recording setting)")
    parser.add_argument("--no-loop", action="store_true",
                        help="Disable looping (overrides recording setting)")
    parser.add_argument("--loop-count", type=int, default=None,
                        help="Number of loops (0=infinite)")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without executing")
    parser.add_argument("--debug-port", type=int, default=0,
                        help="Start debug web server on this port")
    parser.add_argument("-s", "--serial", help="Override device serial")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not args.recording.exists():
        print(f"File not found: {args.recording}", file=sys.stderr)
        return 1

    recording = UniversalRecording.load(args.recording)

    if args.serial:
        recording.meta.device = args.serial

    loop_flag = None
    if args.loop:
        loop_flag = True
    elif args.no_loop:
        loop_flag = False

    speed = args.speed if args.speed > 0 else recording.settings.speed

    player = UniversalPlayer(
        recording=recording,
        recording_path=args.recording.resolve(),
        replay_mode=args.replay_mode,
        speed=speed,
        loop=loop_flag,
        loop_count=args.loop_count,
        dry_run=args.dry_run,
        debug_port=args.debug_port,
    )

    player.play()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
