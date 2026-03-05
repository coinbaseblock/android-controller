#!/usr/bin/env python3
"""
Visual Flow Editor - Web-based UXUI editor for .urf.json files.

A full single-page web application for visually editing Universal Recording
Format files. Supports both raw touch recordings and scripted flows,
with the ability to mix them freely.

Usage:
    flow-editor-web.py [--port 9090] [--dir /work]

Then open: http://localhost:9090

Features:
- Visual timeline with color-coded frame types
- Drag-and-drop reorder
- Inline frame editing (all properties)
- Add/insert frames (tap, swipe, element, app, key, wait, screenshot, etc.)
- Enable/disable toggle per frame
- Bulk timing adjustments (scale, shift)
- Collapse raw touches to gestures
- Settings panel (loop, speed, error handling, send-to)
- Dual view: Timeline (raw+script) or Script-only
- Load/Save .urf.json files from /work directory
- Import legacy formats (touch JSON/CSV)
"""

from __future__ import annotations

import io
import json
import os
import re as _re
import shutil
import signal
import subprocess
import sys
import threading
import time
import argparse
import zipfile
from datetime import datetime
import xml.etree.ElementTree as _ET
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse, parse_qs

# Add script dir to path for imports
sys.path.insert(0, str(Path(__file__).parent))
from universal_format import UniversalRecording, Frame


# ============================================================
# Process Manager for Play/Record subprocesses
# ============================================================

class ProcessManager:
    """Manages background player/recorder subprocesses."""

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._status: str = "idle"  # idle | playing | recording | stopped | error
        self._current_step: int = 0
        self._total_steps: int = 0
        self._frames_count: int = 0
        self._message: str = ""
        self._lock = threading.Lock()
        self._monitor_thread: Optional[threading.Thread] = None

    @property
    def status(self) -> str:
        with self._lock:
            # Check if process died
            if self._proc and self._proc.poll() is not None:
                self._status = "idle"
                self._proc = None
            return self._status

    def get_status_dict(self) -> dict:
        with self._lock:
            if self._proc and self._proc.poll() is not None:
                self._status = "idle"
                self._proc = None
            return {
                "status": self._status,
                "current_step": self._current_step,
                "total_steps": self._total_steps,
                "frames_count": self._frames_count,
                "message": self._message,
            }

    def start_play(self, recording_path: str, work_dir: str) -> dict:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                return {"ok": False, "error": "Process already running"}

            # Resolve player script path (handle underscore vs hyphen naming)
            script = str(Path(__file__).parent / "universal_player.py")
            if not Path(script).exists():
                alt = str(Path(__file__).parent / "universal-player.py")
                if Path(alt).exists():
                    script = alt
                else:
                    return {"ok": False, "error": "Player script not found"}

            cmd = [sys.executable, "-u", script, recording_path, "--replay-mode", "auto"]

            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    cwd=work_dir,
                )
                self._status = "playing"
                self._current_step = 0
                self._total_steps = 0
                self._message = ""
                self._start_monitor()
                return {"ok": True}
            except Exception as e:
                self._status = "error"
                self._message = str(e)
                return {"ok": False, "error": str(e)}

    def start_record(self, output_path: str, package: str, device: str, work_dir: str) -> dict:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                return {"ok": False, "error": "Process already running"}

            # Pre-flight: verify ADB connectivity and device presence
            try:
                prefix = ["adb", "-s", device] if device else ["adb"]
                chk = subprocess.run(
                    prefix + ["get-state"],
                    capture_output=True, text=True, timeout=5,
                )
                state = chk.stdout.strip()
                if chk.returncode != 0 or "device" not in state:
                    stderr_hint = chk.stderr.strip()[:200] if chk.stderr else ""
                    return {"ok": False, "error": f"No Android device connected (state={state}). {stderr_hint}".strip()}
            except FileNotFoundError:
                return {"ok": False, "error": "ADB not found in PATH"}
            except subprocess.TimeoutExpired:
                return {"ok": False, "error": "ADB timed out - check connection"}

            # Resolve recorder script path (handle underscore vs hyphen naming)
            script = str(Path(__file__).parent / "universal_recorder.py")
            if not Path(script).exists():
                alt = str(Path(__file__).parent / "universal-recorder.py")
                if Path(alt).exists():
                    script = alt
                else:
                    return {"ok": False, "error": "Recorder script not found"}

            cmd = [
                sys.executable, script,
                "--mode", "raw",
                "--annotate",
                "-p", package,
                "-o", output_path,
            ]
            if device:
                cmd += ["-s", device]

            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.PIPE,
                    text=True,
                    cwd=work_dir,
                    preexec_fn=os.setsid,
                )
                self._status = "recording"
                self._frames_count = 0
                self._message = ""
                self._start_monitor()
            except Exception as e:
                self._status = "error"
                self._message = str(e)
                return {"ok": False, "error": str(e)}

        # Brief check outside lock: verify process survived startup
        time.sleep(0.5)
        with self._lock:
            if self._proc and self._proc.poll() is not None:
                err_msg = self._message or "Recording process exited immediately"
                self._status = "idle"
                self._proc = None
                return {"ok": False, "error": err_msg}

        return {"ok": True}

    def stop(self) -> dict:
        with self._lock:
            if not self._proc or self._proc.poll() is not None:
                self._status = "idle"
                self._proc = None
                return {"ok": True}

            try:
                # Send SIGINT (Ctrl+C) for graceful shutdown
                os.killpg(os.getpgid(self._proc.pid), signal.SIGINT)
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
                    self._proc.wait(timeout=3)
            except (ProcessLookupError, OSError):
                pass

            self._status = "idle"
            self._proc = None
            return {"ok": True}

    def _start_monitor(self) -> None:
        if self._monitor_thread and self._monitor_thread.is_alive():
            return
        self._monitor_thread = threading.Thread(target=self._monitor_output, daemon=True)
        self._monitor_thread.start()

    def _monitor_output(self) -> None:
        """Read subprocess stdout to track progress."""
        proc = self._proc
        if not proc or not proc.stdout:
            return

        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue

                with self._lock:
                    # Track player progress
                    if "STEP" in line or "> " in line:
                        self._current_step += 1
                    if "Frames:" in line:
                        try:
                            parts = line.split("Frames:")[1].strip()
                            num = int(parts.split(",")[0].strip())
                            self._total_steps = num
                        except (ValueError, IndexError):
                            pass
                    # Track recorder frames
                    if "Touch DOWN" in line or "Touch UP" in line:
                        self._frames_count += 1

                    # Print to server console for debugging
                    print(f"  [{self._status}] {line}", flush=True)

        except Exception:
            pass
        finally:
            with self._lock:
                if self._proc == proc:
                    self._status = "idle"
                    self._proc = None


# Global process manager
_proc_manager = ProcessManager()


class SequenceProcessManager:
    """Manages a background sequence runner subprocess."""

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._status: str = "idle"
        self._current_loop: int = 0
        self._current_script_idx: int = 0
        self._current_script_name: str = ""
        self._total_scripts: int = 0
        self._message: str = ""
        self._lock = threading.Lock()
        self._monitor_thread: Optional[threading.Thread] = None

    @property
    def status(self) -> str:
        with self._lock:
            if self._proc and self._proc.poll() is not None:
                self._status = "idle"
                self._proc = None
            return self._status

    def get_status_dict(self) -> dict:
        with self._lock:
            if self._proc and self._proc.poll() is not None:
                self._status = "idle"
                self._proc = None
            return {
                "status": self._status,
                "current_loop": self._current_loop,
                "current_script_idx": self._current_script_idx,
                "current_script_name": self._current_script_name,
                "total_scripts": self._total_scripts,
                "message": self._message,
            }

    def start(self, seq_file: str, work_dir: str) -> dict:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                return {"ok": False, "error": "Sequence already running"}

            script = str(Path(__file__).parent / "sequence_runner.py")
            if not Path(script).exists():
                return {"ok": False, "error": "sequence_runner.py not found"}

            cmd = [sys.executable, "-u", script, "--sequence", seq_file]

            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    cwd=work_dir,
                    preexec_fn=os.setsid,
                )
                self._status = "running"
                self._current_loop = 0
                self._current_script_idx = 0
                self._current_script_name = ""
                self._message = ""
                self._start_monitor()
                return {"ok": True}
            except Exception as e:
                self._status = "error"
                self._message = str(e)
                return {"ok": False, "error": str(e)}

    def stop(self) -> dict:
        with self._lock:
            if not self._proc or self._proc.poll() is not None:
                self._status = "idle"
                self._proc = None
                return {"ok": True}

            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGINT)
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
                    self._proc.wait(timeout=3)
            except (ProcessLookupError, OSError):
                pass

            self._status = "idle"
            self._proc = None
            return {"ok": True}

    def _start_monitor(self) -> None:
        if self._monitor_thread and self._monitor_thread.is_alive():
            return
        self._monitor_thread = threading.Thread(target=self._monitor_output, daemon=True)
        self._monitor_thread.start()

    def _monitor_output(self) -> None:
        proc = self._proc
        if not proc or not proc.stdout:
            return

        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue

                with self._lock:
                    # Parse sequence runner output
                    if "Sequence Loop" in line:
                        try:
                            loop_num = int(line.split("Sequence Loop")[1].split("===")[0].strip())
                            self._current_loop = loop_num
                        except (ValueError, IndexError):
                            pass
                    if "Script [" in line:
                        try:
                            part = line.split("Script [")[1]
                            idx_part = part.split("]")[0]
                            cur, total = idx_part.split("/")
                            self._current_script_idx = int(cur)
                            self._total_scripts = int(total)
                            name_part = part.split("]: ")[1] if "]: " in part else ""
                            self._current_script_name = name_part.split(" ---")[0].strip()
                        except (ValueError, IndexError):
                            pass

                    print(f"  [sequence] {line}", flush=True)

        except Exception:
            pass
        finally:
            with self._lock:
                if self._proc == proc:
                    self._status = "idle"
                    self._proc = None


_seq_manager = SequenceProcessManager()

# UI dump cache for Ctrl+hover inspect (avoids repeated slow uiautomator dumps)
_ui_dump_cache_lock = threading.Lock()
_ui_dump_cache: dict = {"xml": "", "time": 0.0, "serial": ""}
_UI_DUMP_TTL = 3.0  # seconds


