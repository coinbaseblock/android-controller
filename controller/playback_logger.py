#!/usr/bin/env python3
"""
Playback Logger - Structured JSON logging for Universal Player.

Logs every frame (ctrl) execution grouped by recording and loop iteration.
When looping, new loop entries are appended with timestamps.
Generates a self-contained HTML viewer that can be opened in a browser.

Log structure:
{
  "recording": { name, file, device, replay_mode, speed, total_frames },
  "runs": [
    {
      "loop": 1,
      "started_at": "ISO timestamp",
      "completed_at": "ISO timestamp",
      "status": "success" | "error" | "running",
      "ctrls": [
        {
          "frame_id": 1,
          "index": 0,
          "type": "gesture",
          "action": "tap",
          "timestamp": "ISO timestamp",
          "result": "success" | "error",
          "error": null,
          "exec_ms": 150,
          "description": "Tap element [Login] (540, 1200)",
          "details": { ... }
        }
      ]
    }
  ]
}
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


class PlaybackLogger:
    """Logs playback frame details as structured JSON, grouped by loop."""

    def __init__(
        self,
        recording_path: Path,
        recording_name: str,
        app_package: str,
        device: str,
        screen_width: int,
        screen_height: int,
        replay_mode: str,
        speed: float,
        total_frames: int,
    ):
        self._lock = threading.Lock()

        # Log file path: {recording_dir}/logs/{stem}-playback.json
        log_dir = recording_path.parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stem = recording_path.stem.replace(".urf", "")
        self.log_path = log_dir / f"{stem}-playback.json"
        self.viewer_path = log_dir / f"{stem}-playback.html"

        # Build initial structure
        self._data: Dict[str, Any] = {
            "recording": {
                "name": recording_name,
                "file": str(recording_path),
                "app_package": app_package,
                "device": device,
                "screen": f"{screen_width}x{screen_height}",
                "replay_mode": replay_mode,
                "speed": speed,
                "total_frames": total_frames,
            },
            "runs": [],
        }

        self._current_run: Optional[Dict[str, Any]] = None

        # Load existing runs if log file already exists (append across restarts)
        if self.log_path.exists():
            try:
                existing = json.loads(self.log_path.read_text(encoding="utf-8"))
                if existing.get("recording", {}).get("file") == str(recording_path):
                    self._data["runs"] = existing.get("runs", [])
            except (json.JSONDecodeError, Exception):
                pass

        self._save()

    def start_loop(self, loop_idx: int) -> None:
        """Start logging a new loop iteration."""
        with self._lock:
            self._current_run = {
                "loop": loop_idx,
                "started_at": datetime.now().isoformat(timespec="milliseconds"),
                "completed_at": None,
                "status": "running",
                "ctrls": [],
            }
            self._data["runs"].append(self._current_run)
            self._save()

    def log_ctrl(
        self,
        frame_id: int,
        index: int,
        frame_type: str,
        action: str,
        description: str,
        result: str,
        error: Optional[str],
        exec_ms: int,
        details: Dict[str, Any],
    ) -> None:
        """Log a single frame/ctrl execution."""
        entry = {
            "frame_id": frame_id,
            "index": index,
            "type": frame_type,
            "action": action,
            "timestamp": datetime.now().isoformat(timespec="milliseconds"),
            "result": result,
            "error": error,
            "exec_ms": exec_ms,
            "description": description,
            "details": details,
        }
        with self._lock:
            if self._current_run is not None:
                self._current_run["ctrls"].append(entry)
                self._save()

    def end_loop(self, success: bool) -> None:
        """Mark current loop as completed and regenerate viewer."""
        with self._lock:
            if self._current_run:
                self._current_run["completed_at"] = datetime.now().isoformat(
                    timespec="milliseconds"
                )
                self._current_run["status"] = "success" if success else "error"
                self._current_run = None
                self._save()
                self._generate_viewer()

    def get_data(self) -> Dict[str, Any]:
        """Return current log data (for API consumption)."""
        with self._lock:
            return json.loads(json.dumps(self._data))

    def _save(self) -> None:
        """Persist JSON log to disk using atomic write (temp file + rename)."""
        try:
            content = json.dumps(self._data, indent=2, ensure_ascii=False)
            # Atomic write: write to temp file in the same directory, then rename.
            # os.replace() is atomic on POSIX, preventing partial reads.
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self.log_path.parent),
                suffix=".tmp",
                prefix=self.log_path.stem,
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, str(self.log_path))
            except Exception:
                # Clean up temp file on failure
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception:
            pass

    def _generate_viewer(self) -> None:
        """Generate a self-contained HTML viewer with embedded log data."""
        try:
            json_str = json.dumps(self._data, indent=2, ensure_ascii=False)
            html = _VIEWER_HTML_TEMPLATE.replace("/*__LOG_DATA__*/", json_str)
            self.viewer_path.write_text(html, encoding="utf-8")
        except Exception:
            pass


def build_ctrl_details(frame: Any) -> Dict[str, Any]:
    """Build a details dict from a Frame object for logging."""
    d: Dict[str, Any] = {"t_offset_ms": frame.t}

    if frame.type == "touch":
        d["action"] = frame.action
        d["x"] = frame.x
        d["y"] = frame.y
        if frame.pressure:
            d["pressure"] = round(frame.pressure, 3)

    elif frame.type == "gesture":
        d["action"] = frame.action
        d["x"] = frame.x
        d["y"] = frame.y
        if frame.action == "swipe":
            d["x2"] = frame.x2
            d["y2"] = frame.y2
        d["duration_ms"] = frame.duration_ms
        if frame.element:
            d["element"] = frame.element

    elif frame.type == "element":
        if frame.resource_id:
            d["resource_id"] = frame.resource_id
        if frame.text:
            d["text"] = frame.text
        d["timeout"] = frame.timeout

    elif frame.type == "app":
        d["action"] = frame.action
        d["package"] = frame.package
        if frame.activity:
            d["activity"] = frame.activity

    elif frame.type == "key":
        d["keycode"] = frame.keycode
        d["key_name"] = frame.key_name

    elif frame.type == "screenshot":
        d["stage"] = frame.stage
        d["send"] = frame.send

    elif frame.type == "wait":
        d["duration_ms"] = frame.duration_ms

    elif frame.type == "shell":
        d["command"] = frame.command

    elif frame.type == "extract":
        d["label"] = frame.extract_label
        if frame.extract_fields:
            d["field_count"] = len(frame.extract_fields)
        d["all_text"] = frame.extract_all_text
        d["screenshot"] = frame.extract_screenshot

    elif frame.type == "marker":
        d["note"] = frame.note

    return d


# ---------------------------------------------------------------------------
# Self-contained HTML viewer template
# ---------------------------------------------------------------------------

_VIEWER_HTML_TEMPLATE = r"""<!DOCTYPE html>
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
.container{max-width:1400px;margin:0 auto;padding:20px}
.info-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:24px}
.info-card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px 16px}
.info-label{font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.5px}
.info-value{font-size:16px;font-weight:600;color:#c9d1d9;margin-top:2px}
.run-card{background:#161b22;border:1px solid #30363d;border-radius:8px;margin-bottom:16px;overflow:hidden}
.run-header{padding:12px 16px;cursor:pointer;display:flex;justify-content:space-between;align-items:center;background:#161b22;border-bottom:1px solid #21262d;transition:background .15s}
.run-header:hover{background:#1c2129}
.run-header .left{display:flex;align-items:center;gap:12px}
.run-header .loop-num{font-weight:700;font-size:16px;color:#58a6ff}
.run-header .time{font-size:13px;color:#8b949e}
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
</style>
</head>
<body>

<div class="header">
    <h1>Playback Log Viewer</h1>
    <span class="rec-name" id="recName">-</span>
</div>

<div class="container">
    <div class="info-grid" id="infoGrid"></div>

    <div class="summary" id="summary"></div>

    <div class="search-bar">
        <input type="text" id="searchInput" placeholder="Filter by type, action, description..." oninput="filterCtrls()">
    </div>

    <div id="runsContainer"></div>
</div>

<script>
const LOG_DATA = /*__LOG_DATA__*/null;

function init() {
    if (!LOG_DATA) {
        document.getElementById('runsContainer').innerHTML = '<div class="empty">No log data available</div>';
        return;
    }
    renderRecordingInfo(LOG_DATA.recording);
    renderRuns(LOG_DATA.runs);
    renderSummary(LOG_DATA.runs);
}

function renderRecordingInfo(rec) {
    document.getElementById('recName').textContent = rec.name || rec.file || '-';
    const grid = document.getElementById('infoGrid');
    const items = [
        ['Recording', rec.name],
        ['Device', rec.device],
        ['App', rec.app_package],
        ['Screen', rec.screen],
        ['Mode', rec.replay_mode],
        ['Speed', rec.speed + 'x'],
        ['Frames', rec.total_frames],
        ['Total Runs', LOG_DATA.runs.length],
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
    const el = document.getElementById('summary');
    el.innerHTML = [
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
    container.innerHTML = runs.map((run, ri) => renderRun(run, ri)).join('');
}

function renderRun(run, ri) {
    const ctrls = run.ctrls || [];
    const errCount = ctrls.filter(c => c.result === 'error').length;
    const badgeCls = run.status === 'success' ? 'badge-success' : run.status === 'error' ? 'badge-error' : 'badge-running';
    const startTime = run.started_at ? formatTime(run.started_at) : '-';
    const endTime = run.completed_at ? formatTime(run.completed_at) : '-';
    const durationMs = run.started_at && run.completed_at ? (new Date(run.completed_at) - new Date(run.started_at)) : 0;
    const durStr = durationMs > 0 ? formatDuration(durationMs) : '-';

    let html = '<div class="run-card" data-run="' + ri + '">';
    html += '<div class="run-header" onclick="toggleRun(' + ri + ')">';
    html += '<div class="left">';
    html += '<span class="arrow" id="arrow-' + ri + '">&#9654;</span>';
    html += '<span class="loop-num">Loop #' + run.loop + '</span>';
    html += '<span class="time">' + startTime + '</span>';
    html += '</div>';
    html += '<span class="badge ' + badgeCls + '">' + esc(run.status) + '</span>';
    html += '</div>';

    html += '<div class="run-body" id="run-' + ri + '">';

    // Stats bar
    html += '<div class="run-stats">';
    html += '<div class="stat">Ctrls: <strong style="margin-left:4px">' + ctrls.length + '</strong></div>';
    html += '<div class="stat">Errors: <strong style="margin-left:4px;color:' + (errCount > 0 ? '#f85149' : '#3fb950') + '">' + errCount + '</strong></div>';
    html += '<div class="stat">Duration: <strong style="margin-left:4px">' + durStr + '</strong></div>';
    html += '<div class="stat">End: <strong style="margin-left:4px">' + endTime + '</strong></div>';
    html += '</div>';

    // Ctrls table
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

        // Detail row (hidden by default)
        html += '<tr class="detail-row" id="detail-' + ri + '-' + ci + '"><td colspan="7"><div class="detail-json">' + esc(JSON.stringify(ctrl.details || {}, null, 2)) + '</div></td></tr>';
    });
    html += '</table>';
    html += '</div></div>';
    return html;
}

function toggleRun(ri) {
    const body = document.getElementById('run-' + ri);
    const arrow = document.getElementById('arrow-' + ri);
    body.classList.toggle('open');
    arrow.classList.toggle('open');
}

function toggleDetail(ri, ci, event) {
    event.stopPropagation();
    const el = document.getElementById('detail-' + ri + '-' + ci);
    el.classList.toggle('open');
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

function formatDuration(ms) {
    if (ms < 1000) return ms + 'ms';
    const s = Math.floor(ms / 1000);
    if (s < 60) return s + 's';
    const m = Math.floor(s / 60);
    return m + 'm ' + (s % 60) + 's';
}

function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

init();
</script>
</body>
</html>"""
