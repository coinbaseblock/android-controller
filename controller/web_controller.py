#!/usr/bin/env python3
"""
Web Controller - Interactive web-based Android device controller with recording.

Features:
1. Live screen mirroring via periodic screenshot polling
2. Click-to-tap: click on the screen image to send taps with correct timing
3. Record from BOTH computer clicks AND mobile device touches
4. Ctrl+hover element inspection: show UI element info and all text nearby
5. Playback with accurate delay timing matching the original recording

Usage:
    web-controller.py [--port 8080] [-s DEVICE_SERIAL] [--fps 2]
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs

# Import from local modules
from element_interactor import (
    adb_cmd,
    run_adb as run_adb_text,
    get_ui_xml,
    parse_all_elements,
    ElementInfo,
    AdbError,
)
from universal_format import (
    Frame,
    UniversalRecording,
    RecordingMeta,
    RecordingSettings,
    KEY_NAMES,
)

# ---------- getevent parsing for mobile touch capture ----------

GETEVENT_RE = re.compile(
    r"\[\s*(\d+\.\d+)\]\s+(\S+):\s+(\S+)\s+(\S+)\s+([0-9a-fA-F]+)"
)


class DeviceTouchMonitor(threading.Thread):
    """Monitor device touches via `adb shell getevent -lt` in a background thread."""

    def __init__(self, serial: Optional[str], on_touch_event=None):
        super().__init__(daemon=True)
        self.serial = serial
        self.on_touch_event = on_touch_event
        self._stop_event = threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self.last_x: Optional[int] = None
        self.last_y: Optional[int] = None
        self.active = False

    def run(self) -> None:
        cmd = list(adb_cmd(self.serial)) + ["shell", "getevent", "-lt"]
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1,
            )
            if not self._proc.stdout:
                return

            for line in self._proc.stdout:
                if self._stop_event.is_set():
                    break
                self._parse_line(line.strip())

        except Exception:
            pass
        finally:
            if self._proc:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._proc.kill()

    def _parse_line(self, line: str) -> None:
        match = GETEVENT_RE.match(line)
        if not match:
            return

        timestamp_raw, device, ev_type, code, value_hex = match.groups()
        timestamp = float(timestamp_raw)
        value = int(value_hex, 16)

        if ev_type == "EV_ABS":
            if code == "ABS_MT_POSITION_X":
                self.last_x = value
            elif code == "ABS_MT_POSITION_Y":
                self.last_y = value
        elif ev_type == "EV_SYN" and code == "SYN_REPORT":
            if self.last_x is not None and self.last_y is not None:
                action = "down" if not self.active else "move"
                self.active = True
                if self.on_touch_event:
                    self.on_touch_event({
                        "source": "device",
                        "action": action,
                        "x": self.last_x,
                        "y": self.last_y,
                        "timestamp": timestamp,
                        "time_mono": time.monotonic(),
                    })
            elif self.active:
                self.active = False
                if self.on_touch_event and self.last_x is not None and self.last_y is not None:
                    self.on_touch_event({
                        "source": "device",
                        "action": "up",
                        "x": self.last_x,
                        "y": self.last_y,
                        "timestamp": timestamp,
                        "time_mono": time.monotonic(),
                    })

    def stop(self) -> None:
        self._stop_event.set()
        if self._proc:
            self._proc.terminate()


# ---------- Controller State ----------

class ControllerState:
    """Shared state for the web controller."""

    def __init__(self, serial: Optional[str]):
        self.serial = serial
        self.screen_width = 1080
        self.screen_height = 2400
        self.last_screenshot_b64 = ""
        self.last_screenshot_time = 0.0

        # Recording state
        self.recording = False
        self.record_start_mono = 0.0
        self.current_recording: Optional[UniversalRecording] = None
        self.pending_device_touches: List[Dict[str, Any]] = []

        # Element inspection cache
        self.cached_elements: List[ElementInfo] = []
        self.cached_elements_time = 0.0
        self.element_cache_ttl = 2.0  # seconds

        # Device touch events for the UI (last N events)
        self.device_touch_events: List[Dict[str, Any]] = []
        self.max_touch_events = 50

        # Playback state
        self.playing = False
        self.play_progress = 0
        self.play_total = 0

        # Lock for thread safety
        self.lock = threading.Lock()

    def detect_screen_size(self) -> None:
        try:
            prefix = adb_cmd(self.serial)
            output = run_adb_text(prefix + ["shell", "wm", "size"])
            match = re.search(r"(\d+)x(\d+)", output)
            if match:
                self.screen_width = int(match.group(1))
                self.screen_height = int(match.group(2))
        except (AdbError, Exception):
            pass

    def detect_touch_device_range(self) -> Tuple[int, int]:
        """Try to detect the touch input device coordinate range."""
        try:
            prefix = adb_cmd(self.serial)
            output = run_adb_text(prefix + ["shell", "getevent", "-lp"])
            # Parse ABS_MT_POSITION_X max and ABS_MT_POSITION_Y max
            x_max = y_max = 0
            for line in output.splitlines():
                if "ABS_MT_POSITION_X" in line:
                    m = re.search(r"max\s+(\d+)", line)
                    if m:
                        x_max = int(m.group(1))
                elif "ABS_MT_POSITION_Y" in line:
                    m = re.search(r"max\s+(\d+)", line)
                    if m:
                        y_max = int(m.group(1))
            return (x_max or self.screen_width, y_max or self.screen_height)
        except (AdbError, Exception):
            return (self.screen_width, self.screen_height)


# ---------- Screenshot capture thread ----------

class ScreenshotThread(threading.Thread):
    """Periodically capture screenshots for live view."""

    def __init__(self, state: ControllerState, interval: float = 0.5):
        super().__init__(daemon=True)
        self.state = state
        self.interval = interval
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                prefix = adb_cmd(self.state.serial)
                result = subprocess.run(
                    prefix + ["exec-out", "screencap", "-p"],
                    capture_output=True, timeout=10,
                )
                if result.returncode == 0 and result.stdout:
                    b64 = base64.b64encode(result.stdout).decode("ascii")
                    with self.state.lock:
                        self.state.last_screenshot_b64 = b64
                        self.state.last_screenshot_time = time.monotonic()
            except Exception:
                pass
            self._stop_event.wait(self.interval)

    def stop(self) -> None:
        self._stop_event.set()


# ---------- Playback thread ----------

class PlaybackThread(threading.Thread):
    """Play back a recording with accurate timing."""

    def __init__(self, state: ControllerState, recording: UniversalRecording):
        super().__init__(daemon=True)
        self.state = state
        self.recording = recording
        self._stop_event = threading.Event()

    def run(self) -> None:
        frames = self.recording.get_enabled_frames()
        speed = self.recording.settings.speed or 1.0
        self.state.playing = True
        self.state.play_total = len(frames)

        loop_count = self.recording.settings.loop_count or 1
        loop_forever = self.recording.settings.loop and loop_count == 0
        current_loop = 0

        while not self._stop_event.is_set():
            current_loop += 1
            if not loop_forever and current_loop > loop_count:
                break

            prev_t = 0
            cmd_overhead = 0.0  # Track adb command time to compensate
            for idx, frame in enumerate(frames):
                if self._stop_event.is_set():
                    break

                self.state.play_progress = idx + 1

                # Calculate delay and subtract previous command overhead
                delay_ms = frame.t - prev_t
                if delay_ms > 0:
                    actual_delay = delay_ms / 1000.0 / speed
                    adjusted_delay = max(0.0, actual_delay - cmd_overhead)
                    if adjusted_delay > 0:
                        self._stop_event.wait(adjusted_delay)
                        if self._stop_event.is_set():
                            break

                prev_t = frame.t
                cmd_start = time.monotonic()
                self._execute_frame(frame)
                cmd_overhead = time.monotonic() - cmd_start

            if not self._stop_event.is_set() and self.recording.settings.loop:
                delay = self.recording.settings.loop_delay
                if delay > 0:
                    self._stop_event.wait(delay)

        self.state.playing = False
        self.state.play_progress = 0

    def _execute_frame(self, frame: Frame) -> None:
        prefix = adb_cmd(self.state.serial)
        try:
            if frame.type == "touch":
                if frame.action == "down":
                    subprocess.run(
                        prefix + ["shell", "input", "tap", str(frame.x), str(frame.y)],
                        capture_output=True, timeout=10,
                    )
            elif frame.type == "gesture":
                if frame.action in ("tap", "long_press"):
                    duration = frame.duration_ms if frame.action == "long_press" else 0
                    if duration > 0:
                        subprocess.run(
                            prefix + ["shell", "input", "swipe",
                                       str(frame.x), str(frame.y),
                                       str(frame.x), str(frame.y),
                                       str(duration)],
                            capture_output=True, timeout=10,
                        )
                    else:
                        subprocess.run(
                            prefix + ["shell", "input", "tap", str(frame.x), str(frame.y)],
                            capture_output=True, timeout=10,
                        )
                elif frame.action == "swipe":
                    subprocess.run(
                        prefix + ["shell", "input", "swipe",
                                   str(frame.x), str(frame.y),
                                   str(frame.x2), str(frame.y2),
                                   str(frame.duration_ms or 300)],
                        capture_output=True, timeout=10,
                    )
            elif frame.type == "element":
                # Resolve element to coordinates
                try:
                    xml = get_ui_xml(self.state.serial)
                    elements = parse_all_elements(xml)
                    from element_interactor import find_element
                    el = find_element(elements, resource_id=frame.resource_id, text=frame.text)
                    if el:
                        subprocess.run(
                            prefix + ["shell", "input", "tap", str(el.center[0]), str(el.center[1])],
                            capture_output=True, timeout=10,
                        )
                except Exception:
                    pass
            elif frame.type == "key":
                subprocess.run(
                    prefix + ["shell", "input", "keyevent", str(frame.keycode)],
                    capture_output=True, timeout=10,
                )
            elif frame.type == "wait":
                self._stop_event.wait(frame.duration_ms / 1000.0)
            elif frame.type == "app":
                if frame.action == "open":
                    subprocess.run(
                        prefix + ["shell", "monkey", "-p", frame.package,
                                   "-c", "android.intent.category.LAUNCHER", "1"],
                        capture_output=True, timeout=10,
                    )
                elif frame.action == "close":
                    subprocess.run(
                        prefix + ["shell", "am", "force-stop", frame.package],
                        capture_output=True, timeout=10,
                    )
        except Exception:
            pass

    def stop(self) -> None:
        self._stop_event.set()


# ---------- Web Handler ----------

CONTROLLER_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Android Web Controller</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Segoe UI', system-ui, sans-serif; background: #0d1117; color: #e1e4e8; overflow-x: hidden; }
.header { background: #161b22; padding: 12px 20px; border-bottom: 1px solid #30363d; display: flex; justify-content: space-between; align-items: center; }
.header h1 { font-size: 16px; font-weight: 600; }
.header-right { display: flex; align-items: center; gap: 12px; }
.status-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.status-dot.connected { background: #3fb950; }
.status-dot.disconnected { background: #f85149; }
.main-layout { display: flex; height: calc(100vh - 50px); }
.screen-panel { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 16px; position: relative; }
.screen-container { position: relative; display: inline-block; cursor: crosshair; user-select: none; }
.screen-container img { max-height: calc(100vh - 120px); max-width: 100%; border-radius: 8px; border: 2px solid #30363d; display: block; }
.screen-container .no-screen { width: 360px; height: 640px; display: flex; align-items: center; justify-content: center; border: 2px dashed #30363d; border-radius: 8px; color: #8b949e; font-size: 14px; }
.click-marker { position: absolute; width: 30px; height: 30px; border-radius: 50%; border: 3px solid #58a6ff; background: rgba(88,166,255,0.2); transform: translate(-50%, -50%); pointer-events: none; animation: click-fade 0.8s ease-out forwards; z-index: 10; }
.click-marker.device { border-color: #3fb950; background: rgba(63,185,80,0.2); }
@keyframes click-fade { 0% { opacity: 1; transform: translate(-50%,-50%) scale(1); } 100% { opacity: 0; transform: translate(-50%,-50%) scale(2); } }
.side-panel { width: 340px; background: #161b22; border-left: 1px solid #30363d; display: flex; flex-direction: column; overflow-y: auto; }
.panel-section { padding: 14px; border-bottom: 1px solid #21262d; }
.panel-section h3 { font-size: 12px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 10px; }
.btn { padding: 6px 16px; border: none; border-radius: 6px; font-size: 13px; cursor: pointer; font-weight: 600; transition: background 0.15s; }
.btn-record { background: #da3633; color: white; }
.btn-record:hover { background: #f85149; }
.btn-record.active { background: #f85149; animation: pulse-record 1.5s infinite; }
@keyframes pulse-record { 0%,100% { box-shadow: 0 0 0 0 rgba(248,81,73,0.4); } 50% { box-shadow: 0 0 0 8px rgba(248,81,73,0); } }
.btn-play { background: #238636; color: white; }
.btn-play:hover { background: #2ea043; }
.btn-stop { background: #6e7681; color: white; }
.btn-stop:hover { background: #8b949e; }
.btn-save { background: #1f6feb; color: white; }
.btn-save:hover { background: #388bfd; }
.btn-sm { padding: 4px 10px; font-size: 12px; }
.btn-group { display: flex; gap: 8px; flex-wrap: wrap; }
.recording-list { max-height: 300px; overflow-y: auto; font-family: 'Consolas', monospace; font-size: 12px; }
.recording-item { padding: 4px 8px; border-bottom: 1px solid #21262d; display: flex; justify-content: space-between; }
.recording-item .ri-action { color: #bc8cff; }
.recording-item .ri-coord { color: #58a6ff; }
.recording-item .ri-time { color: #8b949e; font-size: 11px; }
.recording-item .ri-source { font-size: 10px; padding: 1px 4px; border-radius: 3px; }
.recording-item .ri-source.pc { background: #1f3d5a; color: #58a6ff; }
.recording-item .ri-source.device { background: #1f3d2a; color: #3fb950; }
.element-tooltip { position: absolute; background: #1c2128; border: 1px solid #444c56; border-radius: 8px; padding: 10px 14px; font-size: 12px; color: #e1e4e8; pointer-events: none; z-index: 100; max-width: 400px; white-space: pre-wrap; box-shadow: 0 4px 16px rgba(0,0,0,0.5); display: none; }
.element-tooltip .et-header { color: #58a6ff; font-weight: 600; margin-bottom: 4px; }
.element-tooltip .et-row { margin: 2px 0; }
.element-tooltip .et-label { color: #8b949e; }
.element-tooltip .et-value { color: #e1e4e8; }
.element-tooltip .et-text { color: #3fb950; }
.element-tooltip .et-bounds { color: #d29922; font-size: 11px; }
.element-highlight { position: absolute; border: 2px solid rgba(88,166,255,0.6); background: rgba(88,166,255,0.1); pointer-events: none; z-index: 5; border-radius: 2px; }
.info-bar { position: absolute; bottom: 8px; left: 50%; transform: translateX(-50%); background: rgba(22,27,34,0.9); border: 1px solid #30363d; border-radius: 6px; padding: 4px 12px; font-size: 11px; color: #8b949e; z-index: 20; white-space: nowrap; }
.device-touch-indicator { position: absolute; width: 24px; height: 24px; border-radius: 50%; border: 2px solid #3fb950; background: rgba(63,185,80,0.15); transform: translate(-50%, -50%); pointer-events: none; z-index: 8; transition: opacity 0.3s; }
.play-progress { margin-top: 8px; font-size: 12px; color: #8b949e; }
.play-progress .pp-bar { width: 100%; height: 4px; background: #21262d; border-radius: 2px; margin-top: 4px; }
.play-progress .pp-fill { height: 100%; background: #238636; border-radius: 2px; transition: width 0.3s; }
.stat-row { display: flex; justify-content: space-between; align-items: center; margin: 4px 0; font-size: 12px; }
.stat-label { color: #8b949e; }
.stat-value { color: #e1e4e8; font-weight: 600; }
.ctrl-hint { position: absolute; top: 8px; left: 50%; transform: translateX(-50%); background: rgba(88,166,255,0.15); border: 1px solid rgba(88,166,255,0.3); border-radius: 6px; padding: 4px 12px; font-size: 11px; color: #58a6ff; z-index: 20; display: none; }
</style>
</head>
<body>
<div class="header">
    <h1>Android Web Controller</h1>
    <div class="header-right">
        <span class="status-dot" id="statusDot"></span>
        <span id="statusText" style="font-size:13px;color:#8b949e;">Connecting...</span>
        <span id="screenSize" style="font-size:12px;color:#6e7681;"></span>
    </div>
</div>
<div class="main-layout">
    <div class="screen-panel">
        <div class="screen-container" id="screenContainer">
            <div class="no-screen" id="noScreen">Waiting for device screenshot...</div>
            <img id="screenImg" style="display:none;" alt="device screen">
            <div class="ctrl-hint" id="ctrlHint">Ctrl+Hover: Element Inspector Mode</div>
        </div>
        <div class="info-bar" id="infoBar">Click on screen to tap | Hold Ctrl+hover for element info</div>
    </div>
    <div class="side-panel">
        <div class="panel-section">
            <h3>Controls</h3>
            <div class="btn-group">
                <button class="btn btn-record" id="btnRecord" onclick="toggleRecord()">Record</button>
                <button class="btn btn-play" id="btnPlay" onclick="playRecording()">Play</button>
                <button class="btn btn-stop" id="btnStop" onclick="stopPlayback()">Stop</button>
                <button class="btn btn-save" id="btnSave" onclick="saveRecording()">Save</button>
            </div>
            <div style="margin-top: 8px;">
                <label style="font-size: 12px; color: #8b949e; display: flex; align-items: center; gap: 6px;">
                    <input type="checkbox" id="chkDeviceTouch" checked> Record device touches
                </label>
            </div>
            <div class="play-progress" id="playProgress" style="display:none;">
                <span id="playText">Playing: 0/0</span>
                <div class="pp-bar"><div class="pp-fill" id="playFill"></div></div>
            </div>
        </div>
        <div class="panel-section">
            <h3>Quick Actions</h3>
            <div class="btn-group">
                <button class="btn btn-sm btn-stop" onclick="sendKey(4)">Back</button>
                <button class="btn btn-sm btn-stop" onclick="sendKey(3)">Home</button>
                <button class="btn btn-sm btn-stop" onclick="sendKey(187)">Recents</button>
                <button class="btn btn-sm btn-stop" onclick="sendKey(26)">Power</button>
            </div>
        </div>
        <div class="panel-section">
            <h3>Recording Info</h3>
            <div class="stat-row"><span class="stat-label">Frames</span><span class="stat-value" id="frameCount">0</span></div>
            <div class="stat-row"><span class="stat-label">Duration</span><span class="stat-value" id="recDuration">0.0s</span></div>
            <div class="stat-row"><span class="stat-label">Source</span><span class="stat-value" id="recSources">-</span></div>
        </div>
        <div class="panel-section" style="flex:1;">
            <h3>Recorded Frames</h3>
            <div class="recording-list" id="recordingList"></div>
        </div>
    </div>
</div>

<div class="element-tooltip" id="elementTooltip"></div>

<script>
const screenImg = document.getElementById('screenImg');
const noScreen = document.getElementById('noScreen');
const screenContainer = document.getElementById('screenContainer');
const tooltip = document.getElementById('elementTooltip');
const ctrlHint = document.getElementById('ctrlHint');
const infoBar = document.getElementById('infoBar');

let isRecording = false;
let isPlaying = false;
let ctrlDown = false;
let screenW = 1080, screenH = 2400;
let lastElementsData = null;
let elementHighlights = [];
let pollTimer = null;
let recordedFrames = [];
let pcCount = 0, deviceCount = 0;

// Screenshot polling
function pollScreenshot() {
    fetch('/api/screenshot')
        .then(r => r.json())
        .then(data => {
            if (data.b64) {
                screenImg.src = 'data:image/png;base64,' + data.b64;
                screenImg.style.display = 'block';
                noScreen.style.display = 'none';
                document.getElementById('statusDot').className = 'status-dot connected';
                document.getElementById('statusText').textContent = 'Connected';
            }
            if (data.screen_width) {
                screenW = data.screen_width;
                screenH = data.screen_height;
                document.getElementById('screenSize').textContent = screenW + 'x' + screenH;
            }
        })
        .catch(() => {
            document.getElementById('statusDot').className = 'status-dot disconnected';
            document.getElementById('statusText').textContent = 'Disconnected';
        });
}

function pollState() {
    fetch('/api/state')
        .then(r => r.json())
        .then(data => {
            // Update play state
            if (data.playing) {
                document.getElementById('playProgress').style.display = 'block';
                document.getElementById('playText').textContent = 'Playing: ' + data.play_progress + '/' + data.play_total;
                const pct = data.play_total > 0 ? (data.play_progress / data.play_total * 100) : 0;
                document.getElementById('playFill').style.width = pct + '%';
                isPlaying = true;
            } else {
                document.getElementById('playProgress').style.display = 'none';
                isPlaying = false;
            }

            // Update device touches
            if (data.device_touches && data.device_touches.length > 0) {
                showDeviceTouches(data.device_touches);
            }

            // Update recording frames from server
            if (data.frames) {
                recordedFrames = data.frames;
                updateRecordingList();
            }
        })
        .catch(() => {});
}

// Convert page click coords to device coords
function pageToDevice(pageX, pageY) {
    const rect = screenImg.getBoundingClientRect();
    const relX = (pageX - rect.left) / rect.width;
    const relY = (pageY - rect.top) / rect.height;
    const devX = Math.round(relX * screenW);
    const devY = Math.round(relY * screenH);
    return { x: devX, y: devY, relX, relY };
}

// Convert device coords to page position (relative to screenContainer)
function deviceToPage(devX, devY) {
    const rect = screenImg.getBoundingClientRect();
    const contRect = screenContainer.getBoundingClientRect();
    const px = (devX / screenW) * rect.width + (rect.left - contRect.left);
    const py = (devY / screenH) * rect.height + (rect.top - contRect.top);
    return { x: px, y: py };
}

// Show click marker animation
function showClickMarker(pageX, pageY, isDevice) {
    const rect = screenContainer.getBoundingClientRect();
    const marker = document.createElement('div');
    marker.className = 'click-marker' + (isDevice ? ' device' : '');
    marker.style.left = (pageX - rect.left) + 'px';
    marker.style.top = (pageY - rect.top) + 'px';
    screenContainer.appendChild(marker);
    setTimeout(() => marker.remove(), 800);
}

// Show device touch indicators
function showDeviceTouches(touches) {
    // Remove old indicators
    document.querySelectorAll('.device-touch-indicator').forEach(el => el.remove());

    for (const t of touches) {
        if (t.action === 'up') continue;
        const pos = deviceToPage(t.x, t.y);
        const indicator = document.createElement('div');
        indicator.className = 'device-touch-indicator';
        indicator.style.left = pos.x + 'px';
        indicator.style.top = pos.y + 'px';
        screenContainer.appendChild(indicator);

        // Show marker animation for new downs
        if (t.action === 'down') {
            const contRect = screenContainer.getBoundingClientRect();
            showClickMarker(pos.x + contRect.left, pos.y + contRect.top, true);
        }
    }
}

// Handle click on screen
screenContainer.addEventListener('click', function(e) {
    if (e.target !== screenImg) return;
    if (ctrlDown) return; // Don't tap in inspect mode

    const coords = pageToDevice(e.clientX, e.clientY);
    if (coords.x < 0 || coords.x > screenW || coords.y < 0 || coords.y > screenH) return;

    showClickMarker(e.clientX, e.clientY, false);

    // Send tap to server
    fetch('/api/tap', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            x: coords.x,
            y: coords.y,
            timestamp: Date.now(),
        })
    });

    infoBar.textContent = 'Tapped: (' + coords.x + ', ' + coords.y + ')';
});

// Ctrl key tracking
document.addEventListener('keydown', function(e) {
    if (e.key === 'Control') {
        ctrlDown = true;
        ctrlHint.style.display = 'block';
        screenContainer.style.cursor = 'help';
    }
});
document.addEventListener('keyup', function(e) {
    if (e.key === 'Control') {
        ctrlDown = false;
        ctrlHint.style.display = 'none';
        tooltip.style.display = 'none';
        screenContainer.style.cursor = 'crosshair';
        clearHighlights();
    }
});

// Ctrl+hover element inspection
let inspectThrottle = null;
screenContainer.addEventListener('mousemove', function(e) {
    if (!ctrlDown || e.target !== screenImg) {
        if (!ctrlDown) {
            tooltip.style.display = 'none';
            clearHighlights();
        }
        return;
    }

    const coords = pageToDevice(e.clientX, e.clientY);

    // Throttle requests
    if (inspectThrottle) return;
    inspectThrottle = setTimeout(() => { inspectThrottle = null; }, 200);

    fetch('/api/inspect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ x: coords.x, y: coords.y })
    })
    .then(r => r.json())
    .then(data => {
        if (!ctrlDown) return;
        showElementTooltip(e.clientX, e.clientY, data);
        showElementHighlights(data.elements || []);
    })
    .catch(() => {});
});

function showElementTooltip(px, py, data) {
    if (!data.elements || data.elements.length === 0) {
        tooltip.style.display = 'none';
        return;
    }

    let html = '<div class="et-header">Elements at (' + data.x + ', ' + data.y + ')</div>';

    for (const el of data.elements) {
        html += '<div style="margin-top:6px;padding-top:4px;border-top:1px solid #30363d;">';
        html += '<div class="et-row"><span class="et-label">class: </span><span class="et-value">' + escHtml(el.cls || '') + '</span></div>';
        if (el.resource_id) {
            html += '<div class="et-row"><span class="et-label">id: </span><span class="et-value">' + escHtml(el.resource_id) + '</span></div>';
        }
        if (el.text) {
            html += '<div class="et-row"><span class="et-label">text: </span><span class="et-text">"' + escHtml(el.text) + '"</span></div>';
        }
        if (el.content_desc) {
            html += '<div class="et-row"><span class="et-label">desc: </span><span class="et-text">"' + escHtml(el.content_desc) + '"</span></div>';
        }
        html += '<div class="et-row"><span class="et-bounds">[' + el.bounds.join(',') + '] center=(' + el.center[0] + ',' + el.center[1] + ')</span></div>';
        let flags = [];
        if (el.clickable) flags.push('clickable');
        if (el.enabled) flags.push('enabled');
        if (el.focusable) flags.push('focusable');
        if (flags.length) {
            html += '<div class="et-row" style="font-size:11px;color:#6e7681;">' + flags.join(' | ') + '</div>';
        }
        html += '</div>';
    }

    // Show all nearby text
    if (data.nearby_texts && data.nearby_texts.length > 0) {
        html += '<div style="margin-top:8px;padding-top:6px;border-top:1px solid #444c56;">';
        html += '<div class="et-label" style="margin-bottom:4px;">Nearby text:</div>';
        for (const t of data.nearby_texts) {
            html += '<div class="et-text" style="font-size:11px;">- "' + escHtml(t) + '"</div>';
        }
        html += '</div>';
    }

    tooltip.innerHTML = html;
    tooltip.style.display = 'block';

    // Position tooltip
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    let tx = px + 16, ty = py + 16;
    if (tx + 400 > vw) tx = px - 420;
    if (ty + tooltip.offsetHeight > vh) ty = vh - tooltip.offsetHeight - 8;
    tooltip.style.left = tx + 'px';
    tooltip.style.top = ty + 'px';
}

function showElementHighlights(elements) {
    clearHighlights();
    const rect = screenImg.getBoundingClientRect();
    const contRect = screenContainer.getBoundingClientRect();
    const scaleX = rect.width / screenW;
    const scaleY = rect.height / screenH;
    const offsetX = rect.left - contRect.left;
    const offsetY = rect.top - contRect.top;

    for (const el of elements) {
        const b = el.bounds;
        if (!b || b.length < 4) continue;
        const div = document.createElement('div');
        div.className = 'element-highlight';
        div.style.left = (b[0] * scaleX + offsetX) + 'px';
        div.style.top = (b[1] * scaleY + offsetY) + 'px';
        div.style.width = ((b[2] - b[0]) * scaleX) + 'px';
        div.style.height = ((b[3] - b[1]) * scaleY) + 'px';
        screenContainer.appendChild(div);
        elementHighlights.push(div);
    }
}

function clearHighlights() {
    elementHighlights.forEach(el => el.remove());
    elementHighlights = [];
}

// Recording controls
function toggleRecord() {
    isRecording = !isRecording;
    const btn = document.getElementById('btnRecord');

    if (isRecording) {
        btn.textContent = 'Stop Rec';
        btn.classList.add('active');
        fetch('/api/record/start', { method: 'POST' });
        pcCount = 0;
        deviceCount = 0;
    } else {
        btn.textContent = 'Record';
        btn.classList.remove('active');
        fetch('/api/record/stop', { method: 'POST' })
            .then(r => r.json())
            .then(data => {
                if (data.frames) {
                    recordedFrames = data.frames;
                    updateRecordingList();
                }
            });
    }
}

function playRecording() {
    fetch('/api/play', { method: 'POST' });
}

function stopPlayback() {
    fetch('/api/play/stop', { method: 'POST' });
}

function saveRecording() {
    const name = prompt('Recording name:', 'recording-' + new Date().toISOString().slice(0,16).replace(/[T:]/g, '-'));
    if (!name) return;
    fetch('/api/record/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name })
    })
    .then(r => r.json())
    .then(data => {
        if (data.path) {
            infoBar.textContent = 'Saved: ' + data.path;
        }
    });
}

function sendKey(keycode) {
    fetch('/api/key', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ keycode: keycode, timestamp: Date.now() })
    });
}

function updateRecordingList() {
    const list = document.getElementById('recordingList');
    pcCount = 0;
    deviceCount = 0;

    let html = '';
    const frames = recordedFrames;
    for (const f of frames) {
        const src = f.source || 'pc';
        if (src === 'pc') pcCount++;
        else deviceCount++;

        const tSec = (f.t / 1000).toFixed(1);
        let desc = '';
        if (f.type === 'gesture' || f.type === 'touch') {
            desc = f.action + ' (' + f.x + ',' + f.y + ')';
        } else if (f.type === 'key') {
            desc = 'key ' + (f.key_name || f.keycode);
        } else if (f.type === 'wait') {
            desc = 'wait ' + f.duration_ms + 'ms';
        } else {
            desc = f.type + ' ' + (f.action || '');
        }

        html += '<div class="recording-item">';
        html += '<span><span class="ri-action">#' + f.id + '</span> ';
        html += '<span class="ri-coord">' + desc + '</span></span>';
        html += '<span><span class="ri-source ' + src + '">' + src.toUpperCase() + '</span> ';
        html += '<span class="ri-time">' + tSec + 's</span></span>';
        html += '</div>';
    }

    list.innerHTML = html;
    list.scrollTop = list.scrollHeight;

    document.getElementById('frameCount').textContent = frames.length;
    if (frames.length > 0) {
        const dur = (frames[frames.length - 1].t / 1000).toFixed(1);
        document.getElementById('recDuration').textContent = dur + 's';
    }
    document.getElementById('recSources').textContent = 'PC:' + pcCount + ' Device:' + deviceCount;
}

function escHtml(s) {
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// Polling intervals
setInterval(pollScreenshot, 500);
setInterval(pollState, 800);
pollScreenshot();
pollState();
</script>
</body>
</html>"""


