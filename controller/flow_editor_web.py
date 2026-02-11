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

import json
import os
import sys
import threading
import argparse
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse, parse_qs

# Add script dir to path for imports
sys.path.insert(0, str(Path(__file__).parent))
from universal_format import UniversalRecording, Frame


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
.topbar { background: var(--bg2); border-bottom: 1px solid var(--border); padding: 8px 16px; display: flex; align-items: center; gap: 12px; flex-shrink: 0; }
.topbar h1 { font-size: 15px; font-weight: 600; white-space: nowrap; }
.topbar .spacer { flex: 1; }
.main { display: flex; flex: 1; overflow: hidden; }
.sidebar { width: 280px; background: var(--bg2); border-right: 1px solid var(--border); display: flex; flex-direction: column; flex-shrink: 0; overflow: hidden; }
.content { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
.timeline { flex: 1; overflow-y: auto; padding: 12px; }
.detail-panel { width: 340px; background: var(--bg2); border-left: 1px solid var(--border); overflow-y: auto; flex-shrink: 0; }

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
.badge-marker { background: #55555533; color: #888; }
.frame-desc { font-size: 13px; margin-top: 3px; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.frame-note { font-size: 11px; color: var(--text2); font-style: italic; margin-top: 2px; }
.frame-actions { display: flex; flex-direction: column; justify-content: center; gap: 2px; padding: 4px 8px; }
.frame-actions button { background: none; border: none; color: var(--text2); font-size: 14px; padding: 2px 4px; border-radius: 3px; }
.frame-actions button:hover { background: var(--bg3); color: var(--text); }
.drop-indicator { height: 3px; background: var(--blue); border-radius: 2px; margin: 2px 0; }

/* === DETAIL PANEL === */
.detail-panel { padding: 0; }
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

/* === SCROLLBAR === */
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: var(--text2); }
</style>
</head>
<body>
<div class="app" id="app">

  <!-- TOP BAR -->
  <div class="topbar">
    <h1>Flow Editor</h1>
    <span id="fileName" style="color:var(--text2);font-size:13px;">No file loaded</span>
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
      <div class="sidebar-section">
        <h3>Files</h3>
        <div class="file-list" id="fileList"></div>
      </div>

      <!-- Meta -->
      <div class="sidebar-section" id="metaPanel">
        <h3>Recording Info</h3>
        <div class="setting-row"><label>Name</label><input id="metaName" onchange="updateMeta()"></div>
        <div class="setting-row"><label>Package</label><input id="metaPkg" onchange="updateMeta()"></div>
        <div class="setting-row"><label>Device</label><input id="metaDev" onchange="updateMeta()"></div>
        <div class="meta-row"><span class="label">Mode</span><span class="value" id="metaMode">-</span></div>
        <div class="meta-row"><span class="label">Frames</span><span class="value" id="metaFrames">0</span></div>
        <div class="meta-row"><span class="label">Duration</span><span class="value" id="metaDur">0s</span></div>
      </div>

      <!-- Settings -->
      <div class="sidebar-section">
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

      <!-- Bulk Actions -->
      <div class="sidebar-section">
        <h3>Bulk Actions</h3>
        <div class="btn-group" style="flex-wrap:wrap;gap:6px">
          <button class="btn btn-sm" onclick="collapseRaw()" title="Collapse raw touches into gestures">Collapse Raw</button>
          <button class="btn btn-sm" onclick="compactFrames()" title="Remove disabled frames">Compact</button>
          <button class="btn btn-sm" onclick="scaleTime()" title="Scale all timing">Scale Time</button>
          <button class="btn btn-sm" onclick="shiftTime()" title="Shift all timing">Shift Time</button>
          <button class="btn btn-sm btn-danger" onclick="clearAll()" title="Delete all frames">Clear All</button>
        </div>
      </div>
    </div>

    <!-- MAIN CONTENT -->
    <div class="content">
      <!-- Toolbar -->
      <div class="toolbar">
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
      <div class="timeline" id="timeline"></div>
    </div>

    <!-- DETAIL PANEL -->
    <div class="detail-panel" id="detailPanel">
      <div class="detail-header">
        <h3>Frame Detail</h3>
        <button class="btn btn-sm" onclick="closeDetail()">Close</button>
      </div>
      <div class="detail-body" id="detailBody">
        <p style="color:var(--text2)">Select a frame to edit</p>
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

const FRAME_TYPES = [
  {type:'gesture',  icon:'👆', name:'Gesture',    desc:'Tap / Swipe / Long Press'},
  {type:'element',  icon:'🎯', name:'Element',    desc:'Tap element by ID/text'},
  {type:'app',      icon:'📱', name:'App',        desc:'Open / Close app'},
  {type:'screenshot',icon:'📸',name:'Screenshot', desc:'Capture screen'},
  {type:'key',      icon:'⌨️', name:'Key',        desc:'Key event (Back/Home)'},
  {type:'wait',     icon:'⏳', name:'Wait',       desc:'Pause for duration'},
  {type:'swipe',    icon:'↕️', name:'Swipe',      desc:'Swipe gesture'},
  {type:'shell',    icon:'💻', name:'Shell',      desc:'ADB shell command'},
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

async function loadFileList() {
  const data = await api('GET', '/files');
  const el = document.getElementById('fileList');
  el.innerHTML = data.files.map(f =>
    `<div class="file-item ${f===filePath?'active':''}" onclick="loadFile('${f}')">
      <span class="file-icon">&#128196;</span>${f.split('/').pop()}
    </div>`
  ).join('') || '<div style="color:var(--text2);padding:8px;font-size:12px">No .urf.json files</div>';
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
    case 'marker': return f.note || '(marker)';
    default: return f.action || f.type;
  }
}

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
          ${['touch','gesture','element','app','key','screenshot','wait','shell','marker'].map(t =>
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
    html += `
      <div class="field"><label>Keycode</label>
        <select onchange="updateFrameField(${f.id},'keycode',+this.value);updateFrameField(${f.id},'key_name',this.options[this.selectedIndex].text)">
          <option value="4" ${f.keycode===4?'selected':''}>BACK (4)</option>
          <option value="3" ${f.keycode===3?'selected':''}>HOME (3)</option>
          <option value="26" ${f.keycode===26?'selected':''}>POWER (26)</option>
          <option value="82" ${f.keycode===82?'selected':''}>MENU (82)</option>
          <option value="187" ${f.keycode===187?'selected':''}>APP_SWITCH (187)</option>
          <option value="24" ${f.keycode===24?'selected':''}>VOLUME_UP (24)</option>
          <option value="25" ${f.keycode===25?'selected':''}>VOLUME_DOWN (25)</option>
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

  html += `<div class="field"><label>Note</label><input value="${esc(f.note||'')}" onchange="updateFrameField(${f.id},'note',this.value)"></div>`;

  body.innerHTML = html;
}

// ============================================================
// ACTIONS
// ============================================================
function selectFrame(id) { selectedId = id; renderTimeline(); renderDetail(); }
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
      </select></div>`;
  } else if (type === 'screenshot') {
    html += `
      <div class="field"><label>Stage</label><input id="addStage" value="capture"></div>
      <div class="field"><label>Send</label><select id="addSend"><option value="true">Yes</option><option value="false">No</option></select></div>`;
  } else if (type === 'wait') {
    html += `<div class="field"><label>Duration (ms)</label><input id="addDur" type="number" value="2000"></div>`;
  } else if (type === 'shell') {
    html += `<div class="field"><label>Command</label><input id="addCmd" placeholder="am broadcast ..."></div>`;
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
    f.key_name = {3:'HOME',4:'BACK',26:'POWER',82:'MENU',187:'APP_SWITCH'}[kc] || '';
  } else if (type === 'screenshot') {
    f.stage = document.getElementById('addStage')?.value || 'capture';
    f.send = document.getElementById('addSend')?.value === 'true';
  } else if (type === 'wait') {
    f.duration_ms = +(document.getElementById('addDur')?.value || 2000);
  } else if (type === 'shell') {
    f.command = document.getElementById('addCmd')?.value || '';
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

// INIT
loadFileList();
</script>
</body>
</html>"""


# ============================================================
# HTTP Server
# ============================================================

class EditorHandler(BaseHTTPRequestHandler):
    work_dir: str = "/work"

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
        return {"files": files}

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

    # Response helpers
    def _html(self, content: str) -> None:
        data = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
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
