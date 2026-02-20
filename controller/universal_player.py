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
        replay_mode: str = "auto",
        speed: float = 1.0,
        loop: Optional[bool] = None,
        loop_count: Optional[int] = None,
        dry_run: bool = False,
        debug_port: int = 0,
    ):
        self.recording = recording
        self.replay_mode = replay_mode
        self.speed = speed
        self.dry_run = dry_run
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

        if debug_port > 0 and DebugServer is not None:
            self._debug_server = DebugServer(self.state, port=debug_port)

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

        self._running = True
        loop_idx = 0

        try:
            while self._running:
                loop_idx += 1
                self.state.current_loop = loop_idx
                self.state.status = "running"

                if self.loop_count > 0 and loop_idx > self.loop_count:
                    break

                self.log(f"=== Loop {loop_idx} ===")
                success = self._play_one_loop(loop_idx)

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
        prev_t = 0

        for i, frame in enumerate(self._frames):
            if not self._running:
                return False

            # Wait for paused state
            while self.state.status == "paused":
                time.sleep(0.5)

            self.state.current_step = i + 1
            self.state.current_step_desc = self._describe_frame(frame)

            # Timing: wait based on time offset between frames
            delay_ms = frame.t - prev_t
            if delay_ms > 0 and self.speed > 0:
                actual_delay = (delay_ms / 1000.0) / self.speed
                if actual_delay > 0.01:
                    time.sleep(actual_delay)

            # Execute
            success = self._execute_frame(frame, i, loop_idx)

            if not success:
                if self.on_error == "skip":
                    self.log(f"  Skipping failed frame #{frame.id}", "WARN")
                elif self.on_error == "pause":
                    self.state.status = "paused"
                    self.log("Paused due to error. Resume from debug dashboard.", "WARN")
                elif self.on_error == "stop":
                    self.log("Stopping due to error.", "ERROR")
                    self._running = False
                    return False
                # "retry" is handled inside _execute_frame

            prev_t = frame.t

        # Flush any remaining touch buffer at end of loop
        self._flush_touch_buffer()
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

    # ---------- Helpers ----------

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
