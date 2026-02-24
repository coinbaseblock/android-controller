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
                <a href="/logs" target="_blank" class="btn btn-secondary" style="text-decoration:none;display:inline-block">View JSON Log</a>
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


# Live log viewer - fetches data from /api/playback-log with auto-refresh
LOG_VIEWER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Playback Log Viewer</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0f1117;color:#e1e4e8;line-height:1.5}
.header{background:#161b22;padding:16px 24px;border-bottom:1px solid #30363d;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
.header h1{font-size:18px;font-weight:600}
.rec-name{color:#58a6ff;font-size:14px}
.toolbar{display:flex;gap:8px;align-items:center}
.toolbar label{font-size:13px;color:#8b949e}
.container{max-width:1400px;margin:0 auto;padding:20px}
.info-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:24px}
.info-card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px 16px}
.info-label{font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.5px}
.info-value{font-size:16px;font-weight:600;color:#c9d1d9;margin-top:2px}
.run-card{background:#161b22;border:1px solid #30363d;border-radius:8px;margin-bottom:16px;overflow:hidden}
.run-header{padding:12px 16px;cursor:pointer;display:flex;justify-content:space-between;align-items:center;background:#161b22;border-bottom:1px solid #21262d;transition:background .15s}
.run-header:hover{background:#1c2129}
.run-header .left{display:flex;align-items:center;gap:12px}
.loop-num{font-weight:700;font-size:16px;color:#58a6ff}
.run-time{font-size:13px;color:#8b949e}
.badge{padding:2px 10px;border-radius:10px;font-size:12px;font-weight:600;text-transform:uppercase}
.badge-success{background:#1f3d2a;color:#3fb950}
.badge-error{background:#3d1f1f;color:#f85149}
.badge-running{background:#1f303d;color:#58a6ff}
.run-body{display:none;padding:0}
.run-body.open{display:block}
.ctrl-table{width:100%;border-collapse:collapse;font-size:13px}
.ctrl-table th{text-align:left;padding:8px 12px;background:#0d1117;color:#8b949e;font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.5px;position:sticky;top:0}
.ctrl-table td{padding:6px 12px;border-bottom:1px solid #21262d;vertical-align:top}
.ctrl-table tr:hover{background:#1c2129}
.ctrl-table tr.error-row{background:#1f121544}
.type-badge{display:inline-block;padding:1px 8px;border-radius:4px;font-size:11px;font-weight:600;min-width:65px;text-align:center}
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
.ts-col{color:#8b949e;font-family:'Consolas',monospace;font-size:12px;white-space:nowrap}
.desc-col{max-width:400px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.detail-toggle{cursor:pointer;color:#58a6ff;font-size:11px;margin-left:4px}
.detail-toggle:hover{text-decoration:underline}
.detail-row{display:none;background:#0d1117}
.detail-row.open{display:table-row}
.detail-row td{padding:8px 16px}
.detail-json{font-family:'Consolas',monospace;font-size:12px;color:#c9d1d9;white-space:pre-wrap;word-break:break-all;background:#0d1117;padding:8px 12px;border-radius:4px;max-height:300px;overflow-y:auto}
.summary{display:flex;gap:16px;margin-bottom:20px;flex-wrap:wrap}
.summary-item{font-size:13px;color:#8b949e}
.summary-item strong{color:#c9d1d9}
.arrow{display:inline-block;transition:transform .2s;margin-right:6px}
.arrow.open{transform:rotate(90deg)}
.run-stats{display:flex;gap:16px;font-size:12px;color:#8b949e;padding:8px 16px;border-top:1px solid #21262d;background:#0d1117}
.run-stats .stat{display:flex;align-items:center;gap:4px}
.search-bar{margin-bottom:16px}
.search-bar input{width:100%;padding:8px 12px;background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#c9d1d9;font-size:14px;outline:none}
.search-bar input:focus{border-color:#58a6ff}
.empty{text-align:center;padding:40px;color:#8b949e}
.back-link{color:#58a6ff;text-decoration:none;font-size:13px}
.back-link:hover{text-decoration:underline}
</style>
</head>
<body>

<div class="header">
    <div>
        <h1>Playback Log Viewer</h1>
        <span class="rec-name" id="recName">-</span>
    </div>
    <div class="toolbar">
        <a href="/" class="back-link">&larr; Dashboard</a>
        <label><input type="checkbox" id="autoRefresh" checked> Auto-refresh (2s)</label>
    </div>
</div>

<div class="container">
    <div class="info-grid" id="infoGrid"></div>
    <div class="summary" id="summary"></div>
    <div class="search-bar">
        <input type="text" id="searchInput" placeholder="Filter by type, action, description..." oninput="filterCtrls()">
    </div>
    <div id="runsContainer"><div class="empty">Loading...</div></div>
</div>

<script>
let lastRunCount = 0;
let openRuns = {};
let openDetails = {};

async function fetchLog() {
    try {
        const resp = await fetch('/api/playback-log');
        const data = await resp.json();
        if (data && data.recording) renderAll(data);
    } catch(e) { console.error('Fetch error:', e); }
}

function renderAll(data) {
    renderRecInfo(data.recording, data.runs);
    renderSummary(data.runs);
    renderRuns(data.runs);
}

function renderRecInfo(rec, runs) {
    if (!rec) return;
    document.getElementById('recName').textContent = rec.name || rec.file || '-';
    const grid = document.getElementById('infoGrid');
    const items = [
        ['Recording', rec.name], ['Device', rec.device], ['App', rec.app_package],
        ['Screen', rec.screen], ['Mode', rec.replay_mode], ['Speed', rec.speed + 'x'],
        ['Frames', rec.total_frames], ['Total Runs', runs.length],
    ];
    grid.innerHTML = items.map(([l, v]) =>
        '<div class="info-card"><div class="info-label">' + esc(l) + '</div><div class="info-value">' + esc(String(v || '-')) + '</div></div>'
    ).join('');
}

function renderSummary(runs) {
    const total = runs.length;
    const success = runs.filter(r => r.status === 'success').length;
    const errors = runs.filter(r => r.status === 'error').length;
    const totalCtrls = runs.reduce((s, r) => s + (r.ctrls || []).length, 0);
    document.getElementById('summary').innerHTML = [
        '<div class="summary-item">Runs: <strong>' + total + '</strong></div>',
        '<div class="summary-item" style="color:#3fb950">Success: <strong>' + success + '</strong></div>',
        '<div class="summary-item" style="color:#f85149">Errors: <strong>' + errors + '</strong></div>',
        '<div class="summary-item">Total Ctrls: <strong>' + totalCtrls + '</strong></div>',
    ].join('');
}

function renderRuns(runs) {
    const container = document.getElementById('runsContainer');
    if (!runs || runs.length === 0) {
        container.innerHTML = '<div class="empty">No runs recorded yet</div>';
        return;
    }
    // Auto-open latest run on new run arrival
    if (runs.length > lastRunCount) { openRuns[runs.length - 1] = true; }
    lastRunCount = runs.length;
    container.innerHTML = runs.map((run, ri) => renderRun(run, ri)).join('');
    filterCtrls();
}

function renderRun(run, ri) {
    const ctrls = run.ctrls || [];
    const errCount = ctrls.filter(c => c.result === 'error').length;
    const badgeCls = run.status === 'success' ? 'badge-success' : run.status === 'error' ? 'badge-error' : 'badge-running';
    const startTime = run.started_at ? fmtTime(run.started_at) : '-';
    const endTime = run.completed_at ? fmtTime(run.completed_at) : '-';
    const dur = run.started_at && run.completed_at ? fmtDur(new Date(run.completed_at) - new Date(run.started_at)) : '-';
    const isOpen = !!openRuns[ri];

    let h = '<div class="run-card">';
    h += '<div class="run-header" onclick="toggleRun(' + ri + ')">';
    h += '<div class="left"><span class="arrow' + (isOpen ? ' open' : '') + '" id="arrow-' + ri + '">&#9654;</span>';
    h += '<span class="loop-num">Loop #' + run.loop + '</span>';
    h += '<span class="run-time">' + startTime + '</span></div>';
    h += '<span class="badge ' + badgeCls + '">' + esc(run.status) + '</span></div>';
    h += '<div class="run-body' + (isOpen ? ' open' : '') + '" id="run-' + ri + '">';
    h += '<div class="run-stats">';
    h += '<div class="stat">Ctrls: <strong style="margin-left:4px">' + ctrls.length + '</strong></div>';
    h += '<div class="stat">Errors: <strong style="margin-left:4px;color:' + (errCount > 0 ? '#f85149' : '#3fb950') + '">' + errCount + '</strong></div>';
    h += '<div class="stat">Duration: <strong style="margin-left:4px">' + dur + '</strong></div>';
    h += '<div class="stat">End: <strong style="margin-left:4px">' + endTime + '</strong></div></div>';
    h += '<table class="ctrl-table"><tr><th>#</th><th>Type</th><th>Action</th><th>Description</th><th>Time</th><th>Exec</th><th>Result</th></tr>';
    ctrls.forEach((c, ci) => {
        const rc = c.result === 'error' ? ' class="error-row"' : '';
        const tc = 'type-' + (c.type || 'touch');
        const resCls = c.result === 'error' ? 'result-err' : 'result-ok';
        const resIcon = c.result === 'error' ? '&#10007;' : '&#10003;';
        const ct = c.timestamp ? fmtTime(c.timestamp) : '-';
        const dKey = ri + '-' + ci;
        const detailOpen = !!openDetails[dKey];
        h += '<tr' + rc + ' data-desc="' + esc(c.description || '').toLowerCase() + '" data-type="' + esc(c.type || '') + '">';
        h += '<td>' + c.frame_id + '</td>';
        h += '<td><span class="type-badge ' + tc + '">' + esc(c.type || '-') + '</span></td>';
        h += '<td>' + esc(c.action || '-') + '</td>';
        h += '<td class="desc-col">' + esc(c.description || '-') + ' <span class="detail-toggle" onclick="toggleDetail(\'' + dKey + '\',event)">[detail]</span></td>';
        h += '<td class="ts-col">' + ct + '</td>';
        h += '<td class="ts-col">' + (c.exec_ms != null ? c.exec_ms + 'ms' : '-') + '</td>';
        h += '<td class="' + resCls + '">' + resIcon + (c.error ? ' ' + esc(c.error) : '') + '</td></tr>';
        h += '<tr class="detail-row' + (detailOpen ? ' open' : '') + '" id="detail-' + dKey + '"><td colspan="7"><div class="detail-json">' + esc(JSON.stringify(c.details || {}, null, 2)) + '</div></td></tr>';
    });
    h += '</table></div></div>';
    return h;
}

function toggleRun(ri) {
    openRuns[ri] = !openRuns[ri];
    const body = document.getElementById('run-' + ri);
    const arrow = document.getElementById('arrow-' + ri);
    if (body) body.classList.toggle('open');
    if (arrow) arrow.classList.toggle('open');
}

function toggleDetail(key, event) {
    event.stopPropagation();
    openDetails[key] = !openDetails[key];
    const el = document.getElementById('detail-' + key);
    if (el) el.classList.toggle('open');
}

function filterCtrls() {
    const q = (document.getElementById('searchInput').value || '').toLowerCase().trim();
    document.querySelectorAll('.ctrl-table tr[data-desc]').forEach(tr => {
        if (!q) { tr.style.display = ''; return; }
        const d = tr.getAttribute('data-desc') || '';
        const t = tr.getAttribute('data-type') || '';
        tr.style.display = (d.includes(q) || t.includes(q)) ? '' : 'none';
    });
}

function fmtTime(iso) {
    try { const d = new Date(iso); return d.toLocaleTimeString('en-GB', {hour:'2-digit',minute:'2-digit',second:'2-digit',fractionalSecondDigits:3}); }
    catch(e) { return iso; }
}
function fmtDur(ms) {
    if (ms < 1000) return ms + 'ms';
    const s = Math.floor(ms / 1000);
    if (s < 60) return s + 's';
    return Math.floor(s / 60) + 'm ' + (s % 60) + 's';
}
function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

// Initial load + auto-refresh
fetchLog();
setInterval(() => { if (document.getElementById('autoRefresh').checked) fetchLog(); }, 2000);
</script>
</body>
</html>"""


class DebugHandler(BaseHTTPRequestHandler):
    engine_state: Any = None
    on_action: Any = None
    playback_logger: Any = None  # PlaybackLogger instance (set by DebugServer)

    def log_message(self, format: str, *args: Any) -> None:
        pass  # Suppress default HTTP logging

    def do_GET(self) -> None:
        if self.path == "/" or self.path == "/index.html":
            self._respond_html(DASHBOARD_HTML)
        elif self.path == "/logs":
            self._serve_log_viewer()
        elif self.path == "/api/state":
            self._respond_json(self.engine_state.to_dict() if self.engine_state else {})
        elif self.path == "/api/playback-log":
            self._serve_playback_log()
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

    def _serve_log_viewer(self) -> None:
        """Serve the live playback log viewer page."""
        self._respond_html(LOG_VIEWER_HTML)

    def _serve_playback_log(self) -> None:
        """Return the current playback log JSON."""
        if self.playback_logger:
            self._respond_json(self.playback_logger.get_data())
        else:
            self._respond_json({"recording": None, "runs": []})

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
    def __init__(self, state: Any, port: int = 8080, on_action: Any = None,
                 playback_logger: Any = None):
        self.state = state
        self.port = port
        self.on_action = on_action
        self.playback_logger = playback_logger
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        DebugHandler.engine_state = self.state
        DebugHandler.on_action = self.on_action
        DebugHandler.playback_logger = self.playback_logger
        self._server = HTTPServer(("0.0.0.0", self.port), DebugHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server = None
