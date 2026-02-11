#!/usr/bin/env python3
"""
Debug Server - Web dashboard for live monitoring of automation flows.

Provides:
- Real-time status of running flow (current loop, step, errors)
- Latest screenshot display
- Error history with screenshots
- Pause/resume control
- JSON API for programmatic access

Usage:
    Started automatically by automation-engine.py --debug-port 8080
    Or standalone: debug-server.py --port 8080 --state-dir /work/debug-state
"""

from __future__ import annotations

import base64
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Optional


class DebugState:
    """Shared state protocol matching EngineState."""
    status: str
    current_loop: int
    current_step: int
    current_step_desc: str
    total_loops: int
    total_steps: int
    last_screenshot: str
    last_error: str
    last_error_screenshot: str
    history: list

    def to_dict(self) -> dict:
        ...


# The HTML dashboard - single-page app
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Android Automation Debug</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Segoe UI', system-ui, sans-serif; background: #0f1117; color: #e1e4e8; }
.header { background: #161b22; padding: 16px 24px; border-bottom: 1px solid #30363d; display: flex; justify-content: space-between; align-items: center; }
.header h1 { font-size: 18px; font-weight: 600; }
.status-badge { padding: 4px 12px; border-radius: 12px; font-size: 13px; font-weight: 600; text-transform: uppercase; }
.status-idle { background: #30363d; color: #8b949e; }
.status-running { background: #1f3d2a; color: #3fb950; }
.status-paused { background: #3d2f1f; color: #d29922; }
.status-error { background: #3d1f1f; color: #f85149; }
.container { max-width: 1200px; margin: 0 auto; padding: 20px; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-top: 20px; }
.card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; }
.card h2 { font-size: 14px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 12px; }
.metric { font-size: 28px; font-weight: 700; color: #58a6ff; }
.metric-label { font-size: 12px; color: #8b949e; margin-top: 4px; }
.progress-row { display: flex; gap: 24px; margin-top: 16px; }
.progress-item { flex: 1; }
.screenshot-container { text-align: center; }
.screenshot-container img { max-width: 100%; max-height: 500px; border-radius: 4px; border: 1px solid #30363d; }
.screenshot-container .no-image { color: #8b949e; padding: 40px; border: 2px dashed #30363d; border-radius: 8px; }
.log-container { max-height: 400px; overflow-y: auto; font-family: 'Consolas', monospace; font-size: 13px; }
.log-entry { padding: 4px 8px; border-bottom: 1px solid #21262d; }
.log-entry .ts { color: #8b949e; margin-right: 8px; }
.log-info { color: #58a6ff; }
.log-ok { color: #3fb950; }
.log-warn { color: #d29922; }
.log-error { color: #f85149; }
.log-step { color: #bc8cff; }
.controls { margin-top: 20px; display: flex; gap: 10px; }
.btn { padding: 8px 20px; border: none; border-radius: 6px; font-size: 14px; cursor: pointer; font-weight: 600; }
.btn-primary { background: #238636; color: white; }
.btn-primary:hover { background: #2ea043; }
.btn-danger { background: #da3633; color: white; }
.btn-danger:hover { background: #f85149; }
.btn-secondary { background: #30363d; color: #e1e4e8; }
.btn-secondary:hover { background: #3d444d; }
.error-panel { background: #1f1215; border: 1px solid #f8514933; border-radius: 8px; padding: 16px; margin-top: 20px; }
.error-panel h3 { color: #f85149; margin-bottom: 8px; }
.full-width { grid-column: 1 / -1; }
@media (max-width: 768px) { .grid { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<div class="header">
    <h1>Android Automation Monitor</h1>
    <span id="statusBadge" class="status-badge status-idle">IDLE</span>
</div>
<div class="container">
    <div class="grid">
        <div class="card">
            <h2>Progress</h2>
            <div class="progress-row">
                <div class="progress-item">
                    <div class="metric" id="loopCount">0</div>
                    <div class="metric-label">Current Loop</div>
                </div>
                <div class="progress-item">
                    <div class="metric" id="stepCount">0 / 0</div>
                    <div class="metric-label">Current Step</div>
                </div>
            </div>
            <div style="margin-top: 12px; color: #8b949e;" id="stepDesc">-</div>
        </div>

        <div class="card">
            <h2>Controls</h2>
            <div class="controls">
                <button class="btn btn-primary" onclick="doAction('resume')">Resume</button>
                <button class="btn btn-secondary" onclick="doAction('pause')">Pause</button>
                <button class="btn btn-danger" onclick="doAction('stop')">Stop</button>
            </div>
            <div style="margin-top: 16px;">
                <button class="btn btn-secondary" onclick="refreshNow()">Refresh Now</button>
                <label style="margin-left: 16px; font-size: 13px;">
                    <input type="checkbox" id="autoRefresh" checked> Auto-refresh (2s)
                </label>
            </div>
        </div>

        <div class="card">
            <h2>Latest Screenshot</h2>
            <div class="screenshot-container" id="screenshotContainer">
                <div class="no-image">No screenshot yet</div>
            </div>
        </div>

        <div class="card">
            <h2>Error Screenshot</h2>
            <div class="screenshot-container" id="errorScreenshotContainer">
                <div class="no-image">No errors</div>
            </div>
            <div id="errorMessage" style="margin-top: 8px; color: #f85149; font-size: 13px;"></div>
        </div>

        <div class="card full-width">
            <h2>Activity Log</h2>
            <div class="log-container" id="logContainer"></div>
        </div>
    </div>
</div>

<script>
let lastScreenshot = '';
let lastErrorScreenshot = '';

async function fetchState() {
    try {
        const resp = await fetch('/api/state');
        const data = await resp.json();
        updateUI(data);
    } catch(e) {
        console.error('Fetch error:', e);
    }
}

function updateUI(data) {
    // Status badge
    const badge = document.getElementById('statusBadge');
    badge.textContent = data.status.toUpperCase();
    badge.className = 'status-badge status-' + data.status;

    // Progress
    document.getElementById('loopCount').textContent = data.current_loop;
    document.getElementById('stepCount').textContent = data.current_step + ' / ' + data.total_steps;
    document.getElementById('stepDesc').textContent = data.current_step_desc || '-';

    // Screenshots
    if (data.last_screenshot && data.last_screenshot !== lastScreenshot) {
        lastScreenshot = data.last_screenshot;
        loadImage('screenshotContainer', data.last_screenshot);
    }
    if (data.last_error_screenshot && data.last_error_screenshot !== lastErrorScreenshot) {
        lastErrorScreenshot = data.last_error_screenshot;
        loadImage('errorScreenshotContainer', data.last_error_screenshot);
    }

    // Error
    document.getElementById('errorMessage').textContent = data.last_error || '';

    // Log
    const logEl = document.getElementById('logContainer');
    const entries = data.recent_history || [];
    logEl.innerHTML = entries.map(e => {
        const cls = 'log-' + (e.level || 'info').toLowerCase();
        return '<div class="log-entry"><span class="ts">' + e.ts + '</span><span class="' + cls + '">' + escapeHtml(e.msg) + '</span></div>';
    }).join('');
    logEl.scrollTop = logEl.scrollHeight;
}

async function loadImage(containerId, path) {
    try {
        const resp = await fetch('/api/screenshot?path=' + encodeURIComponent(path));
        if (resp.ok) {
            const blob = await resp.blob();
            const url = URL.createObjectURL(blob);
            document.getElementById(containerId).innerHTML = '<img src="' + url + '" alt="screenshot">';
        }
    } catch(e) {}
}

async function doAction(action) {
    await fetch('/api/action', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({action: action})
    });
    setTimeout(fetchState, 500);
}

function refreshNow() { fetchState(); }
function escapeHtml(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

// Auto-refresh
setInterval(() => {
    if (document.getElementById('autoRefresh').checked) fetchState();
}, 2000);

// Initial load
fetchState();
</script>
</body>
</html>"""


class DebugHandler(BaseHTTPRequestHandler):
    engine_state: Any = None
    on_action: Any = None

    def log_message(self, format: str, *args: Any) -> None:
        pass  # Suppress default HTTP logging

    def do_GET(self) -> None:
        if self.path == "/" or self.path == "/index.html":
            self._respond_html(DASHBOARD_HTML)
        elif self.path == "/api/state":
            self._respond_json(self.engine_state.to_dict() if self.engine_state else {})
        elif self.path.startswith("/api/screenshot"):
            self._serve_screenshot()
        else:
            self._respond(404, b"Not Found")

    def do_POST(self) -> None:
        if self.path == "/api/action":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            action = body.get("action", "")

            if action == "resume" and self.engine_state:
                self.engine_state.status = "running"
            elif action == "pause" and self.engine_state:
                self.engine_state.status = "paused"
            elif action == "stop" and self.engine_state:
                self.engine_state.status = "stopped"

            if self.on_action:
                self.on_action(action)

            self._respond_json({"ok": True, "action": action})
        else:
            self._respond(404, b"Not Found")

    def _serve_screenshot(self) -> None:
        # Parse path from query param
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        img_path = params.get("path", [""])[0]

        if not img_path:
            self._respond(400, b"Missing path param")
            return

        p = Path(img_path)
        if p.exists() and p.suffix.lower() == ".png":
            data = p.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)
        else:
            self._respond(404, b"Screenshot not found")

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


class DebugServer:
    def __init__(self, state: Any, port: int = 8080, on_action: Any = None):
        self.state = state
        self.port = port
        self.on_action = on_action
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        DebugHandler.engine_state = self.state
        DebugHandler.on_action = self.on_action
        self._server = HTTPServer(("0.0.0.0", self.port), DebugHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server = None