EDITOR_HTML = r"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Flow Editor - Android Automation</title>
<style>
:root {
  --bg: #0d1117; --bg2: #161b22; --bg3: #1c2128; --border: #30363d;
  --text: #e6edf3; --text2: #8b949e; --blue: #58a6ff; --green: #3fb950;
  --red: #f85149; --orange: #d29922; --purple: #bc8cff; --cyan: #39d2c0;
  --pink: #f778ba;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, 'Segoe UI', system-ui, sans-serif; background: var(--bg); color: var(--text); font-size: 14px; }
button { font-family: inherit; cursor: pointer; }
input, select, textarea { font-family: inherit; background: var(--bg); color: var(--text); border: 1px solid var(--border); border-radius: 6px; padding: 6px 10px; font-size: 13px; }
input:focus, select:focus, textarea:focus { outline: none; border-color: var(--blue); }

/* === LAYOUT === */
.app { display: flex; flex-direction: column; height: 100vh; }
.topbar { background: var(--bg2); border-bottom: 1px solid var(--border); padding: 8px 16px; display: flex; align-items: center; gap: 12px; flex-shrink: 0; position: relative; z-index: 20; }
.topbar h1 { font-size: 15px; font-weight: 600; white-space: nowrap; }
.topbar .spacer { flex: 1; }
.main { display: flex; flex: 1; overflow: hidden; }
.sidebar { width: 280px; background: var(--bg2); border-right: 1px solid var(--border); display: flex; flex-direction: column; flex-shrink: 0; overflow-y: auto; }
.content { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
.timeline { flex: 1; overflow-y: auto; padding: 12px; }
.bottom-panel { display: flex; border-top: 2px solid var(--blue); flex-shrink: 0; min-height: 120px; max-height: 320px; background: var(--bg2); }
.bottom-panel > .bp-col { flex: 1; overflow-y: auto; border-right: 1px solid var(--border); min-width: 0; }
.bottom-panel > .bp-col:last-child { border-right: none; }
.bottom-panel .sidebar-section { border-bottom: 1px solid var(--border); padding: 10px 12px; }
.bottom-panel .sidebar-section:last-child { border-bottom: none; }
.bottom-panel .sidebar-section h3 { font-size: 11px; text-transform: uppercase; letter-spacing: .7px; color: var(--blue); margin-bottom: 6px; }
/* detail-panel styles now in .detail-section and .right-panel */

/* === BUTTONS === */
.btn { padding: 5px 14px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; font-weight: 500; background: var(--bg3); color: var(--text); transition: all .15s; }
.btn:hover { border-color: var(--text2); }
.btn-sm { padding: 3px 10px; font-size: 12px; }
.btn-primary { background: #238636; border-color: #238636; color: #fff; }
.btn-primary:hover { background: #2ea043; }
.btn-danger { background: #da3633; border-color: #da3633; color: #fff; }
.btn-danger:hover { background: #f85149; }
.btn-blue { background: #1f6feb; border-color: #1f6feb; color: #fff; }
.btn-blue:hover { background: #388bfd; }
.btn-group { display: flex; gap: 4px; }

/* === SIDEBAR === */
.sidebar-section { padding: 12px; border-bottom: 1px solid var(--border); }
.sidebar-section h3 { font-size: 11px; text-transform: uppercase; letter-spacing: .7px; color: var(--text2); margin-bottom: 8px; }
.file-list { max-height: 200px; overflow-y: auto; }
.file-item { padding: 6px 8px; border-radius: 4px; cursor: pointer; font-size: 13px; display: flex; align-items: center; gap: 6px; }
.file-item:hover { background: var(--bg3); }
.file-item.active { background: #1f6feb33; color: var(--blue); }
.btn-delete-file { background: none; border: none; color: var(--text2); cursor: pointer; font-size: 16px; padding: 0 4px; line-height: 1; border-radius: 3px; opacity: 0; transition: all .15s; flex-shrink: 0; }
.file-item:hover .btn-delete-file { opacity: 1; }
.btn-delete-file:hover { color: #f85149; background: rgba(248,81,73,.15); }
.file-icon { font-size: 11px; opacity: .6; }
.meta-row { display: flex; justify-content: space-between; font-size: 12px; margin-bottom: 4px; }
.meta-row .label { color: var(--text2); }
.meta-row .value { color: var(--text); font-weight: 500; }
.setting-row { margin-bottom: 8px; }
.setting-row label { display: block; font-size: 11px; color: var(--text2); margin-bottom: 3px; text-transform: uppercase; letter-spacing: .5px; }
.setting-row input, .setting-row select { width: 100%; }
.setting-row .inline { display: flex; gap: 8px; align-items: center; }
.setting-row .inline input[type="checkbox"] { width: auto; }

/* === TOOLBAR === */
.toolbar { background: var(--bg2); border-bottom: 1px solid var(--border); padding: 8px 12px; display: flex; gap: 8px; align-items: center; flex-shrink: 0; flex-wrap: wrap; }
.toolbar .sep { width: 1px; height: 24px; background: var(--border); margin: 0 4px; }
.view-tabs { display: flex; border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
.view-tab { padding: 4px 12px; font-size: 12px; background: var(--bg3); color: var(--text2); border: none; border-right: 1px solid var(--border); cursor: pointer; }
.view-tab:last-child { border-right: none; }
.view-tab.active { background: #1f6feb; color: #fff; }
.frame-count { font-size: 12px; color: var(--text2); margin-left: 8px; }

/* === FRAME CARD === */
.frame-card { display: flex; align-items: stretch; margin-bottom: 4px; border-radius: 6px; border: 1px solid var(--border); background: var(--bg2); transition: all .15s; cursor: pointer; position: relative; }
.frame-card:hover { border-color: var(--text2); }
.frame-card.selected { border-color: var(--blue); box-shadow: 0 0 0 1px var(--blue); }
.frame-card.disabled { opacity: .4; }
.frame-card.playing { border-color: #f0e68c; box-shadow: 0 0 0 2px #f0e68c88, 0 0 12px #f0e68c44; background: #f0e68c10; }
@keyframes playingPulse { 0%,100% { box-shadow: 0 0 0 2px #f0e68c88, 0 0 12px #f0e68c44; } 50% { box-shadow: 0 0 0 2px #f0e68ccc, 0 0 18px #f0e68c66; } }
.frame-card.playing { animation: playingPulse 1.5s ease-in-out infinite; }
.frame-card.dragging { opacity: .5; border-style: dashed; }
.frame-type-bar { width: 4px; border-radius: 6px 0 0 6px; flex-shrink: 0; }
.type-touch { background: var(--orange); }
.type-gesture { background: var(--green); }
.type-element { background: var(--blue); }
.type-app { background: var(--purple); }
.type-key { background: var(--cyan); }
.type-screenshot { background: var(--pink); }
.type-wait { background: var(--text2); }
.type-shell { background: var(--red); }
.type-extract { background: #f0e68c; }
.type-marker { background: #555; }
.frame-body { flex: 1; padding: 8px 10px; min-width: 0; }
.frame-header { display: flex; align-items: center; gap: 8px; }
.frame-id { font-size: 11px; color: var(--text2); min-width: 28px; }
.frame-time { font-size: 11px; color: var(--text2); min-width: 55px; font-family: monospace; }
.frame-type-badge { font-size: 11px; padding: 1px 8px; border-radius: 10px; font-weight: 600; text-transform: uppercase; }
.badge-touch { background: #d2992233; color: var(--orange); }
.badge-gesture { background: #3fb95033; color: var(--green); }
.badge-element { background: #58a6ff33; color: var(--blue); }
.badge-app { background: #bc8cff33; color: var(--purple); }
.badge-key { background: #39d2c033; color: var(--cyan); }
.badge-screenshot { background: #f778ba33; color: var(--pink); }
.badge-wait { background: #8b949e33; color: var(--text2); }
.badge-shell { background: #f8514933; color: var(--red); }
.badge-extract { background: #f0e68c33; color: #f0e68c; }
.badge-marker { background: #55555533; color: #888; }
.frame-desc { font-size: 13px; margin-top: 3px; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.frame-note { font-size: 11px; color: var(--text2); font-style: italic; margin-top: 2px; }
.frame-actions { display: flex; flex-direction: column; justify-content: center; gap: 2px; padding: 4px 8px; }
.frame-actions button { background: none; border: none; color: var(--text2); font-size: 14px; padding: 2px 4px; border-radius: 3px; }
.frame-actions button:hover { background: var(--bg3); color: var(--text); }
.drop-indicator { height: 3px; background: var(--blue); border-radius: 2px; margin: 2px 0; }

/* === DETAIL PANEL === */
/* detail-panel padding now handled by .detail-section */
.detail-header { padding: 12px; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 8px; }
.detail-header h3 { font-size: 14px; flex: 1; }
.detail-body { padding: 12px; }
.field { margin-bottom: 10px; }
.field label { display: block; font-size: 11px; text-transform: uppercase; letter-spacing: .5px; color: var(--text2); margin-bottom: 3px; }
.field input, .field select, .field textarea { width: 100%; }
.field textarea { min-height: 60px; resize: vertical; }
.field-row { display: flex; gap: 8px; }
.field-row .field { flex: 1; }

/* === ADD FRAME MODAL === */
.modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,.6); z-index: 100; display: flex; align-items: center; justify-content: center; }
.modal { background: var(--bg2); border: 1px solid var(--border); border-radius: 12px; width: 500px; max-height: 80vh; overflow-y: auto; }
.modal-header { padding: 16px; border-bottom: 1px solid var(--border); display: flex; align-items: center; }
.modal-header h2 { flex: 1; font-size: 16px; }
.modal-body { padding: 16px; }
.modal-footer { padding: 12px 16px; border-top: 1px solid var(--border); display: flex; justify-content: flex-end; gap: 8px; }
.type-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
.type-option { padding: 12px; border: 1px solid var(--border); border-radius: 8px; cursor: pointer; text-align: center; transition: all .15s; }
.type-option:hover { border-color: var(--blue); background: #1f6feb11; }
.type-option.selected { border-color: var(--blue); background: #1f6feb22; }
.type-option .icon { font-size: 20px; margin-bottom: 4px; }
.type-option .name { font-size: 12px; font-weight: 600; }

/* === TOAST === */
.toast { position: fixed; bottom: 20px; right: 20px; background: var(--bg3); border: 1px solid var(--border); padding: 10px 20px; border-radius: 8px; z-index: 200; animation: fadeIn .2s; }
.toast.success { border-color: var(--green); color: var(--green); }
.toast.error { border-color: var(--red); color: var(--red); }
@keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }

/* === HOVER SCOPE HIGHLIGHT === */
[data-hover-scope] { transition: box-shadow .15s, outline .15s; position: relative; }
[data-hover-scope].hover-active { outline: 1px solid var(--blue); outline-offset: -1px; box-shadow: inset 0 0 0 1px rgba(88,166,255,.12); }
.hover-scope-label { position: absolute; top: 2px; right: 4px; font-size: 9px; color: var(--blue); background: rgba(88,166,255,.15); padding: 1px 5px; border-radius: 3px; pointer-events: none; z-index: 50; opacity: 0; transition: opacity .15s; text-transform: uppercase; letter-spacing: .5px; }
[data-hover-scope].hover-active > .hover-scope-label { opacity: 1; }

/* === PLAY/REC CONTROLS === */
.playback-controls { display: flex; align-items: center; gap: 8px; }
.btn-play { background: #238636; border-color: #238636; color: #fff; display: inline-flex; align-items: center; gap: 6px; position: relative; z-index: 1; pointer-events: auto; cursor: pointer; transition: all .2s ease; }
.btn-play:hover { background: #2ea043; transform: scale(1.05); }
.btn-play:active { transform: scale(0.95); }
.btn-play:disabled { opacity: .4; cursor: not-allowed; transform: none; pointer-events: none; }
.btn-play.active { background: #1a7f37; animation: pulse-green 1.5s ease-in-out infinite; }
.btn-play.active .play-icon { animation: spin-play 1s linear infinite; }
.btn-rec { background: #da3633; border-color: #da3633; color: #fff; display: inline-flex; align-items: center; gap: 6px; position: relative; z-index: 1; pointer-events: auto; cursor: pointer; transition: all .2s ease; }
.btn-rec:hover { background: #f85149; transform: scale(1.05); }
.btn-rec:active { transform: scale(0.95); }
.btn-rec:disabled { opacity: .4; cursor: not-allowed; transform: none; pointer-events: none; }
.btn-rec.active { background: #b62324; animation: pulse-red 1.5s ease-in-out infinite; border-color: #ff4444; }
.btn-stop { background: #6e7681; border-color: #6e7681; color: #fff; display: inline-flex; align-items: center; gap: 6px; position: relative; z-index: 1; pointer-events: auto; cursor: pointer; transition: all .2s ease; }
.btn-stop:hover { background: #8b949e; transform: scale(1.05); }
.btn-stop:active { transform: scale(0.95); }
.btn-stop:disabled { opacity: .4; cursor: not-allowed; transform: none; pointer-events: none; }

/* Loop Run button */
.btn-loop-run { background: #8957e5; border-color: #8957e5; color: #fff; display: inline-flex; align-items: center; gap: 5px; position: relative; z-index: 1; pointer-events: auto; cursor: pointer; transition: all .2s ease; font-weight: 600; }
.btn-loop-run:hover { background: #a371f7; transform: scale(1.05); }
.btn-loop-run:active { transform: scale(0.95); }
.btn-loop-run:disabled { opacity: .4; cursor: not-allowed; transform: none; pointer-events: none; }
.btn-loop-run.active { background: #6e40c9; animation: pulse-loop 1.5s ease-in-out infinite; }
@keyframes pulse-loop { 0%,100% { box-shadow: 0 0 0 0 rgba(137,87,229,.5); } 50% { box-shadow: 0 0 8px 4px rgba(137,87,229,.3); } }
.loop-run-controls { display: flex; align-items: center; gap: 6px; }
.loop-run-controls input[type="number"] { width: 48px; padding: 2px 4px; border: 1px solid var(--border); border-radius: 4px; background: var(--bg2); color: var(--text1); font-size: 11px; text-align: center; }
.loop-run-controls label { font-size: 10px; color: var(--text2); display: flex; align-items: center; gap: 3px; cursor: pointer; white-space: nowrap; }
.loop-run-controls label input[type="checkbox"] { margin: 0; cursor: pointer; }
.loop-run-status { font-size: 11px; color: var(--text2); max-width: 240px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.btn-loop-stop { background: #6e7681; border-color: #6e7681; color: #fff; display: inline-flex; align-items: center; gap: 4px; cursor: pointer; transition: all .2s ease; }
.btn-loop-stop:hover { background: #8b949e; transform: scale(1.05); }

/* Visual feedback on click (no overlay blocking clicks) */

.rec-dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; background: #ff4444; transition: all .2s; }
.rec-dot.active { animation: blink-dot 1s ease-in-out infinite; box-shadow: 0 0 6px 2px rgba(255, 68, 68, 0.6); }
.play-icon { font-size: 12px; display: inline-block; }

.playback-status { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--text2); }
.playback-status .status-badge { padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; text-transform: uppercase; transition: all .3s ease; }
.status-idle { background: #6e768133; color: #8b949e; }
.status-recording { background: #da363344; color: #f85149; animation: badge-glow-red 2s ease-in-out infinite; }
.status-playing { background: #23863644; color: #3fb950; animation: badge-glow-green 2s ease-in-out infinite; }
.status-error { background: #da363333; color: #f85149; animation: shake .5s ease-in-out; }

.playback-progress { flex: 1; max-width: 200px; height: 6px; background: var(--bg3); border-radius: 3px; overflow: hidden; position: relative; }
.playback-progress-bar { height: 100%; background: linear-gradient(90deg, #238636, #3fb950); border-radius: 3px; transition: width .3s ease; width: 0%; position: relative; }
.playback-progress-bar::after { content: ''; position: absolute; inset: 0; background: linear-gradient(90deg, transparent, rgba(255,255,255,.15), transparent); animation: shimmer 2s infinite; }
.playback-progress-bar.recording { background: linear-gradient(90deg, #da3633, #f85149); animation: rec-pulse-bar 1.5s ease-in-out infinite; }

.playback-step { font-size: 11px; color: var(--text2); font-family: monospace; min-width: 60px; }
.playback-timer { font-size: 12px; color: var(--text); font-family: monospace; font-weight: 600; min-width: 55px; letter-spacing: .5px; }
.playback-timer.recording { color: #f85149; }
.playback-timer.playing { color: #3fb950; }

@keyframes pulse-green {
  0%, 100% { box-shadow: 0 0 0 0 rgba(35, 134, 54, 0.6); }
  50% { box-shadow: 0 0 0 8px rgba(35, 134, 54, 0); }
}
@keyframes pulse-red {
  0%, 100% { box-shadow: 0 0 0 0 rgba(218, 54, 51, 0.6); }
  50% { box-shadow: 0 0 0 8px rgba(218, 54, 51, 0); }
}
@keyframes blink-dot {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.2; transform: scale(0.8); }
}
@keyframes badge-glow-red {
  0%, 100% { box-shadow: 0 0 0 0 rgba(218, 54, 51, 0); }
  50% { box-shadow: 0 0 8px 2px rgba(218, 54, 51, 0.3); }
}
@keyframes badge-glow-green {
  0%, 100% { box-shadow: 0 0 0 0 rgba(35, 134, 54, 0); }
  50% { box-shadow: 0 0 8px 2px rgba(35, 134, 54, 0.3); }
}
@keyframes shimmer {
  0% { transform: translateX(-100%); }
  100% { transform: translateX(100%); }
}
@keyframes rec-pulse-bar {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.7; }
}
@keyframes shake {
  0%, 100% { transform: translateX(0); }
  25% { transform: translateX(-4px); }
  75% { transform: translateX(4px); }
}
@keyframes ctrlCapturePulse {
  0% { opacity: 1; transform: translate(-50%, -50%) scale(0.8); }
  50% { opacity: 1; transform: translate(-50%, -50%) scale(1.1); }
  100% { opacity: 0; transform: translate(-50%, -50%) scale(1.3); }
}
@keyframes spin-play {
  0% { transform: rotate(0deg); }
  100% { transform: rotate(360deg); }
}

/* === SCROLLBAR === */
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: var(--text2); }

/* === RIGHT PANEL (Device + Detail) === */
.right-panel {
  width: 370px; background: var(--bg2); border-left: 1px solid var(--border);
  display: flex; flex-direction: column; flex-shrink: 0; overflow: hidden;
}
.right-panel-tabs {
  display: flex; border-bottom: 1px solid var(--border); flex-shrink: 0;
}
.right-panel-tab {
  flex: 1; padding: 8px 12px; font-size: 12px; font-weight: 600;
  text-align: center; cursor: pointer; border: none;
  background: var(--bg3); color: var(--text2); transition: all .15s;
  text-transform: uppercase; letter-spacing: .5px;
}
.right-panel-tab:hover { color: var(--text); }
.right-panel-tab.active { background: var(--bg2); color: var(--blue); border-bottom: 2px solid var(--blue); }
.right-tab-content { display: none; flex-direction: column; flex: 1; overflow: hidden; }
.right-tab-content.active { display: flex; }

/* === INSPECT TOOLTIP (Ctrl+Hover) === */
.inspect-tooltip {
  position: absolute; z-index: 100; background: rgba(13,17,23,0.95);
  border: 1px solid var(--blue); border-radius: 8px; padding: 10px 14px;
  font-size: 11px; color: var(--text); max-width: 340px; pointer-events: none;
  box-shadow: 0 4px 16px rgba(0,0,0,.5); backdrop-filter: blur(4px);
  white-space: pre-wrap; word-break: break-all;
}
.inspect-tooltip .it-header { font-weight: 700; color: var(--blue); margin-bottom: 6px; font-size: 12px; }
.inspect-tooltip .it-row { margin-bottom: 3px; line-height: 1.4; }
.inspect-tooltip .it-label { color: var(--text2); }
.inspect-tooltip .it-value { color: var(--text); font-family: monospace; font-size: 10px; }
.inspect-tooltip .it-text { color: var(--green); font-weight: 600; }
.inspect-highlight {
  position: absolute; border: 2px solid var(--green); background: rgba(63,185,80,0.12);
  pointer-events: none; z-index: 90; border-radius: 2px; transition: all .1s;
}
.rec-web-indicator {
  display: inline-flex; align-items: center; gap: 4px; font-size: 10px;
  background: rgba(218,54,51,.15); color: #f85149; padding: 2px 8px;
  border-radius: 10px; font-weight: 600; margin-left: 4px;
}
.rec-web-indicator.mobile { background: rgba(63,185,80,.15); color: #3fb950; }

/* Touch events for mobile browser */
.screen-container { touch-action: none; }

/* Device tab */
.device-section { display: flex; flex-direction: column; flex: 1; overflow-y: auto; }
.device-header {
  padding: 8px 12px; border-bottom: 1px solid var(--border);
  display: flex; align-items: center; gap: 8px; flex-shrink: 0;
}
.device-header h3 { font-size: 13px; flex: 1; white-space: nowrap; }
.device-controls { display: flex; align-items: center; gap: 6px; font-size: 12px; }
.screen-container {
  position: relative; overflow: hidden; flex-shrink: 0; background: #000;
  cursor: crosshair;
}
.screen-container img { width: 100%; display: block; }
.screen-container .element-overlay {
  position: absolute; border: 1.5px solid var(--blue);
  background: rgba(88,166,255,0.08); cursor: pointer;
  transition: background .15s; z-index: 10;
}
.screen-container .element-overlay:hover {
  background: rgba(88,166,255,0.25); border-color: #fff; z-index: 11;
}
.screen-container .element-overlay .el-label {
  position: absolute; bottom: -1px; left: 0; font-size: 8px;
  background: var(--blue); color: #000; padding: 0 3px;
  white-space: nowrap; max-width: 100%; overflow: hidden;
  text-overflow: ellipsis; pointer-events: none; line-height: 1.4;
}
.touch-feedback {
  position: absolute; width: 30px; height: 30px; border-radius: 50%;
  border: 2px solid var(--blue); background: rgba(88,166,255,0.3);
  pointer-events: none; transform: translate(-50%, -50%);
  animation: touchPulse 0.5s ease-out forwards; z-index: 50;
}
@keyframes touchPulse {
  0% { transform: translate(-50%, -50%) scale(0.5); opacity: 1; }
  100% { transform: translate(-50%, -50%) scale(1.5); opacity: 0; }
}
.swipe-line {
  position: absolute; pointer-events: none; z-index: 50;
  border: 2px dashed var(--green); opacity: 0.8;
}
.nav-bar {
  display: flex; justify-content: center; gap: 20px; padding: 10px;
  background: var(--bg3); border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border); flex-shrink: 0;
}
.nav-btn {
  width: 42px; height: 42px; border: 1px solid var(--border); border-radius: 50%;
  background: var(--bg2); color: var(--text); font-size: 18px; cursor: pointer;
  display: flex; align-items: center; justify-content: center; transition: all .15s;
}
.nav-btn:hover { background: var(--bg3); border-color: var(--text2); }
.nav-btn:active { transform: scale(0.88); background: var(--blue); color: #fff; }
.quick-controls {
  padding: 8px 10px; display: flex; gap: 6px; flex-shrink: 0;
  border-bottom: 1px solid var(--border);
}
.quick-controls input { flex: 1; }
.extra-controls {
  padding: 6px 10px; display: flex; gap: 4px; flex-wrap: wrap; flex-shrink: 0;
  border-bottom: 1px solid var(--border);
}
.ctrl-divider {
  width: 1px; height: 18px; background: var(--border); flex-shrink: 0;
}
.btn-swipe-dir { font-size: 11px; min-width: 28px; padding: 3px 6px; }
.btn-swipe-dir:active { background: var(--blue) !important; color: #fff !important; transform: scale(0.9); }
.swipe-settings-row {
  padding: 4px 10px; display: flex; align-items: center; gap: 10px; flex-shrink: 0;
  border-bottom: 1px solid var(--border); font-size: 11px; color: var(--text2);
}
.swipe-settings-row label { display: flex; align-items: center; gap: 4px; }
.swipe-settings-row input { width: 56px; padding: 2px 4px; font-size: 11px; }
.device-status {
  padding: 5px 12px; font-size: 11px; color: var(--text2);
  text-align: center; flex-shrink: 0; border-top: 1px solid var(--border);
}
.no-screen {
  color: var(--text2); padding: 40px 20px; text-align: center;
  border: 2px dashed var(--border); border-radius: 8px; margin: 12px;
}

/* Element list in device panel */
.element-list-panel { flex: 1; overflow-y: auto; padding: 8px; }
.element-list-panel h4 {
  font-size: 11px; text-transform: uppercase; letter-spacing: .5px;
  color: var(--text2); margin-bottom: 6px;
}
.el-item {
  padding: 5px 8px; border-radius: 4px; font-size: 11px;
  cursor: pointer; border: 1px solid transparent; margin-bottom: 2px;
}
.el-item:hover { background: var(--bg3); border-color: var(--border); }
.el-item .el-id { color: var(--blue); font-weight: 600; }
.el-item .el-text { color: var(--text); }
.el-item .el-class { color: var(--text2); font-size: 10px; }

/* Detail tab */
.detail-section { display: flex; flex-direction: column; flex: 1; overflow-y: auto; }
/* btn-realtime-on removed - device panel is always visible */

/* === DEBUG FRAME OVERLAY === */
.btn-debug {
  padding: 3px 8px; font-size: 11px; font-weight: 600; border-radius: 4px;
  border: 1px solid var(--border); background: var(--bg3); color: var(--text2);
  cursor: pointer; transition: all .2s; display: inline-flex; align-items: center; gap: 4px;
}
.btn-debug:hover { border-color: var(--orange); color: var(--orange); }
.btn-debug.active {
  background: rgba(210,153,34,0.2); border-color: var(--orange); color: var(--orange);
  box-shadow: 0 0 6px rgba(210,153,34,0.3);
}
.btn-debug .debug-dot {
  width: 6px; height: 6px; border-radius: 50%; background: var(--text2); transition: all .2s;
}
.btn-debug.active .debug-dot {
  background: var(--orange); box-shadow: 0 0 4px var(--orange);
  animation: debug-blink 1.5s ease-in-out infinite;
}
@keyframes debug-blink {
  0%, 100% { opacity: 1; } 50% { opacity: 0.4; }
}
.debug-overlay-rect {
  position: absolute; pointer-events: none; z-index: 8;
  border-radius: 4px; transition: opacity .15s;
}
.debug-overlay-rect.clickable {
  border: 2px solid rgba(63,185,80,0.85);
  background: rgba(63,185,80,0.08);
  box-shadow: 0 0 0 1px rgba(63,185,80,0.15), inset 0 0 8px rgba(63,185,80,0.05);
}
.debug-overlay-rect.has-text {
  border: 2px solid rgba(210,153,34,0.85);
  background: rgba(210,153,34,0.08);
  box-shadow: 0 0 0 1px rgba(210,153,34,0.15), inset 0 0 8px rgba(210,153,34,0.05);
}
.debug-overlay-rect.has-id {
  border: 2px solid rgba(88,166,255,0.85);
  background: rgba(88,166,255,0.08);
  box-shadow: 0 0 0 1px rgba(88,166,255,0.15), inset 0 0 8px rgba(88,166,255,0.05);
}
.debug-overlay-rect.other {
  border: 1px dashed rgba(139,148,158,0.4);
  background: rgba(139,148,158,0.03);
}
/* Card-style label header */
.debug-overlay-rect .debug-card-header {
  position: absolute; top: -1px; left: -1px; right: -1px;
  display: flex; align-items: center; gap: 3px;
  padding: 1px 4px; font-size: 7px; line-height: 1.4;
  border-radius: 3px 3px 0 0; pointer-events: none;
  white-space: nowrap; overflow: hidden;
}
.debug-overlay-rect.clickable .debug-card-header {
  background: rgba(63,185,80,0.9); color: #000;
}
.debug-overlay-rect.has-text .debug-card-header {
  background: rgba(210,153,34,0.9); color: #000;
}
.debug-overlay-rect.has-id .debug-card-header {
  background: rgba(88,166,255,0.9); color: #000;
}
.debug-overlay-rect.other .debug-card-header {
  background: rgba(139,148,158,0.6); color: #000;
}
/* Index badge */
.debug-card-header .debug-idx {
  display: inline-flex; align-items: center; justify-content: center;
  min-width: 12px; height: 12px; border-radius: 6px;
  background: rgba(0,0,0,0.3); color: #fff; font-size: 7px;
  font-weight: 700; padding: 0 2px; flex-shrink: 0;
}
/* Class/type tag */
.debug-card-header .debug-cls {
  font-weight: 700; text-overflow: ellipsis; overflow: hidden; flex-shrink: 0; max-width: 70px;
}
/* Text/ID info */
.debug-card-header .debug-info {
  font-weight: 400; opacity: 0.85; text-overflow: ellipsis; overflow: hidden; flex: 1;
}
/* Bottom info bar (bounds/center) */
.debug-overlay-rect .debug-card-footer {
  position: absolute; bottom: -1px; left: -1px; right: -1px;
  padding: 0 4px; font-size: 6px; line-height: 1.5;
  border-radius: 0 0 3px 3px; pointer-events: none;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  font-family: 'Consolas', monospace; opacity: 0.8;
}
.debug-overlay-rect.clickable .debug-card-footer { background: rgba(63,185,80,0.7); color: #000; }
.debug-overlay-rect.has-text .debug-card-footer { background: rgba(210,153,34,0.7); color: #000; }
.debug-overlay-rect.has-id .debug-card-footer { background: rgba(88,166,255,0.7); color: #000; }
.debug-overlay-rect.other .debug-card-footer { background: rgba(139,148,158,0.4); color: #000; }
/* Legend bar */
.debug-legend {
  position: absolute; bottom: 4px; left: 4px; display: flex; gap: 6px;
  padding: 3px 8px; background: rgba(13,17,23,0.85); border-radius: 6px;
  border: 1px solid rgba(48,54,61,0.8); z-index: 12; pointer-events: none;
  font-size: 9px; color: #e1e4e8; backdrop-filter: blur(4px);
}
.debug-legend-item { display: flex; align-items: center; gap: 3px; }
.debug-legend-dot {
  width: 8px; height: 8px; border-radius: 2px; border: 1.5px solid;
}
.debug-legend-dot.lg-click { border-color: #3fb950; background: rgba(63,185,80,0.2); }
.debug-legend-dot.lg-text { border-color: #d29922; background: rgba(210,153,34,0.2); }
.debug-legend-dot.lg-id { border-color: #58a6ff; background: rgba(88,166,255,0.2); }
.debug-legend-dot.lg-other { border-color: #8b949e; background: rgba(139,148,158,0.2); }
.debug-count-badge {
  font-size: 9px; padding: 0 5px; border-radius: 8px; font-weight: 700;
  background: var(--border); color: var(--text2); margin-left: 2px;
  transition: all .2s;
}
.btn-debug.active .debug-count-badge {
  background: rgba(210,153,34,0.3); color: var(--orange);
}
</style>
</head>
<body>
<div class="app" id="app">

  <!-- TOP BAR -->
  <div class="topbar" data-hover-scope="topbar">
    <h1>Flow Editor</h1>
    <span id="fileName" style="color:var(--text2);font-size:13px;">No file loaded</span>
    <div class="sep" style="width:1px;height:24px;background:var(--border);margin:0 4px"></div>
    <button class="btn btn-sm" onclick="newRecording()" title="Create a new empty recording" style="background:#238636;color:#fff;border-color:#238636;font-weight:600">+ New</button>
    <div class="sep" style="width:1px;height:24px;background:var(--border);margin:0 4px"></div>
    <!-- Play/Rec Controls -->
    <div class="playback-controls">
      <button type="button" class="btn btn-sm btn-play" id="btnPlay" title="Play recording">
        <span class="play-icon">&#9654;</span> Play
      </button>
      <button type="button" class="btn btn-sm btn-rec" id="btnRec" title="Record touches">
        <span class="rec-dot" id="recDot"></span> Rec
      </button>
      <button type="button" class="btn btn-sm btn-stop" id="btnStop" disabled title="Stop">
        &#9632; Stop
      </button>
    </div>
    <div class="playback-status" id="playbackStatus">
      <span class="status-badge status-idle" id="statusBadge">IDLE</span>
      <span class="playback-timer" id="playbackTimer" style="display:none">00:00</span>
      <div class="playback-progress" id="progressContainer" style="display:none">
        <div class="playback-progress-bar" id="progressBar"></div>
      </div>
      <span class="playback-step" id="stepInfo"></span>
    </div>
    <div class="sep" style="width:1px;height:24px;background:var(--border);margin:0 4px"></div>
    <!-- Loop Run Controls -->
    <div class="loop-run-controls">
      <button type="button" class="btn btn-sm btn-loop-run" id="btnLoopRun" onclick="loopRunAll()" title="Run all sequence scripts with loop">
        &#9654;&#8635; Loop Run
      </button>
      <label title="Number of loops (0 = forever)">Loops
        <input type="number" id="loopRunCount" value="3" min="0" step="1">
      </label>
      <label title="Run forever until stopped"><input type="checkbox" id="loopRunForever"> Forever</label>
      <button type="button" class="btn btn-sm btn-loop-stop" id="btnLoopStop" onclick="loopRunStop()" style="display:none" title="Stop loop run">
        &#9632; Stop
      </button>
      <span class="loop-run-status" id="loopRunStatus"></span>
    </div>
    <span class="spacer"></span>
    <button class="btn btn-sm" onclick="loadFileList()">Refresh</button>
    <button class="btn btn-sm btn-primary" onclick="saveFile()">Save</button>
    <button class="btn btn-sm" onclick="saveAsFile()">Save As</button>
    <button class="btn btn-sm" onclick="exportScript()">Export Script</button>
  </div>

  <div class="main">

    <!-- SIDEBAR -->
    <div class="sidebar">
      <!-- Files -->
      <div class="sidebar-section" data-hover-scope="files">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
          <h3 style="margin-bottom:0">Files</h3>
          <button class="btn btn-sm" onclick="newRecording()" style="font-size:10px;padding:2px 8px">+ New</button>
        </div>
        <div id="fileFilter" style="margin-bottom:6px">
          <input type="text" id="fileSearch" placeholder="Filter files..." oninput="filterFiles()" style="width:100%;font-size:11px;padding:4px 8px;border-radius:4px;border:1px solid var(--border);background:var(--bg);color:var(--text)">
        </div>
        <div class="file-list" id="fileList" style="max-height:300px"></div>
        <div style="font-size:10px;color:var(--text2);padding:4px 0;margin-top:4px" id="fileCount"></div>
      </div>

      <!-- Station Manager -->
      <div class="sidebar-section" data-hover-scope="stations">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
          <h3 style="margin-bottom:0">Stations</h3>
          <button class="btn btn-sm" onclick="stationCreateAll()" style="font-size:10px;padding:2px 6px" title="Create all missing station files">+ Create All</button>
        </div>
        <div id="stationGrid" style="display:grid;grid-template-columns:repeat(6,1fr);gap:4px;margin-bottom:6px"></div>
        <div style="display:flex;gap:4px;flex-wrap:wrap">
          <button class="btn btn-sm" onclick="stationAddAllToSeq()" style="font-size:10px;padding:2px 6px" title="Add all stations to Sequence Runner">Add All to Seq</button>
          <button class="btn btn-sm" onclick="stationRefresh()" style="font-size:10px;padding:2px 6px">Refresh</button>
        </div>
      </div>

      <!-- Meta -->
      <div class="sidebar-section" id="metaPanel" data-hover-scope="meta">
        <h3>Recording Info</h3>
        <div class="setting-row"><label>Name</label><input id="metaName" onchange="updateMeta()"></div>
        <div class="setting-row"><label>Package</label><input id="metaPkg" onchange="updateMeta()"></div>
        <div class="setting-row"><label>Device</label><input id="metaDev" onchange="updateMeta()"></div>
        <div class="meta-row"><span class="label">Mode</span><span class="value" id="metaMode">-</span></div>
        <div class="meta-row"><span class="label">Frames</span><span class="value" id="metaFrames">0</span></div>
        <div class="meta-row"><span class="label">Duration</span><span class="value" id="metaDur">0s</span></div>
      </div>

      <!-- Settings -->
      <div class="sidebar-section" data-hover-scope="settings">
        <h3>Playback Settings</h3>
        <div class="setting-row"><label>Speed</label><input type="number" step="0.1" min="0.1" id="setSpeed" value="1.0" onchange="updateSettings()"></div>
        <div class="setting-row"><label>Loop</label>
          <div class="inline"><input type="checkbox" id="setLoop" onchange="updateSettings()"><span style="font-size:12px">Enabled</span>
            <input type="number" min="0" id="setLoopCount" value="0" style="width:60px" onchange="updateSettings()" title="0=infinite">
          </div>
        </div>
        <div class="setting-row"><label>Loop Delay (s)</label><input type="number" step="0.5" min="0" id="setLoopDelay" value="5" onchange="updateSettings()"></div>
        <div class="setting-row"><label>On Error</label>
          <select id="setOnError" onchange="updateSettings()">
            <option value="retry">Retry</option><option value="skip">Skip</option>
            <option value="pause">Pause</option><option value="stop">Stop</option>
          </select>
        </div>
        <div class="setting-row"><label>Max Retries</label><input type="number" min="0" id="setRetries" value="3" onchange="updateSettings()"></div>
      </div>

    </div>

    <!-- MAIN CONTENT -->
    <div class="content">
      <!-- Toolbar -->
      <div class="toolbar" data-hover-scope="toolbar">
        <div class="view-tabs">
          <button class="view-tab active" data-view="all" onclick="setView('all',this)">All Frames</button>
          <button class="view-tab" data-view="script" onclick="setView('script',this)">Script Only</button>
          <button class="view-tab" data-view="raw" onclick="setView('raw',this)">Raw Only</button>
        </div>
        <span class="frame-count" id="frameCount">0 frames</span>
        <div class="sep"></div>
        <button class="btn btn-sm btn-primary" onclick="openAddModal(-1)">+ Add Frame</button>
        <button class="btn btn-sm" onclick="insertBefore()" title="Insert before selected">Insert Before</button>
        <button class="btn btn-sm" onclick="duplicateSelected()">Duplicate</button>
        <button class="btn btn-sm btn-danger" onclick="deleteSelected()">Delete</button>
        <div class="sep"></div>
        <button class="btn btn-sm" onclick="toggleSelected()" title="Enable/disable selected">Toggle</button>
        <button class="btn btn-sm" onclick="moveUp()">Move Up</button>
        <button class="btn btn-sm" onclick="moveDown()">Move Down</button>
      </div>

      <!-- Timeline -->
      <div class="timeline" id="timeline" data-hover-scope="timeline"></div>

      <!-- Bottom Panel: moved from sidebar for better visibility -->
      <div class="bottom-panel">
        <div class="bp-col">
          <!-- Bulk Actions -->
          <div class="sidebar-section" data-hover-scope="bulk-actions">
            <h3>Bulk Actions</h3>
            <div class="btn-group" style="flex-wrap:wrap;gap:6px">
              <button class="btn btn-sm" onclick="collapseRaw()" title="Collapse raw touches into gestures">Collapse Raw</button>
              <button class="btn btn-sm" onclick="compactFrames()" title="Remove disabled frames">Compact</button>
              <button class="btn btn-sm" onclick="scaleTime()" title="Scale all timing">Scale Time</button>
              <button class="btn btn-sm" onclick="shiftTime()" title="Shift all timing">Shift Time</button>
              <button class="btn btn-sm btn-danger" onclick="clearAll()" title="Delete all frames">Clear All</button>
            </div>
          </div>
          <!-- Disk Space -->
          <div class="sidebar-section" data-hover-scope="disk-space">
            <h3>Disk Space</h3>
            <div id="diskSpaceWarning" style="display:none;font-size:11px;color:#f85149;background:#f8514920;border:1px solid #f8514940;border-radius:4px;padding:4px 8px;margin-bottom:6px"></div>
            <div id="diskSpaceInfo" style="font-size:11px;color:var(--text2);margin-bottom:6px">Loading...</div>
            <div id="diskSpaceBar" style="height:6px;background:#21262d;border-radius:3px;overflow:hidden;margin-bottom:6px">
              <div id="diskSpaceUsed" style="height:100%;background:var(--green);border-radius:3px;transition:width .3s"></div>
            </div>
            <div id="diskBreakdown" style="font-size:10px;color:var(--text2);margin-bottom:6px;display:none"></div>
            <div class="btn-group" style="gap:4px;flex-wrap:wrap">
              <button class="btn btn-sm" onclick="refreshDiskSpace()" title="Refresh disk space info">Refresh</button>
              <button class="btn btn-sm" onclick="smartClean()" title="Smart clean: keep recent files (6h), delete older ones" style="color:#d29922;border-color:#d2992240">Smart Clean</button>
              <button class="btn btn-sm btn-danger" onclick="deepClean()" title="Deep clean: remove ALL captures, sent images, logs, cache, temp files">Deep Clean</button>
            </div>
            <div style="margin-top:6px;font-size:10px;color:var(--text2);display:flex;align-items:center;gap:6px;flex-wrap:wrap">
              <label style="display:flex;align-items:center;gap:4px;cursor:pointer">
                <input type="checkbox" id="diskAutoCleanEnabled" style="width:auto" onchange="toggleDiskAutoClean(this.checked)"> Auto Clean
              </label>
              <label style="display:flex;align-items:center;gap:4px">Threshold
                <input type="number" id="diskAutoCleanThreshold" min="70" max="98" step="1" value="85" style="width:52px" onchange="updateDiskAutoCleanThreshold(this.value)">%
              </label>
            </div>
            <div id="diskCategoryActions" style="display:none;margin-top:6px;font-size:10px">
              <div class="btn-group" style="gap:3px;flex-wrap:wrap">
                <button class="btn btn-sm" onclick="deleteAllCaptures()" title="Delete all capture screenshots" style="font-size:10px;padding:1px 6px">Del Captures</button>
                <button class="btn btn-sm" onclick="deleteAllSent()" title="Delete all sent images" style="font-size:10px;padding:1px 6px">Del Sent</button>
                <button class="btn btn-sm" onclick="clearAllLogs()" title="Delete all log files" style="font-size:10px;padding:1px 6px">Del Logs</button>
              </div>
            </div>
          </div>
          <!-- Station Data Viewer -->
          <div class="sidebar-section" data-hover-scope="station-data">
            <h3>Station Data</h3>
            <div id="extractLogSummary" style="font-size:11px;color:var(--text2);margin-bottom:6px">No extract logs yet</div>
            <div class="btn-group" style="gap:4px;flex-wrap:wrap">
              <button class="btn btn-sm btn-primary" onclick="window.open('/station-data','_blank')" title="Open station data viewer in new tab">Open Data Viewer</button>
              <button class="btn btn-sm btn-primary" onclick="window.open('/charger-view','_blank')" title="Open charger status view (organized by connector and round)">⚡ Charger View</button>
              <button class="btn btn-sm" onclick="refreshExtractLogs()" title="Refresh extract log count">Refresh</button>
              <button class="btn btn-sm" onclick="downloadExtractLogs()" title="Download all extract logs as ZIP">Download</button>
              <button class="btn btn-sm btn-danger" onclick="deleteAllExtractLogs()" title="Delete all extract log files">Delete All</button>
            </div>
          </div>
          <!-- Station Logs Viewer -->
          <div class="sidebar-section" data-hover-scope="station-logs">
            <h3>Station Logs</h3>
            <div id="stationLogSummary" style="font-size:11px;color:var(--text2);margin-bottom:6px">No playback logs yet</div>
            <div class="btn-group" style="gap:4px;flex-wrap:wrap">
              <button class="btn btn-sm btn-primary" onclick="window.open('/station-logs','_blank')" title="View per-station round-by-round playback logs">Open Log Viewer</button>
              <button class="btn btn-sm" onclick="refreshStationLogs()" title="Refresh station log count">Refresh</button>
              <button class="btn btn-sm" onclick="downloadStationLogs()" title="Download all station logs as ZIP">Download</button>
              <button class="btn btn-sm btn-danger" onclick="deleteAllStationLogs()" title="Delete all station log files">Delete All</button>
            </div>
          </div>
          <!-- Log Management -->
          <div class="sidebar-section" data-hover-scope="log-management">
            <h3>Log Management</h3>
            <div class="btn-group" style="gap:4px;flex-wrap:wrap">
              <button class="btn btn-sm btn-primary" onclick="downloadAllLogs()" title="Download ALL logs (extract + station) as ZIP">Download All Logs</button>
              <button class="btn btn-sm btn-danger" onclick="clearAllLogs()" title="Delete ALL log files to free up space">Clear All Logs</button>
            </div>
          </div>
        </div>
        <div class="bp-col" style="flex:1.5">
          <!-- Navigation Bar -->
          <div class="sidebar-section" data-hover-scope="navigation">
            <h3>Navigation</h3>
            <div class="nav-bar" style="margin:0">
              <button class="nav-btn" onclick="sendKey(4)" title="Back">&#9665;</button>
              <button class="nav-btn" onclick="sendKey(3)" title="Home">&#9711;</button>
              <button class="nav-btn" onclick="sendKey(187)" title="Recent Apps">&#9634;</button>
              <button class="nav-btn" onclick="sendKey(26)" title="Power" style="font-size:14px">&#9211;</button>
            </div>
          </div>
          <!-- Device Controls: Text Input + Extra Keys + Swipe -->
          <div class="sidebar-section" data-hover-scope="device-controls">
            <h3>Device Controls</h3>
            <div class="quick-controls" style="margin-bottom:6px">
              <input type="text" id="textInput" placeholder="Type text and press Enter..." onkeydown="if(event.key==='Enter'){sendText();event.preventDefault();}">
              <button class="btn btn-sm" onclick="sendText()">Send</button>
            </div>
            <div class="extra-controls">
              <button class="btn btn-sm" onclick="sendKey(24)" title="Volume Up">Vol+</button>
              <button class="btn btn-sm" onclick="sendKey(25)" title="Volume Down">Vol-</button>
              <button class="btn btn-sm" onclick="sendKey(67)" title="Delete/Backspace">Del</button>
              <button class="btn btn-sm" onclick="sendKey(61)" title="Tab">Tab</button>
              <button class="btn btn-sm" onclick="sendKey(66)" title="Enter">Enter</button>
              <button class="btn btn-sm" onclick="rotateScreen()" title="Rotate">Rotate</button>
              <button class="btn btn-sm" onclick="sendKey(224)" title="Wake Up">Wake</button>
              <span class="ctrl-divider"></span>
              <button class="btn btn-sm btn-swipe-dir" onclick="sendSwipe('up')" title="Swipe Up">&#9650;</button>
              <button class="btn btn-sm btn-swipe-dir" onclick="sendSwipe('down')" title="Swipe Down">&#9660;</button>
              <button class="btn btn-sm btn-swipe-dir" onclick="sendSwipe('left')" title="Swipe Left">&#9664;</button>
              <button class="btn btn-sm btn-swipe-dir" onclick="sendSwipe('right')" title="Swipe Right">&#9654;</button>
            </div>
            <div class="swipe-settings-row" style="border-bottom:none;padding:0">
              <label>Dist <input type="number" id="swipeDistance" value="500" min="50" max="2000" step="50" title="Swipe distance in device pixels"> px</label>
              <label>Dur <input type="number" id="swipeDuration" value="300" min="100" max="2000" step="50" title="Swipe duration in ms"> ms</label>
            </div>
          </div>
        </div>
        <div class="bp-col" style="flex:1.2">
          <!-- Sequence Runner -->
          <div class="sidebar-section" data-hover-scope="sequence">
            <h3>Sequence Runner</h3>
            <div id="seqList" style="max-height:150px;overflow-y:auto;margin-bottom:8px;font-size:12px"></div>
            <div id="seqFilePicker" style="display:none;margin-bottom:6px"></div>
            <div class="btn-group" style="flex-wrap:wrap;gap:4px">
              <button class="btn btn-sm" onclick="seqAddCurrent()" title="Add current file to sequence">+ Current</button>
              <button class="btn btn-sm" onclick="seqAddFromList()" title="Pick file to add">+ Pick</button>
              <button class="btn btn-sm" onclick="seqAddAllFiles()" title="Add all files sorted by name">+ All</button>
              <button class="btn btn-sm btn-danger" onclick="seqClear()" title="Clear sequence" style="font-size:10px">Clear</button>
            </div>
            <div style="margin-top:8px">
              <div class="field-row" style="gap:6px;margin-bottom:4px">
                <div class="field"><label style="font-size:10px">Loop</label>
                  <select id="seqLoop" style="font-size:11px"><option value="false">No</option><option value="true" selected>Yes</option></select>
                </div>
                <div class="field"><label style="font-size:10px">Count</label>
                  <input id="seqLoopCount" type="number" value="0" min="0" style="width:50px;font-size:11px" title="0 = infinite">
                </div>
              </div>
              <div class="field-row" style="gap:6px;margin-bottom:4px">
                <div class="field"><label style="font-size:10px">Script Delay</label>
                  <input id="seqScriptDelay" type="number" value="2" min="0" step="0.5" style="width:50px;font-size:11px" title="Seconds between scripts">
                </div>
                <div class="field"><label style="font-size:10px">Loop Delay</label>
                  <input id="seqLoopDelay" type="number" value="10" min="0" step="1" style="width:50px;font-size:11px" title="Seconds between loops">
                </div>
              </div>
              <div class="field-row" style="gap:6px;margin-bottom:6px">
                <div class="field"><label style="font-size:10px">On Error</label>
                  <select id="seqOnError" style="font-size:11px"><option value="skip">Skip</option><option value="stop">Stop</option></select>
                </div>
              </div>
            </div>
            <div class="btn-group" style="gap:6px">
              <button class="btn btn-sm btn-primary" onclick="seqPlay()" title="Run sequence">&#9654; Run Sequence</button>
              <button class="btn btn-sm" onclick="seqStop()" title="Stop sequence">&#9632; Stop</button>
            </div>
            <div class="btn-group" style="gap:4px;margin-top:4px">
              <button class="btn btn-sm" onclick="seqSave()" title="Save sequence to .seq.json">Save .seq.json</button>
              <button class="btn btn-sm" onclick="seqLoad()" title="Load sequence from .seq.json">Load .seq.json</button>
            </div>
            <div id="seqStatus" style="font-size:11px;color:var(--text2);margin-top:6px"></div>
          </div>
        </div>
      </div>
    </div>

    <!-- RIGHT PANEL: Device + Detail -->
    <div class="right-panel" id="rightPanel">
      <div class="right-panel-tabs">
        <button class="right-panel-tab active" data-tab="device" onclick="switchRightTab('device',this)">Device</button>
        <button class="right-panel-tab" data-tab="detail" onclick="switchRightTab('detail',this)">Detail</button>
        <button class="right-panel-tab" data-tab="elements" onclick="switchRightTab('elements',this)">Elements</button>
      </div>

      <!-- Device Tab -->
      <div class="right-tab-content active" id="tabDevice">
        <div class="device-section">
          <div class="device-header">
            <h3>Screen</h3>
            <div class="device-controls">
              <button class="btn-debug" id="btnDebugFrames" onclick="toggleDebugFrames()" title="Toggle debug bounding box overlay on all UI elements">
                <span class="debug-dot"></span> Debug <span class="debug-count-badge" id="debugCountBadge" style="display:none">0</span>
              </button>
              <label style="font-size:11px;display:flex;align-items:center;gap:4px">
                <input type="checkbox" id="autoRefresh" checked style="width:auto"> Auto
              </label>
              <button class="btn btn-sm" onclick="captureScreen()">Refresh</button>
            </div>
          </div>
          <div class="screen-container" id="screenContainer">
            <div class="no-screen" id="noScreen">Connecting to device...</div>
            <img id="screenImg" style="display:none" alt="Device Screen">
            <div id="debugOverlays" style="position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;display:none"></div>
            <div id="elementOverlays" style="position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none"></div>
          </div>
          <div class="device-status" id="deviceStatus">Ready - tap screen to interact</div>
        </div>
      </div>

      <!-- Detail Tab -->
      <div class="right-tab-content" id="tabDetail">
        <div class="detail-section" data-hover-scope="detail">
          <div class="detail-header" style="padding:12px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px">
            <h3 style="font-size:14px;flex:1">Frame Detail</h3>
          </div>
          <div class="detail-body" id="detailBody" style="padding:12px">
            <p style="color:var(--text2)">Select a frame to edit</p>
          </div>
        </div>
      </div>

      <!-- Elements Tab -->
      <div class="right-tab-content" id="tabElements">
        <div style="padding:8px;border-bottom:1px solid var(--border);flex-shrink:0">
          <button class="btn btn-sm btn-blue" onclick="captureScreenWithElements()" style="width:100%">Scan Elements</button>
        </div>
        <div class="element-list-panel" id="elementListPanel" style="flex:1;overflow-y:auto;padding:8px">
          <h4>Interactive Elements</h4>
          <div style="color:var(--text2);font-size:12px;padding:4px">Click "Scan Elements" to detect UI elements</div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- ADD FRAME MODAL -->
<div class="modal-overlay" id="addModal" style="display:none" onclick="if(event.target===this)closeAddModal()">
  <div class="modal">
    <div class="modal-header"><h2>Add Frame</h2><button class="btn btn-sm" onclick="closeAddModal()">X</button></div>
    <div class="modal-body">
      <div class="type-grid" id="typeGrid"></div>
      <div id="addForm" style="margin-top:16px"></div>
    </div>
    <div class="modal-footer">
      <button class="btn" onclick="closeAddModal()">Cancel</button>
      <button class="btn btn-primary" onclick="confirmAdd()">Add</button>
    </div>
  </div>
</div>

<script>
// ============================================================
// STATE
// ============================================================
let recording = null;  // full recording object
let filePath = '';
let selectedId = -1;
let currentView = 'all'; // all | script | raw
let addInsertPos = -1;   // -1 = append
let addSelectedType = '';
let dragSrcIdx = -1;
let lastHoverKey = '';
let lastHoverAt = 0;

// Web recording state (accurate timing from JS)
let webRecording = false;
let webRecStartTime = 0;
let webRecFrameCount = 0;
let _stopInProgress = false;  // guard against double-click on Stop button

// Ctrl+hover inspect state
let ctrlHeld = false;
let inspectDebounceTimer = 0;
let inspectTooltipEl = null;
let inspectHighlightEls = [];
let inspectRequestId = 0;   // monotonic counter to discard stale responses
let inspectBusy = false;     // guard against overlapping API calls

const FRAME_TYPES = [
  {type:'gesture',  icon:'👆', name:'Gesture',    desc:'Tap / Swipe / Long Press'},
  {type:'element',  icon:'🎯', name:'Element',    desc:'Tap element by ID/text'},
  {type:'app',      icon:'📱', name:'App',        desc:'Open / Close app'},
  {type:'screenshot',icon:'📸',name:'Screenshot', desc:'Capture screen'},
  {type:'key',      icon:'⌨️', name:'Key',        desc:'Key event (Back/Home)'},
  {type:'wait',     icon:'⏳', name:'Wait',       desc:'Pause for duration'},
  {type:'swipe',    icon:'↕️', name:'Swipe',      desc:'Swipe gesture'},
  {type:'shell',    icon:'💻', name:'Shell',      desc:'ADB shell command'},
  {type:'extract',  icon:'📋', name:'Extract',    desc:'Capture UI data to log'},
  {type:'marker',   icon:'📌', name:'Marker',     desc:'Bookmark / Note'},
];

// ============================================================
// API
// ============================================================
async function api(method, path, body) {
  const opts = {method, headers:{'Content-Type':'application/json'}};
  if (body !== undefined) opts.body = JSON.stringify(body);
  const r = await fetch('/api' + path, opts);
  return r.json();
}

let _availableFiles = [];  // all loadable file paths

async function loadFileList() {
  const data = await api('GET', '/files');
  _availableFiles = data.files || [];
  renderFileList();
}

function renderFileList() {
  const el = document.getElementById('fileList');
  const search = (document.getElementById('fileSearch')?.value || '').toLowerCase();
  const filtered = search ? _availableFiles.filter(f => f.toLowerCase().includes(search)) : _availableFiles;

  // Sort: .urf.json files first, then by name
  const sorted = [...filtered].sort((a, b) => {
    const aName = a.split('/').pop();
    const bName = b.split('/').pop();
    return aName.localeCompare(bName);
  });

  el.innerHTML = sorted.map(f => {
    const name = f.split('/').pop();
    const isActive = f === filePath;
    // Detect station number from station-NN pattern
    const stMatch = name.match(/^station-(\d+)\.urf\.json$/);
    const numMatch = stMatch || name.match(/^(\d+)[.-]/);
    const numBadge = numMatch ? `<span style="background:${stMatch?'var(--green)':'var(--blue)'};color:#fff;border-radius:50%;min-width:18px;height:18px;display:inline-flex;align-items:center;justify-content:center;font-size:9px;font-weight:700;flex-shrink:0">${numMatch[1]}</span>` : '<span class="file-icon" style="font-size:11px;opacity:.6">&#128196;</span>';
    const displayName = name.replace('.urf.json', '');
    return `<div class="file-item ${isActive?'active':''}" data-path="${esc(f)}" style="display:flex;align-items:center;gap:4px">
      ${numBadge}
      <span style="flex:1;cursor:pointer;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px" onclick="loadFile('${f}')" title="${esc(f)}">${esc(displayName)}</span>
      <button class="btn-delete-file" onclick="event.stopPropagation();deleteFile('${f}')" title="Delete file">&times;</button>
    </div>`;
  }).join('') || '<div style="color:var(--text2);padding:8px;font-size:12px">No .urf.json files found. Use "+ New" or Station Manager to create files.</div>';

  const countEl = document.getElementById('fileCount');
  if (countEl) countEl.textContent = sorted.length + ' file' + (sorted.length !== 1 ? 's' : '') + (search ? ' (filtered)' : '');
}

function filterFiles() { renderFileList(); }

function newRecording() {
  const name = prompt('Recording name (e.g. station-01, station-02):');
  if (!name) return;
  // Sanitize filename
  const safeName = name.replace(/[^a-zA-Z0-9\u0E00-\u0E7F_.-]/g, '-').replace(/-+/g, '-');
  const fileName = safeName.endsWith('.urf.json') ? safeName : safeName + '.urf.json';
  const pkg = recording?.meta?.app_package || '';
  const askPkg = pkg || prompt('App package name (e.g. com.example.app):', '') || '';

  recording = {
    version: 2,
    meta: {
      name: name,
      created: new Date().toISOString(),
      device: recording?.meta?.device || '',
      screen_width: screenNaturalW || 1080,
      screen_height: screenNaturalH || 2400,
      app_package: askPkg,
      recording_mode: 'mixed',
      total_duration_ms: 0,
    },
    settings: {
      loop: false, loop_count: 0, loop_delay: 5, speed: 1.0,
      on_error: 'retry', max_retries: 3, retry_delay: 2.0,
      screenshot_dir: '/img/captures',
      send_to: { method: 'cp', path: '/img/sent' },
      notify: true, notify_method: 'stdout', notify_url: '',
    },
    frames: [],
  };
  filePath = '/work/' + fileName;
  document.getElementById('fileName').textContent = fileName;

  // Save empty file immediately so it appears in the list
  api('POST', '/save', {path: filePath, recording: recording}).then(() => {
    loadFileList();
    refreshAll();
    toast('Created: ' + fileName, 'success');
  });
}

async function loadFile(path) {
  const data = await api('GET', '/load?path=' + encodeURIComponent(path));
  if (data.error) { toast(data.error, 'error'); return; }
  recording = data;
  filePath = path;
  document.getElementById('fileName').textContent = path.split('/').pop();
  selectedId = -1;
  refreshAll();
  toast('Loaded: ' + path.split('/').pop());
}

async function deleteFile(path) {
  const name = path.split('/').pop();
  if (!confirm('Delete "' + name + '"?')) return;
  const res = await api('POST', '/delete', {path: path});
  if (res.ok) {
    toast('Deleted: ' + name, 'success');
    if (filePath === path) { recording = null; filePath = ''; document.getElementById('fileName').textContent = 'No file loaded'; refreshAll(); }
    loadFileList();
  } else {
    toast('Delete failed: ' + (res.error || ''), 'error');
  }
}

// ============================================================
// STATION MANAGER
// ============================================================
let _stationFiles = {};  // {start: '/work/station-start.urf.json', 1: '...', ..., end: '...'}

function stationRefresh() {
  // Build map of existing station files from available files
  _stationFiles = {};
  _stationFiles['start'] = null;
  for (let i = 1; i <= 11; i++) _stationFiles[i] = null;
  _stationFiles['end'] = null;
  _availableFiles.forEach(f => {
    const name = f.split('/').pop();
    if (name === 'station-start.urf.json') { _stationFiles['start'] = f; return; }
    if (name === 'station-end.urf.json') { _stationFiles['end'] = f; return; }
    const m = name.match(/^station-(\d+)\.urf\.json$/);
    if (m) _stationFiles[parseInt(m[1])] = f;
  });
  stationRenderGrid();
}

function stationRenderGrid() {
  const grid = document.getElementById('stationGrid');
  if (!grid) return;

  // Helper to render a marker button (start/end)
  function renderMarker(key, label) {
    const exists = !!_stationFiles[key];
    const isActive = _stationFiles[key] === filePath;
    const bgColor = isActive ? 'var(--blue)' : exists ? (key === 'start' ? '#2e7d32' : '#c62828') : 'var(--bg3)';
    const color = (isActive || exists) ? '#fff' : 'var(--text2)';
    const border = isActive ? '2px solid var(--blue)' : '1px solid var(--border)';
    const title = exists ? 'Click to edit station-' + key : 'Click to create station-' + key;
    return `<div onclick="stationClick('${key}')" title="${title}" style="
      display:flex;align-items:center;justify-content:center;gap:4px;
      width:100%;padding:4px 0;border-radius:6px;font-size:11px;font-weight:700;
      background:${bgColor};color:${color};border:${border};cursor:pointer;
      transition:all .15s;position:relative;
    " onmouseover="this.style.transform='scale(1.03)'" onmouseout="this.style.transform='scale(1)'">
      ${label}
      ${!exists ? '<span style="font-size:8px;opacity:.6;margin-left:2px">+</span>' : ''}
    </div>`;
  }

  let html = '';

  // START marker (full width above grid)
  html += `<div style="grid-column:1/-1;margin-bottom:2px">${renderMarker('start', '&#9654; START')}</div>`;

  // Numbered stations 1-11
  for (let i = 1; i <= 11; i++) {
    const exists = !!_stationFiles[i];
    const isActive = _stationFiles[i] === filePath;
    const num = String(i).padStart(2, '0');
    const bg = isActive ? 'var(--blue)' : exists ? 'var(--green)' : 'var(--bg3)';
    const color = (isActive || exists) ? '#fff' : 'var(--text2)';
    const border = isActive ? '2px solid var(--blue)' : '1px solid var(--border)';
    const title = exists ? 'Click to edit station-' + num : 'Click to create station-' + num;
    html += `<div onclick="stationClick(${i})" title="${title}" style="
      display:flex;align-items:center;justify-content:center;
      width:100%;aspect-ratio:1;border-radius:6px;font-size:12px;font-weight:700;
      background:${bg};color:${color};border:${border};cursor:pointer;
      transition:all .15s;position:relative;
    " onmouseover="this.style.transform='scale(1.1)'" onmouseout="this.style.transform='scale(1)'">
      ${i}
      ${!exists ? '<span style="position:absolute;bottom:1px;right:2px;font-size:7px;opacity:.6">+</span>' : ''}
    </div>`;
  }

  // END marker (full width below grid)
  html += `<div style="grid-column:1/-1;margin-top:2px">${renderMarker('end', '&#9632; END')}</div>`;

  grid.innerHTML = html;
}

function stationClick(num) {
  const key = (num === 'start' || num === 'end') ? num : parseInt(num);
  const path = _stationFiles[key];
  if (path) {
    loadFile(path);
  } else {
    stationCreate(key);
  }
}

async function stationCreate(num) {
  let fileName, displayName;
  if (num === 'start' || num === 'end') {
    fileName = 'station-' + num + '.urf.json';
    displayName = num === 'start' ? 'Station START' : 'Station END';
  } else {
    const nn = String(num).padStart(2, '0');
    fileName = 'station-' + nn + '.urf.json';
    displayName = 'Station ' + nn;
  }
  const path = '/work/' + fileName;
  const pkg = recording?.meta?.app_package || '';
  const newRec = {
    version: 2,
    meta: {
      name: displayName,
      created: new Date().toISOString(),
      device: recording?.meta?.device || '',
      screen_width: screenNaturalW || 1080,
      screen_height: screenNaturalH || 2400,
      app_package: pkg,
      recording_mode: 'mixed',
      total_duration_ms: 0,
    },
    settings: {
      loop: false, loop_count: 0, loop_delay: 5, speed: 1.0,
      on_error: 'retry', max_retries: 3, retry_delay: 2.0,
      screenshot_dir: '/img/captures',
      send_to: { method: 'cp', path: '/img/sent' },
      notify: true, notify_method: 'stdout', notify_url: '',
    },
    frames: [],
  };
  const res = await api('POST', '/save', {path: path, recording: newRec});
  if (res.ok) {
    toast('Created: ' + fileName, 'success');
    await loadFileList();
    stationRefresh();
    loadFile(path);
  } else {
    toast('Error: ' + (res.error || ''), 'error');
  }
}

async function stationCreateAll() {
  if (!confirm('Create all missing station files (start, 01-11, end)?')) return;
  let created = 0;
  // Create start/end and numbered stations
  const allKeys = ['start', 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 'end'];
  for (const key of allKeys) {
    if (!_stationFiles[key]) {
      let fileName, displayName;
      if (key === 'start' || key === 'end') {
        fileName = 'station-' + key + '.urf.json';
        displayName = key === 'start' ? 'Station START' : 'Station END';
      } else {
        const nn = String(key).padStart(2, '0');
        fileName = 'station-' + nn + '.urf.json';
        displayName = 'Station ' + nn;
      }
      const path = '/work/' + fileName;
      const pkg = recording?.meta?.app_package || '';
      const newRec = {
        version: 2,
        meta: {
          name: displayName,
          created: new Date().toISOString(),
          device: recording?.meta?.device || '',
          screen_width: screenNaturalW || 1080,
          screen_height: screenNaturalH || 2400,
          app_package: pkg,
          recording_mode: 'mixed',
          total_duration_ms: 0,
        },
        settings: {
          loop: false, loop_count: 0, loop_delay: 5, speed: 1.0,
          on_error: 'retry', max_retries: 3, retry_delay: 2.0,
          screenshot_dir: '/img/captures',
          send_to: { method: 'cp', path: '/img/sent' },
          notify: true, notify_method: 'stdout', notify_url: '',
        },
        frames: [],
      };
      await api('POST', '/save', {path: path, recording: newRec});
      created++;
    }
  }
  toast('Created ' + created + ' station files', 'success');
  await loadFileList();
  stationRefresh();
}

function stationAddAllToSeq() {
  // Add all existing station files (in order) to sequence runner: start -> 1..11 -> end
  // Only adds stations that actually exist
  let added = 0;
  const orderedKeys = ['start', 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 'end'];
  for (const key of orderedKeys) {
    const path = _stationFiles[key];
    if (path) {
      const name = path.split('/').pop();
      seqScripts.push({path, name, enabled: true});
      added++;
    }
  }
  seqRenderList();
  toast('Added ' + added + ' stations to sequence (start \u2192 stations \u2192 end)');
}

// Initialize station grid after file list loads
const _origLoadFileList = loadFileList;
loadFileList = async function() {
  await _origLoadFileList();
  stationRefresh();
};

async function saveFile() {
  if (!recording || !filePath) { toast('No file to save', 'error'); return; }
  updateMetaFromUI();
  updateSettingsFromUI();
  const res = await api('POST', '/save', {path: filePath, recording: recording});
  if (res.ok) toast('Saved!', 'success');
  else toast('Save failed: ' + (res.error||''), 'error');
}

async function saveAsFile() {
  if (!recording) return;
  const name = prompt('Save as (filename):', filePath ? filePath.split('/').pop() : 'new-flow.urf.json');
  if (!name) return;
  const dir = filePath ? filePath.substring(0, filePath.lastIndexOf('/')) : '/work';
  const newPath = dir + '/' + name;
  updateMetaFromUI();
  updateSettingsFromUI();
  const res = await api('POST', '/save', {path: newPath, recording: recording});
  if (res.ok) { filePath = newPath; document.getElementById('fileName').textContent = name; loadFileList(); toast('Saved: ' + name, 'success'); }
  else toast('Save failed', 'error');
}

async function exportScript() {
  if (!recording) return;
  const name = prompt('Export script-only as:', 'script-export.urf.json');
  if (!name) return;
  const dir = filePath ? filePath.substring(0, filePath.lastIndexOf('/')) : '/work';
  const exported = JSON.parse(JSON.stringify(recording));
  exported.frames = exported.frames.filter(f => f.type !== 'touch' && f.enabled !== false);
  exported.frames.forEach((f,i) => f.id = i+1);
  exported.meta.recording_mode = 'script';
  const res = await api('POST', '/save', {path: dir+'/'+name, recording: exported});
  if (res.ok) { loadFileList(); toast('Exported!', 'success'); }
}

// ============================================================
// RENDERING
// ============================================================
function refreshAll() {
  renderTimeline();
  renderMeta();
  renderSettings();
  renderDetail();
}

function renderTimeline() {
  if (!recording) { document.getElementById('timeline').innerHTML = '<p style="color:var(--text2);padding:20px">Load a file from the sidebar</p>'; return; }
  const frames = getVisibleFrames();
  document.getElementById('frameCount').textContent = frames.length + ' frames' + (frames.length !== recording.frames.length ? ' (filtered from '+recording.frames.length+')' : '');

  const html = frames.map((f, idx) => {
    const sel = f.id === selectedId ? ' selected' : '';
    const dis = f.enabled === false ? ' disabled' : '';
    const tSec = (f.t / 1000).toFixed(1);
    return `<div class="frame-card${sel}${dis}" data-id="${f.id}" data-idx="${idx}"
                 onclick="selectFrame(${f.id})" draggable="true"
                 onmouseenter="logHoverFrame(${f.id})"
                 ondragstart="onDragStart(event,${idx})" ondragover="onDragOver(event)" ondrop="onDrop(event,${idx})" ondragend="onDragEnd(event)">
      <div class="frame-type-bar type-${f.type}"></div>
      <div class="frame-body">
        <div class="frame-header">
          <span class="frame-id">#${f.id}</span>
          <span class="frame-time">${tSec}s</span>
          <span class="frame-type-badge badge-${f.type}">${f.type}</span>
        </div>
        <div class="frame-desc">${describeFrame(f)}</div>
        ${f.note ? '<div class="frame-note">' + esc(f.note) + '</div>' : ''}
      </div>
      <div class="frame-actions">
        <button onclick="event.stopPropagation();toggleFrame(${f.id})" title="Toggle">${f.enabled!==false?'&#128308;':'&#9898;'}</button>
        <button onclick="event.stopPropagation();deleteFrame(${f.id})" title="Delete">&#128465;</button>
      </div>
    </div>`;
  }).join('');
  document.getElementById('timeline').innerHTML = html;
}

function getVisibleFrames() {
  if (!recording) return [];
  if (currentView === 'script') return recording.frames.filter(f => f.type !== 'touch');
  if (currentView === 'raw') return recording.frames.filter(f => f.type === 'touch' || f.type === 'gesture');
  return recording.frames;
}

function describeFrame(f) {
  switch(f.type) {
    case 'touch': return `${f.action} (${f.x}, ${f.y})`;
    case 'gesture': {
      let s = `${f.action || 'tap'} (${f.x}, ${f.y})`;
      if (f.action === 'swipe') s += ` → (${f.x2}, ${f.y2})`;
      if (f.element) {
        const el = f.element;
        const lbl = el.text || (el.resource_id||'').split('/').pop();
        if (lbl) s += ` [${lbl}]`;
      }
      return s;
    }
    case 'element': {
      const lbl = f.text || (f.resource_id||'').split('/').pop() || '?';
      return `${f.action||'tap'}: ${lbl}`;
    }
    case 'app': return `${f.action}: ${f.package || '?'}`;
    case 'key': return `${f.key_name || f.keycode}`;
    case 'screenshot': return `${f.stage || 'capture'}${f.send ? ' +send' : ''}`;
    case 'wait': return `${f.duration_ms}ms`;
    case 'shell': return f.command || '?';
    case 'extract': return `${f.extract_label || 'data'}${f.extract_all_text ? ' +text' : ''}${f.extract_screenshot ? ' +img' : ''}`;
    case 'marker': return f.note || '(marker)';
    default: return f.action || f.type;
  }
}

function buildFrameHoverText(f) {
  const parts = [
    `Frame #${f.id} (${f.type})`,
    `time=${f.t}ms`,
    `enabled=${f.enabled !== false}`,
    `desc=${describeFrame(f)}`,
  ];
  if (f.note) parts.push(`note=${f.note}`);
  if (f.x !== undefined && f.y !== undefined) parts.push(`pos=(${f.x},${f.y})`);
  if (f.action === 'swipe' && f.x2 !== undefined) parts.push(`to=(${f.x2},${f.y2})`);
  if (f.resource_id) parts.push(`resource_id=${f.resource_id}`);
  if (f.text) parts.push(`text=${f.text}`);
  if (f.package) parts.push(`package=${f.package}`);
  if (f.command) parts.push(`command=${f.command}`);
  if (f.duration_ms) parts.push(`duration=${f.duration_ms}ms`);
  if (f.element) parts.push('element=' + JSON.stringify(f.element));
  if (f.extract_label) parts.push(`extract_label=${f.extract_label}`);
  if (f.extract_fields) parts.push(`extract_fields=${f.extract_fields.length}`);
  return parts.join(' | ');
}

function logHoverFrame(frameId) {
  if (!recording) return;
  const frame = recording.frames.find(f => f.id === frameId);
  if (!frame) return;

  const hoverKey = `frame-${frame.id}-${frame.t}-${frame.type}`;
  const now = Date.now();
  if (hoverKey === lastHoverKey && (now - lastHoverAt) < 700) return;
  lastHoverKey = hoverKey;
  lastHoverAt = now;

  api('POST', '/hover-log', {
    scope: 'timeline-frame',
    frame_id: frame.id,
    message: buildFrameHoverText(frame),
  }).catch(() => {});
}

// ============================================================
// GENERIC HOVER-TO-TERMINAL (all sections)
// ============================================================
let lastScopeKey = '';
let lastScopeAt = 0;
let activeScopeEl = null;

function collectScopeText(scope, el) {
  switch(scope) {
    case 'topbar': {
      const fname = document.getElementById('fileName')?.textContent || '';
      return `File: ${fname}`;
    }
    case 'files': {
      const items = el.querySelectorAll('.file-item');
      if (!items.length) return 'No files loaded';
      const names = Array.from(items).map(e => {
        const active = e.classList.contains('active') ? ' (active)' : '';
        return e.textContent.trim() + active;
      });
      return `Files (${names.length}): ${names.join(', ')}`;
    }
    case 'meta': {
      if (!recording) return 'No recording loaded';
      const m = recording.meta || {};
      const parts = [
        `Name: ${m.name || '-'}`,
        `Package: ${m.app_package || '-'}`,
        `Device: ${m.device || '-'}`,
        `Mode: ${m.recording_mode || 'mixed'}`,
        `Frames: ${recording.frames.length}`,
      ];
      const maxT = recording.frames.length ? Math.max(...recording.frames.map(f=>f.t)) : 0;
      parts.push(`Duration: ${(maxT/1000).toFixed(1)}s`);
      return parts.join(' | ');
    }
    case 'settings': {
      if (!recording) return 'No recording loaded';
      const s = recording.settings || {};
      return [
        `Speed: ${s.speed || 1}x`,
        `Loop: ${s.loop ? 'on' : 'off'} (count=${s.loop_count||0}, delay=${s.loop_delay||5}s)`,
        `OnError: ${s.on_error || 'retry'}`,
        `MaxRetries: ${s.max_retries || 3}`,
      ].join(' | ');
    }
    case 'bulk-actions': {
      return 'Actions: Collapse Raw, Compact, Scale Time, Shift Time, Clear All';
    }
    case 'toolbar': {
      const view = currentView;
      const frames = getVisibleFrames();
      return `View: ${view} | Showing: ${frames.length} frames | Selected: ${selectedId >= 0 ? '#'+selectedId : 'none'}`;
    }
    case 'timeline': {
      if (!recording || !recording.frames.length) return 'Timeline: empty';
      const frames = getVisibleFrames();
      const types = {};
      frames.forEach(f => { types[f.type] = (types[f.type]||0) + 1; });
      const breakdown = Object.entries(types).map(([t,c]) => `${t}:${c}`).join(', ');
      const maxT = Math.max(...frames.map(f=>f.t));
      return `Timeline: ${frames.length} frames | ${(maxT/1000).toFixed(1)}s | ${breakdown}`;
    }
    case 'detail': {
      if (!recording || selectedId < 0) return 'No frame selected';
      const f = recording.frames.find(fr => fr.id === selectedId);
      if (!f) return 'Frame not found';
      return buildFrameHoverText(f);
    }
    default: {
      // Fallback: collect visible text
      const txt = el.innerText.replace(/\s+/g, ' ').trim();
      return txt.substring(0, 300);
    }
  }
}

function initHoverScopes() {
  document.querySelectorAll('[data-hover-scope]').forEach(el => {
    // Inject scope label
    const lbl = document.createElement('span');
    lbl.className = 'hover-scope-label';
    lbl.textContent = el.dataset.hoverScope;
    el.appendChild(lbl);
  });

  document.addEventListener('mouseover', (e) => {
    const scopeEl = e.target.closest('[data-hover-scope]');
    if (!scopeEl) {
      if (activeScopeEl) { activeScopeEl.classList.remove('hover-active'); activeScopeEl = null; }
      return;
    }

    // Highlight active scope
    if (activeScopeEl && activeScopeEl !== scopeEl) activeScopeEl.classList.remove('hover-active');
    scopeEl.classList.add('hover-active');
    activeScopeEl = scopeEl;

    const scope = scopeEl.dataset.hoverScope;
    const now = Date.now();
    // Rate limit: don't re-send for same scope within 700ms
    if (scope === lastScopeKey && (now - lastScopeAt) < 700) return;
    lastScopeKey = scope;
    lastScopeAt = now;

    const message = collectScopeText(scope, scopeEl);
    if (!message) return;

    api('POST', '/hover-log', {
      scope: scope,
      message: message,
    }).catch(() => {});
  });

  document.addEventListener('mouseout', (e) => {
    if (!e.relatedTarget || !e.relatedTarget.closest('[data-hover-scope]')) {
      if (activeScopeEl) { activeScopeEl.classList.remove('hover-active'); activeScopeEl = null; }
    }
  });
}

// Init hover scopes after DOM is ready
initHoverScopes();

// Bind button events via addEventListener as fallback (in case inline onclick is blocked)
document.getElementById('btnPlay').addEventListener('click', function(e) {
  e.stopPropagation();
  if (!this.disabled) startPlay();
});
document.getElementById('btnRec').addEventListener('click', function(e) {
  e.stopPropagation();
  if (!this.disabled) startRecord();
});
document.getElementById('btnStop').addEventListener('click', function(e) {
  e.stopPropagation();
  if (!this.disabled) stopPlayback();
});

function renderMeta() {
  if (!recording) return;
  const m = recording.meta || {};
  document.getElementById('metaName').value = m.name || '';
  document.getElementById('metaPkg').value = m.app_package || '';
  document.getElementById('metaDev').value = m.device || '';
  document.getElementById('metaMode').textContent = m.recording_mode || 'mixed';
  document.getElementById('metaFrames').textContent = recording.frames.length;
  const maxT = recording.frames.length ? Math.max(...recording.frames.map(f=>f.t)) : 0;
  document.getElementById('metaDur').textContent = (maxT/1000).toFixed(1) + 's';
}

function renderSettings() {
  if (!recording) return;
  const s = recording.settings || {};
  document.getElementById('setSpeed').value = s.speed || 1;
  document.getElementById('setLoop').checked = !!s.loop;
  document.getElementById('setLoopCount').value = s.loop_count || 0;
  document.getElementById('setLoopDelay').value = s.loop_delay || 5;
  document.getElementById('setOnError').value = s.on_error || 'retry';
  document.getElementById('setRetries').value = s.max_retries || 3;
}

function renderDetail() {
  const body = document.getElementById('detailBody');
  if (!recording || selectedId < 0) {
    body.innerHTML = '<p style="color:var(--text2)">Select a frame to edit</p>';
    return;
  }
  const f = recording.frames.find(fr => fr.id === selectedId);
  if (!f) { body.innerHTML = '<p style="color:var(--text2)">Frame not found</p>'; return; }

  let html = `
    <div class="field-row">
      <div class="field"><label>ID</label><input value="${f.id}" disabled></div>
      <div class="field"><label>Time (ms)</label><input type="number" value="${f.t}" onchange="updateFrameField(${f.id},'t',+this.value)"></div>
    </div>
    <div class="field-row">
      <div class="field"><label>Type</label>
        <select onchange="changeFrameType(${f.id},this.value)">
          ${['touch','gesture','element','app','key','screenshot','wait','shell','extract','marker'].map(t =>
            `<option value="${t}" ${t===f.type?'selected':''}>${t}</option>`
          ).join('')}
        </select>
      </div>
      <div class="field"><label>Enabled</label>
        <select onchange="updateFrameField(${f.id},'enabled',this.value==='true')">
          <option value="true" ${f.enabled!==false?'selected':''}>Yes</option>
          <option value="false" ${f.enabled===false?'selected':''}>No</option>
        </select>
      </div>
    </div>`;

  // Type-specific fields
  if (f.type === 'touch' || f.type === 'gesture') {
    html += `
      <div class="field"><label>Action</label>
        <select onchange="updateFrameField(${f.id},'action',this.value)">
          ${['tap','swipe','long_press','down','move','up'].map(a =>
            `<option value="${a}" ${a===f.action?'selected':''}>${a}</option>`
          ).join('')}
        </select>
      </div>
      <div class="field-row">
        <div class="field"><label>X</label><input type="number" value="${f.x||0}" onchange="updateFrameField(${f.id},'x',+this.value)"></div>
        <div class="field"><label>Y</label><input type="number" value="${f.y||0}" onchange="updateFrameField(${f.id},'y',+this.value)"></div>
      </div>`;
    if (f.action === 'swipe') {
      html += `
        <div class="field-row">
          <div class="field"><label>X2</label><input type="number" value="${f.x2||0}" onchange="updateFrameField(${f.id},'x2',+this.value)"></div>
          <div class="field"><label>Y2</label><input type="number" value="${f.y2||0}" onchange="updateFrameField(${f.id},'y2',+this.value)"></div>
        </div>`;
    }
    html += `<div class="field"><label>Duration (ms)</label><input type="number" value="${f.duration_ms||0}" onchange="updateFrameField(${f.id},'duration_ms',+this.value)"></div>`;
    if (f.element) {
      html += `<div class="field"><label>Element (auto)</label><textarea disabled>${JSON.stringify(f.element,null,2)}</textarea></div>`;
    }
  }

  if (f.type === 'element') {
    html += `
      <div class="field"><label>Action</label>
        <select onchange="updateFrameField(${f.id},'action',this.value)">
          <option value="tap" ${f.action==='tap'?'selected':''}>tap</option>
          <option value="long_press" ${f.action==='long_press'?'selected':''}>long_press</option>
        </select>
      </div>
      <div class="field"><label>Resource ID</label><input value="${esc(f.resource_id||'')}" onchange="updateFrameField(${f.id},'resource_id',this.value)"></div>
      <div class="field"><label>Text</label><input value="${esc(f.text||'')}" onchange="updateFrameField(${f.id},'text',this.value)"></div>
      <div class="field"><label>Timeout (s)</label><input type="number" value="${f.timeout||10}" onchange="updateFrameField(${f.id},'timeout',+this.value)"></div>`;
  }

  if (f.type === 'app') {
    html += `
      <div class="field"><label>Action</label>
        <select onchange="updateFrameField(${f.id},'action',this.value)">
          <option value="open" ${f.action==='open'?'selected':''}>Open</option>
          <option value="close" ${f.action==='close'?'selected':''}>Close</option>
        </select>
      </div>
      <div class="field"><label>Package</label><input value="${esc(f.package||'')}" onchange="updateFrameField(${f.id},'package',this.value)"></div>
      <div class="field"><label>Activity</label><input value="${esc(f.activity||'')}" onchange="updateFrameField(${f.id},'activity',this.value)"></div>`;
  }

  if (f.type === 'key') {
    const keyOptions = [
      [4,'BACK'],[3,'HOME'],[26,'POWER'],[82,'MENU'],[187,'APP_SWITCH'],
      [24,'VOLUME_UP'],[25,'VOLUME_DOWN'],
      [113,'CTRL_LEFT'],[114,'CTRL_RIGHT'],
      [57,'ALT_LEFT'],[58,'ALT_RIGHT'],
      [59,'SHIFT_LEFT'],[60,'SHIFT_RIGHT'],
      [66,'ENTER'],[67,'DEL'],[61,'TAB'],[111,'ESCAPE'],
    ];
    html += `
      <div class="field"><label>Keycode</label>
        <select onchange="updateFrameField(${f.id},'keycode',+this.value);updateFrameField(${f.id},'key_name',this.options[this.selectedIndex].text)">
          ${keyOptions.map(([kc,kn]) => `<option value="${kc}" ${f.keycode===kc?'selected':''}>${kn} (${kc})</option>`).join('')}
        </select>
      </div>`;
  }

  if (f.type === 'screenshot') {
    html += `
      <div class="field"><label>Stage</label><input value="${esc(f.stage||'')}" onchange="updateFrameField(${f.id},'stage',this.value)"></div>
      <div class="field"><label>Send</label>
        <select onchange="updateFrameField(${f.id},'send',this.value==='true')">
          <option value="true" ${f.send?'selected':''}>Yes</option>
          <option value="false" ${!f.send?'selected':''}>No</option>
        </select>
      </div>`;
  }

  if (f.type === 'wait') {
    html += `<div class="field"><label>Duration (ms)</label><input type="number" value="${f.duration_ms||0}" onchange="updateFrameField(${f.id},'duration_ms',+this.value)"></div>`;
  }

  if (f.type === 'shell') {
    html += `<div class="field"><label>Command</label><input value="${esc(f.command||'')}" onchange="updateFrameField(${f.id},'command',this.value)"></div>`;
  }

  if (f.type === 'extract') {
    html += `
      <div class="field"><label>Label</label><input value="${esc(f.extract_label||'')}" onchange="updateFrameField(${f.id},'extract_label',this.value)"></div>
      <div class="field"><label>Extract Fields</label>
        <textarea style="width:100%;font-family:monospace;font-size:12px;min-height:80px"
          onchange="try{updateFrameField(${f.id},'extract_fields',JSON.parse(this.value))}catch(e){}">${JSON.stringify(f.extract_fields||[],null,2)}</textarea>
      </div>
      <div class="field"><label>Capture all text</label>
        <select onchange="updateFrameField(${f.id},'extract_all_text',this.value==='true')">
          <option value="true" ${f.extract_all_text?'selected':''}>Yes</option>
          <option value="false" ${!f.extract_all_text?'selected':''}>No</option>
        </select>
      </div>
      <div class="field"><label>Take screenshot</label>
        <select onchange="updateFrameField(${f.id},'extract_screenshot',this.value==='true')">
          <option value="true" ${f.extract_screenshot?'selected':''}>Yes</option>
          <option value="false" ${!f.extract_screenshot?'selected':''}>No</option>
        </select>
      </div>`;
  }

  html += `<div class="field"><label>Note</label><input value="${esc(f.note||'')}" onchange="updateFrameField(${f.id},'note',this.value)"></div>`;

  body.innerHTML = html;
}

// ============================================================
// ACTIONS
// ============================================================
function selectFrame(id) {
  selectedId = id; renderTimeline(); renderDetail();
  // Auto-switch to Detail tab when a frame is selected
  const detailTab = document.querySelector('.right-panel-tab[data-tab="detail"]');
  if (detailTab) switchRightTab('detail', detailTab);
}
function closeDetail() { selectedId = -1; renderTimeline(); renderDetail(); }

function updateFrameField(id, key, val) {
  const f = recording.frames.find(fr => fr.id === id);
  if (f) { f[key] = val; renderTimeline(); }
}

function changeFrameType(id, newType) {
  const f = recording.frames.find(fr => fr.id === id);
  if (f) { f.type = newType; renderTimeline(); renderDetail(); }
}

function toggleFrame(id) {
  const f = recording.frames.find(fr => fr.id === id);
  if (f) { f.enabled = f.enabled === false ? true : false; renderTimeline(); renderDetail(); }
}

function deleteFrame(id) {
  if (!recording) return;
  recording.frames = recording.frames.filter(f => f.id !== id);
  if (selectedId === id) selectedId = -1;
  refreshAll();
}

function deleteSelected() { if (selectedId >= 0) deleteFrame(selectedId); }

function toggleSelected() { if (selectedId >= 0) toggleFrame(selectedId); }

function duplicateSelected() {
  if (!recording || selectedId < 0) return;
  const f = recording.frames.find(fr => fr.id === selectedId);
  if (!f) return;
  const dup = JSON.parse(JSON.stringify(f));
  dup.id = nextId();
  dup.t += 500;
  const idx = recording.frames.indexOf(f);
  recording.frames.splice(idx + 1, 0, dup);
  selectedId = dup.id;
  refreshAll();
}

function moveUp() {
  if (!recording || selectedId < 0) return;
  const idx = recording.frames.findIndex(f => f.id === selectedId);
  if (idx <= 0) return;
  [recording.frames[idx-1], recording.frames[idx]] = [recording.frames[idx], recording.frames[idx-1]];
  renderTimeline();
}

function moveDown() {
  if (!recording || selectedId < 0) return;
  const idx = recording.frames.findIndex(f => f.id === selectedId);
  if (idx < 0 || idx >= recording.frames.length - 1) return;
  [recording.frames[idx], recording.frames[idx+1]] = [recording.frames[idx+1], recording.frames[idx]];
  renderTimeline();
}

function insertBefore() {
  if (selectedId < 0) { openAddModal(-1); return; }
  const idx = recording.frames.findIndex(f => f.id === selectedId);
  openAddModal(idx);
}

// ============================================================
// ADD MODAL
// ============================================================
function openAddModal(pos) {
  addInsertPos = pos;
  addSelectedType = '';
  const grid = document.getElementById('typeGrid');
  grid.innerHTML = FRAME_TYPES.map(t =>
    `<div class="type-option" data-type="${t.type}" onclick="selectAddType('${t.type}',this)">
      <div class="icon">${t.icon}</div><div class="name">${t.name}</div>
    </div>`
  ).join('');
  document.getElementById('addForm').innerHTML = '';
  document.getElementById('addModal').style.display = 'flex';
}
function closeAddModal() { document.getElementById('addModal').style.display = 'none'; }

function selectAddType(type, el) {
  addSelectedType = type;
  document.querySelectorAll('.type-option').forEach(e => e.classList.remove('selected'));
  el.classList.add('selected');
  renderAddForm(type);
}

function renderAddForm(type) {
  const form = document.getElementById('addForm');
  const pkg = recording?.meta?.app_package || '';
  let html = `<div class="field"><label>Time (ms)</label><input id="addT" type="number" value="${getInsertTime()}"></div>`;

  if (type === 'gesture') {
    html += `
      <div class="field"><label>Action</label><select id="addAction"><option>tap</option><option>swipe</option><option>long_press</option></select></div>
      <div class="field-row"><div class="field"><label>X</label><input id="addX" type="number" value="540"></div><div class="field"><label>Y</label><input id="addY" type="number" value="1200"></div></div>
      <div class="field-row"><div class="field"><label>X2 (swipe)</label><input id="addX2" type="number" value="540"></div><div class="field"><label>Y2</label><input id="addY2" type="number" value="600"></div></div>
      <div class="field"><label>Duration (ms)</label><input id="addDur" type="number" value="50"></div>`;
  } else if (type === 'swipe') {
    html += `
      <div class="field-row"><div class="field"><label>Start X</label><input id="addX" type="number" value="540"></div><div class="field"><label>Start Y</label><input id="addY" type="number" value="1800"></div></div>
      <div class="field-row"><div class="field"><label>End X</label><input id="addX2" type="number" value="540"></div><div class="field"><label>End Y</label><input id="addY2" type="number" value="600"></div></div>
      <div class="field"><label>Duration (ms)</label><input id="addDur" type="number" value="300"></div>`;
  } else if (type === 'element') {
    html += `
      <div class="field"><label>Resource ID</label><input id="addResId" placeholder="com.example:id/btn"></div>
      <div class="field"><label>Text</label><input id="addText" placeholder="Button text"></div>
      <div class="field"><label>Timeout (s)</label><input id="addTimeout" type="number" value="10"></div>`;
  } else if (type === 'app') {
    html += `
      <div class="field"><label>Action</label><select id="addAction"><option value="open">Open</option><option value="close">Close</option></select></div>
      <div class="field"><label>Package</label><input id="addPkg" value="${esc(pkg)}"></div>`;
  } else if (type === 'key') {
    html += `
      <div class="field"><label>Key</label><select id="addKeycode">
        <option value="4">BACK (4)</option><option value="3">HOME (3)</option>
        <option value="26">POWER (26)</option><option value="82">MENU (82)</option>
        <option value="187">APP_SWITCH (187)</option>
        <option value="24">VOLUME_UP (24)</option><option value="25">VOLUME_DOWN (25)</option>
        <option value="113">CTRL_LEFT (113)</option><option value="114">CTRL_RIGHT (114)</option>
        <option value="66">ENTER (66)</option><option value="67">DEL (67)</option>
        <option value="61">TAB (61)</option><option value="111">ESCAPE (111)</option>
      </select></div>`;
  } else if (type === 'screenshot') {
    html += `
      <div class="field"><label>Stage</label><input id="addStage" value="capture"></div>
      <div class="field"><label>Send</label><select id="addSend"><option value="true">Yes</option><option value="false">No</option></select></div>`;
  } else if (type === 'wait') {
    html += `<div class="field"><label>Duration (ms)</label><input id="addDur" type="number" value="2000"></div>`;
  } else if (type === 'shell') {
    html += `<div class="field"><label>Command</label><input id="addCmd" placeholder="am broadcast ..."></div>`;
  } else if (type === 'extract') {
    html += `
      <div class="field"><label>Label</label><input id="addExtLabel" placeholder="station_status" value="station_status"></div>
      <div class="field"><label>Extract Fields (JSON)</label>
        <textarea id="addExtFields" rows="4" style="width:100%;font-family:monospace;font-size:12px" placeholder='[{"name":"station_name","resource_id":"","text_match":""}]'>[{"name":"status","resource_id":"","text_match":""}]</textarea>
      </div>
      <div class="field"><label>Capture all text</label>
        <select id="addExtAllText"><option value="true" selected>Yes</option><option value="false">No</option></select>
      </div>
      <div class="field"><label>Take screenshot</label>
        <select id="addExtScreenshot"><option value="true" selected>Yes</option><option value="false">No</option></select>
      </div>`;
  } else if (type === 'marker') {
    // just note
  }
  html += `<div class="field"><label>Note</label><input id="addNote" placeholder="Optional note"></div>`;
  form.innerHTML = html;
}

function getInsertTime() {
  if (!recording || !recording.frames.length) return 0;
  if (addInsertPos >= 0 && addInsertPos < recording.frames.length) return recording.frames[addInsertPos].t;
  return recording.frames[recording.frames.length-1].t + 1000;
}

function confirmAdd() {
  if (!addSelectedType || !recording) return;
  const t = +(document.getElementById('addT')?.value || 0);
  const note = document.getElementById('addNote')?.value || '';
  let type = addSelectedType;
  const f = {id: nextId(), t, type, enabled: true, note};

  if (type === 'gesture') {
    f.action = document.getElementById('addAction')?.value || 'tap';
    f.x = +(document.getElementById('addX')?.value||0);
    f.y = +(document.getElementById('addY')?.value||0);
    f.x2 = +(document.getElementById('addX2')?.value||0);
    f.y2 = +(document.getElementById('addY2')?.value||0);
    f.duration_ms = +(document.getElementById('addDur')?.value||50);
  } else if (type === 'swipe') {
    f.type = 'gesture'; f.action = 'swipe';
    f.x = +(document.getElementById('addX')?.value||0);
    f.y = +(document.getElementById('addY')?.value||0);
    f.x2 = +(document.getElementById('addX2')?.value||0);
    f.y2 = +(document.getElementById('addY2')?.value||0);
    f.duration_ms = +(document.getElementById('addDur')?.value||300);
  } else if (type === 'element') {
    f.action = 'tap';
    f.resource_id = document.getElementById('addResId')?.value || '';
    f.text = document.getElementById('addText')?.value || '';
    f.timeout = +(document.getElementById('addTimeout')?.value || 10);
  } else if (type === 'app') {
    f.action = document.getElementById('addAction')?.value || 'open';
    f.package = document.getElementById('addPkg')?.value || '';
  } else if (type === 'key') {
    const kc = +(document.getElementById('addKeycode')?.value || 4);
    f.keycode = kc;
    f.key_name = {3:'HOME',4:'BACK',26:'POWER',82:'MENU',187:'APP_SWITCH',24:'VOLUME_UP',25:'VOLUME_DOWN',113:'CTRL_LEFT',114:'CTRL_RIGHT',57:'ALT_LEFT',58:'ALT_RIGHT',59:'SHIFT_LEFT',60:'SHIFT_RIGHT',66:'ENTER',67:'DEL',61:'TAB',111:'ESCAPE'}[kc] || '';
  } else if (type === 'screenshot') {
    f.stage = document.getElementById('addStage')?.value || 'capture';
    f.send = document.getElementById('addSend')?.value === 'true';
  } else if (type === 'wait') {
    f.duration_ms = +(document.getElementById('addDur')?.value || 2000);
  } else if (type === 'shell') {
    f.command = document.getElementById('addCmd')?.value || '';
  } else if (type === 'extract') {
    f.extract_label = document.getElementById('addExtLabel')?.value || 'extract';
    try { f.extract_fields = JSON.parse(document.getElementById('addExtFields')?.value || '[]'); } catch(e) { f.extract_fields = []; }
    f.extract_all_text = document.getElementById('addExtAllText')?.value === 'true';
    f.extract_screenshot = document.getElementById('addExtScreenshot')?.value === 'true';
  }

  if (addInsertPos >= 0) recording.frames.splice(addInsertPos, 0, f);
  else recording.frames.push(f);

  selectedId = f.id;
  closeAddModal();
  refreshAll();
}

// ============================================================
// BULK ACTIONS
// ============================================================
async function collapseRaw() {
  if (!recording) return;
  const res = await api('POST', '/collapse', {recording});
  if (res.frames) { recording.frames = res.frames; refreshAll(); toast('Collapsed!', 'success'); }
}

function compactFrames() {
  if (!recording) return;
  const before = recording.frames.length;
  recording.frames = recording.frames.filter(f => f.enabled !== false);
  recording.frames.forEach((f,i) => f.id = i+1);
  refreshAll();
  toast(`Removed ${before - recording.frames.length} disabled frames`, 'success');
}

function scaleTime() {
  const s = prompt('Scale factor (e.g. 2.0 = double all delays, 0.5 = halve):', '1.0');
  if (!s) return;
  const scale = parseFloat(s);
  recording.frames.forEach(f => { f.t = Math.round(f.t * scale); if (f.duration_ms) f.duration_ms = Math.round(f.duration_ms * scale); });
  refreshAll();
  toast(`Scaled timing by ${scale}x`);
}

function shiftTime() {
  const s = prompt('Shift all frames by ms (e.g. 1000 or -500):', '0');
  if (!s) return;
  const shift = parseInt(s);
  recording.frames.forEach(f => { f.t = Math.max(0, f.t + shift); });
  refreshAll();
  toast(`Shifted by ${shift}ms`);
}

function clearAll() {
  if (!confirm('Delete ALL frames?')) return;
  recording.frames = [];
  selectedId = -1;
  refreshAll();
}

// ============================================================
// SEQUENCE RUNNER
// ============================================================
let seqScripts = [];  // [{path, name, enabled}]
let seqRunning = false;
let seqPollTimer = null;
let seqDragSrcIdx = -1;

function seqRenderList() {
  const el = document.getElementById('seqList');
  if (!seqScripts.length) {
    el.innerHTML = '<div style="color:var(--text2);font-style:italic;padding:4px">No scripts in sequence. Use "+ Pick" to add station recordings.</div>';
    return;
  }

  el.innerHTML = seqScripts.map((s, i) => {
    const isLast = i === seqScripts.length - 1;
    const numLabel = i + 1;
    // Extract short display name
    const displayName = s.name.replace('.urf.json', '');
    return `
    <div class="seq-item" data-seq-idx="${i}" draggable="true"
         ondragstart="seqDragStart(event,${i})" ondragover="seqDragOver(event)" ondrop="seqDrop(event,${i})" ondragend="seqDragEnd(event)"
         style="position:relative">
      <div style="display:flex;align-items:center;gap:3px;padding:3px 5px;background:${s.enabled?'var(--bg3)':'var(--bg2)'};border-radius:6px;border:1px solid ${s.enabled?'var(--border)':'transparent'};${!s.enabled?'opacity:.35':''}cursor:grab;">
        <span style="min-width:20px;height:20px;display:flex;align-items:center;justify-content:center;background:${s.enabled?'var(--blue)':'var(--text2)'};color:#000;border-radius:50%;font-size:9px;font-weight:700;flex-shrink:0">${numLabel}</span>
        <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px" title="${esc(s.path)}">${esc(displayName)}</span>
        <button onclick="seqDuplicate(${i})" style="background:none;border:none;color:var(--blue);cursor:pointer;font-size:10px;padding:0 2px" title="Duplicate">&#x2398;</button>
        <button onclick="seqMoveUp(${i})" style="background:none;border:none;color:var(--text2);cursor:pointer;font-size:10px;padding:0 2px" title="Move up">&uarr;</button>
        <button onclick="seqMoveDown(${i})" style="background:none;border:none;color:var(--text2);cursor:pointer;font-size:10px;padding:0 2px" title="Move down">&darr;</button>
        <button onclick="seqRemove(${i})" style="background:none;border:none;color:var(--red);cursor:pointer;font-size:11px;padding:0 2px" title="Remove">&times;</button>
      </div>
      ${!isLast ? '<div style="text-align:center;color:var(--text2);font-size:8px;line-height:1;padding:0">&#9660;</div>' : ''}
    </div>`;
  }).join('');

  // Show flow summary with file names
  const enabled = seqScripts.filter(s => s.enabled);
  if (enabled.length > 0) {
    const flowNames = enabled.map(s => s.name.replace('.urf.json', ''));
    // Compact display: group consecutive duplicates
    const compact = [];
    let prev = null, count = 0;
    flowNames.forEach(n => {
      if (n === prev) { count++; }
      else { if (prev !== null) compact.push(count > 1 ? prev + 'x' + count : prev); prev = n; count = 1; }
    });
    if (prev !== null) compact.push(count > 1 ? prev + 'x' + count : prev);
    const flowText = compact.join(' &rarr; ');
    const loopIcon = document.getElementById('seqLoop')?.value === 'true' ? ' &#8634;' : '';
    el.innerHTML += `<div style="color:var(--blue);font-size:10px;text-align:center;padding:4px;margin-top:2px;background:var(--bg);border-radius:4px;border:1px dashed var(--border);word-break:break-all">${flowText}${loopIcon}</div>`;
    el.innerHTML += `<div style="color:var(--text2);font-size:10px;text-align:center;padding:2px">${enabled.length} scripts</div>`;
  }
}

// Drag-and-drop for sequence items
function seqDragStart(e, idx) { seqDragSrcIdx = idx; e.currentTarget.style.opacity = '0.4'; e.dataTransfer.effectAllowed = 'move'; }
function seqDragOver(e) { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; }
function seqDrop(e, targetIdx) {
  e.preventDefault();
  if (seqDragSrcIdx < 0 || seqDragSrcIdx === targetIdx) return;
  const item = seqScripts.splice(seqDragSrcIdx, 1)[0];
  seqScripts.splice(targetIdx, 0, item);
  seqDragSrcIdx = -1;
  seqRenderList();
}
function seqDragEnd(e) { seqDragSrcIdx = -1; e.currentTarget.style.opacity = '1'; seqRenderList(); }

function seqAddCurrent() {
  if (!filePath) { toast('No file loaded', 'error'); return; }
  const name = filePath.split('/').pop();
  seqScripts.push({path: filePath, name, enabled: true});
  seqRenderList();
  toast('Added: ' + name);
}

function seqAddAllFiles() {
  // Add all .urf.json files sorted by name
  const sorted = [..._availableFiles].filter(f => f.endsWith('.urf.json')).sort((a, b) => a.split('/').pop().localeCompare(b.split('/').pop()));
  sorted.forEach(f => seqScripts.push({path: f, name: f.split('/').pop(), enabled: true}));
  seqRenderList();
  toast('Added ' + sorted.length + ' files');
}

let _seqPickerOpen = false;
function seqAddFromList() {
  if (_seqPickerOpen) return;
  if (!_availableFiles.length) { toast('No files found - click Refresh first', 'error'); return; }
  _seqPickerOpen = true;
  const container = document.getElementById('seqFilePicker');
  // Always allow adding files, even if already in sequence (user wants duplicates like 1 1 2 3 ...)
  const sorted = [..._availableFiles].sort((a, b) => a.split('/').pop().localeCompare(b.split('/').pop()));
  const html = `<div style="background:var(--bg);border:1px solid var(--blue);border-radius:6px;max-height:250px;overflow-y:auto;margin-bottom:6px">
    ${sorted.map((f, i) => {
      const name = f.split('/').pop();
      const inCount = seqScripts.filter(s => s.path === f).length;
      return `<div onclick="seqPickFile('${f.replace(/'/g, "\\'")}')"
        style="padding:5px 8px;cursor:pointer;font-size:12px;display:flex;align-items:center;gap:6px;border-bottom:1px solid var(--border);"
        onmouseover="this.style.background='var(--bg3)'" onmouseout="this.style.background=''">
        ${inCount > 0 ? '<span style="color:var(--blue);font-size:9px;min-width:14px">'+inCount+'x</span>' : '<span style="min-width:14px"></span>'}
        <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(name)}</span>
      </div>`;
    }).join('')}
  </div>
  <div style="display:flex;gap:4px;font-size:10px;color:var(--text2);margin-bottom:4px">Click to add (can add same file multiple times)</div>
  <div style="display:flex;gap:4px">
    <button class="btn btn-sm" onclick="seqPickClose()">Done</button>
  </div>`;
  container.innerHTML = html;
  container.style.display = 'block';
}

function seqPickFile(path) {
  const name = path.split('/').pop();
  seqScripts.push({path, name, enabled: true});
  seqRenderList();
  toast('Added: ' + name);
  // Don't close - allow adding multiple files quickly
  // Update count badges in picker
  const container = document.getElementById('seqFilePicker');
  if (container && _seqPickerOpen) seqAddFromList();  // refresh picker
  _seqPickerOpen = false;  // seqAddFromList will set it true again
}

function seqPickClose() {
  _seqPickerOpen = false;
  const container = document.getElementById('seqFilePicker');
  if (container) { container.innerHTML = ''; container.style.display = 'none'; }
}

function seqToggle(i, checked) { seqScripts[i].enabled = checked; seqRenderList(); }
function seqRemove(i) { seqScripts.splice(i, 1); seqRenderList(); }
function seqDuplicate(i) { seqScripts.splice(i + 1, 0, {...seqScripts[i]}); seqRenderList(); }
function seqMoveUp(i) { if (i <= 0) return; [seqScripts[i-1], seqScripts[i]] = [seqScripts[i], seqScripts[i-1]]; seqRenderList(); }
function seqMoveDown(i) { if (i >= seqScripts.length-1) return; [seqScripts[i], seqScripts[i+1]] = [seqScripts[i+1], seqScripts[i]]; seqRenderList(); }
function seqClear() { seqScripts = []; seqRenderList(); }

async function seqPlay() {
  const enabled = seqScripts.filter(s => s.enabled);
  if (!enabled.length) { toast('No scripts in sequence', 'error'); return; }
  const body = {
    scripts: enabled.map(s => ({path: s.path, enabled: true})),
    settings: {
      loop: document.getElementById('seqLoop')?.value === 'true',
      loop_count: +(document.getElementById('seqLoopCount')?.value || 0),
      loop_delay: +(document.getElementById('seqLoopDelay')?.value || 10),
      script_delay: +(document.getElementById('seqScriptDelay')?.value || 2),
      speed: recording?.settings?.speed || 1.0,
      on_script_error: document.getElementById('seqOnError')?.value || 'skip',
      replay_mode: 'auto',
    }
  };
  const res = await api('POST', '/sequence/play', body);
  if (res.ok) {
    seqRunning = true;
    toast('Sequence started!', 'success');
    seqPollStatus();
  } else {
    toast('Error: ' + (res.error || 'Unknown'), 'error');
  }
}

async function seqStop() {
  await api('POST', '/sequence/stop', {});
  seqRunning = false;
  if (seqPollTimer) { clearInterval(seqPollTimer); seqPollTimer = null; }
  document.getElementById('seqStatus').textContent = 'Stopped';
  toast('Sequence stopped');
}

function seqPollStatus() {
  if (seqPollTimer) clearInterval(seqPollTimer);
  seqPollTimer = setInterval(async () => {
    const st = await api('GET', '/sequence/status');
    const el = document.getElementById('seqStatus');
    if (st.status === 'running') {
      el.innerHTML = `<span style="color:var(--green)">Running</span> | Loop ${st.current_loop} | Script ${st.current_script_idx}/${st.total_scripts}: ${st.current_script_name || ''}`;
    } else {
      el.innerHTML = `<span style="color:var(--text2)">${st.status || 'idle'}</span>`;
      if (st.status !== 'running') {
        seqRunning = false;
        clearInterval(seqPollTimer);
        seqPollTimer = null;
      }
    }
  }, 1500);
}

// ============================================================
// LOOP RUN ALL - Quick loop run button
// ============================================================
let loopRunPollTimer = null;

// Toggle count input when forever is checked
document.addEventListener('DOMContentLoaded', () => {
  const foreverCb = document.getElementById('loopRunForever');
  const countInput = document.getElementById('loopRunCount');
  if (foreverCb && countInput) {
    foreverCb.addEventListener('change', () => {
      countInput.disabled = foreverCb.checked;
      if (foreverCb.checked) countInput.value = '0';
    });
  }
});

async function loopRunAll() {
  // Determine which scripts to run
  let scripts = [];
  const seqEnabled = seqScripts.filter(s => s.enabled);

  if (seqEnabled.length > 0) {
    // Use the sequence list if it has scripts
    scripts = seqEnabled.map(s => ({path: s.path, enabled: true}));
  } else if (_availableFiles && _availableFiles.length > 0) {
    // Otherwise use all available .urf.json files
    const sorted = [..._availableFiles].filter(f => f.endsWith('.urf.json')).sort((a, b) => a.split('/').pop().localeCompare(b.split('/').pop()));
    if (!sorted.length) { toast('No .urf.json files found', 'error'); return; }
    scripts = sorted.map(f => ({path: f, enabled: true}));
    toast('Running all ' + sorted.length + ' files', 'info');
  } else {
    toast('No scripts in sequence and no files available', 'error');
    return;
  }

  const forever = document.getElementById('loopRunForever')?.checked || false;
  const loopCount = forever ? 0 : +(document.getElementById('loopRunCount')?.value || 3);
  const scriptDelay = +(document.getElementById('seqScriptDelay')?.value || 2);
  const loopDelay = +(document.getElementById('seqLoopDelay')?.value || 10);
  const onError = document.getElementById('seqOnError')?.value || 'skip';

  const body = {
    scripts: scripts,
    settings: {
      loop: true,
      loop_count: loopCount,
      loop_delay: loopDelay,
      script_delay: scriptDelay,
      speed: recording?.settings?.speed || 1.0,
      on_script_error: onError,
      replay_mode: 'auto',
    }
  };

  const res = await api('POST', '/sequence/play', body);
  if (res.ok) {
    seqRunning = true;
    document.getElementById('btnLoopRun').style.display = 'none';
    document.getElementById('btnLoopStop').style.display = '';
    const label = forever ? 'forever' : loopCount + 'x';
    toast('Loop Run started (' + label + ', ' + scripts.length + ' scripts)', 'success');
    loopRunPollStatus();
  } else {
    toast('Error: ' + (res.error || 'Unknown'), 'error');
  }
}

async function loopRunStop() {
  await api('POST', '/sequence/stop', {});
  seqRunning = false;
  document.getElementById('btnLoopRun').style.display = '';
  document.getElementById('btnLoopStop').style.display = 'none';
  document.getElementById('loopRunStatus').textContent = '';
  if (loopRunPollTimer) { clearInterval(loopRunPollTimer); loopRunPollTimer = null; }
  toast('Loop Run stopped');
}

function loopRunPollStatus() {
  if (loopRunPollTimer) clearInterval(loopRunPollTimer);
  loopRunPollTimer = setInterval(async () => {
    const st = await api('GET', '/sequence/status');
    const el = document.getElementById('loopRunStatus');
    const seqEl = document.getElementById('seqStatus');
    if (st.status === 'running') {
      const msg = 'Loop ' + st.current_loop + ' | Script ' + st.current_script_idx + '/' + st.total_scripts + ': ' + (st.current_script_name || '');
      el.innerHTML = '<span style="color:#a371f7">' + msg + '</span>';
      if (seqEl) seqEl.innerHTML = '<span style="color:var(--green)">Running</span> | ' + msg;
    } else {
      el.textContent = st.status || 'idle';
      if (seqEl) seqEl.innerHTML = '<span style="color:var(--text2)">' + (st.status || 'idle') + '</span>';
      if (st.status !== 'running') {
        seqRunning = false;
        document.getElementById('btnLoopRun').style.display = '';
        document.getElementById('btnLoopStop').style.display = 'none';
        clearInterval(loopRunPollTimer);
        loopRunPollTimer = null;
        el.textContent = '';
      }
    }
  }, 1500);
}

async function seqSave() {
  if (!seqScripts.length) { toast('No scripts to save', 'error'); return; }
  const name = prompt('Sequence name:', 'my_sequence');
  if (!name) return;
  const body = {
    name: name,
    scripts: seqScripts,
    settings: {
      loop: document.getElementById('seqLoop')?.value === 'true',
      loop_count: +(document.getElementById('seqLoopCount')?.value || 0),
      loop_delay: +(document.getElementById('seqLoopDelay')?.value || 10),
      script_delay: +(document.getElementById('seqScriptDelay')?.value || 2),
      speed: recording?.settings?.speed || 1.0,
      on_script_error: document.getElementById('seqOnError')?.value || 'skip',
      replay_mode: 'auto',
    }
  };
  const res = await api('POST', '/sequence/save', body);
  if (res.ok) {
    toast('Saved: ' + (res.path || ''), 'success');
    loadFileList();
  } else {
    toast('Error: ' + (res.error || 'Unknown'), 'error');
  }
}

async function seqLoad() {
  const name = prompt('Sequence filename (without .seq.json):');
  if (!name) return;
  const res = await api('GET', '/sequence/load?name=' + encodeURIComponent(name));
  if (res.error) { toast('Error: ' + res.error, 'error'); return; }
  seqScripts = (res.scripts || []).map(s => ({path: s.path, name: (s.path||'').split('/').pop(), enabled: s.enabled !== false}));
  const st = res.settings || {};
  if (st.loop !== undefined) document.getElementById('seqLoop').value = st.loop ? 'true' : 'false';
  if (st.loop_count !== undefined) document.getElementById('seqLoopCount').value = st.loop_count;
  if (st.loop_delay !== undefined) document.getElementById('seqLoopDelay').value = st.loop_delay;
  if (st.script_delay !== undefined) document.getElementById('seqScriptDelay').value = st.script_delay;
  if (st.on_script_error !== undefined) document.getElementById('seqOnError').value = st.on_script_error;
  seqRenderList();
  toast('Loaded sequence: ' + (res.name || name), 'success');
}

// Init sequence list on load
setTimeout(() => seqRenderList(), 100);

// ============================================================
// EXTRACT LOG / STATION DATA
// ============================================================
async function refreshExtractLogs() {
  try {
    const data = await api('GET', '/extract-logs');
    const logs = data.logs || [];
    const el = document.getElementById('extractLogSummary');
    if (!logs.length) {
      el.textContent = 'No extract logs yet';
    } else {
      const totalEntries = logs.reduce((s, l) => s + l.entries, 0);
      el.innerHTML = '<strong>' + logs.length + '</strong> log files, <strong>' + totalEntries + '</strong> total entries';
    }
  } catch (e) {
    // ignore
  }
}
// Auto-refresh extract log summary periodically
setTimeout(() => refreshExtractLogs(), 2000);
setInterval(() => refreshExtractLogs(), 30000);

async function refreshStationLogs() {
  try {
    const data = await api('GET', '/station-logs');
    const logs = data.logs || [];
    const el = document.getElementById('stationLogSummary');
    if (!logs.length) {
      el.textContent = 'No playback logs yet';
    } else {
      const totalRuns = logs.reduce((s, l) => s + l.total_runs, 0);
      const totalOk = logs.reduce((s, l) => s + l.success_runs, 0);
      const totalErr = logs.reduce((s, l) => s + l.error_runs, 0);
      el.innerHTML = '<strong>' + logs.length + '</strong> stations, <strong>' + totalRuns + '</strong> rounds (' +
        '<span style="color:var(--green)">' + totalOk + ' OK</span>' +
        (totalErr > 0 ? ', <span style="color:var(--red)">' + totalErr + ' err</span>' : '') + ')';
    }
  } catch (e) {
    // ignore
  }
}
// Auto-refresh station log summary periodically (10s to match Station Logs page)
setTimeout(() => refreshStationLogs(), 2500);
setInterval(() => refreshStationLogs(), 10000);

// ============================================================
// DISK SPACE
// ============================================================
let _lastDiskPct = 0;
let _diskRefreshTimer = null;
let _diskAutoCleanEnabled = localStorage.getItem('diskAutoCleanEnabled') === '1';
let _diskAutoCleanThreshold = Number(localStorage.getItem('diskAutoCleanThreshold') || '85');
let _lastAutoCleanAt = 0;

function _getDiskRefreshIntervalMs() {
  return _lastDiskPct > 90 ? 15000 : (_lastDiskPct > 80 ? 30000 : 60000);
}

function scheduleDiskRefresh() {
  if (_diskRefreshTimer) clearTimeout(_diskRefreshTimer);
  _diskRefreshTimer = setTimeout(async () => {
    await refreshDiskSpace();
    scheduleDiskRefresh();
  }, _getDiskRefreshIntervalMs());
}

function applyDiskAutoCleanState() {
  const enabled = document.getElementById('diskAutoCleanEnabled');
  const threshold = document.getElementById('diskAutoCleanThreshold');
  if (enabled) enabled.checked = _diskAutoCleanEnabled;
  if (threshold) threshold.value = String(_diskAutoCleanThreshold);
}

function toggleDiskAutoClean(enabled) {
  _diskAutoCleanEnabled = !!enabled;
  localStorage.setItem('diskAutoCleanEnabled', _diskAutoCleanEnabled ? '1' : '0');
  toast(_diskAutoCleanEnabled ? 'Auto Clean enabled' : 'Auto Clean disabled', 'info');
}

function updateDiskAutoCleanThreshold(value) {
  const n = Number(value);
  _diskAutoCleanThreshold = Number.isFinite(n) ? Math.max(70, Math.min(98, Math.round(n))) : 85;
  localStorage.setItem('diskAutoCleanThreshold', String(_diskAutoCleanThreshold));
  applyDiskAutoCleanState();
}

async function maybeAutoClean() {
  if (!_diskAutoCleanEnabled || _lastDiskPct < _diskAutoCleanThreshold) return;
  // Prevent spam cleanup calls while disk remains high.
  const now = Date.now();
  if (now - _lastAutoCleanAt < 3 * 60 * 1000) return;
  _lastAutoCleanAt = now;
  try {
    const res = await api('POST', '/auto-cleanup', { threshold_pct: _diskAutoCleanThreshold });
    if (res && res.ok && !res.skipped) {
      toast('Auto Clean: deleted ' + res.deleted + ' files, freed ' + res.freed_mb + ' MB', 'success');
      refreshExtractLogs();
      refreshStationLogs();
      await refreshDiskSpace();
    }
  } catch (e) {
    // keep UI responsive; skip noisy errors for background cleanup
  }
}

async function refreshDiskSpace() {
  try {
    const data = await api('GET', '/disk-space');
    const el = document.getElementById('diskSpaceInfo');
    const bar = document.getElementById('diskSpaceUsed');
    const bd = document.getElementById('diskBreakdown');
    const warn = document.getElementById('diskSpaceWarning');
    const catActions = document.getElementById('diskCategoryActions');
    if (!data.ok) { el.textContent = 'Error: ' + (data.error || 'unknown'); return; }
    const pct = data.used_pct;
    _lastDiskPct = pct;
    const color = pct > 90 ? 'var(--red)' : pct > 75 ? '#d29922' : 'var(--green)';
    bar.style.width = pct + '%';
    bar.style.background = color;
    el.innerHTML = '<strong>' + data.used_gb + '</strong> / ' + data.total_gb + ' GB used (' +
      '<span style="color:' + color + '">' + pct + '%</span>) | ' +
      'Free: <strong>' + data.free_gb + ' GB</strong>' +
      (data.log_count > 0 ? ' | Logs: <strong>' + data.log_size_mb + ' MB</strong> (' + data.log_count + ' files)' : '');
    // Warning banner for low disk space
    if (pct > 90) {
      warn.innerHTML = '\u26a0 <strong>CRITICAL</strong>: Disk almost full (' + pct + '%). Use Smart Clean or Deep Clean now!';
      warn.style.display = '';
    } else if (pct > 80) {
      warn.innerHTML = '\u26a0 Disk space is low (' + pct + '%). Consider cleaning old files.';
      warn.style.display = '';
      warn.style.color = '#d29922';
      warn.style.background = '#d2992220';
      warn.style.borderColor = '#d2992240';
    } else {
      warn.style.display = 'none';
    }
    // Show breakdown if available
    if (data.breakdown) {
      const b = data.breakdown;
      const parts = [];
      if (b.captures_count > 0) parts.push('Captures: ' + b.captures_mb + ' MB (' + b.captures_count + ')');
      if (b.sent_count > 0) parts.push('Sent: ' + b.sent_mb + ' MB (' + b.sent_count + ')');
      if (b.img_total_count > 0) parts.push('Img total: ' + b.img_total_mb + ' MB');
      if (b.urf_count > 0) parts.push('Recordings: ' + b.urf_mb + ' MB (' + b.urf_count + ')');
      if (b.pycache_count > 0) parts.push('Cache: ' + b.pycache_mb + ' MB');
      if (b.tmp_count > 0) parts.push('Temp: ' + b.tmp_mb + ' MB');
      if (parts.length > 0) {
        bd.innerHTML = parts.join(' | ');
        bd.style.display = '';
      } else {
        bd.style.display = 'none';
      }
      // Show per-category delete buttons when there's something to delete
      catActions.style.display = (b.captures_count > 0 || b.sent_count > 0 || data.log_count > 0) ? '' : 'none';
    }
    maybeAutoClean();
  } catch (e) { /* ignore */ }
}
setTimeout(() => refreshDiskSpace(), 1500);
// Refresh more often when disk is low; recompute interval each cycle.
setTimeout(() => {
  applyDiskAutoCleanState();
  scheduleDiskRefresh();
}, 1600);

async function deepClean() {
  if (!confirm('Deep Clean: Delete ALL captures, sent images, logs, cache, and temp files?\\nThis will free maximum disk space but cannot be undone.')) return;
  try {
    toast('Deep cleaning...', 'info');
    const res = await api('POST', '/deep-clean', {});
    if (res.ok) {
      let msg = 'Deep clean complete! Deleted ' + res.deleted + ' files, freed ' + res.freed_mb + ' MB';
      if (res.details) {
        const d = res.details;
        const parts = [];
        if (d.logs > 0) parts.push(d.logs + ' logs');
        if (d.captures > 0) parts.push(d.captures + ' captures');
        if (d.sent > 0) parts.push(d.sent + ' sent imgs');
        if (d.pycache > 0) parts.push(d.pycache + ' cache');
        if (d.tmp > 0) parts.push(d.tmp + ' temp');
        if (d.system_cache > 0) parts.push(d.system_cache + ' sys cache');
        if (parts.length > 0) msg += ' (' + parts.join(', ') + ')';
      }
      toast(msg, 'success');
    } else {
      toast('Deep clean error: ' + (res.error || 'unknown'), 'error');
    }
    refreshExtractLogs();
    refreshStationLogs();
    refreshDiskSpace();
  } catch (e) { toast('Error: ' + e.message, 'error'); }
}

async function smartClean() {
  if (!confirm('Smart Clean: Delete captures, sent images, and logs older than 6 hours?\\nRecent files will be kept.')) return;
  try {
    toast('Smart cleaning (keeping files < 6h old)...', 'info');
    const res = await api('POST', '/smart-clean', { keep_hours: 6 });
    if (res.ok) {
      let msg = 'Smart clean done! Deleted ' + res.deleted + ' files, freed ' + res.freed_mb + ' MB';
      if (res.details) {
        const d = res.details;
        const parts = [];
        if (d.logs > 0) parts.push(d.logs + ' logs');
        if (d.captures > 0) parts.push(d.captures + ' captures');
        if (d.sent > 0) parts.push(d.sent + ' sent imgs');
        if (d.pycache > 0) parts.push(d.pycache + ' cache');
        if (d.tmp > 0) parts.push(d.tmp + ' temp');
        if (d.system_cache > 0) parts.push(d.system_cache + ' sys cache');
        if (parts.length > 0) msg += ' (' + parts.join(', ') + ')';
      }
      toast(msg, 'success');
    } else {
      toast('Smart clean error: ' + (res.error || 'unknown'), 'error');
    }
    refreshExtractLogs();
    refreshStationLogs();
    refreshDiskSpace();
  } catch (e) { toast('Error: ' + e.message, 'error'); }
}

async function deleteAllCaptures() {
  if (!confirm('Delete ALL capture screenshots? This cannot be undone.')) return;
  try {
    toast('Deleting captures...', 'info');
    const res = await api('POST', '/delete-all-captures', {});
    if (res.ok) {
      toast('Deleted ' + res.deleted + ' captures, freed ' + res.freed_mb + ' MB', 'success');
    } else {
      toast('Error: ' + (res.error || 'unknown'), 'error');
    }
    refreshDiskSpace();
  } catch (e) { toast('Error: ' + e.message, 'error'); }
}

async function deleteAllSent() {
  if (!confirm('Delete ALL sent images? This cannot be undone.')) return;
  try {
    toast('Deleting sent images...', 'info');
    const res = await api('POST', '/delete-all-sent', {});
    if (res.ok) {
      toast('Deleted ' + res.deleted + ' sent images, freed ' + res.freed_mb + ' MB', 'success');
    } else {
      toast('Error: ' + (res.error || 'unknown'), 'error');
    }
    refreshDiskSpace();
  } catch (e) { toast('Error: ' + e.message, 'error'); }
}

// ============================================================
// LOG MANAGEMENT: DELETE / DOWNLOAD / CLEAR
// ============================================================
async function deleteAllExtractLogs() {
  if (!confirm('Delete ALL extract log files? This cannot be undone.')) return;
  try {
    const res = await api('POST', '/delete-all-extract-logs', {});
    toast('Deleted ' + (res.deleted || 0) + ' extract log files', 'success');
    refreshExtractLogs();
    refreshDiskSpace();
  } catch (e) { toast('Error: ' + e.message, 'error'); }
}

async function deleteAllStationLogs() {
  if (!confirm('Delete ALL station playback log files? This cannot be undone.')) return;
  try {
    const res = await api('POST', '/delete-all-station-logs', {});
    toast('Deleted ' + (res.deleted || 0) + ' station log files', 'success');
    refreshStationLogs();
    refreshDiskSpace();
  } catch (e) { toast('Error: ' + e.message, 'error'); }
}

async function clearAllLogs() {
  if (!confirm('Delete ALL log files (extract + station + all others)? This will free up disk space but cannot be undone.')) return;
  try {
    const res = await api('POST', '/clear-all-logs', {});
    toast('Cleared ' + (res.deleted || 0) + ' log files', 'success');
    refreshExtractLogs();
    refreshStationLogs();
    refreshDiskSpace();
  } catch (e) { toast('Error: ' + e.message, 'error'); }
}

function downloadExtractLogs() {
  window.open('/api/download-extract-logs', '_blank');
}

function downloadStationLogs() {
  window.open('/api/download-station-logs', '_blank');
}

function downloadAllLogs() {
  window.open('/api/download-all-logs', '_blank');
}

// ============================================================
// DRAG & DROP
// ============================================================
function onDragStart(e, idx) { dragSrcIdx = idx; e.currentTarget.classList.add('dragging'); e.dataTransfer.effectAllowed = 'move'; }
function onDragOver(e) { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; }
function onDrop(e, targetIdx) {
  e.preventDefault();
  if (dragSrcIdx < 0 || dragSrcIdx === targetIdx) return;
  const visible = getVisibleFrames();
  const srcFrame = visible[dragSrcIdx];
  const targetFrame = visible[targetIdx];
  const srcRealIdx = recording.frames.indexOf(srcFrame);
  const targetRealIdx = recording.frames.indexOf(targetFrame);
  recording.frames.splice(srcRealIdx, 1);
  const insertIdx = targetRealIdx > srcRealIdx ? targetRealIdx : targetRealIdx;
  recording.frames.splice(insertIdx, 0, srcFrame);
  renderTimeline();
}
function onDragEnd(e) { dragSrcIdx = -1; e.currentTarget.classList.remove('dragging'); }

// ============================================================
// VIEW
// ============================================================
function setView(view, el) {
  currentView = view;
  document.querySelectorAll('.view-tab').forEach(t => t.classList.remove('active'));
  el.classList.add('active');
  renderTimeline();
}

// ============================================================
// SETTINGS & META
// ============================================================
function updateMeta() { updateMetaFromUI(); renderMeta(); }
function updateSettings() { updateSettingsFromUI(); }

function updateMetaFromUI() {
  if (!recording) return;
  recording.meta.name = document.getElementById('metaName').value;
  recording.meta.app_package = document.getElementById('metaPkg').value;
  recording.meta.device = document.getElementById('metaDev').value;
}

function updateSettingsFromUI() {
  if (!recording) return;
  recording.settings.speed = +(document.getElementById('setSpeed').value || 1);
  recording.settings.loop = document.getElementById('setLoop').checked;
  recording.settings.loop_count = +(document.getElementById('setLoopCount').value || 0);
  recording.settings.loop_delay = +(document.getElementById('setLoopDelay').value || 5);
  recording.settings.on_error = document.getElementById('setOnError').value;
  recording.settings.max_retries = +(document.getElementById('setRetries').value || 3);
}

// ============================================================
// UTILS
// ============================================================
function nextId() { return recording ? Math.max(0, ...recording.frames.map(f=>f.id)) + 1 : 1; }
function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

function toast(msg, type='') {
  const el = document.createElement('div');
  el.className = 'toast' + (type ? ' '+type : '');
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3000);
}

// ============================================================
// PLAY / RECORD CONTROLS
// ============================================================
let playbackState = 'idle'; // idle | playing | recording
let statusPollTimer = null;
let elapsedTimer = null;
let elapsedStartTime = null;

function formatElapsed(ms) {
  const totalSec = Math.floor(ms / 1000);
  const min = Math.floor(totalSec / 60);
  const sec = totalSec % 60;
  return String(min).padStart(2, '0') + ':' + String(sec).padStart(2, '0');
}

function startElapsedTimer() {
  elapsedStartTime = Date.now();
  const timerEl = document.getElementById('playbackTimer');
  timerEl.style.display = '';
  timerEl.textContent = '00:00';
  stopElapsedTimer();
  elapsedTimer = setInterval(() => {
    const elapsed = Date.now() - elapsedStartTime;
    timerEl.textContent = formatElapsed(elapsed);
  }, 500);
}

function stopElapsedTimer() {
  if (elapsedTimer) { clearInterval(elapsedTimer); elapsedTimer = null; }
}

let _lastPlayingFrameId = -1;
function highlightPlayingFrame(currentStep) {
  // Map current_step (1-based, enabled-only) to a frame id
  let targetId = -1;
  if (recording && currentStep > 0) {
    const enabledFrames = recording.frames.filter(f => f.enabled !== false);
    const idx = currentStep - 1;
    if (idx >= 0 && idx < enabledFrames.length) {
      targetId = enabledFrames[idx].id;
    }
  }
  if (targetId === _lastPlayingFrameId) return;
  _lastPlayingFrameId = targetId;

  // Remove previous highlight
  document.querySelectorAll('.frame-card.playing').forEach(el => el.classList.remove('playing'));

  if (targetId < 0) return;

  // Add highlight to current frame
  const card = document.querySelector(`.frame-card[data-id="${targetId}"]`);
  if (card) {
    card.classList.add('playing');
    card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
}

function updatePlaybackUI(state, info) {
  playbackState = state;
  const btnPlay = document.getElementById('btnPlay');
  const btnRec = document.getElementById('btnRec');
  const btnStop = document.getElementById('btnStop');
  const statusBadge = document.getElementById('statusBadge');
  const recDot = document.getElementById('recDot');
  const progressContainer = document.getElementById('progressContainer');
  const progressBar = document.getElementById('progressBar');
  const stepInfo = document.getElementById('stepInfo');
  const timerEl = document.getElementById('playbackTimer');

  // Reset all
  btnPlay.classList.remove('active');
  btnRec.classList.remove('active');
  recDot.classList.remove('active');
  progressBar.classList.remove('recording');
  timerEl.classList.remove('recording', 'playing');

  if (state === 'idle') {
    btnPlay.disabled = false;
    btnRec.disabled = false;
    btnStop.disabled = true;
    statusBadge.className = 'status-badge status-idle';
    statusBadge.textContent = 'IDLE';
    progressContainer.style.display = 'none';
    timerEl.style.display = 'none';
    stepInfo.textContent = '';
    highlightPlayingFrame(-1);
    stopStatusPoll();
    stopElapsedTimer();
  } else if (state === 'playing') {
    btnPlay.classList.add('active');
    btnPlay.disabled = true;
    btnRec.disabled = true;
    btnStop.disabled = false;
    statusBadge.className = 'status-badge status-playing';
    statusBadge.textContent = 'PLAYING';
    progressContainer.style.display = '';
    timerEl.style.display = '';
    timerEl.classList.add('playing');
    if (info) {
      const pct = info.total_steps > 0 ? (info.current_step / info.total_steps * 100) : 0;
      progressBar.style.width = pct + '%';
      stepInfo.textContent = info.current_step + '/' + info.total_steps;
      highlightPlayingFrame(info.current_step);
    }
  } else if (state === 'recording') {
    btnRec.classList.add('active');
    recDot.classList.add('active');
    btnPlay.disabled = true;
    btnRec.disabled = true;
    btnStop.disabled = false;
    statusBadge.className = 'status-badge status-recording';
    statusBadge.textContent = 'REC';
    progressContainer.style.display = '';
    progressBar.classList.add('recording');
    progressBar.style.width = '100%';
    timerEl.style.display = '';
    timerEl.classList.add('recording');
    if (info) {
      stepInfo.textContent = (info.frames_count || 0) + ' frames';
    }
  } else if (state === 'error') {
    btnPlay.disabled = false;
    btnRec.disabled = false;
    btnStop.disabled = true;
    statusBadge.className = 'status-badge status-error';
    statusBadge.textContent = 'ERROR';
    progressContainer.style.display = 'none';
    timerEl.style.display = 'none';
    stepInfo.textContent = info?.message || '';
    highlightPlayingFrame(-1);
    stopStatusPoll();
    stopElapsedTimer();
  }
}

async function startPlay() {
  if (!recording || !filePath) { toast('No file loaded', 'error'); return; }
  // Immediately disable Play button to prevent double-click
  document.getElementById('btnPlay').disabled = true;
  // Save first
  await saveFile();
  try {
    const res = await api('POST', '/play', { path: filePath });
    if (res.ok) {
      updatePlaybackUI('playing', {current_step: 0, total_steps: recording.frames.length});
      startElapsedTimer();
      toast('Playback started', 'success');
      startStatusPoll();
    } else {
      document.getElementById('btnPlay').disabled = false;
      toast('Play failed: ' + (res.error || ''), 'error');
    }
  } catch (e) {
    document.getElementById('btnPlay').disabled = false;
    toast('Play error: ' + e.message, 'error');
  }
}

async function startRecord() {
  // Auto-create a new recording if none is loaded
  if (!recording) {
    newRecording();
    if (!recording) return;  // user cancelled
  }
  const pkg = recording.meta?.app_package || '';
  if (!pkg) {
    const p = prompt('Enter app package name:');
    if (!p) return;
    recording.meta.app_package = p;
    document.getElementById('metaPkg').value = p;
  }

  // Immediately disable Record button to prevent double-click
  document.getElementById('btnRec').disabled = true;

  // Start web recording (accurate JS timing for web clicks)
  webRecording = true;
  webRecStartTime = Date.now();
  webRecFrameCount = 0;

  // Also start device getevent recording for mobile touches
  let deviceRecOk = false;
  try {
    const effectivePkg = recording.meta.app_package;
    const res = await api('POST', '/record', {
      path: filePath || '/work/new-recording.urf.json',
      package: effectivePkg,
      device: recording.meta?.device || '',
    });
    deviceRecOk = !!res.ok;
    if (!deviceRecOk) {
      // Device recording failed (no ADB etc) - continue with web-only recording
      console.log('Device recording not available: ' + (res.error || ''));
    }
  } catch (e) {
    console.log('Device recording error: ' + e.message);
  }

  updatePlaybackUI('recording', {frames_count: 0});
  startElapsedTimer();
  startStatusPoll();

  if (deviceRecOk) {
    toast('Recording started - tap on screen or device', 'success');
  } else {
    toast('Recording started (web only) - tap on screen to record', 'success');
  }
}

async function stopPlayback() {
  // Guard: prevent double-click / concurrent invocations
  if (_stopInProgress) return;
  _stopInProgress = true;

  // Immediately disable the Stop button and stop status polling
  // to prevent race conditions with the poll reloading the file
  document.getElementById('btnStop').disabled = true;
  stopStatusPoll();
  stopElapsedTimer();

  const wasWebRecording = webRecording;
  const webFrames = wasWebRecording && recording ? [...recording.frames] : [];

  // Stop web recording
  webRecording = false;

  // Update total duration from web frames
  if (wasWebRecording && recording && recording.frames.length) {
    recording.meta.total_duration_ms = Math.max(...recording.frames.map(f => f.t));
  }

  try {
    // Stop device recording subprocess
    const res = await api('POST', '/stop');

    if (wasWebRecording && recording) {
      // Web recording frames are the source of truth.
      // The device recorder (universal_recorder.py via getevent) captures the
      // same actions sent by the web UI (adb input tap/keyevent/ctrl) which
      // produces duplicate gesture, key, and extract frames.
      // Skip device merge entirely — just save the web frames as-is.
      await saveFile();
      refreshAll();
      toast('Recording stopped - ' + recording.frames.length + ' frames saved', 'success');
    } else {
      toast('Stopped', 'success');
      // Reload file if was playing
      if (filePath) {
        setTimeout(() => loadFile(filePath), 500);
      }
    }

    updatePlaybackUI('idle', null);
  } catch (e) {
    toast('Stop error: ' + e.message, 'error');
    updatePlaybackUI('idle', null);
    // Still save web frames if we have them
    if (wasWebRecording && recording && recording.frames.length) {
      await saveFile();
    }
  } finally {
    _stopInProgress = false;
  }
}

function startStatusPoll() {
  stopStatusPoll();
  statusPollTimer = setInterval(async () => {
    // Skip polling while stopPlayback() is handling its own shutdown sequence
    if (_stopInProgress) return;
    try {
      const res = await api('GET', '/status');
      if (res.status === 'idle' || res.status === 'stopped') {
        updatePlaybackUI('idle', null);
        if (filePath) {
          loadFile(filePath);
          loadFileList();
        }
      } else if (res.status === 'playing') {
        updatePlaybackUI('playing', res);
      } else if (res.status === 'recording') {
        updatePlaybackUI('recording', res);
      } else if (res.status === 'error') {
        updatePlaybackUI('error', res);
      }
    } catch (e) { /* ignore poll errors */ }
  }, 1000);
}

function stopStatusPoll() {
  if (statusPollTimer) { clearInterval(statusPollTimer); statusPollTimer = null; }
}

// ============================================================
// RIGHT PANEL TABS
// ============================================================
function switchRightTab(tabName, btn) {
  document.querySelectorAll('.right-panel-tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.right-tab-content').forEach(c => c.classList.remove('active'));
  btn.classList.add('active');
  const tabEl = document.getElementById('tab' + tabName.charAt(0).toUpperCase() + tabName.slice(1));
  if (tabEl) tabEl.classList.add('active');
}

// ============================================================
// DEBUG FRAME OVERLAY
// ============================================================
let debugFramesEnabled = false;
let debugElementsCache = [];

function toggleDebugFrames() {
  debugFramesEnabled = !debugFramesEnabled;
  const btn = document.getElementById('btnDebugFrames');
  const overlay = document.getElementById('debugOverlays');
  btn.classList.toggle('active', debugFramesEnabled);
  overlay.style.display = debugFramesEnabled ? 'block' : 'none';
  if (debugFramesEnabled) {
    // Fetch elements immediately when toggled on
    fetchDebugElements();
  } else {
    overlay.innerHTML = '';
    const badge = document.getElementById('debugCountBadge');
    badge.style.display = 'none';
  }
}

async function fetchDebugElements() {
  if (!debugFramesEnabled) return;
  try {
    const resp = await fetch('/api/elements');
    const data = await resp.json();
    debugElementsCache = data.elements || [];
    renderDebugOverlays(debugElementsCache);
  } catch (e) {
    console.error('Debug elements fetch error:', e);
  }
}

function renderDebugOverlays(elements) {
  const container = document.getElementById('debugOverlays');
  const img = document.getElementById('screenImg');
  container.innerHTML = '';

  if (!screenNaturalW || !screenNaturalH || !elements.length) {
    document.getElementById('debugCountBadge').style.display = 'none';
    return;
  }

  const displayW = img.clientWidth;
  const displayH = img.clientHeight;
  const scaleX = displayW / screenNaturalW;
  const scaleY = displayH / screenNaturalH;

  let visibleCount = 0;
  let clickCount = 0, textCount = 0, idCount = 0, otherCount = 0;

  elements.forEach((el) => {
    const b = el.bounds;
    if (!b || b.length < 4) return;
    const [x1, y1, x2, y2] = b;
    const w = x2 - x1;
    const h = y2 - y1;
    if (w <= 0 || h <= 0) return;
    // Skip full-screen containers
    if (w >= screenNaturalW * 0.95 && h >= screenNaturalH * 0.95) return;

    visibleCount++;
    const div = document.createElement('div');
    div.className = 'debug-overlay-rect';

    // Classify element: clickable > has-text > has-id > other
    let category;
    if (el.clickable) {
      div.classList.add('clickable'); category = 'click'; clickCount++;
    } else if (el.text && el.text.trim()) {
      div.classList.add('has-text'); category = 'text'; textCount++;
    } else if (el.resource_id) {
      div.classList.add('has-id'); category = 'id'; idCount++;
    } else {
      div.classList.add('other'); category = 'other'; otherCount++;
    }

    div.style.left = (x1 * scaleX) + 'px';
    div.style.top = (y1 * scaleY) + 'px';
    div.style.width = (w * scaleX) + 'px';
    div.style.height = (h * scaleY) + 'px';

    const scaledH = h * scaleY;
    const scaledW = w * scaleX;

    // Card header: [idx] ClassName | text/id
    if (scaledH > 10 && scaledW > 20) {
      const header = document.createElement('div');
      header.className = 'debug-card-header';

      // Index badge
      const idx = document.createElement('span');
      idx.className = 'debug-idx';
      idx.textContent = visibleCount;
      header.appendChild(idx);

      // Short class name
      const clsName = (el.cls || '').split('.').pop() || '';
      if (clsName) {
        const cls = document.createElement('span');
        cls.className = 'debug-cls';
        cls.textContent = clsName;
        header.appendChild(cls);
      }

      // Text or resource_id info
      const infoText = el.text ? ('"' + (el.text.length > 15 ? el.text.slice(0, 15) + '..' : el.text) + '"')
        : el.resource_id ? el.resource_id.split('/').pop() : '';
      if (infoText) {
        const info = document.createElement('span');
        info.className = 'debug-info';
        info.textContent = infoText;
        header.appendChild(info);
      }

      div.appendChild(header);
    }

    // Card footer: bounds info (only for elements tall enough)
    if (scaledH > 20 && scaledW > 40 && category !== 'other') {
      const footer = document.createElement('div');
      footer.className = 'debug-card-footer';
      footer.textContent = '[' + x1 + ',' + y1 + '][' + x2 + ',' + y2 + ']';
      div.appendChild(footer);
    }

    container.appendChild(div);
  });

  // Add legend bar
  const legend = document.createElement('div');
  legend.className = 'debug-legend';
  const legendItems = [
    { cls: 'lg-click', label: 'Clickable', count: clickCount },
    { cls: 'lg-text', label: 'Text', count: textCount },
    { cls: 'lg-id', label: 'ID', count: idCount },
    { cls: 'lg-other', label: 'Other', count: otherCount },
  ];
  legendItems.forEach(item => {
    if (item.count === 0) return;
    const li = document.createElement('span');
    li.className = 'debug-legend-item';
    li.innerHTML = '<span class="debug-legend-dot ' + item.cls + '"></span>' + item.label + ' ' + item.count;
    legend.appendChild(li);
  });
  container.appendChild(legend);

  // Update badge count
  const badge = document.getElementById('debugCountBadge');
  badge.textContent = visibleCount;
  badge.style.display = visibleCount > 0 ? 'inline' : 'none';
}

// ============================================================
// DEVICE SCREEN & CONTROLS
// ============================================================
let screenElements = [];
let screenNaturalW = 0;
let screenNaturalH = 0;
let screenRefreshTimer = null;
let captureInFlight = false;

// Screen auto-refresh
function startScreenRefresh() {
  stopScreenRefresh();
  screenRefreshTimer = setInterval(() => {
    if (document.getElementById('autoRefresh')?.checked) {
      captureScreen();
    }
  }, 2000);
}

function stopScreenRefresh() {
  if (screenRefreshTimer) { clearInterval(screenRefreshTimer); screenRefreshTimer = null; }
}

async function captureScreen() {
  if (captureInFlight) return;
  captureInFlight = true;
  const status = document.getElementById('deviceStatus');
  status.textContent = 'Capturing...';

  try {
    // Fetch screenshot (elements are fetched separately on demand)
    const imgResp = await fetch('/api/screenshot');
    if (!imgResp.ok) throw new Error('Screenshot failed');
    const blob = await imgResp.blob();

    const img = document.getElementById('screenImg');
    const noScreen = document.getElementById('noScreen');
    const url = URL.createObjectURL(blob);

    img.onload = function() {
      screenNaturalW = this.naturalWidth;
      screenNaturalH = this.naturalHeight;
      noScreen.style.display = 'none';
      img.style.display = 'block';
      URL.revokeObjectURL(url);
      // Refresh debug overlays if enabled
      if (debugFramesEnabled) {
        fetchDebugElements();
      }
    };
    img.src = url;
    status.textContent = 'Screen updated | ' + new Date().toLocaleTimeString();
  } catch (e) {
    status.textContent = 'Error: ' + e.message;
  } finally {
    captureInFlight = false;
  }
}

async function captureScreenWithElements() {
  if (captureInFlight) return;
  captureInFlight = true;
  const status = document.getElementById('deviceStatus');
  status.textContent = 'Capturing screen + elements...';

  try {
    const [imgResp, elData] = await Promise.all([
      fetch('/api/screenshot').then(r => {
        if (!r.ok) throw new Error('Screenshot failed');
        return r.blob();
      }),
      fetch('/api/elements').then(r => r.json()),
    ]);

    const img = document.getElementById('screenImg');
    const noScreen = document.getElementById('noScreen');
    const url = URL.createObjectURL(imgResp);

    img.onload = function() {
      screenNaturalW = this.naturalWidth;
      screenNaturalH = this.naturalHeight;
      noScreen.style.display = 'none';
      img.style.display = 'block';
      renderElementOverlays(elData.elements || []);
      // Also refresh debug overlays with same data
      if (debugFramesEnabled) {
        renderDebugOverlays(elData.elements || []);
      }
      URL.revokeObjectURL(url);
    };
    img.src = url;

    screenElements = elData.elements || [];
    renderElementList(screenElements);
    // Update debug cache if debug is on
    if (debugFramesEnabled) {
      debugElementsCache = screenElements;
    }

    const count = screenElements.length;
    const clickable = screenElements.filter(e => e.clickable || e.text || e.resource_id).length;
    status.textContent = count + ' elements (' + clickable + ' interactive) | ' + new Date().toLocaleTimeString();
  } catch (e) {
    status.textContent = 'Error: ' + e.message;
  } finally {
    captureInFlight = false;
  }
}

function renderElementOverlays(elements) {
  const img = document.getElementById('screenImg');
  const container = document.getElementById('elementOverlays');
  container.innerHTML = '';

  if (!screenNaturalW || !screenNaturalH) return;

  const displayW = img.clientWidth;
  const displayH = img.clientHeight;
  const scaleX = displayW / screenNaturalW;
  const scaleY = displayH / screenNaturalH;

  elements.forEach((el, idx) => {
    if (!el.bounds || el.bounds.length < 4) return;
    const [x1, y1, x2, y2] = el.bounds;
    const w = x2 - x1;
    const h = y2 - y1;
    if (w <= 0 || h <= 0) return;
    if (w >= screenNaturalW * 0.95 && h >= screenNaturalH * 0.95) return;
    if (!el.clickable && !el.text && !el.resource_id) return;

    const div = document.createElement('div');
    div.className = 'element-overlay';
    div.style.left = (x1 * scaleX) + 'px';
    div.style.top = (y1 * scaleY) + 'px';
    div.style.width = (w * scaleX) + 'px';
    div.style.height = (h * scaleY) + 'px';
    div.style.pointerEvents = 'auto';

    const label = el.text || (el.resource_id || '').split('/').pop() || '';
    if (label) {
      const lbl = document.createElement('span');
      lbl.className = 'el-label';
      lbl.textContent = label;
      div.appendChild(lbl);
    }

    div.onclick = (e) => { e.stopPropagation(); addElementFrame(el); };
    div.title = [el.resource_id, el.text, el.cls, '[' + el.bounds.join(',') + ']'].filter(Boolean).join('\n');

    container.appendChild(div);
  });
}

function renderElementList(elements) {
  const panel = document.getElementById('elementListPanel');
  const clickable = elements.filter(el => el.clickable || el.text || el.resource_id);

  if (!clickable.length) {
    panel.innerHTML = '<h4>Interactive Elements</h4><div style="color:var(--text2);padding:8px;font-size:12px">No interactive elements found</div>';
    return;
  }

  let html = '<h4>Interactive Elements (' + clickable.length + ')</h4>';
  html += clickable.map((el) => {
    const globalIdx = elements.indexOf(el);
    const rid = el.resource_id ? el.resource_id.split('/').pop() : '';
    return '<div class="el-item" onclick="addElementFrame(screenElements[' + globalIdx + '])" title="' + esc(el.resource_id || '') + '">'
      + (rid ? '<span class="el-id">' + esc(rid) + '</span> ' : '')
      + (el.text ? '<span class="el-text">' + esc(el.text) + '</span> ' : '')
      + '<span class="el-class">' + esc(el.cls.split('.').pop()) + '</span>'
      + '</div>';
  }).join('');
  panel.innerHTML = html;
}

function addElementFrame(el) {
  if (!recording) {
    toast('Load or create a file first', 'error');
    return;
  }
  // Use accurate web recording timestamp if recording is active
  const t = webRecording
    ? Date.now() - webRecStartTime
    : (recording.frames.length ? recording.frames[recording.frames.length - 1].t + 1000 : 0);
  const f = {
    id: nextId(),
    t: t,
    type: 'element',
    action: 'tap',
    resource_id: el.resource_id || '',
    text: el.text || '',
    timeout: 10,
    enabled: true,
    note: '',
  };
  recording.frames.push(f);
  if (webRecording) {
    webRecFrameCount++;
    const stepInfo = document.getElementById('stepInfo');
    if (stepInfo) stepInfo.textContent = webRecFrameCount + ' frames';
  }
  selectedId = f.id;
  refreshAll();

  // Also send tap to device at element center during recording
  if (webRecording && el.center) {
    api('POST', '/tap', { x: el.center[0], y: el.center[1] }).then(() => {
      setTimeout(captureScreen, 500);
    }).catch(() => {});
  }

  toast('Added element: ' + (el.text || (el.resource_id||'').split('/').pop() || 'element') + (webRecording ? ' [REC]' : ''), 'success');
}

// ============================================================
// SCREEN TOUCH INTERACTION (tap & swipe) + WEB RECORDING
// ============================================================
let touchStartPos = null;
let touchStartTime = 0;

function screenPosToDevice(px, py) {
  const img = document.getElementById('screenImg');
  if (!img || !screenNaturalW || !screenNaturalH) return null;
  const displayW = img.clientWidth;
  const displayH = img.clientHeight;
  if (displayW <= 0 || displayH <= 0) return null;
  return {
    x: Math.round(px / displayW * screenNaturalW),
    y: Math.round(py / displayH * screenNaturalH),
  };
}

function addWebRecFrame(action, startX, startY, endX, endY, durationMs) {
  if (!webRecording || !recording) return;
  const t = Date.now() - webRecStartTime;
  const f = {
    id: nextId(),
    t: t,
    type: 'gesture',
    action: action,
    x: startX,
    y: startY,
    x2: endX || startX,
    y2: endY || startY,
    duration_ms: durationMs || 50,
    enabled: true,
    note: '',
  };
  recording.frames.push(f);
  webRecFrameCount++;
  renderTimeline();
  // Scroll to bottom of timeline
  const tl = document.getElementById('timeline');
  if (tl) tl.scrollTop = tl.scrollHeight;
  // Update step info
  const stepInfo = document.getElementById('stepInfo');
  if (stepInfo) stepInfo.textContent = webRecFrameCount + ' frames';
}

async function handleScreenInteraction(startPx, startPy, endPx, endPy, elapsed) {
  const img = document.getElementById('screenImg');
  if (!img || !screenNaturalW || !screenNaturalH) return;

  const startDev = screenPosToDevice(startPx, startPy);
  const endDev = screenPosToDevice(endPx, endPy);
  if (!startDev || !endDev) return;

  const dx = endPx - startPx;
  const dy = endPy - startPy;
  const dist = Math.sqrt(dx * dx + dy * dy);

  // Show visual touch feedback
  showTouchFeedback(startPx, startPy);

  const status = document.getElementById('deviceStatus');

  if (dist < 10) {
    // Tap
    status.textContent = 'Tap: (' + startDev.x + ', ' + startDev.y + ')...';
    // Record frame BEFORE sending to device (accurate timing)
    addWebRecFrame('tap', startDev.x, startDev.y, startDev.x, startDev.y, Math.max(50, elapsed));
    try {
      const res = await api('POST', '/tap', { x: startDev.x, y: startDev.y });
      if (res.ok) {
        status.textContent = 'Tapped (' + startDev.x + ', ' + startDev.y + ')' + (webRecording ? ' [REC]' : '');
        setTimeout(captureScreen, 500);
      } else {
        status.textContent = 'Tap failed: ' + (res.error || '');
      }
    } catch (err) {
      status.textContent = 'Tap error: ' + err.message;
    }
  } else {
    // Swipe
    const duration = Math.max(150, Math.min(elapsed, 1500));
    status.textContent = 'Swipe: (' + startDev.x + ',' + startDev.y + ') -> (' + endDev.x + ',' + endDev.y + ')...';
    // Record frame BEFORE sending to device (accurate timing)
    addWebRecFrame('swipe', startDev.x, startDev.y, endDev.x, endDev.y, duration);
    try {
      const res = await api('POST', '/swipe', { x1: startDev.x, y1: startDev.y, x2: endDev.x, y2: endDev.y, duration: duration });
      if (res.ok) {
        status.textContent = 'Swiped (' + startDev.x + ',' + startDev.y + ') -> (' + endDev.x + ',' + endDev.y + ')' + (webRecording ? ' [REC]' : '');
        setTimeout(captureScreen, 500);
      } else {
        status.textContent = 'Swipe failed: ' + (res.error || '');
      }
    } catch (err) {
      status.textContent = 'Swipe error: ' + err.message;
    }
  }
}

function initScreenTouch() {
  const container = document.getElementById('screenContainer');
  if (!container) return;

  // --- Mouse events (desktop) ---
  container.addEventListener('mousedown', (e) => {
    if (e.target.classList.contains('element-overlay')) return;
    if (ctrlHeld) return; // Ctrl+hover mode, don't start touch
    const rect = container.getBoundingClientRect();
    touchStartPos = { x: e.clientX - rect.left, y: e.clientY - rect.top };
    touchStartTime = Date.now();
    e.preventDefault();
  });

  container.addEventListener('mouseup', async (e) => {
    if (!touchStartPos) return;
    const rect = container.getBoundingClientRect();
    const endPos = { x: e.clientX - rect.left, y: e.clientY - rect.top };
    const elapsed = Date.now() - touchStartTime;
    await handleScreenInteraction(touchStartPos.x, touchStartPos.y, endPos.x, endPos.y, elapsed);
    touchStartPos = null;
  });

  container.addEventListener('mouseleave', () => { touchStartPos = null; });

  // --- Touch events (mobile browser) ---
  let mobileTouchStart = null;
  let mobileTouchStartTime = 0;

  container.addEventListener('touchstart', (e) => {
    if (e.target.classList.contains('element-overlay')) return;
    const touch = e.touches[0];
    const rect = container.getBoundingClientRect();
    mobileTouchStart = { x: touch.clientX - rect.left, y: touch.clientY - rect.top };
    mobileTouchStartTime = Date.now();
    e.preventDefault();
  }, { passive: false });

  container.addEventListener('touchend', async (e) => {
    if (!mobileTouchStart) return;
    const touch = e.changedTouches[0];
    const rect = container.getBoundingClientRect();
    const endPos = { x: touch.clientX - rect.left, y: touch.clientY - rect.top };
    const elapsed = Date.now() - mobileTouchStartTime;
    await handleScreenInteraction(mobileTouchStart.x, mobileTouchStart.y, endPos.x, endPos.y, elapsed);
    mobileTouchStart = null;
    e.preventDefault();
  }, { passive: false });

  container.addEventListener('touchcancel', () => { mobileTouchStart = null; });

  // --- Ctrl+Hover element inspection ---
  container.addEventListener('mousemove', (e) => {
    if (!ctrlHeld) { hideInspectTooltip(); return; }
    const rect = container.getBoundingClientRect();
    const px = e.clientX - rect.left;
    const py = e.clientY - rect.top;
    const dev = screenPosToDevice(px, py);
    if (!dev) return;

    // Debounce: short delay so rapid mouse moves collapse into one request.
    // Skip if a previous request is still in-flight to avoid queuing.
    clearTimeout(inspectDebounceTimer);
    inspectDebounceTimer = setTimeout(async () => {
      if (inspectBusy) return;        // previous request still running
      const myId = ++inspectRequestId;
      inspectBusy = true;
      try {
        const data = await api('POST', '/inspect', { x: dev.x, y: dev.y });
        if (myId === inspectRequestId && ctrlHeld) {
          showInspectTooltip(container, px, py, dev.x, dev.y, data);
        }
      } catch (err) {
        // ignore
      } finally {
        inspectBusy = false;
      }
    }, 120);
  });
}

// ============================================================
// CTRL+HOVER ELEMENT INSPECTION
// ============================================================
// Track Ctrl press timing for recording capture
let ctrlDownTime = 0;
let ctrlCaptureTriggered = false;
let ctrlCaptureInFlight = false;  // prevent concurrent capture API calls

document.addEventListener('keydown', (e) => {
  if (e.key === 'Control') {
    if (e.repeat) return; // Ignore held-down key repeats
    ctrlHeld = true;
    ctrlDownTime = Date.now();
    ctrlCaptureTriggered = false;
    // Show cursor change immediately so user knows inspect mode is active
    const sc = document.getElementById('screenContainer');
    if (sc) sc.style.cursor = 'crosshair';

    // During recording: Ctrl press triggers a capture/extract frame
    if (webRecording && recording && !ctrlCaptureTriggered) {
      ctrlCaptureTriggered = true;
      captureCtrlFrame();
    }
  }
});
document.addEventListener('keyup', (e) => {
  if (e.key === 'Control') {
    ctrlHeld = false;
    ctrlDownTime = 0;
    clearTimeout(inspectDebounceTimer);
    inspectRequestId++;          // invalidate any in-flight request
    hideInspectTooltip();
    const sc = document.getElementById('screenContainer');
    if (sc) sc.style.cursor = '';
  }
});
// Also handle window blur (e.g. Alt+Tab while Ctrl held)
window.addEventListener('blur', () => {
  if (ctrlHeld) {
    ctrlHeld = false;
    ctrlDownTime = 0;
    clearTimeout(inspectDebounceTimer);
    inspectRequestId++;
    hideInspectTooltip();
    const sc = document.getElementById('screenContainer');
    if (sc) sc.style.cursor = '';
  }
});

// ============================================================
// CTRL CAPTURE DURING RECORDING
// ============================================================
async function captureCtrlFrame() {
  if (!webRecording || !recording) return;
  if (ctrlCaptureInFlight) return; // prevent concurrent captures from piling up

  const t = Date.now() - webRecStartTime;
  const captureId = nextId();

  // Add extract frame that captures all visible text + screenshot
  const f = {
    id: captureId,
    t: t,
    type: 'extract',
    extract_label: 'ctrl_capture_' + captureId,
    extract_all_text: true,
    extract_screenshot: true,
    extract_fields: [],
    enabled: true,
    note: 'Ctrl capture at ' + formatElapsed(t),
  };
  recording.frames.push(f);
  webRecFrameCount++;

  // Update UI
  renderTimeline();
  const tl = document.getElementById('timeline');
  if (tl) tl.scrollTop = tl.scrollHeight;
  const stepInfo = document.getElementById('stepInfo');
  if (stepInfo) stepInfo.textContent = webRecFrameCount + ' frames';

  // Show visual feedback
  showCtrlCaptureFeedback();
  toast('Ctrl Capture #' + captureId + ' - fetching screen data...', 'success');

  // Fetch current screen elements AND save to JSON log immediately
  ctrlCaptureInFlight = true;
  try {
    const elData = await api('GET', '/elements');
    if (elData && elData.elements) {
      // Build extract_fields with full element data (text + coords)
      const fields = elData.elements
        .filter(el => el.text && el.text.trim())
        .map(el => ({
          name: el.resource_id ? el.resource_id.split('/').pop() : el.text.trim().substring(0, 30),
          resource_id: el.resource_id || '',
          text: el.text.trim(),
          class: el.cls || '',
          bounds: el.bounds || [],
          center: el.center || [],
          clickable: el.clickable || false,
        }));

      // Store fields in the extract frame
      f.extract_fields = fields;

      const textElements = fields.map(el => el.text);
      if (textElements.length) {
        f.note = 'Ctrl capture at ' + formatElapsed(t) + ' | ' + fields.length + ' elements | texts: ' + textElements.slice(0, 8).join(', ');
        if (textElements.length > 8) f.note += ' ...+' + (textElements.length - 8) + ' more';
      }
      renderTimeline();

      // Flash debug overlays to show captured elements (auto-hide after 3s)
      if (debugFramesEnabled) {
        debugElementsCache = elData.elements;
        renderDebugOverlays(elData.elements);
      } else {
        // Temporarily show captured frames even if debug is off
        const overlay = document.getElementById('debugOverlays');
        overlay.style.display = 'block';
        renderDebugOverlays(elData.elements);
        setTimeout(() => {
          if (!debugFramesEnabled) {
            overlay.style.display = 'none';
            overlay.innerHTML = '';
          }
        }, 3000);
      }

      // Save to JSON log file immediately (append)
      try {
        const saveRes = await api('POST', '/extract-save', {
          recording_path: filePath || '/work/unnamed',
          label: f.extract_label,
          timestamp: new Date().toISOString(),
          elements: fields,
        });
        if (saveRes.ok) {
          toast('Ctrl #' + captureId + ': ' + fields.length + ' elements saved to JSON (' + saveRes.entries + ' entries)', 'success');
        }
      } catch (saveErr) {
        console.log('Extract save error (non-fatal):', saveErr);
      }
    } else {
      toast('Ctrl Capture #' + captureId + ' - no elements found on screen', 'warn');
    }
  } catch (err) {
    toast('Ctrl Capture #' + captureId + ' - element fetch failed: ' + err.message, 'error');
  } finally {
    ctrlCaptureInFlight = false;
  }
}

function showCtrlCaptureFeedback() {
  const indicator = document.createElement('div');
  indicator.style.cssText = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);' +
    'background:rgba(88,166,255,0.9);color:#fff;padding:12px 24px;border-radius:12px;' +
    'font-size:16px;font-weight:700;z-index:9999;pointer-events:none;' +
    'animation:ctrlCapturePulse 0.8s ease-out forwards;';
  indicator.textContent = 'Ctrl Captured!';
  document.body.appendChild(indicator);
  setTimeout(() => indicator.remove(), 1000);
}

function showInspectTooltip(container, px, py, devX, devY, data) {
  hideInspectTooltip();
  if (!data || !data.elements || !data.elements.length) {
    // Show position-only tooltip
    const tip = document.createElement('div');
    tip.className = 'inspect-tooltip';
    tip.innerHTML = '<div class="it-header">Inspect (' + devX + ', ' + devY + ')</div>'
      + '<div class="it-row" style="color:var(--text2)">No elements at this position</div>';
    tip.style.left = Math.min(px + 15, container.clientWidth - 350) + 'px';
    tip.style.top = Math.min(py + 15, container.clientHeight - 100) + 'px';
    container.appendChild(tip);
    inspectTooltipEl = tip;
    return;
  }

  const img = document.getElementById('screenImg');
  const displayW = img.clientWidth;
  const displayH = img.clientHeight;

  // Draw highlight boxes for matching elements
  data.elements.forEach((el) => {
    if (!el.bounds || el.bounds.length < 4) return;
    const [x1, y1, x2, y2] = el.bounds;
    const scaleX = displayW / screenNaturalW;
    const scaleY = displayH / screenNaturalH;
    const hl = document.createElement('div');
    hl.className = 'inspect-highlight';
    hl.style.left = (x1 * scaleX) + 'px';
    hl.style.top = (y1 * scaleY) + 'px';
    hl.style.width = ((x2 - x1) * scaleX) + 'px';
    hl.style.height = ((y2 - y1) * scaleY) + 'px';
    container.appendChild(hl);
    inspectHighlightEls.push(hl);
  });

  // Build tooltip HTML
  let html = '<div class="it-header">Inspect (' + devX + ', ' + devY + ') - ' + data.elements.length + ' element(s)</div>';
  data.elements.forEach((el, i) => {
    html += '<div style="border-top:1px solid var(--border);padding-top:4px;margin-top:4px">';
    html += '<div class="it-row"><span class="it-label">Class: </span><span class="it-value">' + esc((el.cls || '').split('.').pop()) + '</span></div>';
    if (el.text) html += '<div class="it-row"><span class="it-label">Text: </span><span class="it-text">' + esc(el.text) + '</span></div>';
    if (el.resource_id) html += '<div class="it-row"><span class="it-label">ID: </span><span class="it-value">' + esc(el.resource_id) + '</span></div>';
    if (el.content_desc) html += '<div class="it-row"><span class="it-label">Desc: </span><span class="it-value">' + esc(el.content_desc) + '</span></div>';
    html += '<div class="it-row"><span class="it-label">Bounds: </span><span class="it-value">[' + el.bounds.join(',') + ']</span>';
    if (el.clickable) html += ' <span style="color:var(--orange)">clickable</span>';
    if (!el.enabled) html += ' <span style="color:var(--red)">disabled</span>';
    html += '</div>';
    html += '</div>';
  });

  // Show all text found at this position
  if (data.all_text && data.all_text.length) {
    html += '<div style="border-top:1px solid var(--border);padding-top:4px;margin-top:6px">';
    html += '<div class="it-row"><span class="it-label">All text at point:</span></div>';
    data.all_text.forEach(t => {
      html += '<div class="it-row" style="padding-left:8px"><span class="it-text">' + esc(t) + '</span></div>';
    });
    html += '</div>';
  }

  const tip = document.createElement('div');
  tip.className = 'inspect-tooltip';
  tip.innerHTML = html;
  // Position tooltip (keep within container bounds)
  const tipX = px + 20 > container.clientWidth - 350 ? Math.max(0, px - 350) : px + 20;
  const tipY = py + 20;
  tip.style.left = tipX + 'px';
  tip.style.top = tipY + 'px';
  container.appendChild(tip);
  inspectTooltipEl = tip;
}

function hideInspectTooltip() {
  if (inspectTooltipEl) { inspectTooltipEl.remove(); inspectTooltipEl = null; }
  inspectHighlightEls.forEach(el => el.remove());
  inspectHighlightEls = [];
  clearTimeout(inspectDebounceTimer);
}

function showTouchFeedback(x, y) {
  const container = document.getElementById('screenContainer');
  const fb = document.createElement('div');
  fb.className = 'touch-feedback';
  fb.style.left = x + 'px';
  fb.style.top = y + 'px';
  container.appendChild(fb);
  setTimeout(() => fb.remove(), 600);
}

// ============================================================
// DEVICE CONTROL COMMANDS
// ============================================================
async function sendKey(keycode) {
  const status = document.getElementById('deviceStatus');
  status.textContent = 'Sending key ' + keycode + '...';

  // Record key frame during web recording
  if (webRecording && recording) {
    const keyNames = {3:'HOME',4:'BACK',24:'VOLUME_UP',25:'VOLUME_DOWN',26:'POWER',57:'ALT_LEFT',58:'ALT_RIGHT',59:'SHIFT_LEFT',60:'SHIFT_RIGHT',61:'TAB',66:'ENTER',67:'DEL',82:'MENU',111:'ESCAPE',113:'CTRL_LEFT',114:'CTRL_RIGHT',187:'APP_SWITCH',224:'WAKEUP'};
    const f = {
      id: nextId(),
      t: Date.now() - webRecStartTime,
      type: 'key',
      keycode: keycode,
      key_name: keyNames[keycode] || String(keycode),
      enabled: true,
      note: '',
    };
    recording.frames.push(f);
    webRecFrameCount++;
    renderTimeline();
  }

  try {
    const res = await api('POST', '/key', { keycode: keycode });
    if (res.ok) {
      status.textContent = 'Key ' + keycode + ' sent' + (webRecording ? ' [REC]' : '');
      setTimeout(captureScreen, 400);
    } else {
      status.textContent = 'Key failed: ' + (res.error || '');
    }
  } catch (e) {
    status.textContent = 'Key error: ' + e.message;
  }
}

async function sendSwipe(direction) {
  const status = document.getElementById('deviceStatus');
  const distance = parseInt(document.getElementById('swipeDistance').value) || 500;
  const duration = parseInt(document.getElementById('swipeDuration').value) || 300;
  // Use screen center as origin, or default 540x1200 if unknown
  const cx = screenNaturalW ? Math.round(screenNaturalW / 2) : 540;
  const cy = screenNaturalH ? Math.round(screenNaturalH / 2) : 1200;
  let x1 = cx, y1 = cy, x2 = cx, y2 = cy;
  if (direction === 'up')    { y1 = cy + Math.round(distance / 2); y2 = cy - Math.round(distance / 2); }
  if (direction === 'down')  { y1 = cy - Math.round(distance / 2); y2 = cy + Math.round(distance / 2); }
  if (direction === 'left')  { x1 = cx + Math.round(distance / 2); x2 = cx - Math.round(distance / 2); }
  if (direction === 'right') { x1 = cx - Math.round(distance / 2); x2 = cx + Math.round(distance / 2); }
  status.textContent = 'Swipe ' + direction + ': (' + x1 + ',' + y1 + ') -> (' + x2 + ',' + y2 + ')...';

  // Record swipe frame during web recording
  addWebRecFrame('swipe', x1, y1, x2, y2, duration);

  try {
    const res = await api('POST', '/swipe', { x1, y1, x2, y2, duration });
    if (res.ok) {
      status.textContent = 'Swiped ' + direction + (webRecording ? ' [REC]' : '');
      setTimeout(captureScreen, 500);
    } else {
      status.textContent = 'Swipe failed: ' + (res.error || '');
    }
  } catch (e) {
    status.textContent = 'Swipe error: ' + e.message;
  }
}

async function sendText() {
  const input = document.getElementById('textInput');
  const text = input.value.trim();
  if (!text) return;
  const status = document.getElementById('deviceStatus');
  status.textContent = 'Sending text...';
  try {
    const res = await api('POST', '/text', { text: text });
    if (res.ok) {
      status.textContent = 'Text sent: "' + text + '"';
      input.value = '';
      setTimeout(captureScreen, 400);
    } else {
      status.textContent = 'Text failed: ' + (res.error || '');
    }
  } catch (e) {
    status.textContent = 'Text error: ' + e.message;
  }
}

async function rotateScreen() {
  const status = document.getElementById('deviceStatus');
  status.textContent = 'Rotating...';
  try {
    // Toggle auto-rotate off, then rotate
    const res = await api('POST', '/shell', { command: 'settings put system accelerometer_rotation 0' });
    // Get current rotation
    const r2 = await api('POST', '/shell', { command: 'settings get system user_rotation' });
    const current = parseInt(r2.output?.trim()) || 0;
    const next = (current + 1) % 4;
    await api('POST', '/shell', { command: 'settings put system user_rotation ' + next });
    status.textContent = 'Rotated to ' + (next * 90) + ' degrees';
    setTimeout(captureScreen, 800);
  } catch (e) {
    status.textContent = 'Rotate error: ' + e.message;
  }
}

// ============================================================
// INIT
// ============================================================
loadFileList();

// Initialize screen touch interaction
initScreenTouch();

// Re-render debug overlays on container resize
const _screenResizeObs = new ResizeObserver(() => {
  if (debugFramesEnabled && debugElementsCache.length) {
    renderDebugOverlays(debugElementsCache);
  }
});
_screenResizeObs.observe(document.getElementById('screenContainer'));

// Start auto-refresh for device screen
startScreenRefresh();

// Initial screen capture
setTimeout(captureScreen, 500);

// Sync playback state with server on load
(async function initPlaybackState() {
  try {
    const res = await api('GET', '/status');
    if (res.status === 'playing') {
      updatePlaybackUI('playing', res);
      startElapsedTimer();
      startStatusPoll();
    } else if (res.status === 'recording') {
      updatePlaybackUI('recording', res);
      startElapsedTimer();
      startStatusPoll();
    } else {
      updatePlaybackUI('idle', null);
    }
  } catch (e) {
    updatePlaybackUI('idle', null);
  }
})();
</script>
</body>
</html>"""


# ============================================================
# Station Data Viewer - Standalone HTML page
# ============================================================

STATION_DATA_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Station Data Viewer</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0f1117;color:#e1e4e8;line-height:1.5}
.header{background:#161b22;padding:14px 24px;border-bottom:1px solid #30363d;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
.header h1{font-size:18px;font-weight:600;color:#58a6ff}
.header .back-link{color:#8b949e;text-decoration:none;font-size:13px}
.header .back-link:hover{color:#58a6ff}
.controls{padding:12px 24px;background:#161b22;border-bottom:1px solid #21262d;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.controls button{background:#21262d;border:1px solid #30363d;color:#c9d1d9;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:13px}
.controls button:hover{background:#30363d}
.controls button.active{background:#1f6feb;border-color:#1f6feb;color:#fff}
.controls .auto-refresh{display:flex;align-items:center;gap:6px;font-size:12px;color:#8b949e}
.controls .auto-refresh input{accent-color:#1f6feb}
.container{max-width:1600px;margin:0 auto;padding:20px}
.log-grid{display:grid;grid-template-columns:280px 1fr;gap:16px;min-height:calc(100vh - 140px)}
.log-list{background:#161b22;border:1px solid #30363d;border-radius:8px;overflow:hidden}
.log-list h3{padding:12px 16px;font-size:13px;color:#8b949e;border-bottom:1px solid #21262d;text-transform:uppercase;letter-spacing:.5px}
.log-item{padding:10px 16px;border-bottom:1px solid #21262d;cursor:pointer;transition:background .15s}
.log-item:hover{background:#1c2129}
.log-item.active{background:#1f6feb22;border-left:3px solid #1f6feb}
.log-item .name{font-weight:600;font-size:14px;color:#c9d1d9}
.log-item .info{font-size:11px;color:#8b949e;margin-top:2px}
.log-viewer{background:#161b22;border:1px solid #30363d;border-radius:8px;overflow:hidden}
.log-viewer-header{padding:12px 16px;border-bottom:1px solid #21262d;display:flex;justify-content:space-between;align-items:center}
.log-viewer-header h3{font-size:14px;color:#c9d1d9}
.log-viewer-header .entry-count{font-size:12px;color:#8b949e}
.entries{overflow-y:auto;max-height:calc(100vh - 240px);padding:8px}
.entry-card{background:#0d1117;border:1px solid #21262d;border-radius:6px;margin-bottom:8px;overflow:hidden}
.entry-header{padding:8px 12px;display:flex;justify-content:space-between;align-items:center;cursor:pointer;transition:background .15s}
.entry-header:hover{background:#161b22}
.entry-header .entry-time{font-size:12px;color:#58a6ff;font-family:'Consolas',monospace}
.entry-header .entry-label{font-size:12px;color:#8b949e}
.entry-header .entry-count{font-size:11px;background:#21262d;color:#8b949e;padding:1px 8px;border-radius:10px}
.entry-body{display:none;padding:0 12px 12px}
.entry-body.open{display:block}
.element-table{width:100%;border-collapse:collapse;font-size:12px}
.element-table th{text-align:left;padding:4px 8px;background:#161b22;color:#8b949e;font-weight:600;font-size:10px;text-transform:uppercase;letter-spacing:.5px;position:sticky;top:0}
.element-table td{padding:4px 8px;border-bottom:1px solid #21262d;vertical-align:top}
.element-table tr:hover{background:#1c2129}
.text-cell{color:#3fb950;max-width:300px;word-break:break-word}
.id-cell{color:#8b949e;font-family:'Consolas',monospace;font-size:11px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bounds-cell{color:#d2a8ff;font-family:'Consolas',monospace;font-size:11px;white-space:nowrap}
.empty{text-align:center;padding:40px;color:#8b949e}
.summary-bar{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin-bottom:16px}
.summary-card{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:10px 14px}
.summary-card .label{font-size:10px;color:#8b949e;text-transform:uppercase;letter-spacing:.5px}
.summary-card .value{font-size:20px;font-weight:700;color:#c9d1d9;margin-top:2px}
.json-raw{font-family:'Consolas',monospace;font-size:11px;color:#c9d1d9;white-space:pre-wrap;word-break:break-all;background:#0d1117;padding:8px;border-radius:4px;max-height:300px;overflow-y:auto;border:1px solid #21262d}
.view-toggle{display:flex;gap:4px}
.view-toggle button{background:#0d1117;border:1px solid #30363d;color:#8b949e;padding:4px 10px;border-radius:4px;font-size:11px;cursor:pointer}
.view-toggle button.active{background:#1f6feb;border-color:#1f6feb;color:#fff}
</style>
</head>
<body>

<div class="header">
  <h1>Station Data Viewer</h1>
  <a class="back-link" href="/">&larr; Back to Editor</a>
</div>

<div class="controls">
  <button onclick="loadLogs()" title="Refresh log list">Refresh</button>
  <div class="view-toggle">
    <button class="active" id="viewTable" onclick="setViewMode('table')">Table</button>
    <button id="viewJson" onclick="setViewMode('json')">Raw JSON</button>
  </div>
  <div class="auto-refresh">
    <input type="checkbox" id="autoRefresh" checked>
    <label for="autoRefresh">Auto-refresh (10s)</label>
  </div>
  <span style="border-left:1px solid #30363d;height:20px"></span>
  <button onclick="downloadCurrentLog()" title="Download currently selected log file">Download Selected</button>
  <button onclick="window.open('/api/download-extract-logs','_blank')" title="Download all extract logs as ZIP">Download All ZIP</button>
  <button onclick="deleteCurrentLog()" style="background:#3d1f1f;border-color:#f8514966" title="Delete currently selected log file">Delete Selected</button>
  <button onclick="deleteAllExtractLogs()" style="background:#3d1f1f;border-color:#f8514966" title="Delete ALL extract log files">Delete All</button>
</div>

<div class="container">
  <div class="summary-bar" id="summaryBar"></div>
  <div class="log-grid">
    <div class="log-list">
      <h3>Extract Logs</h3>
      <div id="logList"><div class="empty">Loading...</div></div>
    </div>
    <div class="log-viewer">
      <div class="log-viewer-header">
        <h3 id="viewerTitle">Select a log file</h3>
        <span class="entry-count" id="viewerCount"></span>
      </div>
      <div class="entries" id="viewerEntries">
        <div class="empty">Select a log file from the left panel to view its data</div>
      </div>
    </div>
  </div>
</div>

<script>
let logs = [];
let currentLogPath = '';
let currentEntries = [];
let viewMode = 'table';
let autoRefreshTimer = null;

async function apiGet(path) {
  const res = await fetch('/api' + path);
  return res.json();
}

async function loadLogs() {
  try {
    const data = await apiGet('/extract-logs');
    logs = data.logs || [];
    renderLogList();
    renderSummary();
    // Auto-reload current log if selected
    if (currentLogPath) {
      const still = logs.find(l => l.path === currentLogPath);
      if (still) loadLogData(currentLogPath);
    }
  } catch (e) {
    document.getElementById('logList').innerHTML = '<div class="empty">Error loading logs</div>';
  }
}

function renderLogList() {
  const el = document.getElementById('logList');
  if (!logs.length) {
    el.innerHTML = '<div class="empty">No extract logs found.<br><br>Press Ctrl during recording to capture station data,<br>or run a sequence with extract frames.</div>';
    return;
  }
  el.innerHTML = logs.map(l =>
    '<div class="log-item' + (l.path === currentLogPath ? ' active' : '') + '" onclick="loadLogData(\'' + esc(l.path) + '\')">' +
    '<div class="name">' + esc(l.stem) + '</div>' +
    '<div class="info">' + l.entries + ' entries | ' + l.size_kb + ' KB</div>' +
    '</div>'
  ).join('');
}

function renderSummary() {
  const totalLogs = logs.length;
  const totalEntries = logs.reduce((s, l) => s + l.entries, 0);
  const totalSize = logs.reduce((s, l) => s + l.size_kb, 0).toFixed(1);
  const el = document.getElementById('summaryBar');
  el.innerHTML = [
    '<div class="summary-card"><div class="label">Station Logs</div><div class="value">' + totalLogs + '</div></div>',
    '<div class="summary-card"><div class="label">Total Entries</div><div class="value">' + totalEntries + '</div></div>',
    '<div class="summary-card"><div class="label">Total Size</div><div class="value">' + totalSize + ' KB</div></div>',
  ].join('');
}

async function loadLogData(path) {
  currentLogPath = path;
  renderLogList();
  document.getElementById('viewerTitle').textContent = path.split('/').pop();

  try {
    const data = await apiGet('/extract-data?path=' + encodeURIComponent(path));
    if (data.error) {
      document.getElementById('viewerEntries').innerHTML = '<div class="empty">' + esc(data.error) + '</div>';
      return;
    }
    currentEntries = data.entries || [];
    document.getElementById('viewerCount').textContent = currentEntries.length + ' entries';
    renderEntries();
  } catch (e) {
    document.getElementById('viewerEntries').innerHTML = '<div class="empty">Error loading data</div>';
  }
}

function renderEntries() {
  const el = document.getElementById('viewerEntries');
  if (!currentEntries.length) {
    el.innerHTML = '<div class="empty">No entries in this log</div>';
    return;
  }

  if (viewMode === 'json') {
    el.innerHTML = '<div class="json-raw">' + esc(JSON.stringify(currentEntries, null, 2)) + '</div>';
    return;
  }

  el.innerHTML = currentEntries.map((entry, i) => {
    const elements = entry.elements || entry.all_text || [];
    const fields = entry.fields ? Object.entries(entry.fields) : [];
    const ts = entry.timestamp ? formatTime(entry.timestamp) : '';
    const label = entry.label || '';
    const count = elements.length || fields.length;

    let tableHtml = '';
    if (elements.length) {
      tableHtml = '<table class="element-table">' +
        '<tr><th>#</th><th>Text</th><th>Resource ID</th><th>Bounds</th><th>Center</th></tr>' +
        elements.map((el, j) => {
          const text = el.text || '';
          const rid = el.resource_id || '';
          const bounds = el.bounds ? '[' + el.bounds.join(',') + ']' : '';
          const center = el.center ? '(' + el.center.join(',') + ')' : '';
          return '<tr><td>' + (j+1) + '</td>' +
            '<td class="text-cell">' + esc(text) + '</td>' +
            '<td class="id-cell" title="' + esc(rid) + '">' + esc(rid) + '</td>' +
            '<td class="bounds-cell">' + bounds + '</td>' +
            '<td class="bounds-cell">' + center + '</td></tr>';
        }).join('') +
        '</table>';
    } else if (fields.length) {
      tableHtml = '<table class="element-table">' +
        '<tr><th>Field</th><th>Value</th><th>Details</th></tr>' +
        fields.map(([name, val]) => {
          const text = typeof val === 'object' ? (val.text || JSON.stringify(val)) : String(val);
          const detail = typeof val === 'object' ? JSON.stringify(val, null, 1) : '';
          return '<tr><td>' + esc(name) + '</td><td class="text-cell">' + esc(text) + '</td><td class="id-cell">' + esc(detail) + '</td></tr>';
        }).join('') +
        '</table>';
    }

    return '<div class="entry-card">' +
      '<div class="entry-header" onclick="toggleEntry(' + i + ')">' +
      '<span class="entry-time">' + esc(ts) + '</span>' +
      '<span class="entry-label">' + esc(label) + '</span>' +
      '<span class="entry-count">' + count + ' items</span>' +
      '</div>' +
      '<div class="entry-body" id="entry-' + i + '">' + tableHtml + '</div>' +
      '</div>';
  }).join('');
}

function toggleEntry(i) {
  const el = document.getElementById('entry-' + i);
  el.classList.toggle('open');
}

function setViewMode(mode) {
  viewMode = mode;
  document.getElementById('viewTable').classList.toggle('active', mode === 'table');
  document.getElementById('viewJson').classList.toggle('active', mode === 'json');
  renderEntries();
}

function formatTime(iso) {
  try {
    const d = new Date(iso);
    return d.toLocaleString('en-GB', {year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'});
  } catch(e) { return iso; }
}

function esc(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }

// Auto-refresh
function startAutoRefresh() {
  stopAutoRefresh();
  if (document.getElementById('autoRefresh').checked) {
    autoRefreshTimer = setInterval(loadLogs, 10000);
  }
}
function stopAutoRefresh() {
  if (autoRefreshTimer) { clearInterval(autoRefreshTimer); autoRefreshTimer = null; }
}
document.getElementById('autoRefresh').addEventListener('change', startAutoRefresh);

// Download & Delete functions
async function apiPost(path, body) {
  const res = await fetch('/api' + path, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body || {})});
  return res.json();
}

function downloadCurrentLog() {
  if (!currentLogPath) { alert('No log file selected'); return; }
  window.open('/api/download-log?path=' + encodeURIComponent(currentLogPath), '_blank');
}

async function deleteCurrentLog() {
  if (!currentLogPath) { alert('No log file selected'); return; }
  if (!confirm('Delete this log file?\n' + currentLogPath.split('/').pop())) return;
  try {
    await apiPost('/delete-log', {path: currentLogPath});
    currentLogPath = '';
    document.getElementById('viewerTitle').textContent = 'Select a log file';
    document.getElementById('viewerCount').textContent = '';
    document.getElementById('viewerEntries').innerHTML = '<div class="empty">Log file deleted</div>';
    loadLogs();
  } catch(e) { alert('Error: ' + e.message); }
}

async function deleteAllExtractLogs() {
  if (!confirm('Delete ALL extract log files? This cannot be undone.')) return;
  try {
    const res = await apiPost('/delete-all-extract-logs', {});
    alert('Deleted ' + (res.deleted || 0) + ' extract log files');
    currentLogPath = '';
    document.getElementById('viewerTitle').textContent = 'Select a log file';
    document.getElementById('viewerEntries').innerHTML = '<div class="empty">All logs deleted</div>';
    loadLogs();
  } catch(e) { alert('Error: ' + e.message); }
}

// Init
loadLogs();
startAutoRefresh();
</script>
</body>
</html>"""


# ============================================================
# Station Logs Viewer - Per-station round-by-round playback logs
# ============================================================

STATION_LOGS_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Station Logs Viewer</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0f1117;color:#e1e4e8;line-height:1.5}
.header{background:#161b22;padding:14px 24px;border-bottom:1px solid #30363d;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
.header h1{font-size:18px;font-weight:600;color:#58a6ff}
.header .back-link{color:#8b949e;text-decoration:none;font-size:13px}
.header .back-link:hover{color:#58a6ff}
.controls{padding:12px 24px;background:#161b22;border-bottom:1px solid #21262d;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.controls button{background:#21262d;border:1px solid #30363d;color:#c9d1d9;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:13px}
.controls button:hover{background:#30363d}
.controls button.active{background:#1f6feb;border-color:#1f6feb;color:#fff}
.controls .auto-refresh{display:flex;align-items:center;gap:6px;font-size:12px;color:#8b949e}
.controls .auto-refresh input{accent-color:#1f6feb}
.container{max-width:1600px;margin:0 auto;padding:20px}
.summary-bar{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin-bottom:16px}
.summary-card{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:10px 14px}
.summary-card .label{font-size:10px;color:#8b949e;text-transform:uppercase;letter-spacing:.5px}
.summary-card .value{font-size:20px;font-weight:700;color:#c9d1d9;margin-top:2px}
.summary-card .value.success{color:#3fb950}
.summary-card .value.error{color:#f85149}
.log-grid{display:grid;grid-template-columns:280px 1fr;gap:16px;min-height:calc(100vh - 200px)}
.station-list{background:#161b22;border:1px solid #30363d;border-radius:8px;overflow:hidden}
.station-list h3{padding:12px 16px;font-size:13px;color:#8b949e;border-bottom:1px solid #21262d;text-transform:uppercase;letter-spacing:.5px}
.station-item{padding:10px 16px;border-bottom:1px solid #21262d;cursor:pointer;transition:background .15s}
.station-item:hover{background:#1c2129}
.station-item.active{background:#1f6feb22;border-left:3px solid #1f6feb}
.station-item .name{font-weight:600;font-size:14px;color:#c9d1d9}
.station-item .info{font-size:11px;color:#8b949e;margin-top:2px;display:flex;gap:12px}
.station-item .info .ok{color:#3fb950}
.station-item .info .err{color:#f85149}
.log-viewer{background:#161b22;border:1px solid #30363d;border-radius:8px;overflow:hidden}
.log-viewer-header{padding:12px 16px;border-bottom:1px solid #21262d;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
.log-viewer-header h3{font-size:14px;color:#c9d1d9}
.log-viewer-header .meta{font-size:12px;color:#8b949e}
.run-list{overflow-y:auto;max-height:calc(100vh - 280px);padding:8px}
.run-card{background:#0d1117;border:1px solid #21262d;border-radius:6px;margin-bottom:8px;overflow:hidden}
.run-header{padding:10px 14px;display:flex;justify-content:space-between;align-items:center;cursor:pointer;transition:background .15s}
.run-header:hover{background:#161b22}
.run-header .left{display:flex;align-items:center;gap:12px}
.run-header .loop-num{font-weight:700;font-size:15px;color:#58a6ff}
.run-header .time{font-size:12px;color:#8b949e;font-family:'Consolas',monospace}
.run-header .duration{font-size:12px;color:#c9d1d9;font-family:'Consolas',monospace}
.badge{padding:2px 10px;border-radius:10px;font-size:11px;font-weight:600;text-transform:uppercase}
.badge-success{background:#1f3d2a;color:#3fb950}
.badge-error{background:#3d1f1f;color:#f85149}
.badge-running{background:#1f303d;color:#58a6ff}
.run-body{display:none;padding:0}
.run-body.open{display:block}
.ctrl-table{width:100%;border-collapse:collapse;font-size:12px}
.ctrl-table th{text-align:left;padding:6px 10px;background:#161b22;color:#8b949e;font-weight:600;font-size:10px;text-transform:uppercase;letter-spacing:.5px;position:sticky;top:0}
.ctrl-table td{padding:5px 10px;border-bottom:1px solid #21262d;vertical-align:top}
.ctrl-table tr:hover{background:#1c2129}
.ctrl-table tr.error-row{background:#1f121544}
.type-badge{display:inline-block;padding:1px 8px;border-radius:4px;font-size:10px;font-weight:600;min-width:60px;text-align:center}
.type-touch{background:#3d2f1f;color:#d29922}
.type-gesture{background:#1f3d2a;color:#3fb950}
.type-element{background:#1f2a3d;color:#58a6ff}
.type-app{background:#2d1f3d;color:#bc8cff}
.type-key{background:#1f3d3d;color:#39d2c0}
.type-screenshot{background:#3d3d1f;color:#d2d239}
.type-wait{background:#21262d;color:#8b949e}
.type-shell{background:#2a1f1f;color:#f0883e}
.type-extract{background:#1f3d33;color:#3fb9a0}
.type-marker{background:#21262d;color:#8b949e}
.result-ok{color:#3fb950}
.result-err{color:#f85149}
.ts-col{color:#8b949e;font-family:'Consolas',monospace;font-size:11px;white-space:nowrap}
.desc-col{max-width:350px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.detail-toggle{cursor:pointer;color:#58a6ff;font-size:10px;margin-left:4px}
.detail-toggle:hover{text-decoration:underline}
.detail-row{display:none;background:#0d1117}
.detail-row.open{display:table-row}
.detail-row td{padding:8px 14px}
.detail-json{font-family:'Consolas',monospace;font-size:11px;color:#c9d1d9;white-space:pre-wrap;word-break:break-all;background:#0d1117;padding:8px;border-radius:4px;max-height:250px;overflow-y:auto}
.run-stats{display:flex;gap:16px;font-size:11px;color:#8b949e;padding:6px 14px;border-top:1px solid #21262d;background:#0d1117}
.run-stats .stat{display:flex;align-items:center;gap:4px}
.run-stats strong{color:#c9d1d9}
.empty{text-align:center;padding:40px;color:#8b949e}
.arrow{display:inline-block;transition:transform .2s;margin-right:6px;font-size:10px}
.arrow.open{transform:rotate(90deg)}
.search-bar{margin-bottom:12px}
.search-bar input{width:100%;padding:8px 12px;background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#c9d1d9;font-size:13px;outline:none}
.search-bar input:focus{border-color:#58a6ff}
</style>
</head>
<body>

<div class="header">
  <h1>Station Logs</h1>
  <a class="back-link" href="/">&larr; Back to Editor</a>
</div>

<div class="controls">
  <button onclick="loadStationLogs()" title="Refresh station logs">Refresh</button>
  <div class="auto-refresh">
    <input type="checkbox" id="autoRefresh" checked>
    <label for="autoRefresh">Auto-refresh (10s)</label>
  </div>
  <span style="border-left:1px solid #30363d;height:20px"></span>
  <button onclick="downloadCurrentStationLog()" title="Download currently selected station log">Download Selected</button>
  <button onclick="window.open('/api/download-station-logs','_blank')" title="Download all station logs as ZIP">Download All ZIP</button>
  <button onclick="deleteCurrentStationLog()" style="background:#3d1f1f;border-color:#f8514966" title="Delete currently selected station log">Delete Selected</button>
  <button onclick="deleteAllStationLogs()" style="background:#3d1f1f;border-color:#f8514966" title="Delete ALL station log files">Delete All</button>
</div>

<div class="container">
  <div class="summary-bar" id="summaryBar"></div>
  <div class="log-grid">
    <div class="station-list">
      <h3>Stations (1-11)</h3>
      <div id="stationList"><div class="empty">Loading...</div></div>
    </div>
    <div class="log-viewer">
      <div class="log-viewer-header">
        <h3 id="viewerTitle">Select a station</h3>
        <span class="meta" id="viewerMeta"></span>
      </div>
      <div class="search-bar" style="padding:0 16px">
        <input type="text" id="searchInput" placeholder="Filter by type, action, description..." oninput="filterCtrls()">
      </div>
      <div class="run-list" id="runList">
        <div class="empty">Select a station from the left panel to view its round-by-round logs</div>
      </div>
    </div>
  </div>
</div>

<script>
let stationLogs = [];
let currentLogPath = '';
let currentLogData = null;
let autoRefreshTimer = null;

async function apiGet(path) {
  const res = await fetch('/api' + path);
  return res.json();
}

async function loadStationLogs() {
  try {
    const data = await apiGet('/station-logs');
    stationLogs = data.logs || [];
    renderStationList();
    renderSummary();
    if (currentLogPath) {
      const still = stationLogs.find(l => l.path === currentLogPath);
      if (still) loadStationLog(currentLogPath);
    }
  } catch (e) {
    document.getElementById('stationList').innerHTML = '<div class="empty">Error loading station logs</div>';
  }
}

function renderSummary() {
  const totalStations = stationLogs.length;
  const totalRuns = stationLogs.reduce((s, l) => s + l.total_runs, 0);
  const totalSuccess = stationLogs.reduce((s, l) => s + l.success_runs, 0);
  const totalErrors = stationLogs.reduce((s, l) => s + l.error_runs, 0);
  const el = document.getElementById('summaryBar');
  el.innerHTML = [
    '<div class="summary-card"><div class="label">Stations with Logs</div><div class="value">' + totalStations + ' / 11</div></div>',
    '<div class="summary-card"><div class="label">Total Rounds</div><div class="value">' + totalRuns + '</div></div>',
    '<div class="summary-card"><div class="label">Successful</div><div class="value success">' + totalSuccess + '</div></div>',
    '<div class="summary-card"><div class="label">Errors</div><div class="value error">' + totalErrors + '</div></div>',
  ].join('');
}

function renderStationList() {
  const el = document.getElementById('stationList');
  if (!stationLogs.length) {
    el.innerHTML = '<div class="empty">No station playback logs found.<br><br>Run a station recording to generate logs.</div>';
    return;
  }
  el.innerHTML = stationLogs.map(l =>
    '<div class="station-item' + (l.path === currentLogPath ? ' active' : '') + '" onclick="loadStationLog(\'' + esc(l.path) + '\')">' +
    '<div class="name">' + esc(l.station) + '</div>' +
    '<div class="info">' +
      '<span>Rounds: ' + l.total_runs + '</span>' +
      '<span class="ok">OK: ' + l.success_runs + '</span>' +
      '<span class="err">Err: ' + l.error_runs + '</span>' +
      '<span>' + l.size_kb + ' KB</span>' +
    '</div>' +
    (l.last_run_at ? '<div style="font-size:10px;color:#6e7681;margin-top:2px">Last: ' + formatDateTime(l.last_run_at) + '</div>' : '') +
    '</div>'
  ).join('');
}

async function loadStationLog(path) {
  currentLogPath = path;
  renderStationList();
  document.getElementById('viewerTitle').textContent = 'Loading...';

  try {
    const data = await apiGet('/station-log?path=' + encodeURIComponent(path));
    if (data.error) {
      document.getElementById('runList').innerHTML = '<div class="empty">' + esc(data.error) + '</div>';
      return;
    }
    currentLogData = data;
    const rec = data.recording || {};
    document.getElementById('viewerTitle').textContent = rec.name || path.split('/').pop();
    document.getElementById('viewerMeta').innerHTML =
      'Device: ' + esc(rec.device || '-') + ' | Screen: ' + esc(rec.screen || '-') +
      ' | Mode: ' + esc(rec.replay_mode || '-') + ' | Speed: ' + (rec.speed || 1) + 'x' +
      ' | Frames: ' + (rec.total_frames || 0) + ' | Rounds: ' + (data.runs || []).length;
    renderRuns(data.runs || []);
  } catch (e) {
    document.getElementById('runList').innerHTML = '<div class="empty">Error loading log data</div>';
  }
}

function renderRuns(runs) {
  const el = document.getElementById('runList');
  if (!runs.length) {
    el.innerHTML = '<div class="empty">No rounds recorded yet for this station</div>';
    return;
  }
  // Show most recent runs first
  el.innerHTML = runs.slice().reverse().map((run, displayIdx) => {
    const ri = runs.length - 1 - displayIdx; // original index
    return renderRun(run, ri);
  }).join('');
}

function renderRun(run, ri) {
  const ctrls = run.ctrls || [];
  const errCount = ctrls.filter(c => c.result === 'error').length;
  const okCount = ctrls.filter(c => c.result !== 'error').length;
  const badgeCls = run.status === 'success' ? 'badge-success' : run.status === 'error' ? 'badge-error' : 'badge-running';
  const startTime = run.started_at ? formatTime(run.started_at) : '-';
  const durationMs = run.started_at && run.completed_at ? (new Date(run.completed_at) - new Date(run.started_at)) : 0;
  const durStr = durationMs > 0 ? formatDuration(durationMs) : '-';
  const totalExecMs = ctrls.reduce((s, c) => s + (c.exec_ms || 0), 0);

  let html = '<div class="run-card" data-run="' + ri + '">';
  html += '<div class="run-header" onclick="toggleRun(' + ri + ')">';
  html += '<div class="left">';
  html += '<span class="arrow" id="arrow-' + ri + '">&#9654;</span>';
  html += '<span class="loop-num">Round #' + run.loop + '</span>';
  html += '<span class="time">' + startTime + '</span>';
  html += '<span class="duration">' + durStr + '</span>';
  html += '</div>';
  html += '<span class="badge ' + badgeCls + '">' + esc(run.status) + '</span>';
  html += '</div>';

  html += '<div class="run-body" id="run-' + ri + '">';

  // Stats bar
  html += '<div class="run-stats">';
  html += '<div class="stat">Steps: <strong>' + ctrls.length + '</strong></div>';
  html += '<div class="stat">OK: <strong style="color:#3fb950">' + okCount + '</strong></div>';
  html += '<div class="stat">Errors: <strong style="color:' + (errCount > 0 ? '#f85149' : '#3fb950') + '">' + errCount + '</strong></div>';
  html += '<div class="stat">Total exec: <strong>' + formatDuration(totalExecMs) + '</strong></div>';
  html += '<div class="stat">Wall time: <strong>' + durStr + '</strong></div>';
  html += '</div>';

  // Ctrls table
  if (ctrls.length) {
    html += '<table class="ctrl-table">';
    html += '<tr><th>#</th><th>Type</th><th>Action</th><th>Description</th><th>Time</th><th>Exec</th><th>Result</th></tr>';
    ctrls.forEach((ctrl, ci) => {
      const rowCls = ctrl.result === 'error' ? ' class="error-row"' : '';
      const typeCls = 'type-' + (ctrl.type || 'touch');
      const resCls = ctrl.result === 'error' ? 'result-err' : 'result-ok';
      const resIcon = ctrl.result === 'error' ? '&#10007;' : '&#10003;';
      const ctrlTime = ctrl.timestamp ? formatTime(ctrl.timestamp) : '-';

      html += '<tr' + rowCls + ' data-desc="' + esc(ctrl.description || '').toLowerCase() + '" data-type="' + esc(ctrl.type || '') + '">';
      html += '<td>' + ctrl.frame_id + '</td>';
      html += '<td><span class="type-badge ' + typeCls + '">' + esc(ctrl.type || '-') + '</span></td>';
      html += '<td>' + esc(ctrl.action || '-') + '</td>';
      html += '<td class="desc-col">' + esc(ctrl.description || '-') + ' <span class="detail-toggle" onclick="toggleDetail(' + ri + ',' + ci + ',event)">[detail]</span></td>';
      html += '<td class="ts-col">' + ctrlTime + '</td>';
      html += '<td class="ts-col">' + (ctrl.exec_ms != null ? ctrl.exec_ms + 'ms' : '-') + '</td>';
      html += '<td class="' + resCls + '">' + resIcon + (ctrl.error ? ' ' + esc(ctrl.error) : '') + '</td>';
      html += '</tr>';

      html += '<tr class="detail-row" id="detail-' + ri + '-' + ci + '"><td colspan="7"><div class="detail-json">' + esc(JSON.stringify(ctrl.details || {}, null, 2)) + '</div></td></tr>';
    });
    html += '</table>';
  }

  html += '</div></div>';
  return html;
}

function toggleRun(ri) {
  const body = document.getElementById('run-' + ri);
  const arrow = document.getElementById('arrow-' + ri);
  if (body) body.classList.toggle('open');
  if (arrow) arrow.classList.toggle('open');
}

function toggleDetail(ri, ci, event) {
  event.stopPropagation();
  const el = document.getElementById('detail-' + ri + '-' + ci);
  if (el) el.classList.toggle('open');
}

function filterCtrls() {
  const q = document.getElementById('searchInput').value.toLowerCase().trim();
  document.querySelectorAll('.ctrl-table tr[data-desc]').forEach(tr => {
    if (!q) { tr.style.display = ''; return; }
    const desc = tr.getAttribute('data-desc') || '';
    const type = tr.getAttribute('data-type') || '';
    tr.style.display = (desc.includes(q) || type.includes(q)) ? '' : 'none';
  });
}

function formatTime(iso) {
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString('en-GB', {hour:'2-digit',minute:'2-digit',second:'2-digit',fractionalSecondDigits:3});
  } catch(e) { return iso; }
}

function formatDateTime(iso) {
  try {
    const d = new Date(iso);
    return d.toLocaleString('en-GB', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'});
  } catch(e) { return iso; }
}

function formatDuration(ms) {
  if (ms < 1000) return ms + 'ms';
  const s = Math.floor(ms / 1000);
  if (s < 60) return s + '.' + String(ms % 1000).padStart(3, '0').slice(0,1) + 's';
  const m = Math.floor(s / 60);
  return m + 'm ' + (s % 60) + 's';
}

function esc(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }

// Auto-refresh
function startAutoRefresh() {
  stopAutoRefresh();
  if (document.getElementById('autoRefresh').checked) {
    autoRefreshTimer = setInterval(loadStationLogs, 10000);
  }
}
function stopAutoRefresh() {
  if (autoRefreshTimer) { clearInterval(autoRefreshTimer); autoRefreshTimer = null; }
}
document.getElementById('autoRefresh').addEventListener('change', startAutoRefresh);

// Download & Delete functions
async function apiPost(path, body) {
  const res = await fetch('/api' + path, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body || {})});
  return res.json();
}

function downloadCurrentStationLog() {
  if (!currentLogPath) { alert('No station log selected'); return; }
  window.open('/api/download-log?path=' + encodeURIComponent(currentLogPath), '_blank');
}

async function deleteCurrentStationLog() {
  if (!currentLogPath) { alert('No station log selected'); return; }
  if (!confirm('Delete this station log?\n' + currentLogPath.split('/').pop())) return;
  try {
    await apiPost('/delete-log', {path: currentLogPath});
    currentLogPath = '';
    currentLogData = null;
    document.getElementById('viewerTitle').textContent = 'Select a station';
    document.getElementById('viewerMeta').textContent = '';
    document.getElementById('runList').innerHTML = '<div class="empty">Log file deleted</div>';
    loadStationLogs();
  } catch(e) { alert('Error: ' + e.message); }
}

async function deleteAllStationLogs() {
  if (!confirm('Delete ALL station playback log files? This cannot be undone.')) return;
  try {
    const res = await apiPost('/delete-all-station-logs', {});
    alert('Deleted ' + (res.deleted || 0) + ' station log files');
    currentLogPath = '';
    currentLogData = null;
    document.getElementById('viewerTitle').textContent = 'Select a station';
    document.getElementById('runList').innerHTML = '<div class="empty">All station logs deleted</div>';
    loadStationLogs();
  } catch(e) { alert('Error: ' + e.message); }
}

// Init
loadStationLogs();
startAutoRefresh();
</script>
</body>
</html>"""


# ============================================================
# Charger Status Viewer - Organized per-connector status across rounds
# ============================================================

CHARGER_VIEW_HTML = r"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Charger Status View</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0f1117;color:#e1e4e8;line-height:1.5}
.header{background:#161b22;padding:14px 24px;border-bottom:1px solid #30363d;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
.header h1{font-size:18px;font-weight:600;color:#58a6ff}
.header .links{display:flex;gap:12px;align-items:center}
.header a{color:#8b949e;text-decoration:none;font-size:13px}
.header a:hover{color:#58a6ff}
.controls{padding:10px 24px;background:#161b22;border-bottom:1px solid #21262d;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.controls button{background:#21262d;border:1px solid #30363d;color:#c9d1d9;padding:5px 14px;border-radius:6px;cursor:pointer;font-size:13px}
.controls button:hover{background:#30363d}
.auto-refresh{display:flex;align-items:center;gap:6px;font-size:12px;color:#8b949e}
.auto-refresh input{accent-color:#1f6feb}
.container{max-width:1400px;margin:0 auto;padding:20px}
.station-card{background:#161b22;border:1px solid #30363d;border-radius:10px;margin-bottom:24px;overflow:hidden}
.station-header{padding:14px 20px;background:#1c2129;border-bottom:1px solid #21262d}
.station-name{font-size:17px;font-weight:700;color:#f0f6fc;margin-bottom:4px}
.station-meta{font-size:12px;color:#8b949e;display:flex;flex-wrap:wrap;gap:12px}
.station-meta span{display:flex;align-items:center;gap:4px}
.charger-block{border-bottom:1px solid #21262d}
.charger-block:last-child{border-bottom:none}
.charger-title{padding:8px 20px;background:#0d1117;font-size:12px;color:#8b949e;font-family:monospace;letter-spacing:.5px;display:flex;align-items:center;gap:8px}
.charger-title .badge{background:#21262d;color:#c9d1d9;padding:1px 8px;border-radius:10px;font-size:11px}
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;min-width:500px}
thead th{background:#161b22;padding:8px 14px;text-align:left;font-size:11px;color:#8b949e;font-weight:600;text-transform:uppercase;letter-spacing:.5px;white-space:nowrap;border-bottom:1px solid #21262d}
thead th.round-col{text-align:center;min-width:120px}
tbody tr:hover{background:#1c2129}
td{padding:9px 14px;border-bottom:1px solid #0f1117;font-size:13px;vertical-align:middle}
td.connector-cell{font-weight:600;color:#c9d1d9;white-space:nowrap}
td.type-cell{color:#8b949e;font-size:12px;font-family:monospace}
td.spec-cell{color:#d2a8ff;font-size:12px;white-space:nowrap}
td.status-cell{text-align:center}
.status-badge{display:inline-block;padding:3px 10px;border-radius:12px;font-size:12px;font-weight:600;white-space:nowrap}
.status-available{background:#1a3a1a;color:#3fb950;border:1px solid #2ea043}
.status-inuse{background:#3a1a1a;color:#f85149;border:1px solid #da3633}
.status-booked{background:#3a2a1a;color:#d29922;border:1px solid #9e6a03}
.status-closed{background:#2a2a2a;color:#8b949e;border:1px solid #30363d}
.status-unknown{background:#21262d;color:#8b949e;border:1px solid #30363d}
.round-header{font-size:12px;font-weight:700;color:#c9d1d9}
.round-time{font-size:11px;color:#8b949e;font-family:monospace}
.empty-state{text-align:center;padding:60px;color:#8b949e}
.empty-state .icon{font-size:40px;margin-bottom:12px}
.summary-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:20px}
.summary-card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px;text-align:center}
.summary-card .num{font-size:24px;font-weight:700;color:#58a6ff}
.summary-card .label{font-size:11px;color:#8b949e;margin-top:2px}
.loading{text-align:center;padding:60px;color:#8b949e}
.export-btn{background:#1a7f37;border:1px solid #2ea043;color:#fff;padding:5px 14px;border-radius:6px;cursor:pointer;font-size:13px;font-weight:600}
.export-btn:hover{background:#2ea043}
.export-btn:disabled{background:#21262d;border-color:#30363d;color:#484f58;cursor:not-allowed}
.export-info{font-size:11px;color:#8b949e;margin-left:4px}
.legend{display:flex;gap:10px;align-items:center;font-size:12px;color:#8b949e}
.legend-title{font-weight:600;color:#c9d1d9;margin-right:2px}
.legend .status-badge{font-size:11px;padding:2px 8px}
</style>
</head>
<body>
<div class="header">
  <h1>⚡ Charger Status View</h1>
  <div class="links">
    <a href="/">← Editor</a>
    <a href="/station-data">Station Data</a>
    <a href="/station-logs">Station Logs</a>
  </div>
</div>
<div class="controls">
  <button onclick="loadData()">↺ Refresh</button>
  <label class="auto-refresh">
    <input type="checkbox" id="autoRefresh" onchange="toggleAutoRefresh()">
    Auto-refresh (30s)
  </label>
  <span id="lastUpdate" style="font-size:12px;color:#8b949e"></span>
  <div class="legend">
    <span class="legend-title">สถานะ:</span>
    <span class="status-badge status-available">พร้อมใช้งาน</span>
    <span class="status-badge status-inuse">มีผู้ใช้งาน</span>
    <span class="status-badge status-booked">มีการจอง</span>
    <span class="status-badge status-closed">นอกเวลาทำการ</span>
  </div>
  <span style="flex:1"></span>
  <button class="export-btn" id="exportBtn" disabled onclick="exportHTML()">📥 Export HTML</button>
  <span class="export-info" id="exportInfo"></span>
</div>
<div class="container">
  <div id="summaryArea"></div>
  <div id="mainArea"><div class="loading">Loading charger data…</div></div>
</div>

<script>
let refreshTimer = null;
let lastData = null;  // store for export

function statusClass(s) {
  if (!s) return 'status-unknown';
  if (s === 'พร้อมใช้งาน') return 'status-available';
  if (s === 'มีผู้ใช้งาน') return 'status-inuse';
  if (s === 'มีการจอง') return 'status-booked';
  if (s === 'นอกเวลาทำการ') return 'status-closed';
  return 'status-unknown';
}

function toThai(ts) {
  if (!ts) return null;
  const d = new Date(ts);
  if (isNaN(d)) return null;
  return new Date(d.toLocaleString('en-US', { timeZone: 'Asia/Bangkok' }));
}

function fmtTime(ts) {
  if (!ts) return '';
  const d = toThai(ts);
  if (!d) return '';
  return ('0'+d.getHours()).slice(-2) + ':' + ('0'+d.getMinutes()).slice(-2);
}

function fmtDate(ts) {
  if (!ts) return '';
  const d = toThai(ts);
  if (!d) return '';
  return d.getFullYear() + '-' + ('0'+(d.getMonth()+1)).slice(-2) + '-' + ('0'+d.getDate()).slice(-2);
}

async function loadData() {
  try {
    const res = await fetch('/api/charger-data');
    const data = await res.json();
    lastData = data;
    render(data);
    updateExportButton(data);
    document.getElementById('lastUpdate').textContent = 'Updated: ' + new Date().toLocaleTimeString('en-GB', { timeZone: 'Asia/Bangkok', hour: '2-digit', minute: '2-digit', second: '2-digit' });
  } catch(e) {
    document.getElementById('mainArea').innerHTML = '<div class="empty-state"><div class="icon">⚠️</div><p>Error loading data: ' + e.message + '</p></div>';
  }
}

function render(data) {
  const stations = data.stations || [];
  const summaryEl = document.getElementById('summaryArea');
  const mainEl = document.getElementById('mainArea');

  if (!stations.length) {
    summaryEl.innerHTML = '';
    mainEl.innerHTML = '<div class="empty-state"><div class="icon">📭</div><p>No charger data found in extract logs.</p><p style="font-size:12px;margin-top:8px;color:#6e7681">Run a recording that captures charger detail pages (ctrl_capture_4 through ctrl_capture_7).</p></div>';
    return;
  }

  // Summary – count unique loops and max connectors across all rounds
  let totalConnectors = 0, totalRounds = 0;
  stations.forEach(s => {
    totalRounds += s.rounds.length;
    s.rounds.forEach(r => {
      totalConnectors = Math.max(totalConnectors, r.connectors.length);
    });
  });

  summaryEl.innerHTML = `
    <div class="summary-grid">
      <div class="summary-card"><div class="num">${stations.length}</div><div class="label">Station(s)</div></div>
      <div class="summary-card"><div class="num">${totalRounds}</div><div class="label">Capture Loop(s)</div></div>
      <div class="summary-card"><div class="num">${totalConnectors}</div><div class="label">Max Connectors</div></div>
    </div>`;

  // Build station cards
  let html = '';
  for (const station of stations) {
    const rounds = station.rounds;
    if (!rounds.length) continue;

    // Collect all unique charger+connector keys in order
    const connectorKeys = [];
    const connectorMap = {}; // key -> {charger_id, connector, type, max_power, rate}
    for (const round of rounds) {
      for (const c of round.connectors) {
        const k = `${c.charger_id}::${c.connector}`;
        if (!connectorMap[k]) {
          connectorMap[k] = c;
          connectorKeys.push(k);
        }
      }
    }

    // Group connectors by charger_id
    const chargerGroups = {};
    for (const k of connectorKeys) {
      const cid = connectorMap[k].charger_id;
      if (!chargerGroups[cid]) chargerGroups[cid] = [];
      chargerGroups[cid].push(k);
    }

    // Rounds are already grouped by loop on the server side.
    // Deduplicate by loop number (safety net for edge cases).
    const seenLoop = new Set();
    const uniqueRounds = rounds.filter(r => {
      const key = r.loop || r.timestamp;
      if (seenLoop.has(key)) return false;
      seenLoop.add(key);
      return true;
    });

    // Round columns – use the actual loop number from data
    const roundCols = uniqueRounds.map((r, i) => `
      <th class="round-col">
        <div class="round-header">รอบ ${r.loop || (i+1)}</div>
        <div class="round-time">${fmtTime(r.timestamp)}</div>
        <div class="round-time" style="color:#6e7681">${fmtDate(r.timestamp)}</div>
        <div class="round-time" style="color:#484f58">${r.connectors.length} หัวจ่าย</div>
      </th>`).join('');

    // Build round status lookups
    const roundLookup = uniqueRounds.map(r => {
      const m = {};
      for (const c of r.connectors) {
        m[`${c.charger_id}::${c.connector}`] = c.status;
      }
      return m;
    });

    // Build charger blocks
    let chargerBlocksHtml = '';
    for (const [cid, keys] of Object.entries(chargerGroups)) {
      const rows = keys.map(k => {
        const conn = connectorMap[k];
        const statusCells = roundLookup.map(lookup => {
          const s = lookup[k] || '';
          return `<td class="status-cell"><span class="status-badge ${statusClass(s)}">${s || '–'}</span></td>`;
        }).join('');
        return `<tr>
          <td class="connector-cell">${conn.connector || '?'}</td>
          <td class="type-cell">${conn.type || ''}</td>
          <td class="spec-cell">${conn.max_power ? conn.max_power + (conn.rate ? ' | ' + conn.rate : '') : ''}</td>
          ${statusCells}
        </tr>`;
      }).join('');

      chargerBlocksHtml += `
        <div class="charger-block">
          <div class="charger-title">เครื่องชาร์จ: <span class="badge">${cid}</span> <span style="color:#3fb950;font-size:11px">CC (สูงสุด 200A)</span></div>
          <div class="table-wrap">
            <table>
              <thead><tr>
                <th>หัวจ่าย</th>
                <th>ประเภท</th>
                <th>สเปค</th>
                ${roundCols}
              </tr></thead>
              <tbody>${rows}</tbody>
            </table>
          </div>
        </div>`;
    }

    html += `
      <div class="station-card">
        <div class="station-header">
          <div class="station-name">📍 ${station.name}</div>
          <div class="station-meta">
            ${station.address ? '<span>🏠 ' + station.address + '</span>' : ''}
            ${station.hours ? '<span>🕐 ' + station.hours + '</span>' : ''}
          </div>
        </div>
        ${chargerBlocksHtml}
      </div>`;
  }

  mainEl.innerHTML = html;
}

function toggleAutoRefresh() {
  if (document.getElementById('autoRefresh').checked) {
    refreshTimer = setInterval(loadData, 30000);
  } else {
    clearInterval(refreshTimer);
    refreshTimer = null;
  }
}

function getCompleteRoundCount(data) {
  // A "complete round" = a loop number that ALL stations have data for.
  // Find the max loop across all stations, then find the highest loop N
  // such that every station has data for loops 1..N.
  const stations = data.stations || [];
  if (!stations.length) return { complete: 0, total: 0 };

  // Collect loop numbers per station
  const stationLoops = stations.map(s => {
    const loops = new Set();
    (s.rounds || []).forEach(r => loops.add(r.loop));
    return loops;
  });

  // Find max loop across all stations
  let maxLoop = 0;
  stationLoops.forEach(loops => loops.forEach(l => { if (l > maxLoop) maxLoop = l; }));

  // Find highest complete loop: every station must have this loop
  let completeCount = 0;
  for (let l = 1; l <= maxLoop; l++) {
    const allHave = stationLoops.every(loops => loops.has(l));
    if (allHave) completeCount = l;
    else break;  // once a gap is found, stop
  }

  return { complete: completeCount, total: maxLoop };
}

function updateExportButton(data) {
  const btn = document.getElementById('exportBtn');
  const info = document.getElementById('exportInfo');
  const { complete, total } = getCompleteRoundCount(data);

  if (complete === 0) {
    btn.disabled = true;
    info.textContent = total > 0
      ? 'ยังไม่ครบรอบ (มี ' + total + ' รอบแต่ยังไม่สมบูรณ์)'
      : 'ไม่มีข้อมูลรอบ';
    info.style.color = '#d29922';
  } else {
    btn.disabled = false;
    if (complete < total) {
      info.textContent = 'จะ export ' + complete + ' รอบ (จากทั้งหมด ' + total + ' รอบ, เศษไม่รวม)';
      info.style.color = '#d29922';
    } else {
      info.textContent = complete + ' รอบ พร้อม export';
      info.style.color = '#3fb950';
    }
  }
}

function statusColor(s) {
  if (!s) return {bg:'#f0f0f0',fg:'#999999',border:'#cccccc'};
  if (s === 'พร้อมใช้งาน') return {bg:'#d4edda',fg:'#155724',border:'#28a745'};
  if (s === 'มีผู้ใช้งาน') return {bg:'#f8d7da',fg:'#721c24',border:'#dc3545'};
  if (s === 'มีการจอง') return {bg:'#fff3cd',fg:'#856404',border:'#ffc107'};
  if (s === 'นอกเวลาทำการ') return {bg:'#e2e3e5',fg:'#383d41',border:'#6c757d'};
  return {bg:'#f0f0f0',fg:'#999999',border:'#cccccc'};
}

function exportHTML() {
  if (!lastData) return;
  const { complete } = getCompleteRoundCount(lastData);
  if (complete === 0) return;

  const stations = lastData.stations || [];

  // Build loop -> timestamp from the first available station round.
  // This keeps exported columns aligned with source capture timing.
  const loopTimestamps = {};
  for (const station of stations) {
    for (const round of (station.rounds || [])) {
      if (!round || !round.loop || round.loop < 1 || round.loop > complete) continue;
      if (!loopTimestamps[round.loop] && round.timestamp) {
        loopTimestamps[round.loop] = round.timestamp;
      }
    }
  }

  // Build HTML table with inline styles for color support in Excel/WPS
  let html = '<!DOCTYPE html>\n<html lang="th">\n<head><meta charset="UTF-8"><title>Charger Status</title></head>\n<body>\n';
  html += '<style>table{border-collapse:collapse;font-family:sans-serif;font-size:13px}th,td{border:1px solid #ccc;padding:6px 10px;white-space:nowrap}th{background:#f2f2f2;font-weight:bold;text-align:center}.legend{margin:10px 0;font-size:13px}.legend span{display:inline-block;padding:3px 10px;border-radius:4px;margin-right:8px;font-weight:600}</style>\n';

  // Legend
  html += '<div class="legend"><b>สถานะ: </b>';
  html += '<span style="background:#d4edda;color:#155724;border:1px solid #28a745">พร้อมใช้งาน</span>';
  html += '<span style="background:#f8d7da;color:#721c24;border:1px solid #dc3545">มีผู้ใช้งาน</span>';
  html += '<span style="background:#fff3cd;color:#856404;border:1px solid #ffc107">มีการจอง</span>';
  html += '<span style="background:#e2e3e5;color:#383d41;border:1px solid #6c757d">นอกเวลาทำการ</span>';
  html += '</div>\n';

  html += '<table>\n<thead><tr>';
  const headers = ['สถานี', 'เครื่องชาร์จ', 'หัวจ่าย', 'ประเภท', 'กำลังไฟ', 'อัตราค่าบริการ'];
  for (const h of headers) html += '<th rowspan="2">' + h + '</th>';
  for (let i = 1; i <= complete; i++) html += '<th>รอบ ' + i + '</th>';
  html += '</tr><tr>';
  for (let i = 1; i <= complete; i++) {
    const ts = loopTimestamps[i];
    const timeLabel = ts ? (fmtDate(ts) + ' ' + fmtTime(ts)).trim() : '-';
    html += '<th style="font-size:11px;color:#666;font-weight:500">' + timeLabel + '</th>';
  }
  html += '</tr></thead>\n<tbody>\n';

  for (const station of stations) {
    const rounds = (station.rounds || []).filter(r => r.loop >= 1 && r.loop <= complete);

    const connectorKeys = [];
    const connectorMap = {};
    for (const round of station.rounds) {
      for (const c of round.connectors) {
        const k = c.charger_id + '::' + c.connector;
        if (!connectorMap[k]) {
          connectorMap[k] = c;
          connectorKeys.push(k);
        }
      }
    }

    const roundByLoop = {};
    for (const r of rounds) {
      if (!roundByLoop[r.loop]) roundByLoop[r.loop] = {};
      for (const c of r.connectors) {
        roundByLoop[r.loop][c.charger_id + '::' + c.connector] = c.status;
      }
    }

    for (const k of connectorKeys) {
      const conn = connectorMap[k];
      html += '<tr>';
      html += '<td>' + (station.name || '') + '</td>';
      html += '<td style="text-align:center">' + (conn.charger_id || '') + '</td>';
      html += '<td>' + (conn.connector || '') + '</td>';
      html += '<td>' + (conn.type || '') + '</td>';
      html += '<td>' + (conn.max_power || '') + '</td>';
      html += '<td>' + (conn.rate || '') + '</td>';
      for (let i = 1; i <= complete; i++) {
        const s = (roundByLoop[i] && roundByLoop[i][k]) || '';
        const c = statusColor(s);
        html += '<td style="text-align:center;background:' + c.bg + ';color:' + c.fg + ';font-weight:600">' + (s || '–') + '</td>';
      }
      html += '</tr>\n';
    }
  }

  html += '</tbody>\n</table>\n</body>\n</html>';

  // Download as .html (Excel/WPS can open HTML tables directly with colors)
  const blob = new Blob([html], { type: 'text/html;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  const now = toThai(new Date().toISOString());
  const ts = now.getFullYear() + ('0'+(now.getMonth()+1)).slice(-2) + ('0'+now.getDate()).slice(-2) + '_' + ('0'+now.getHours()).slice(-2) + ('0'+now.getMinutes()).slice(-2);
  a.download = 'charger_status_' + ts + '_' + complete + 'rounds.html';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

loadData();
</script>
</body>
</html>"""


# ============================================================
# HTTP Server
# ============================================================

class EditorHandler(BaseHTTPRequestHandler):
    work_dir: str = "/work"
    # Short TTL cache for station logs to ensure consistent data across
    # concurrent requests (sidebar + detail page) during active run loops.
    _station_logs_cache: dict | None = None
    _station_logs_cache_ts: float = 0.0
    _STATION_LOGS_CACHE_TTL: float = 3.0  # seconds

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            self._html(EDITOR_HTML)
        elif path == "/api/files":
            self._json(self._list_files())
        elif path == "/api/load":
            fp = params.get("path", [""])[0]
            self._json(self._load_file(fp))
        elif path == "/api/status":
            self._json(_proc_manager.get_status_dict())
        elif path == "/api/screenshot":
            self._capture_screen(params)
        elif path == "/api/elements":
            self._json(self._capture_elements(params))
        elif path == "/api/devices":
            self._json(self._check_devices())
        elif path == "/api/sequence/status":
            self._json(_seq_manager.get_status_dict())
        elif path == "/api/sequence/load":
            name = params.get("name", [""])[0]
            self._json(self._seq_load(name))
        elif path == "/api/extract-logs":
            self._json(self._list_extract_logs())
        elif path == "/api/extract-data":
            fp = params.get("path", [""])[0]
            self._json(self._read_extract_log(fp))
        elif path == "/station-data":
            self._html(STATION_DATA_HTML)
        elif path == "/station-logs":
            self._html(STATION_LOGS_HTML)
        elif path == "/charger-view":
            self._html(CHARGER_VIEW_HTML)
        elif path == "/api/charger-data":
            self._json(self._charger_data())
        elif path == "/api/station-logs":
            self._json(self._list_station_logs())
        elif path == "/api/station-log":
            fp = params.get("path", [""])[0]
            self._json(self._read_station_log(fp))
        elif path == "/api/disk-space":
            self._json(self._disk_space())
        elif path == "/api/download-log":
            self._download_single_log(params)
        elif path == "/api/download-all-logs":
            self._download_all_logs_zip()
        elif path == "/api/download-extract-logs":
            self._download_extract_logs_zip()
        elif path == "/api/download-station-logs":
            self._download_station_logs_zip()
        else:
            self._error(404, "Not Found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._read_body()

        if path == "/api/save":
            self._json(self._save_file(body))
        elif path == "/api/collapse":
            self._json(self._collapse(body))
        elif path == "/api/hover-log":
            self._json(self._hover_log(body))
        elif path == "/api/play":
            self._json(self._start_play(body))
        elif path == "/api/record":
            self._json(self._start_record(body))
        elif path == "/api/stop":
            self._json(_proc_manager.stop())
        elif path == "/api/tap":
            self._json(self._device_tap(body))
        elif path == "/api/swipe":
            self._json(self._device_swipe(body))
        elif path == "/api/key":
            self._json(self._device_key(body))
        elif path == "/api/text":
            self._json(self._device_text(body))
        elif path == "/api/shell":
            self._json(self._device_shell(body))
        elif path == "/api/inspect":
            self._json(self._inspect_at_point(body))
        elif path == "/api/sequence/play":
            self._json(self._seq_play(body))
        elif path == "/api/sequence/stop":
            self._json(_seq_manager.stop())
        elif path == "/api/sequence/save":
            self._json(self._seq_save(body))
        elif path == "/api/delete":
            self._json(self._delete_file(body))
        elif path == "/api/extract-save":
            self._json(self._save_extract_data(body))
        elif path == "/api/delete-all-extract-logs":
            self._json(self._delete_all_extract_logs())
        elif path == "/api/delete-all-station-logs":
            self._json(self._delete_all_station_logs())
        elif path == "/api/clear-all-logs":
            self._json(self._clear_all_logs())
        elif path == "/api/deep-clean":
            self._json(self._deep_clean())
        elif path == "/api/smart-clean":
            keep_hours = body.get("keep_hours", 6)
            self._json(self._smart_clean(keep_hours=keep_hours))
        elif path == "/api/auto-cleanup":
            threshold = body.get("threshold_pct", 80.0)
            result = self._auto_cleanup(threshold_pct=threshold)
            self._json(result or {"ok": True, "skipped": True, "reason": "Disk usage below threshold"})
        elif path == "/api/delete-all-captures":
            self._json(self._delete_all_captures())
        elif path == "/api/delete-all-sent":
            self._json(self._delete_all_sent())
        elif path == "/api/delete-log":
            self._json(self._delete_single_log(body))
        else:
            self._error(404, "Not Found")

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length))

    def _list_files(self) -> dict:
        files = []
        work = Path(self.work_dir)
        if work.exists():
            for p in sorted(work.rglob("*.urf.json")):
                files.append(str(p))
            # Also list legacy .json files that might be recordings
            for p in sorted(work.glob("*.json")):
                if not p.name.endswith(".urf.json"):
                    try:
                        data = json.loads(p.read_text(encoding="utf-8"))
                        if "frames" in data or (isinstance(data, list) and data and "timestamp" in data[0]):
                            files.append(str(p))
                    except Exception:
                        pass
        # Also list sequence files
        sequences = []
        if work.exists():
            for p in sorted(work.glob("*.seq.json")):
                sequences.append(str(p))
        return {"files": files, "sequences": sequences}

    def _load_file(self, path: str) -> dict:
        if not path:
            return {"error": "No path specified"}
        p = Path(path)
        if not p.exists():
            return {"error": f"File not found: {path}"}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            # Handle URF format
            if "frames" in data and "meta" in data:
                return data
            # Handle legacy touch event array
            if isinstance(data, list) and data and "timestamp" in data[0]:
                return self._convert_legacy(data, path)
            return {"error": "Unknown file format"}
        except Exception as e:
            return {"error": str(e)}

    def _convert_legacy(self, events: list, source: str) -> dict:
        """Convert legacy touch event JSON to URF format."""
        frames = []
        base_ts = events[0].get("timestamp", 0) if events else 0
        for i, ev in enumerate(events):
            ts = ev.get("timestamp", 0)
            frames.append({
                "id": i + 1,
                "t": int((ts - base_ts) * 1000),
                "type": "touch",
                "action": ev.get("action", "down"),
                "x": int(ev.get("x", 0)),
                "y": int(ev.get("y", 0)),
                "enabled": True,
            })
        return {
            "version": 2,
            "meta": {
                "name": f"Imported: {Path(source).name}",
                "created": "",
                "device": "",
                "screen_width": 1080,
                "screen_height": 2400,
                "app_package": "",
                "recording_mode": "raw",
                "total_duration_ms": frames[-1]["t"] if frames else 0,
            },
            "settings": {
                "loop": False, "loop_count": 0, "loop_delay": 5,
                "speed": 1.0, "on_error": "retry", "max_retries": 3,
                "retry_delay": 2.0, "screenshot_dir": "/img/captures",
                "send_to": {"method": "cp", "path": "/img/sent"},
                "notify": True, "notify_method": "stdout", "notify_url": "",
            },
            "frames": frames,
        }

    def _save_file(self, body: dict) -> dict:
        path = body.get("path", "")
        rec_data = body.get("recording")
        if not path or not rec_data:
            return {"error": "Missing path or recording data"}
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            # Update total_duration
            frames = rec_data.get("frames", [])
            if frames:
                rec_data.setdefault("meta", {})["total_duration_ms"] = max(f.get("t", 0) for f in frames)
            p.write_text(json.dumps(rec_data, indent=2, ensure_ascii=False), encoding="utf-8")
            return {"ok": True}
        except Exception as e:
            return {"error": str(e)}

    def _delete_file(self, body: dict) -> dict:
        """Delete a .urf.json or .json recording file."""
        path = body.get("path", "")
        if not path:
            return {"error": "No path specified"}
        p = Path(path)
        if not p.exists():
            return {"error": f"File not found: {path}"}
        # Safety: only allow deleting .json files inside work_dir
        try:
            p.resolve().relative_to(Path(self.work_dir).resolve())
        except ValueError:
            return {"error": "Cannot delete files outside work directory"}
        if not p.name.endswith(".json"):
            return {"error": "Can only delete .json files"}
        try:
            p.unlink()
            return {"ok": True}
        except Exception as e:
            return {"error": str(e)}

    def _collapse(self, body: dict) -> dict:
        """Collapse raw touches to gestures server-side."""
        rec_data = body.get("recording")
        if not rec_data:
            return {"error": "No recording data"}
        try:
            rec = UniversalRecording()
            rec.frames = [Frame.from_dict(f) for f in rec_data.get("frames", [])]
            collapsed = rec.collapse_raw_to_gestures()
            return {"frames": [f.to_dict() for f in collapsed]}
        except Exception as e:
            return {"error": str(e)}

    def _start_play(self, body: dict) -> dict:
        recording_path = body.get("path", "")
        if not recording_path:
            return {"ok": False, "error": "No recording path specified"}
        if not Path(recording_path).exists():
            return {"ok": False, "error": f"File not found: {recording_path}"}
        # Auto-cleanup before playback if disk space is low
        self._auto_cleanup(threshold_pct=80.0)
        return _proc_manager.start_play(recording_path, self.work_dir)

    def _start_record(self, body: dict) -> dict:
        output_path = body.get("path", "")
        package = body.get("package", "")
        device = body.get("device", "")
        if not package:
            return {"ok": False, "error": "No package specified"}
        if not output_path:
            output_path = str(Path(self.work_dir) / "new-recording.urf.json")
        return _proc_manager.start_record(output_path, package, device, self.work_dir)

    def _capture_screen(self, params: dict) -> None:
        """Capture device screen via ADB and return as PNG."""
        serial = params.get("serial", [""])[0]
        try:
            prefix = ["adb", "-s", serial] if serial else ["adb"]
            result = subprocess.run(
                prefix + ["exec-out", "screencap", "-p"],
                capture_output=True,
                timeout=10,
            )
            if result.returncode != 0 or not result.stdout:
                self._json({"error": "Screenshot capture failed"})
                return
            data = result.stdout
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        except subprocess.TimeoutExpired:
            self._json({"error": "Screenshot timed out"})
        except FileNotFoundError:
            self._json({"error": "ADB not found"})
        except Exception as e:
            self._json({"error": str(e)})

    def _capture_elements(self, params: dict) -> dict:
        """Capture UI hierarchy via ADB and return parsed elements."""
        serial = params.get("serial", [""])[0]
        try:
            prefix = ["adb", "-s", serial] if serial else ["adb"]
            remote = "/sdcard/window_dump.xml"

            # Dump UI hierarchy on device
            subprocess.run(
                prefix + ["shell", "uiautomator", "dump", remote],
                capture_output=True, text=True, timeout=15,
            )

            # Read XML directly via exec-out (avoids needing adb pull)
            r2 = subprocess.run(
                prefix + ["exec-out", "cat", remote],
                capture_output=True, text=True, timeout=10,
            )

            if r2.returncode != 0 or not r2.stdout.strip():
                return {"error": "UI dump failed", "elements": []}

            # Parse XML
            root = _ET.fromstring(r2.stdout)
            elements: list = []
            bounds_pat = _re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")

            def walk(node: _ET.Element) -> None:
                if node.tag == "node":
                    attrs = node.attrib
                    bounds_raw = attrs.get("bounds", "")
                    match = bounds_pat.match(bounds_raw)
                    if match:
                        x1, y1, x2, y2 = map(int, match.groups())
                        elements.append({
                            "resource_id": attrs.get("resource-id", ""),
                            "text": attrs.get("text", ""),
                            "cls": attrs.get("class", ""),
                            "bounds": [x1, y1, x2, y2],
                            "center": [(x1 + x2) // 2, (y1 + y2) // 2],
                            "clickable": attrs.get("clickable") == "true",
                            "enabled": attrs.get("enabled") == "true",
                        })
                for child in node:
                    walk(child)

            walk(root)
            return {"elements": elements}
        except _ET.ParseError as e:
            return {"error": f"XML parse error: {e}", "elements": []}
        except subprocess.TimeoutExpired:
            return {"error": "UI dump timed out", "elements": []}
        except FileNotFoundError:
            return {"error": "ADB not found", "elements": []}
        except Exception as e:
            return {"error": str(e), "elements": []}

    # ---------- Device control ----------

    def _check_devices(self) -> dict:
        """List connected ADB devices."""
        try:
            result = subprocess.run(
                ["adb", "devices", "-l"],
                capture_output=True, text=True, timeout=5,
            )
            lines = [l.strip() for l in result.stdout.strip().split("\n")[1:] if l.strip()]
            devices = []
            for line in lines:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "device":
                    devices.append({"serial": parts[0], "info": " ".join(parts[2:])})
            return {"ok": True, "devices": devices, "count": len(devices)}
        except FileNotFoundError:
            return {"ok": False, "error": "ADB not found", "devices": [], "count": 0}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "ADB timed out", "devices": [], "count": 0}
        except Exception as e:
            return {"ok": False, "error": str(e), "devices": [], "count": 0}

    @staticmethod
    def _invalidate_ui_dump_cache() -> None:
        """Clear the cached UI dump so the next inspect fetches fresh data."""
        with _ui_dump_cache_lock:
            _ui_dump_cache["xml"] = ""
            _ui_dump_cache["time"] = 0.0

    def _device_tap(self, body: dict) -> dict:
        """Send tap event to device via ADB."""
        x = int(body.get("x", 0))
        y = int(body.get("y", 0))
        serial = body.get("serial", "")
        try:
            prefix = ["adb", "-s", serial] if serial else ["adb"]
            result = subprocess.run(
                prefix + ["shell", "input", "tap", str(x), str(y)],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                return {"ok": False, "error": result.stderr.strip()[:200]}
            self._invalidate_ui_dump_cache()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _device_swipe(self, body: dict) -> dict:
        """Send swipe event to device via ADB."""
        x1 = int(body.get("x1", 0))
        y1 = int(body.get("y1", 0))
        x2 = int(body.get("x2", 0))
        y2 = int(body.get("y2", 0))
        duration = int(body.get("duration", 300))
        serial = body.get("serial", "")
        try:
            prefix = ["adb", "-s", serial] if serial else ["adb"]
            result = subprocess.run(
                prefix + ["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration)],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode != 0:
                return {"ok": False, "error": result.stderr.strip()[:200]}
            self._invalidate_ui_dump_cache()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _device_key(self, body: dict) -> dict:
        """Send keyevent to device via ADB."""
        keycode = body.get("keycode", "")
        serial = body.get("serial", "")
        if not keycode and keycode != 0:
            return {"ok": False, "error": "No keycode specified"}
        try:
            prefix = ["adb", "-s", serial] if serial else ["adb"]
            result = subprocess.run(
                prefix + ["shell", "input", "keyevent", str(keycode)],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                return {"ok": False, "error": result.stderr.strip()[:200]}
            self._invalidate_ui_dump_cache()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _device_text(self, body: dict) -> dict:
        """Send text input to device via ADB."""
        text = body.get("text", "")
        serial = body.get("serial", "")
        if not text:
            return {"ok": False, "error": "No text specified"}
        try:
            prefix = ["adb", "-s", serial] if serial else ["adb"]
            # adb shell input text requires escaping spaces as %s
            escaped = text.replace(" ", "%s").replace("&", "\\&").replace(
                "(", "\\(").replace(")", "\\)").replace("<", "\\<").replace(
                ">", "\\>").replace("|", "\\|").replace(";", "\\;").replace(
                "'", "\\'").replace('"', '\\"')
            result = subprocess.run(
                prefix + ["shell", "input", "text", escaped],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                return {"ok": False, "error": result.stderr.strip()[:200]}
            self._invalidate_ui_dump_cache()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _device_shell(self, body: dict) -> dict:
        """Run a shell command on device via ADB."""
        command = body.get("command", "")
        serial = body.get("serial", "")
        if not command:
            return {"ok": False, "error": "No command specified"}
        try:
            prefix = ["adb", "-s", serial] if serial else ["adb"]
            result = subprocess.run(
                prefix + ["shell"] + command.split(),
                capture_output=True, text=True, timeout=15,
            )
            return {"ok": True, "output": result.stdout, "error": result.stderr}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _inspect_at_point(self, body: dict) -> dict:
        """Inspect UI elements at a specific screen coordinate.

        Returns all elements containing the point, plus all text content
        found at and around that position.

        Uses a cached UI dump (TTL = _UI_DUMP_TTL seconds) to avoid
        repeated slow uiautomator dump calls during Ctrl+hover.
        """
        x = int(body.get("x", 0))
        y = int(body.get("y", 0))
        serial = body.get("serial", "")

        try:
            prefix = ["adb", "-s", serial] if serial else ["adb"]
            remote = "/sdcard/window_dump.xml"

            # Use cached UI dump if still fresh
            xml_text = ""
            now = time.time()
            with _ui_dump_cache_lock:
                if (
                    _ui_dump_cache["xml"]
                    and _ui_dump_cache["serial"] == serial
                    and (now - _ui_dump_cache["time"]) < _UI_DUMP_TTL
                ):
                    xml_text = _ui_dump_cache["xml"]

            if not xml_text:
                # Dump UI hierarchy
                subprocess.run(
                    prefix + ["shell", "uiautomator", "dump", remote],
                    capture_output=True, text=True, timeout=15,
                )

                # Read XML
                r2 = subprocess.run(
                    prefix + ["exec-out", "cat", remote],
                    capture_output=True, text=True, timeout=10,
                )

                if r2.returncode != 0 or not r2.stdout.strip():
                    return {"error": "UI dump failed", "elements": [], "all_text": []}

                xml_text = r2.stdout
                with _ui_dump_cache_lock:
                    _ui_dump_cache["xml"] = xml_text
                    _ui_dump_cache["time"] = time.time()
                    _ui_dump_cache["serial"] = serial

            root = _ET.fromstring(xml_text)
            bounds_pat = _re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")

            elements: list = []
            all_text: list = []
            seen_text: set = set()

            def walk(node: _ET.Element) -> None:
                if node.tag != "node":
                    for child in node:
                        walk(child)
                    return

                attrs = node.attrib
                bounds_raw = attrs.get("bounds", "")
                match = bounds_pat.match(bounds_raw)
                if not match:
                    for child in node:
                        walk(child)
                    return

                x1, y1, x2, y2 = map(int, match.groups())

                # Check if this element contains the point
                if x1 <= x <= x2 and y1 <= y <= y2:
                    # Collect text from this element and all children
                    text = attrs.get("text", "").strip()
                    if text and text not in seen_text:
                        all_text.append(text)
                        seen_text.add(text)

                    # Collect child text too
                    def collect_child_text(n: _ET.Element) -> None:
                        if n.tag == "node":
                            t = n.attrib.get("text", "").strip()
                            if t and t not in seen_text:
                                all_text.append(t)
                                seen_text.add(t)
                        for c in n:
                            collect_child_text(c)
                    for child in node:
                        collect_child_text(child)

                    # Add element info (only meaningful elements)
                    rid = attrs.get("resource-id", "")
                    cls = attrs.get("class", "")
                    clickable = attrs.get("clickable") == "true"
                    enabled = attrs.get("enabled") == "true"
                    content_desc = attrs.get("content-desc", "").strip()
                    w = x2 - x1
                    h = y2 - y1

                    # Skip full-screen containers that aren't very useful
                    if w * h < 2073600:  # less than full 1080x1920
                        elements.append({
                            "resource_id": rid,
                            "text": text,
                            "cls": cls,
                            "bounds": [x1, y1, x2, y2],
                            "center": [(x1 + x2) // 2, (y1 + y2) // 2],
                            "clickable": clickable,
                            "enabled": enabled,
                            "content_desc": content_desc,
                        })

                for child in node:
                    walk(child)

            walk(root)

            # Sort elements by area (smallest first = most specific)
            elements.sort(key=lambda e: (e["bounds"][2] - e["bounds"][0]) * (e["bounds"][3] - e["bounds"][1]))

            # Limit to most relevant elements
            elements = elements[:10]

            return {"elements": elements, "all_text": all_text, "point": [x, y]}

        except _ET.ParseError as e:
            return {"error": f"XML parse error: {e}", "elements": [], "all_text": []}
        except subprocess.TimeoutExpired:
            return {"error": "UI dump timed out", "elements": [], "all_text": []}
        except FileNotFoundError:
            return {"error": "ADB not found", "elements": [], "all_text": []}
        except Exception as e:
            return {"error": str(e), "elements": [], "all_text": []}

    def _hover_log(self, body: dict) -> dict:
        """Print hover details to terminal for quick inspection while editing."""
        scope = str(body.get("scope", "ui"))
        frame_id = body.get("frame_id")
        msg = str(body.get("message", "")).strip()
        if not msg:
            return {"ok": False, "error": "Missing message"}

        # Scope-specific icons for readability
        scope_icons = {
            "topbar": "📂",
            "files": "📁",
            "meta": "ℹ️",
            "settings": "⚙️",
            "bulk-actions": "🔧",
            "station-data": "📊",
            "station-logs": "📜",
            "sequence": "🔁",
            "toolbar": "🔲",
            "timeline": "📋",
            "timeline-frame": "▶",
            "detail": "📝",
        }
        icon = scope_icons.get(scope, "👁")
        frame_part = f" frame={frame_id}" if frame_id is not None else ""
        print(f"{icon} [{scope}{frame_part}] {msg}", flush=True)
        return {"ok": True}

    # -------- Extract Data / Station Data endpoints --------

    def _save_extract_data(self, body: dict) -> dict:
        """Save captured UI data to a JSONL extract log file (append).

        Called during Ctrl capture in web recording to persist the captured
        screen data immediately as a separate JSON log.
        """
        recording_path = body.get("recording_path", "")
        label = body.get("label", "ctrl_capture")
        elements = body.get("elements", [])
        timestamp = body.get("timestamp", "")

        if not recording_path:
            recording_path = "/work/unnamed"

        # Determine log file path: {work}/logs/{recording-stem}-extract.jsonl
        rec_path = Path(recording_path)
        stem = rec_path.stem.replace(".urf", "")
        log_dir = Path(self.work_dir) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{stem}-extract.jsonl"

        entry = {
            "timestamp": timestamp or datetime.now().isoformat(timespec="milliseconds"),
            "label": label,
            "recording": recording_path,
            "element_count": len(elements),
            "elements": elements,
        }

        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return {"ok": True, "path": str(log_file), "entries": self._count_lines(log_file)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _extract_log_dirs(self) -> list:
        """Return all directories that may contain extract log files.

        The player writes extract logs to ``{screenshot_dir}/../logs`` which
        defaults to ``/img/logs/``, while the web editor itself writes to
        ``{work_dir}/logs``.  We check both so that captures produced by
        the player are always visible in the UI.
        """
        dirs = []
        work_log = Path(self.work_dir) / "logs"
        if work_log.exists():
            dirs.append(work_log)
        img_log = Path("/img/logs")
        if img_log.exists() and img_log.resolve() != work_log.resolve():
            dirs.append(img_log)
        return dirs

    def _charger_data(self) -> dict:
        """Parse extract logs and return structured charger connector status data.

        Reads all JSONL extract logs, finds entries that contain charger data
        (identified by the presence of 'รหัสเครื่องชาร์จ' in all_text), parses
        the connector statuses spatially, and returns data grouped by station
        and **capture loop** (not per-entry).

        During capture the screen is scrolled, so a single loop may produce
        multiple extract entries – some with charger data, some without.  We
        merge all connector data within the same (station, loop) into one
        round so the view shows exactly one column per actual loop pass.
        """
        # Intermediate: stations -> { name -> { meta, loop_data -> { global_loop -> merged_round } } }
        stations: dict = {}
        seen_entries: set = set()
        # Track per-station global loop counter so loops from different log
        # files don't collide (each file restarts loop at 1).
        station_loop_offsets: dict = {}  # station_name -> next global loop

        for log_dir in self._extract_log_dirs():
            for p in sorted(log_dir.glob("*-extract.jsonl")):
                # Track which (station, file_loop) combos this file contributes
                # so we can assign unique global loop numbers per file.
                file_loop_map: dict = {}  # (station_name, file_loop) -> global_loop
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                entry = json.loads(line)
                            except json.JSONDecodeError:
                                continue

                            ts = entry.get("timestamp", "")
                            label = entry.get("label", "")
                            dedup_key = f"{ts}_{label}"
                            if dedup_key in seen_entries:
                                continue
                            seen_entries.add(dedup_key)

                            all_text = entry.get("all_text", [])

                            # Only process entries that have charger ID markers
                            charger_entries = [
                                e for e in all_text
                                if "รหัสเครื่องชาร์จ" in e.get("text", "")
                            ]
                            if not charger_entries:
                                continue

                            charger_ids = []
                            for e in sorted(charger_entries, key=lambda x: x["center"][1]):
                                cid = e["text"].split(":")[-1].strip()
                                if cid not in charger_ids:
                                    charger_ids.append(cid)

                            station_name = self._cv_station_name(all_text)
                            station_address = self._cv_station_address(all_text)
                            station_hours = self._cv_station_hours(all_text)

                            if not station_name:
                                station_name = "Unknown Station"

                            connectors = self._cv_parse_connectors(all_text, charger_ids)
                            # Skip entries that parsed zero connectors (empty scroll captures)
                            if not connectors:
                                continue

                            file_loop = entry.get("loop", 1)

                            if station_name not in stations:
                                stations[station_name] = {
                                    "name": station_name,
                                    "address": station_address,
                                    "hours": station_hours,
                                    "loop_data": {},  # global_loop -> merged round
                                }
                                station_loop_offsets[station_name] = 1

                            # Map file-local loop to a unique global loop number
                            fkey = (station_name, file_loop)
                            if fkey not in file_loop_map:
                                file_loop_map[fkey] = station_loop_offsets[station_name]
                                station_loop_offsets[station_name] += 1
                            loop_num = file_loop_map[fkey]

                            sdata = stations[station_name]
                            if loop_num not in sdata["loop_data"]:
                                # First entry for this loop – create the round
                                sdata["loop_data"][loop_num] = {
                                    "timestamp": ts,
                                    "label": label,
                                    "loop": loop_num,
                                    "step": entry.get("step", 0),
                                    "connectors": {},  # keyed by charger_id::connector
                                }

                            merged = sdata["loop_data"][loop_num]
                            # Use the earliest timestamp for the round header
                            if ts < merged["timestamp"]:
                                merged["timestamp"] = ts

                            # Merge connectors – later captures within the same loop
                            # can add new connectors or update existing ones
                            for c in connectors:
                                ck = f"{c['charger_id']}::{c['connector']}"
                                merged["connectors"][ck] = c

                except Exception:
                    continue

        # Flatten loop_data dicts into sorted round lists
        result_stations = []
        for sdata in stations.values():
            rounds = []
            for loop_num in sorted(sdata["loop_data"]):
                merged = sdata["loop_data"][loop_num]
                rounds.append({
                    "timestamp": merged["timestamp"],
                    "label": merged["label"],
                    "loop": merged["loop"],
                    "step": merged["step"],
                    "connectors": list(merged["connectors"].values()),
                })
            result_stations.append({
                "name": sdata["name"],
                "address": sdata["address"],
                "hours": sdata["hours"],
                "rounds": rounds,
            })

        return {"stations": result_stations}

    def _cv_station_name(self, all_text: list) -> str:
        """Extract the charging station name from all_text elements.

        The station name appears on the detail page above the first charger ID
        marker.  We look for the longest qualifying text element that sits
        above the topmost 'รหัสเครื่องชาร์จ' line and contains a recognisable
        station name pattern (e.g. 'สเตชั่น', 'สถานี', or a long Thai name).
        """
        skip = {
            "เปิด", "24 ชั่วโมง", "ข้อมูลเพิ่มเติม >", "แสดงทั้งหมด",
            "หัวชาร์จที่ว่างตอนนี้", "Auto Charge", "เช็คอิน",
            "CC (กระแสสูงสุด 200A)", "DC CCS COMBO 2", "AC Type 2",
            "พร้อมใช้งาน", "มีผู้ใช้งาน", "นอกเวลาทำการ", "มีการจอง",
            "กดเพื่ออัปเดตข้อมูล", "สถานีที่บันทึกไว้",
            "Low priority", "High priority", "Medium priority",
        }
        skip_substrings = ("ถ.", "แขวง", "เขต", "กม.", "รหัส", "หมายเหตุ",
                           "kWh", "ชั่วโมง", "สูงสุด", "อัตราค่า",
                           "ข้อมูลล่าสุด")
        # Keywords that positively identify station names – if present,
        # bypass the skip_substrings filter (e.g. station names containing "กม.")
        station_keywords = ("สเตชั่น", "สถานี", "เอ็นจีวี", "พีทีที",
                            "เซนเตอร์", "แกรนด์")

        # Regex to detect time-range strings like "05:00 - 23:59 น."
        _time_range_re = _re.compile(r"\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}")
        # Regex to detect standalone time references like "ณ 16:02 น."
        _time_ref_re = _re.compile(r"ณ\s*\d{1,2}:\d{2}")

        # Find the Y of the topmost charger ID marker as an upper bound
        charger_y = None
        for e in all_text:
            if "รหัสเครื่องชาร์จ" in e.get("text", ""):
                ey = e.get("center", [0, 0])[1]
                if charger_y is None or ey < charger_y:
                    charger_y = ey

        candidates = []
        for e in all_text:
            text = e.get("text", "")
            cy = e.get("center", [0, 0])[1]
            if not text or len(text) <= 8:
                continue
            if text in skip:
                continue
            has_station_kw = any(kw in text for kw in station_keywords)
            if not has_station_kw and any(sub in text for sub in skip_substrings):
                continue
            # Skip time-range strings (e.g. "05:00 - 23:59 น.")
            if _time_range_re.search(text):
                continue
            # Skip time-reference strings (e.g. "ข้อมูลล่าสุด ณ 16:02 น.")
            if _time_ref_re.search(text):
                continue
            # Must be above the first charger ID marker
            if charger_y is not None and cy >= charger_y:
                continue
            # Station names are typically long Thai text
            candidates.append((cy, text))

        if candidates:
            # Pick the candidate closest (just above) to the charger section
            candidates.sort(key=lambda x: -x[0])
            return candidates[0][1]
        return ""

    def _cv_station_address(self, all_text: list) -> str:
        """Extract the station address from all_text."""
        for e in sorted(all_text, key=lambda x: x["center"][1]):
            text = e.get("text", "")
            if ("ถ." in text or "แขวง" in text) and len(text) > 15:
                return text
        return ""

    def _cv_station_hours(self, all_text: list) -> str:
        """Extract station opening hours from all_text."""
        for e in sorted(all_text, key=lambda x: x["center"][1]):
            text = e.get("text", "")
            if text.startswith("เปิด") and "ชั่วโมง" in text:
                return text.split("|")[0].strip()
            if text.startswith("เปิด") and ":" in text:
                return text.split("|")[0].strip()
        return ""

    def _cv_parse_connectors(self, all_text: list, charger_ids: list) -> list:
        """Parse connector-level status data from a single all_text list.

        Handles both DC (DC CCS COMBO 2) and AC (AC Type 2) connectors.
        """
        sorted_elems = sorted(all_text, key=lambda x: x["center"][1])

        # Y positions of each charger ID marker
        charger_y: dict = {}
        for e in sorted_elems:
            text = e.get("text", "")
            if "รหัสเครื่องชาร์จ" in text:
                cid = text.split(":")[-1].strip()
                charger_y[cid] = e["center"][1]

        status_set = {"มีผู้ใช้งาน", "พร้อมใช้งาน", "นอกเวลาทำการ", "มีการจอง"}
        connector_types = {"DC CCS COMBO 2", "AC Type 2"}
        type_elems = [e for e in sorted_elems if e.get("text") in connector_types]
        status_elems = [e for e in sorted_elems if e.get("text") in status_set]
        # Include all known side/position names
        side_names = {"ซ้าย-A", "ขวา-B", "กลาง-A", "ซ้าย ที-2"}
        side_elems = [e for e in sorted_elems if e.get("text") in side_names]
        rate_elems = [e for e in sorted_elems if "kWh" in e.get("text", "")]

        connectors = []
        seen: set = set()

        for te in type_elems:
            te_y = te["center"][1]
            conn_type = te["text"]

            # Nearest charger ID marker above this connector
            charger_id = None
            best = float("inf")
            for cid, cy in charger_y.items():
                if cy <= te_y and (te_y - cy) < best:
                    best = te_y - cy
                    charger_id = cid

            # Status on the same row (within 30 px vertical)
            status = None
            for s in status_elems:
                if abs(s["center"][1] - te_y) <= 30:
                    status = s["text"]
                    break

            # Connector side name on same row or just below (within 80 px)
            connector = None
            best_side_dist = float("inf")
            for side in side_elems:
                gap = side["center"][1] - te_y
                if -30 <= gap <= 80:
                    dist = abs(gap)
                    if dist < best_side_dist:
                        best_side_dist = dist
                        connector = side["text"]

            # Rate/power spec below (within 200 px)
            max_power = rate = ""
            for r in rate_elems:
                gap = r["center"][1] - te_y
                if 0 < gap <= 200:
                    parts = r["text"].split("|")
                    max_power = parts[0].replace("สูงสุด", "").strip()
                    rate = parts[1].strip() if len(parts) > 1 else ""
                    break

            key = f"{charger_id}_{connector}"
            if key in seen:
                continue
            seen.add(key)

            connectors.append({
                "charger_id": charger_id or "?",
                "connector": connector or "?",
                "type": conn_type,
                "max_power": max_power,
                "rate": rate,
                "status": status or "ไม่ทราบ",
            })

        connectors.sort(key=lambda c: (c["charger_id"], c["connector"]))
        return connectors

    def _list_extract_logs(self) -> dict:
        """List all extract JSONL log files across known log directories."""
        logs = []
        seen_names: set = set()
        for log_dir in self._extract_log_dirs():
            # Per-recording logs: station-01-extract.jsonl
            for p in sorted(log_dir.glob("*-extract.jsonl")):
                if p.name in seen_names:
                    continue
                seen_names.add(p.name)
                line_count = self._count_lines(p)
                logs.append({
                    "path": str(p),
                    "name": p.name,
                    "stem": p.stem.replace("-extract", ""),
                    "entries": line_count,
                    "size_kb": round(p.stat().st_size / 1024, 1),
                })
            # Legacy per-label logs: extract-ctrl_capture_5.jsonl
            for p in sorted(log_dir.glob("extract-*.jsonl")):
                if p.name in seen_names:
                    continue
                seen_names.add(p.name)
                line_count = self._count_lines(p)
                logs.append({
                    "path": str(p),
                    "name": p.name,
                    "stem": p.stem.replace("extract-", ""),
                    "entries": line_count,
                    "size_kb": round(p.stat().st_size / 1024, 1),
                })
        return {"logs": logs}

    def _read_extract_log(self, path: str) -> dict:
        """Read a JSONL extract log file and return all entries."""
        if not path:
            return {"error": "No path specified"}
        p = Path(path)
        if not p.exists():
            return {"error": f"File not found: {path}"}
        # Safety check – allow both work_dir/logs and /img/logs
        allowed_dirs = [Path(self.work_dir).resolve(), Path("/img/logs").resolve()]
        resolved = p.resolve()
        if not any(self._is_under(resolved, d) for d in allowed_dirs):
            return {"error": "Cannot read files outside allowed directories"}
        try:
            entries = []
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
            return {"path": str(p), "name": p.name, "entries": entries}
        except Exception as e:
            return {"error": str(e)}

    @staticmethod
    def _count_lines(path: Path) -> int:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return sum(1 for line in f if line.strip())
        except Exception:
            return 0

    @staticmethod
    def _is_under(child: Path, parent: Path) -> bool:
        """Check if *child* is under *parent* directory."""
        try:
            child.relative_to(parent)
            return True
        except ValueError:
            return False

    # -------- Station Playback Logs endpoints --------

    def _list_station_logs(self) -> dict:
        """List all station playback JSON log files in the work/logs directory.

        Uses a short TTL cache to ensure consistent data when the sidebar
        and the Station Logs detail page both request data near-simultaneously.
        """
        now = time.time()
        if (
            EditorHandler._station_logs_cache is not None
            and (now - EditorHandler._station_logs_cache_ts) < EditorHandler._STATION_LOGS_CACHE_TTL
        ):
            return EditorHandler._station_logs_cache

        log_dir = Path(self.work_dir) / "logs"
        logs = []
        if log_dir.exists():
            for p in sorted(log_dir.glob("station-*-playback.json")):
                try:
                    raw = p.read_text(encoding="utf-8")
                    if not raw.strip():
                        # File is being written (truncated but not yet filled)
                        continue
                    data = json.loads(raw)
                    runs = data.get("runs", [])
                    rec = data.get("recording", {})
                    total_runs = len(runs)
                    success_runs = sum(1 for r in runs if r.get("status") == "success")
                    error_runs = sum(1 for r in runs if r.get("status") == "error")
                    last_run_at = runs[-1].get("completed_at") or runs[-1].get("started_at") if runs else None
                    logs.append({
                        "path": str(p),
                        "name": p.name,
                        "station": rec.get("name", p.stem.replace("-playback", "")),
                        "total_runs": total_runs,
                        "success_runs": success_runs,
                        "error_runs": error_runs,
                        "total_frames": rec.get("total_frames", 0),
                        "last_run_at": last_run_at,
                        "size_kb": round(p.stat().st_size / 1024, 1),
                    })
                except (json.JSONDecodeError, Exception):
                    # File may be mid-write (partial JSON); skip rather than
                    # reporting 0 runs which causes sidebar/detail mismatch.
                    continue

        result = {"logs": logs}
        EditorHandler._station_logs_cache = result
        EditorHandler._station_logs_cache_ts = now
        return result

    def _read_station_log(self, path: str) -> dict:
        """Read a station playback log JSON file and return its full data."""
        if not path:
            return {"error": "No path specified"}
        p = Path(path)
        if not p.exists():
            return {"error": f"File not found: {path}"}
        try:
            p.resolve().relative_to(Path(self.work_dir).resolve())
        except ValueError:
            return {"error": "Cannot read files outside work directory"}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data
        except Exception as e:
            return {"error": str(e)}

    # -------- Disk Space endpoint --------

    @staticmethod
    def _dir_size(path: Path) -> tuple:
        """Return (total_bytes, file_count) for a directory."""
        total = 0
        count = 0
        if path.exists():
            for f in path.rglob("*"):
                if f.is_file():
                    try:
                        total += f.stat().st_size
                        count += 1
                    except OSError:
                        pass
        return total, count

    def _disk_space(self) -> dict:
        """Return disk usage info with detailed breakdown.

        Shows the most constrained filesystem so the user sees a warning
        before ``No space left on device`` errors occur.  In Docker the
        container overlay (``/``) may be much smaller than a bind-mounted
        volume (``/work``, ``/img``).
        """
        try:
            # Gather usage for all relevant mount points
            candidates = []
            for mount in ["/", str(self.work_dir), "/img"]:
                try:
                    u = shutil.disk_usage(mount)
                    candidates.append((mount, u))
                except OSError:
                    pass

            if not candidates:
                return {"ok": False, "error": "Cannot read any filesystem"}

            # De-duplicate partitions that report the same total (same fs)
            seen_totals: dict[int, tuple] = {}
            for mount, u in candidates:
                key = u.total
                # If we already saw this total, keep the one with less free
                # space (more constrained) – or just skip
                if key not in seen_totals or u.free < seen_totals[key][1].free:
                    seen_totals[key] = (mount, u)

            unique_parts = list(seen_totals.values())

            # Pick the most constrained partition (least free space) for
            # the headline gauge – this is the one that will hit "disk full"
            # first.
            unique_parts.sort(key=lambda x: x[1].free)
            _primary_mount, primary = unique_parts[0]

            total = primary.total
            used = primary.used
            free = primary.free

            img_dir = Path("/img")

            log_dir = Path(self.work_dir) / "logs"
            log_size, log_count = self._dir_size(log_dir)
            # Also count logs under /img/logs if it exists
            img_log_dir = Path("/img/logs")
            if img_log_dir.exists() and img_log_dir.resolve() != log_dir.resolve():
                ils, ilc = self._dir_size(img_log_dir)
                log_size += ils
                log_count += ilc

            # Breakdown: captures, sent images, work data files, __pycache__
            captures_dir = Path("/img/captures")
            sent_dir = Path("/img/sent")
            pycache_size = 0
            pycache_count = 0
            for d in [Path(self.work_dir), Path("/usr/local/bin")]:
                for pc in d.rglob("__pycache__"):
                    if pc.is_dir():
                        s, c = self._dir_size(pc)
                        pycache_size += s
                        pycache_count += c

            captures_size, captures_count = self._dir_size(captures_dir)
            sent_size, sent_count = self._dir_size(sent_dir)
            img_size, img_count = self._dir_size(img_dir)

            # Work dir .urf.json files
            urf_size = 0
            urf_count = 0
            work = Path(self.work_dir)
            if work.exists():
                for f in work.glob("*.urf.json"):
                    try:
                        urf_size += f.stat().st_size
                        urf_count += 1
                    except OSError:
                        pass

            # Temp files in work dir
            tmp_size = 0
            tmp_count = 0
            for pattern in ["*.tmp", "*.bak", "*.pyc", ".tmp_*"]:
                for f in work.glob(pattern):
                    try:
                        tmp_size += f.stat().st_size
                        tmp_count += 1
                    except OSError:
                        pass

            return {
                "ok": True,
                "total_gb": round(total / (1024**3), 2),
                "used_gb": round(used / (1024**3), 2),
                "free_gb": round(free / (1024**3), 2),
                "used_pct": round(used / total * 100, 1) if total else 0,
                "log_size_mb": round(log_size / (1024**2), 2),
                "log_count": log_count,
                "breakdown": {
                    "captures_mb": round(captures_size / (1024**2), 2),
                    "captures_count": captures_count,
                    "sent_mb": round(sent_size / (1024**2), 2),
                    "sent_count": sent_count,
                    "img_total_mb": round(img_size / (1024**2), 2),
                    "img_total_count": img_count,
                    "urf_mb": round(urf_size / (1024**2), 2),
                    "urf_count": urf_count,
                    "pycache_mb": round(pycache_size / (1024**2), 2),
                    "pycache_count": pycache_count,
                    "tmp_mb": round(tmp_size / (1024**2), 2),
                    "tmp_count": tmp_count,
                },
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _deep_clean(self) -> dict:
        """Aggressively clean disk space: logs, captures, sent images, temp files, __pycache__."""
        deleted = 0
        freed = 0
        errors = []
        details = {}

        # 1. Clear transient log files (preserve extract & station playback logs)
        log_dir = Path(self.work_dir) / "logs"
        log_del = 0
        if log_dir.exists():
            for p in log_dir.iterdir():
                if p.is_file():
                    # Skip user data logs that cannot be regenerated
                    if p.name.endswith("-extract.jsonl") or p.name.startswith("extract-"):
                        continue
                    if p.name.endswith("-playback.json"):
                        continue
                    try:
                        freed += p.stat().st_size
                        p.unlink()
                        deleted += 1
                        log_del += 1
                    except Exception as e:
                        errors.append(f"log {p.name}: {e}")
        details["logs"] = log_del

        # 2. Clear /img/captures
        cap_dir = Path("/img/captures")
        cap_del = 0
        if cap_dir.exists():
            for p in cap_dir.rglob("*"):
                if p.is_file():
                    try:
                        freed += p.stat().st_size
                        p.unlink()
                        deleted += 1
                        cap_del += 1
                    except Exception as e:
                        errors.append(f"capture {p.name}: {e}")
        details["captures"] = cap_del

        # 3. Clear /img/sent
        sent_dir = Path("/img/sent")
        sent_del = 0
        if sent_dir.exists():
            for p in sent_dir.rglob("*"):
                if p.is_file():
                    try:
                        freed += p.stat().st_size
                        p.unlink()
                        deleted += 1
                        sent_del += 1
                    except Exception as e:
                        errors.append(f"sent {p.name}: {e}")
        details["sent"] = sent_del

        # 4. Clear __pycache__ directories
        pc_del = 0
        for d in [Path(self.work_dir), Path("/usr/local/bin")]:
            for pc in list(d.rglob("__pycache__")):
                if pc.is_dir():
                    try:
                        for f in pc.rglob("*"):
                            if f.is_file():
                                freed += f.stat().st_size
                                f.unlink()
                                deleted += 1
                                pc_del += 1
                        shutil.rmtree(pc, ignore_errors=True)
                    except Exception as e:
                        errors.append(f"pycache: {e}")
        details["pycache"] = pc_del

        # 5. Clear temp files in work dir
        tmp_del = 0
        work = Path(self.work_dir)
        for pattern in ["*.tmp", "*.bak", "*.pyc", ".tmp_*"]:
            for f in work.glob(pattern):
                try:
                    freed += f.stat().st_size
                    f.unlink()
                    deleted += 1
                    tmp_del += 1
                except Exception as e:
                    errors.append(f"tmp {f.name}: {e}")
        details["tmp"] = tmp_del

        # 6. Clean apt cache and other system temp files
        sys_del = 0
        for sys_dir in [Path("/var/cache/apt"), Path("/tmp"), Path("/var/tmp")]:
            if sys_dir.exists():
                for p in sys_dir.rglob("*"):
                    if p.is_file():
                        try:
                            freed += p.stat().st_size
                            p.unlink()
                            deleted += 1
                            sys_del += 1
                        except Exception:
                            pass
        details["system_cache"] = sys_del

        return {
            "ok": True,
            "deleted": deleted,
            "freed_mb": round(freed / (1024**2), 2),
            "details": details,
            "errors": errors[:10],
        }

    def _smart_clean(self, keep_hours: int = 6) -> dict:
        """Clean old files while keeping recent ones (within keep_hours).

        Unlike deep_clean which deletes everything, smart_clean preserves
        recently-created captures, sent images, and logs.
        """
        import time

        cutoff = time.time() - (keep_hours * 3600)
        deleted = 0
        freed = 0
        errors = []
        details = {}

        def _clean_old_files(directory: Path, label: str, recursive: bool = True,
                             skip_fn=None):
            nonlocal deleted, freed
            count = 0
            if not directory.exists():
                return count
            files = directory.rglob("*") if recursive else directory.iterdir()
            for p in files:
                if not p.is_file():
                    continue
                if skip_fn and skip_fn(p):
                    continue
                try:
                    st = p.stat()
                    if st.st_mtime < cutoff:
                        freed += st.st_size
                        p.unlink()
                        deleted += 1
                        count += 1
                except Exception as e:
                    errors.append(f"{label} {p.name}: {e}")
            return count

        details["captures"] = _clean_old_files(Path("/img/captures"), "capture")
        details["sent"] = _clean_old_files(Path("/img/sent"), "sent")
        def _is_user_data_log(p: Path) -> bool:
            return (p.name.endswith("-extract.jsonl") or p.name.startswith("extract-")
                    or p.name.endswith("-playback.json"))

        details["logs"] = _clean_old_files(Path(self.work_dir) / "logs", "log",
                                           recursive=False, skip_fn=_is_user_data_log)

        # Always clean pycache and temp files fully
        pc_del = 0
        for d in [Path(self.work_dir), Path("/usr/local/bin")]:
            for pc in list(d.rglob("__pycache__")):
                if pc.is_dir():
                    try:
                        for f in pc.rglob("*"):
                            if f.is_file():
                                freed += f.stat().st_size
                                f.unlink()
                                deleted += 1
                                pc_del += 1
                        shutil.rmtree(pc, ignore_errors=True)
                    except Exception as e:
                        errors.append(f"pycache: {e}")
        details["pycache"] = pc_del

        tmp_del = 0
        work = Path(self.work_dir)
        for pattern in ["*.tmp", "*.bak", "*.pyc", ".tmp_*"]:
            for f in work.glob(pattern):
                try:
                    freed += f.stat().st_size
                    f.unlink()
                    deleted += 1
                    tmp_del += 1
                except Exception as e:
                    errors.append(f"tmp {f.name}: {e}")
        details["tmp"] = tmp_del

        # System cache
        sys_del = 0
        for sys_dir in [Path("/var/cache/apt"), Path("/tmp"), Path("/var/tmp")]:
            if sys_dir.exists():
                for p in sys_dir.rglob("*"):
                    if p.is_file():
                        try:
                            freed += p.stat().st_size
                            p.unlink()
                            deleted += 1
                            sys_del += 1
                        except Exception:
                            pass
        details["system_cache"] = sys_del

        return {
            "ok": True,
            "deleted": deleted,
            "freed_mb": round(freed / (1024**2), 2),
            "details": details,
            "keep_hours": keep_hours,
            "errors": errors[:10],
        }

    def _auto_cleanup(self, threshold_pct: float = 80.0) -> dict | None:
        """Auto-cleanup when disk usage exceeds threshold.

        Returns cleanup result dict if cleanup was performed, None if not needed.
        Designed to be called before playback or periodically.
        Checks ALL relevant filesystems (overlay, /work, /img) and triggers
        cleanup based on the most constrained one.
        """
        try:
            # Check all relevant mount points, use the most constrained
            used_pct = 0.0
            for mount in ["/", str(self.work_dir), "/img"]:
                try:
                    u = shutil.disk_usage(mount)
                    pct = u.used / u.total * 100 if u.total else 0
                    if pct > used_pct:
                        used_pct = pct
                except OSError:
                    pass
            if used_pct <= threshold_pct:
                return None

            # Progressive cleanup based on severity
            if used_pct > 95:
                # Critical: deep clean everything
                return self._deep_clean()
            elif used_pct > 90:
                # High: keep only last 2 hours
                return self._smart_clean(keep_hours=2)
            else:
                # Moderate: keep last 6 hours
                return self._smart_clean(keep_hours=6)
        except Exception:
            return None

    def _delete_all_captures(self) -> dict:
        """Delete all capture screenshots."""
        cap_dir = Path("/img/captures")
        deleted = 0
        freed = 0
        errors = []
        if cap_dir.exists():
            for p in cap_dir.rglob("*"):
                if p.is_file():
                    try:
                        freed += p.stat().st_size
                        p.unlink()
                        deleted += 1
                    except Exception as e:
                        errors.append(f"{p.name}: {e}")
        return {"ok": True, "deleted": deleted, "freed_mb": round(freed / (1024**2), 2), "errors": errors}

    def _delete_all_sent(self) -> dict:
        """Delete all sent images."""
        sent_dir = Path("/img/sent")
        deleted = 0
        freed = 0
        errors = []
        if sent_dir.exists():
            for p in sent_dir.rglob("*"):
                if p.is_file():
                    try:
                        freed += p.stat().st_size
                        p.unlink()
                        deleted += 1
                    except Exception as e:
                        errors.append(f"{p.name}: {e}")
        return {"ok": True, "deleted": deleted, "freed_mb": round(freed / (1024**2), 2), "errors": errors}

    # -------- Delete / Clear Log endpoints --------

    def _delete_all_extract_logs(self) -> dict:
        """Delete all extract JSONL log files."""
        deleted = 0
        errors = []
        for log_dir in self._extract_log_dirs():
            for p in list(log_dir.glob("*-extract.jsonl")) + list(log_dir.glob("extract-*.jsonl")):
                try:
                    p.unlink()
                    deleted += 1
                except Exception as e:
                    errors.append(f"{p.name}: {e}")
        return {"ok": True, "deleted": deleted, "errors": errors}

    def _delete_all_station_logs(self) -> dict:
        """Delete all station playback JSON log files."""
        log_dir = Path(self.work_dir) / "logs"
        deleted = 0
        errors = []
        if log_dir.exists():
            for p in list(log_dir.glob("station-*-playback.json")):
                try:
                    p.unlink()
                    deleted += 1
                except Exception as e:
                    errors.append(f"{p.name}: {e}")
        # Invalidate cache so next read reflects deletion immediately
        EditorHandler._station_logs_cache = None
        return {"ok": True, "deleted": deleted, "errors": errors}

    def _clear_all_logs(self) -> dict:
        """Delete ALL log files (extract + station + any other logs)."""
        deleted = 0
        errors = []
        # Clear work/logs
        log_dir = Path(self.work_dir) / "logs"
        if log_dir.exists():
            for p in log_dir.iterdir():
                if p.is_file():
                    try:
                        p.unlink()
                        deleted += 1
                    except Exception as e:
                        errors.append(f"{p.name}: {e}")
        # Also clear /img/logs (where the player writes extract logs)
        img_log = Path("/img/logs")
        if img_log.exists() and img_log.resolve() != log_dir.resolve():
            for p in img_log.iterdir():
                if p.is_file():
                    try:
                        p.unlink()
                        deleted += 1
                    except Exception as e:
                        errors.append(f"{p.name}: {e}")
        return {"ok": True, "deleted": deleted, "errors": errors}

    def _delete_single_log(self, body: dict) -> dict:
        """Delete a single log file by path."""
        path = body.get("path", "")
        if not path:
            return {"error": "No path specified"}
        p = Path(path)
        if not p.exists():
            return {"error": f"File not found: {path}"}
        allowed_dirs = [Path(self.work_dir).resolve(), Path("/img/logs").resolve()]
        resolved = p.resolve()
        if not any(self._is_under(resolved, d) for d in allowed_dirs):
            return {"error": "Cannot delete files outside allowed directories"}
        try:
            p.unlink()
            return {"ok": True}
        except Exception as e:
            return {"error": str(e)}

    # -------- Download Log endpoints --------

    def _download_single_log(self, params: dict) -> None:
        """Download a single log file."""
        path = params.get("path", [""])[0]
        if not path:
            self._json({"error": "No path specified"})
            return
        p = Path(path)
        if not p.exists():
            self._json({"error": f"File not found: {path}"})
            return
        allowed_dirs = [Path(self.work_dir).resolve(), Path("/img/logs").resolve()]
        resolved = p.resolve()
        if not any(self._is_under(resolved, d) for d in allowed_dirs):
            self._json({"error": "Cannot download files outside allowed directories"})
            return
        try:
            data = p.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{p.name}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self._json({"error": str(e)})

    def _download_all_logs_zip(self) -> None:
        """Download all log files as a single ZIP archive."""
        log_dir = Path(self.work_dir) / "logs"
        img_log = Path("/img/logs")
        all_dirs = [d for d in [log_dir, img_log] if d.exists()]
        if not all_dirs:
            self._json({"error": "No log files found"})
            return
        try:
            buf = io.BytesIO()
            seen_names: set = set()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for d in all_dirs:
                    for p in sorted(d.iterdir()):
                        if p.is_file() and p.name not in seen_names:
                            seen_names.add(p.name)
                            zf.write(p, p.name)
            if not seen_names:
                self._json({"error": "No log files found"})
                return
            data = buf.getvalue()
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"logs_{ts}.zip"
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self._json({"error": str(e)})

    def _download_extract_logs_zip(self) -> None:
        """Download only extract log files as ZIP."""
        try:
            buf = io.BytesIO()
            count = 0
            seen_names: set = set()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for log_dir in self._extract_log_dirs():
                    for p in sorted(list(log_dir.glob("*-extract.jsonl")) + list(log_dir.glob("extract-*.jsonl"))):
                        if p.is_file() and p.name not in seen_names:
                            seen_names.add(p.name)
                            zf.write(p, p.name)
                            count += 1
            if count == 0:
                self._json({"error": "No extract log files found"})
                return
            data = buf.getvalue()
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"extract_logs_{ts}.zip"
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self._json({"error": str(e)})

    def _download_station_logs_zip(self) -> None:
        """Download only station playback log files as ZIP."""
        log_dir = Path(self.work_dir) / "logs"
        try:
            buf = io.BytesIO()
            count = 0
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                if log_dir.exists():
                    for p in sorted(log_dir.glob("station-*-playback.json")):
                        if p.is_file():
                            zf.write(p, p.name)
                            count += 1
            if count == 0:
                self._json({"error": "No station log files found"})
                return
            data = buf.getvalue()
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"station_logs_{ts}.zip"
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self._json({"error": str(e)})

    # -------- Sequence Runner endpoints --------

    def _seq_play(self, body: dict) -> dict:
        """Start a sequence run. Saves a temp .seq.json and launches sequence_runner.py."""
        scripts = body.get("scripts", [])
        settings = body.get("settings", {})
        if not scripts:
            return {"ok": False, "error": "No scripts provided"}

        # Auto-cleanup before sequence playback if disk space is low
        self._auto_cleanup(threshold_pct=80.0)

        # Save temp sequence file
        seq_data = {"name": "temp_sequence", "scripts": scripts, "settings": settings}
        seq_path = Path(self.work_dir) / ".tmp_sequence.seq.json"
        seq_path.write_text(json.dumps(seq_data, indent=2, ensure_ascii=False), encoding="utf-8")

        return _seq_manager.start(str(seq_path), self.work_dir)

    def _seq_stop(self) -> dict:
        return _seq_manager.stop()

    def _seq_save(self, body: dict) -> dict:
        """Save a sequence definition to a .seq.json file."""
        name = body.get("name", "sequence")
        scripts = body.get("scripts", [])
        settings = body.get("settings", {})
        if not scripts:
            return {"ok": False, "error": "No scripts to save"}

        # Sanitize filename
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
        filename = f"{safe_name}.seq.json"
        filepath = Path(self.work_dir) / filename

        seq_data = {"name": name, "scripts": scripts, "settings": settings}
        filepath.write_text(json.dumps(seq_data, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"ok": True, "path": str(filepath)}

    def _seq_load(self, name: str) -> dict:
        """Load a .seq.json sequence file."""
        if not name:
            return {"error": "No sequence name specified"}

        # Try exact match, then with extension
        work = Path(self.work_dir)
        candidates = [
            work / f"{name}.seq.json",
            work / name,
        ]
        for p in candidates:
            if p.exists() and p.suffix == ".json":
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    return data
                except Exception as e:
                    return {"error": str(e)}

        # List available sequences
        seqs = [f.stem.replace(".seq", "") for f in work.glob("*.seq.json")]
        return {"error": f"Sequence not found: {name}. Available: {', '.join(seqs) or 'none'}"}

    # Response helpers
    def _html(self, content: str) -> None:
        data = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj: Any) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _error(self, code: int, msg: str) -> None:
        data = msg.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> int:
    parser = argparse.ArgumentParser(description="Visual Flow Editor - Web UI for .urf.json files")
    parser.add_argument("--port", type=int, default=9090, help="HTTP port (default: 9090)")
    parser.add_argument("--dir", default="/work", help="Working directory for files (default: /work)")
    args = parser.parse_args()

    EditorHandler.work_dir = args.dir
    server = HTTPServer(("0.0.0.0", args.port), EditorHandler)

    print(f"Flow Editor running on http://0.0.0.0:{args.port}")
    print(f"Working directory: {args.dir}")
    print("Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
