#!/usr/bin/env python3
"""
Automation Engine - Main orchestrator for Android app automation flows.

Usage:
    automation-engine.py <flow.yaml> [--dry-run] [--debug-port 8080]

Features:
- Load and execute YAML-defined flows
- Loop: open app → navigate → screenshot → send → close → repeat
- Smart element interaction with obstruction detection
- Auto-dismiss dialogs/popups that block target elements
- Error notification with debug screenshot
- Built-in debug web server for live monitoring
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from flow_config import FlowConfig, FlowStep, load_flow, validate_flow
from element_interactor import (
    AdbError,
    ElementNotFoundError,
    ElementObstructedError,
    ObstructionReport,
    launch_app,
    force_stop_app,
    take_screenshot,
    tap_element,
    send_tap,
    send_swipe,
    send_key_event,
    wait_for_element,
    get_current_app,
    get_ui_xml,
    parse_all_elements,
    run_adb,
    adb_cmd,
)

# Try import debug server (optional)
try:
    from debug_server import DebugServer
except ImportError:
    DebugServer = None  # type: ignore[assignment,misc]


@dataclass
class StepResult:
    step_index: int
    action: str
    description: str
    success: bool
    error: str = ""
    screenshot_path: str = ""
    obstruction: Optional[Dict[str, Any]] = None
    timestamp: str = ""
    duration_ms: int = 0


@dataclass
class LoopResult:
    loop_index: int
    started_at: str
    finished_at: str = ""
    success: bool = True
    step_results: List[StepResult] = field(default_factory=list)
    error_step: int = -1
    error_message: str = ""


@dataclass
class EngineState:
    """Live state for debug server."""
    status: str = "idle"  # idle | running | paused | error
    current_loop: int = 0
    current_step: int = 0
    current_step_desc: str = ""
    total_loops: int = 0
    total_steps: int = 0
    last_screenshot: str = ""
    last_error: str = ""
    last_error_screenshot: str = ""
    history: List[Dict[str, Any]] = field(default_factory=list)

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
            "recent_history": self.history[-10:],
        }


class AutomationEngine:
    def __init__(self, config: FlowConfig, dry_run: bool = False, debug_port: int = 0):
        self.config = config
        self.dry_run = dry_run
        self.state = EngineState(total_steps=len(config.steps))
        self.debug_server: Any = None
        self._running = False

        if debug_port > 0 and DebugServer is not None:
            self.debug_server = DebugServer(self.state, port=debug_port)

    @property
    def serial(self) -> Optional[str]:
        return self.config.device or None

    def log(self, msg: str, level: str = "INFO") -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        prefix_map = {"INFO": "ℹ", "OK": "✓", "WARN": "⚠", "ERROR": "✗", "STEP": "→"}
        pfx = prefix_map.get(level, " ")
        line = f"[{ts}] {pfx} {msg}"
        print(line)
        self.state.history.append({"ts": ts, "level": level, "msg": msg})

    def run(self) -> None:
        """Run the automation flow."""
        warnings = validate_flow(self.config)
        for w in warnings:
            self.log(w, "WARN")

        self.log(f"Flow: {self.config.name}")
        self.log(f"Steps: {len(self.config.steps)}, Loop: {self.config.loop}, Package: {self.config.app_package}")

        if self.debug_server:
            self.debug_server.start()
            self.log(f"Debug server started on port {self.debug_server.port}", "INFO")

        self._running = True
        loop_idx = 0

        try:
            while self._running:
                loop_idx += 1
                self.state.current_loop = loop_idx
                self.state.status = "running"

                if self.config.loop_count > 0 and loop_idx > self.config.loop_count:
                    self.log("Reached loop count limit.", "INFO")
                    break

                # Auto-cleanup before each loop if disk is getting full
                self._auto_cleanup_if_needed()

                self.log(f"=== Loop {loop_idx} ===", "INFO")
                result = self._run_one_loop(loop_idx)

                if result.success:
                    self.log(f"Loop {loop_idx} completed successfully.", "OK")
                else:
                    self.log(f"Loop {loop_idx} failed at step {result.error_step}: {result.error_message}", "ERROR")
                    if self.config.on_error.pause_on_error:
                        self.state.status = "paused"
                        self.log("Paused due to error. Waiting for user...", "WARN")
                        self._wait_for_resume()

                if not self.config.loop:
                    break

                if self._running and self.config.loop_delay > 0:
                    self.log(f"Waiting {self.config.loop_delay}s before next loop...", "INFO")
                    time.sleep(self.config.loop_delay)

        except KeyboardInterrupt:
            self.log("Interrupted by user.", "WARN")
        finally:
            self._running = False
            self.state.status = "idle"
            if self.debug_server:
                self.debug_server.stop()
            self.log("Engine stopped.", "INFO")

    def _run_one_loop(self, loop_idx: int) -> LoopResult:
        result = LoopResult(
            loop_index=loop_idx,
            started_at=datetime.now().isoformat(),
        )

        for step_idx, step in enumerate(self.config.steps):
            self.state.current_step = step_idx + 1
            self.state.current_step_desc = step.description or step.action
            desc = step.description or step.action
            self.log(f"Step {step_idx + 1}/{len(self.config.steps)}: {desc}", "STEP")

            step_result = self._execute_step(step, step_idx, loop_idx)
            result.step_results.append(step_result)

            if not step_result.success:
                result.success = False
                result.error_step = step_idx + 1
                result.error_message = step_result.error

                # Handle error
                self._handle_step_error(step, step_idx, step_result)

                # Retry logic
                retried = False
                for retry in range(self.config.on_error.max_retries):
                    self.log(f"Retry {retry + 1}/{self.config.on_error.max_retries}...", "WARN")
                    time.sleep(self.config.on_error.retry_delay)
                    retry_result = self._execute_step(step, step_idx, loop_idx)
                    result.step_results.append(retry_result)
                    if retry_result.success:
                        result.success = True
                        result.error_step = -1
                        result.error_message = ""
                        retried = True
                        break

                if not retried and not result.success:
                    break

        result.finished_at = datetime.now().isoformat()
        return result

    def _execute_step(self, step: FlowStep, step_idx: int, loop_idx: int) -> StepResult:
        start = time.time()
        sr = StepResult(
            step_index=step_idx,
            action=step.action,
            description=step.description,
            success=False,
            timestamp=datetime.now().isoformat(),
        )

        if self.dry_run:
            self.log(f"  [dry-run] Would execute: {step.action}", "INFO")
            sr.success = True
            sr.duration_ms = int((time.time() - start) * 1000)
            return sr

        try:
            if step.action == "open_app":
                self._step_open_app(step)
            elif step.action == "close_app":
                self._step_close_app(step)
            elif step.action == "tap_element":
                self._step_tap_element(step, sr)
            elif step.action == "tap_coord":
                self._step_tap_coord(step)
            elif step.action == "swipe":
                self._step_swipe(step)
            elif step.action == "screenshot":
                self._step_screenshot(step, sr, loop_idx)
            elif step.action == "key_event":
                self._step_key_event(step)
            elif step.action == "wait":
                self._step_wait(step)
            elif step.action == "wait_element":
                self._step_wait_element(step)
            elif step.action == "assert_element":
                self._step_assert_element(step)
            elif step.action == "shell":
                self._step_shell(step)
            else:
                raise RuntimeError(f"Unknown action: {step.action}")

            sr.success = True
            self.log(f"  Done.", "OK")

            # Screenshot after step if requested
            if step.screenshot_after:
                path = self._capture_step_screenshot(step_idx, loop_idx, "after")
                sr.screenshot_path = str(path)

        except ElementObstructedError as e:
            sr.error = str(e)
            sr.obstruction = {
                "type": e.report.obstruction_type,
                "message": e.report.message,
                "dismissible": e.report.dismissible,
            }
            self.log(f"  OBSTRUCTED: {e}", "ERROR")
        except (ElementNotFoundError, AdbError, RuntimeError) as e:
            sr.error = str(e)
            self.log(f"  FAILED: {e}", "ERROR")
        except subprocess.TimeoutExpired:
            sr.error = "ADB command timed out"
            self.log(f"  TIMEOUT", "ERROR")

        sr.duration_ms = int((time.time() - start) * 1000)
        return sr

    # ---------- Step implementations ----------

    def _step_open_app(self, step: FlowStep) -> None:
        pkg = self.config.app_package
        act = self.config.app_activity
        if not pkg:
            raise RuntimeError("app_package not set in flow config")
        self.log(f"  Launching {pkg}/{act or '(main)'}")
        launch_app(self.serial, pkg, act)
        time.sleep(2)  # Give app time to start

    def _step_close_app(self, step: FlowStep) -> None:
        pkg = self.config.app_package
        if not pkg:
            raise RuntimeError("app_package not set in flow config")
        self.log(f"  Force stopping {pkg}")
        force_stop_app(self.serial, pkg)
        time.sleep(1)

    def _step_tap_element(self, step: FlowStep, sr: StepResult) -> None:
        self.log(f"  Looking for element: resource_id={step.resource_id}, text={step.text}")
        el, obstruction = tap_element(
            self.serial,
            resource_id=step.resource_id,
            text=step.text,
            timeout=step.timeout,
        )
        self.log(f"  Tapped at {el.center}")
        if obstruction and obstruction.is_obstructed:
            sr.obstruction = {
                "type": obstruction.obstruction_type,
                "message": obstruction.message,
                "dismissed": True,
            }

    def _step_tap_coord(self, step: FlowStep) -> None:
        self.log(f"  Tap ({step.x}, {step.y})")
        send_tap(self.serial, step.x, step.y)

    def _step_swipe(self, step: FlowStep) -> None:
        self.log(f"  Swipe ({step.start_x},{step.start_y}) → ({step.end_x},{step.end_y})")
        send_swipe(self.serial, step.start_x, step.start_y,
                   step.end_x, step.end_y, step.duration_ms)

    def _step_screenshot(self, step: FlowStep, sr: StepResult, loop_idx: int) -> None:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        stage = step.stage or f"step{sr.step_index}"
        filename = f"loop{loop_idx:04d}-{ts}-{stage}.png"
        out_dir = Path(self.config.screenshot_dir)
        out_path = out_dir / filename

        self.log(f"  Capturing screenshot → {out_path}")
        take_screenshot(self.serial, out_path)
        sr.screenshot_path = str(out_path)
        self.state.last_screenshot = str(out_path)

        if step.send:
            self._send_file(out_path)

    def _step_key_event(self, step: FlowStep) -> None:
        self.log(f"  KeyEvent {step.keycode}")
        send_key_event(self.serial, step.keycode)

    def _step_wait(self, step: FlowStep) -> None:
        self.log(f"  Waiting {step.duration}s")
        time.sleep(step.duration)

    def _step_wait_element(self, step: FlowStep) -> None:
        self.log(f"  Waiting for element: resource_id={step.resource_id}, text={step.text}")
        el, _, _ = wait_for_element(
            self.serial,
            resource_id=step.resource_id,
            text=step.text,
            timeout=step.timeout,
        )
        if el is None:
            raise ElementNotFoundError(
                f"Element not found after {step.timeout}s: "
                f"resource_id={step.resource_id}, text={step.text}"
            )
        self.log(f"  Element found at {el.center}")

    def _step_assert_element(self, step: FlowStep) -> None:
        self.log(f"  Asserting element: resource_id={step.resource_id}, text={step.text}")
        el, _, obstruction = wait_for_element(
            self.serial,
            resource_id=step.resource_id,
            text=step.text,
            timeout=step.timeout,
        )
        if el is None:
            raise ElementNotFoundError(
                f"Assertion failed: element not found "
                f"(resource_id={step.resource_id}, text={step.text})"
            )
        if step.text and el.text != step.text:
            raise RuntimeError(
                f"Assertion failed: expected text '{step.text}', got '{el.text}'"
            )
        self.log(f"  Assertion passed: {el.text or el.resource_id}")

    def _step_shell(self, step: FlowStep) -> None:
        cmd = step.command
        self.log(f"  Shell: {cmd}")
        prefix = adb_cmd(self.serial)
        run_adb(prefix + ["shell"] + cmd.split())

    # ---------- File sending ----------

    def _send_file(self, path: Path) -> None:
        cfg = self.config.send_to
        if cfg.method == "cp":
            dest = Path(cfg.path)
            dest.mkdir(parents=True, exist_ok=True)
            target = dest / path.name
            shutil.copy2(str(path), str(target))
            self.log(f"  Copied to {target}")
        elif cfg.method == "curl":
            if not cfg.url:
                self.log("  send_to.url not configured, skipping send", "WARN")
                return
            cmd = ["curl", "-s", "-X", "POST", "-F", f"file=@{path}"]
            for k, v in cfg.headers.items():
                cmd += ["-H", f"{k}: {v}"]
            cmd.append(cfg.url)
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                self.log(f"  Sent to {cfg.url}")
            else:
                self.log(f"  Send failed: {result.stderr}", "WARN")
        elif cfg.method == "adb-push":
            prefix = adb_cmd(self.serial)
            run_adb(prefix + ["push", str(path), cfg.path])
            self.log(f"  Pushed to device: {cfg.path}")
        else:
            self.log(f"  Unknown send method: {cfg.method}", "WARN")

    # ---------- Error handling ----------

    def _handle_step_error(self, step: FlowStep, step_idx: int, sr: StepResult) -> None:
        error_cfg = self.config.on_error

        # Capture error screenshot
        if error_cfg.screenshot_on_error:
            try:
                path = self._capture_step_screenshot(step_idx, self.state.current_loop, "error")
                self.state.last_error_screenshot = str(path)
                self.state.last_error = sr.error
            except Exception:
                pass

        # Notify
        if error_cfg.notify:
            self._send_notification(step, sr)

    def _send_notification(self, step: FlowStep, sr: StepResult) -> None:
        error_cfg = self.config.on_error
        message = {
            "flow": self.config.name,
            "loop": self.state.current_loop,
            "step": sr.step_index + 1,
            "action": sr.action,
            "description": sr.description,
            "error": sr.error,
            "obstruction": sr.obstruction,
            "error_screenshot": self.state.last_error_screenshot,
            "timestamp": sr.timestamp,
        }

        if error_cfg.notify_method == "stdout":
            print(f"\n{'='*60}")
            print("NOTIFICATION: Step failed!")
            print(json.dumps(message, indent=2, ensure_ascii=False))
            print(f"{'='*60}\n")

        elif error_cfg.notify_method == "webhook":
            if not error_cfg.notify_url:
                self.log("notify_url not configured", "WARN")
                return
            try:
                cmd = [
                    "curl", "-s", "-X", "POST",
                    "-H", "Content-Type: application/json",
                    "-d", json.dumps(message, ensure_ascii=False),
                    error_cfg.notify_url,
                ]
                subprocess.run(cmd, capture_output=True, timeout=10)
                self.log(f"  Notification sent to {error_cfg.notify_url}")
            except Exception as e:
                self.log(f"  Failed to send notification: {e}", "WARN")

        elif error_cfg.notify_method == "file":
            notify_path = Path("/work/notifications")
            notify_path.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            nf = notify_path / f"error-{ts}.json"
            nf.write_text(json.dumps(message, indent=2, ensure_ascii=False), encoding="utf-8")
            self.log(f"  Notification saved to {nf}")

    def _auto_cleanup_if_needed(self) -> None:
        """Auto-cleanup disk space before each loop to prevent disk-full errors."""
        try:
            check_path = self.config.screenshot_dir if Path(self.config.screenshot_dir).exists() else "/img"
            usage = shutil.disk_usage(check_path)
            used_pct = usage.used / usage.total * 100

            if used_pct <= 70:
                return

            free_mb = usage.free / (1024**2)
            self.log(f"Disk usage: {used_pct:.1f}% (free: {free_mb:.0f} MB) — running cleanup", "WARN")

            cleaned = 0
            freed = 0

            if used_pct > 95:
                # Critical: delete all captures, sent, and logs
                for d in [Path("/img/captures"), Path("/img/sent")]:
                    if d.exists():
                        for p in d.rglob("*"):
                            if p.is_file():
                                try:
                                    freed += p.stat().st_size
                                    p.unlink()
                                    cleaned += 1
                                except OSError:
                                    pass
                log_dir = Path(self.config.screenshot_dir).parent / "logs"
                if log_dir.exists():
                    for p in log_dir.iterdir():
                        if p.is_file():
                            try:
                                freed += p.stat().st_size
                                p.unlink()
                                cleaned += 1
                            except OSError:
                                pass
            else:
                # Moderate/High: delete old captures and sent images
                keep_hours = 1 if used_pct > 85 else 3
                cutoff = time.time() - (keep_hours * 3600)
                for d in [Path("/img/captures"), Path("/img/sent")]:
                    if d.exists():
                        for p in d.rglob("*"):
                            if p.is_file():
                                try:
                                    if p.stat().st_mtime < cutoff:
                                        freed += p.stat().st_size
                                        p.unlink()
                                        cleaned += 1
                                except OSError:
                                    pass

            # Always clean temp dirs
            for tmp_dir in [Path("/tmp"), Path("/var/tmp")]:
                if tmp_dir.exists():
                    for p in tmp_dir.rglob("*"):
                        if p.is_file():
                            try:
                                freed += p.stat().st_size
                                p.unlink()
                                cleaned += 1
                            except OSError:
                                pass

            if cleaned > 0:
                self.log(f"Cleaned {cleaned} files, freed {freed / (1024**2):.1f} MB", "WARN")
        except Exception:
            pass  # Don't let cleanup errors break automation

    def _capture_step_screenshot(self, step_idx: int, loop_idx: int, tag: str) -> Path:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_dir = Path(self.config.screenshot_dir)
        filename = f"loop{loop_idx:04d}-step{step_idx:03d}-{tag}-{ts}.png"
        out_path = out_dir / filename
        take_screenshot(self.serial, out_path)
        self.state.last_screenshot = str(out_path)
        return out_path

    def _wait_for_resume(self) -> None:
        """Wait until status changes from 'paused' (via debug server)."""
        while self.state.status == "paused":
            time.sleep(1)


# ---------- CLI ----------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Android Automation Engine - run YAML-defined flows"
    )
    parser.add_argument("flow", type=Path, help="Path to flow YAML file")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without executing")
    parser.add_argument("--debug-port", type=int, default=0,
                        help="Start debug web server on this port (0=disabled)")
    parser.add_argument("-s", "--serial", help="Override device serial from config")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not args.flow.exists():
        print(f"Flow file not found: {args.flow}", file=sys.stderr)
        return 1

    config = load_flow(args.flow)
    if args.serial:
        config.device = args.serial

    engine = AutomationEngine(config, dry_run=args.dry_run, debug_port=args.debug_port)
    engine.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