class WebControllerHandler(BaseHTTPRequestHandler):
    state: ControllerState = None  # type: ignore[assignment]
    playback_thread: Optional[PlaybackThread] = None
    device_monitor: Optional[DeviceTouchMonitor] = None
    touch_range: Tuple[int, int] = (0, 0)

    def log_message(self, fmt: str, *args: Any) -> None:
        pass  # suppress default logging

    def do_GET(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path in ("/", "/index.html"):
            self._respond_html(CONTROLLER_HTML)

        elif parsed.path == "/api/screenshot":
            with self.state.lock:
                b64 = self.state.last_screenshot_b64
            self._respond_json({
                "b64": b64,
                "screen_width": self.state.screen_width,
                "screen_height": self.state.screen_height,
            })

        elif parsed.path == "/api/state":
            with self.state.lock:
                device_touches = list(self.state.device_touch_events[-10:])
                frames = []
                if self.state.current_recording:
                    frames = [f.to_dict() for f in self.state.current_recording.frames]
                    # Add source info
                    for fd in frames:
                        fd.setdefault("source", "pc")

            self._respond_json({
                "recording": self.state.recording,
                "playing": self.state.playing,
                "play_progress": self.state.play_progress,
                "play_total": self.state.play_total,
                "device_touches": device_touches,
                "frames": frames,
            })

        else:
            self._respond(404, b"Not Found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        body = self._read_body()

        if parsed.path == "/api/tap":
            self._handle_tap(body)

        elif parsed.path == "/api/key":
            self._handle_key(body)

        elif parsed.path == "/api/inspect":
            self._handle_inspect(body)

        elif parsed.path == "/api/record/start":
            self._handle_record_start()

        elif parsed.path == "/api/record/stop":
            self._handle_record_stop()

        elif parsed.path == "/api/record/save":
            self._handle_record_save(body)

        elif parsed.path == "/api/play":
            self._handle_play()

        elif parsed.path == "/api/play/stop":
            self._handle_play_stop()

        else:
            self._respond(404, b"Not Found")

    def _handle_tap(self, body: Dict) -> None:
        x = int(body.get("x", 0))
        y = int(body.get("y", 0))

        # Send tap to device
        prefix = adb_cmd(self.state.serial)
        try:
            subprocess.run(
                prefix + ["shell", "input", "tap", str(x), str(y)],
                capture_output=True, timeout=10,
            )
        except Exception:
            pass

        # Add to recording if recording
        if self.state.recording and self.state.current_recording:
            now = time.monotonic()
            t_ms = int((now - self.state.record_start_mono) * 1000)

            frame = Frame(
                id=self.state.current_recording.next_id(),
                t=t_ms,
                type="gesture",
                action="tap",
                x=x, y=y,
                note="pc",  # use note field to track source temporarily
            )

            # Try to annotate with element info
            try:
                self._annotate_frame_element(frame, x, y)
            except Exception:
                pass

            self.state.current_recording.add_frame(frame)

        self._respond_json({"ok": True, "x": x, "y": y})

    def _handle_key(self, body: Dict) -> None:
        keycode = int(body.get("keycode", 4))
        prefix = adb_cmd(self.state.serial)
        try:
            subprocess.run(
                prefix + ["shell", "input", "keyevent", str(keycode)],
                capture_output=True, timeout=10,
            )
        except Exception:
            pass

        if self.state.recording and self.state.current_recording:
            now = time.monotonic()
            t_ms = int((now - self.state.record_start_mono) * 1000)
            frame = Frame(
                id=self.state.current_recording.next_id(),
                t=t_ms,
                type="key",
                keycode=keycode,
                key_name=KEY_NAMES.get(keycode, ""),
                note="pc",
            )
            self.state.current_recording.add_frame(frame)

        self._respond_json({"ok": True})

    def _handle_inspect(self, body: Dict) -> None:
        x = int(body.get("x", 0))
        y = int(body.get("y", 0))

        now = time.monotonic()

        # Use cache if still valid
        if now - self.state.cached_elements_time > self.state.element_cache_ttl:
            try:
                xml = get_ui_xml(self.state.serial)
                self.state.cached_elements = parse_all_elements(xml)
                self.state.cached_elements_time = now
            except Exception:
                pass

        elements = self.state.cached_elements

        # Find all elements containing the point
        matching = []
        nearby_texts = set()
        nearby_radius = 150  # pixels

        for el in elements:
            b = el.bounds
            # Elements containing the point
            if b[0] <= x <= b[2] and b[1] <= y <= b[3]:
                if el.resource_id or el.text or el.clickable or el.content_desc:
                    matching.append({
                        "cls": el.cls,
                        "resource_id": el.resource_id,
                        "text": el.text,
                        "content_desc": el.content_desc,
                        "bounds": list(el.bounds),
                        "center": list(el.center),
                        "clickable": el.clickable,
                        "enabled": el.enabled,
                        "focusable": el.focusable,
                    })

            # Find nearby text
            cx, cy = el.center
            dist = ((cx - x) ** 2 + (cy - y) ** 2) ** 0.5
            if dist < nearby_radius and el.text:
                nearby_texts.add(el.text)

        # Sort by area (smallest = most specific first)
        matching.sort(key=lambda e: (e["bounds"][2] - e["bounds"][0]) * (e["bounds"][3] - e["bounds"][1]))

        self._respond_json({
            "x": x, "y": y,
            "elements": matching[:10],
            "nearby_texts": sorted(nearby_texts)[:20],
        })

    def _handle_record_start(self) -> None:
        self.state.recording = True
        self.state.record_start_mono = time.monotonic()
        self.state.current_recording = UniversalRecording()
        self.state.current_recording.meta.created = datetime.now().isoformat()
        self.state.current_recording.meta.screen_width = self.state.screen_width
        self.state.current_recording.meta.screen_height = self.state.screen_height
        self.state.current_recording.meta.recording_mode = "mixed"
        self._respond_json({"ok": True, "recording": True})

    def _handle_record_stop(self) -> None:
        self.state.recording = False
        frames = []
        if self.state.current_recording:
            # Set total duration
            if self.state.current_recording.frames:
                self.state.current_recording.meta.total_duration_ms = \
                    self.state.current_recording.total_duration()
            frames = [f.to_dict() for f in self.state.current_recording.frames]
            for fd in frames:
                source = "device" if fd.get("note") == "device" else "pc"
                fd["source"] = source

        self._respond_json({"ok": True, "recording": False, "frames": frames})

    def _handle_record_save(self, body: Dict) -> None:
        name = body.get("name", "recording")
        if not self.state.current_recording:
            self._respond_json({"ok": False, "error": "No recording"})
            return

        self.state.current_recording.meta.name = name
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"{timestamp}-{name}.urf.json"
        save_dir = Path("/work/recordings")
        save_dir.mkdir(parents=True, exist_ok=True)
        save_path = save_dir / filename

        self.state.current_recording.save(save_path)
        self._respond_json({"ok": True, "path": str(save_path)})

    def _handle_play(self) -> None:
        if self.state.playing:
            self._respond_json({"ok": False, "error": "Already playing"})
            return

        if not self.state.current_recording or not self.state.current_recording.frames:
            self._respond_json({"ok": False, "error": "No recording to play"})
            return

        # Stop any existing playback thread
        cls = type(self)
        if cls.playback_thread and cls.playback_thread.is_alive():
            cls.playback_thread.stop()
            cls.playback_thread.join(timeout=5)

        cls.playback_thread = PlaybackThread(self.state, self.state.current_recording)
        cls.playback_thread.start()
        self._respond_json({"ok": True})

    def _handle_play_stop(self) -> None:
        cls = type(self)
        if cls.playback_thread and cls.playback_thread.is_alive():
            cls.playback_thread.stop()
        self.state.playing = False
        self._respond_json({"ok": True})

    def _annotate_frame_element(self, frame: Frame, x: int, y: int) -> None:
        """Try to find the UI element at (x,y) and annotate the frame."""
        now = time.monotonic()
        if now - self.state.cached_elements_time > self.state.element_cache_ttl:
            try:
                xml = get_ui_xml(self.state.serial)
                self.state.cached_elements = parse_all_elements(xml)
                self.state.cached_elements_time = now
            except Exception:
                return

        candidates = []
        for el in self.state.cached_elements:
            b = el.bounds
            if b[0] <= x <= b[2] and b[1] <= y <= b[3]:
                if el.resource_id or el.text or el.clickable:
                    candidates.append(el)

        if candidates:
            candidates.sort(key=lambda e: (e.bounds[2] - e.bounds[0]) * (e.bounds[3] - e.bounds[1]))
            best = candidates[0]
            frame.element = {
                "resource_id": best.resource_id,
                "text": best.text,
                "class": best.cls,
                "bounds": list(best.bounds),
            }

    # ---------- Helpers ----------

    def _read_body(self) -> Dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def _respond_html(self, html: str) -> None:
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _respond_json(self, obj: Any) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _respond(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ---------- Device touch callback ----------

def make_device_touch_callback(state: ControllerState):
    """Create a callback for DeviceTouchMonitor that adds touches to state."""
    # Detect touch coordinate range
    touch_x_max, touch_y_max = state.detect_touch_device_range()

    def on_touch(event: Dict[str, Any]) -> None:
        # Scale from getevent coords to screen coords
        raw_x = event["x"]
        raw_y = event["y"]
        if touch_x_max > 0 and touch_y_max > 0:
            scaled_x = int(raw_x * state.screen_width / touch_x_max)
            scaled_y = int(raw_y * state.screen_height / touch_y_max)
        else:
            scaled_x = raw_x
            scaled_y = raw_y

        event["x"] = scaled_x
        event["y"] = scaled_y

        with state.lock:
            state.device_touch_events.append(event)
            if len(state.device_touch_events) > state.max_touch_events:
                state.device_touch_events.pop(0)

            # Add to recording if recording
            if state.recording and state.current_recording and event["action"] == "down":
                t_ms = int((event["time_mono"] - state.record_start_mono) * 1000)
                frame = Frame(
                    id=state.current_recording.next_id(),
                    t=max(0, t_ms),
                    type="gesture",
                    action="tap",
                    x=scaled_x,
                    y=scaled_y,
                    note="device",
                )
                state.current_recording.add_frame(frame)

    return on_touch


# ---------- CLI ----------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Web-based Android device controller with recording"
    )
    parser.add_argument("--port", "-p", type=int, default=8080,
                        help="HTTP server port (default: 8080)")
    parser.add_argument("-s", "--serial", help="ADB device serial")
    parser.add_argument("--fps", type=float, default=2.0,
                        help="Screenshot capture rate in FPS (default: 2)")
    parser.add_argument("--no-device-touch", action="store_true",
                        help="Disable device touch monitoring")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    state = ControllerState(serial=args.serial)
    print("Detecting screen size...")
    state.detect_screen_size()
    print(f"Screen: {state.screen_width}x{state.screen_height}")

    # Start screenshot capture thread
    interval = 1.0 / max(0.1, args.fps)
    screenshot_thread = ScreenshotThread(state, interval=interval)
    screenshot_thread.start()
    print(f"Screenshot capture started ({args.fps} FPS)")

    # Start device touch monitor
    device_monitor = None
    if not args.no_device_touch:
        callback = make_device_touch_callback(state)
        device_monitor = DeviceTouchMonitor(args.serial, on_touch_event=callback)
        device_monitor.start()
        print("Device touch monitor started")

    # Start HTTP server
    WebControllerHandler.state = state
    WebControllerHandler.device_monitor = device_monitor

    server = HTTPServer(("0.0.0.0", args.port), WebControllerHandler)
    print(f"\nWeb Controller running at http://0.0.0.0:{args.port}")
    print("Press Ctrl+C to stop\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        screenshot_thread.stop()
        if device_monitor:
            device_monitor.stop()
        server.shutdown()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
