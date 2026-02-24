#!/usr/bin/env python3
"""
Universal Recording Format (URF) for Android automation.

A single .urf.json file can contain BOTH raw finger recordings AND scripted
actions, freely mixed. Each entry is a "frame" with a type field.

Frame types:
  - "touch"    : Raw finger touch (down/move/up) with coordinates
  - "gesture"  : Collapsed gesture (tap/swipe/long_press/two_finger_tap) from raw touches
  - "element"  : Tap on a specific UI element by resource-id/text
  - "app"      : Open or close an app
  - "key"      : Key event (back, home, etc.)
  - "screenshot": Take a screenshot
  - "wait"     : Explicit pause
  - "shell"    : Run an adb shell command
  - "marker"   : User bookmark / annotation (no action)
  - "extract"  : Capture UI data from screen and save to log (for data collection)

File structure:
{
  "version": 2,
  "meta": {
    "name": "My Recording",
    "created": "2025-01-01T00:00:00",
    "device": "10.1.1.242:43849",
    "screen_width": 1080,
    "screen_height": 2400,
    "app_package": "com.example.app",
    "recording_mode": "raw" | "annotated" | "script" | "mixed",
    "total_duration_ms": 12345
  },
  "settings": {
    "loop": true,
    "loop_count": 0,
    "loop_delay": 5.0,
    "speed": 1.0,
    "on_error": "retry" | "skip" | "pause" | "stop",
    "max_retries": 3,
    "screenshot_dir": "/img/captures",
    "send_to": { "method": "cp", "path": "/img/sent" }
  },
  "frames": [
    {
      "id": 1,
      "t": 0,              // ms offset from recording start
      "type": "touch",
      "action": "down",    // down | move | up
      "x": 540,
      "y": 1200,
      "pressure": 0.5,     // optional
      "note": ""            // user annotation
    },
    {
      "id": 2,
      "t": 150,
      "type": "gesture",
      "action": "tap",     // tap | swipe | long_press
      "x": 540, "y": 1200,
      "x2": 540, "y2": 1200,  // end point (for swipe)
      "duration_ms": 50,
      "element": {             // auto-annotated element info (nullable)
        "resource_id": "com.example:id/btn",
        "text": "Login",
        "class": "android.widget.Button",
        "bounds": [100, 1150, 980, 1250]
      },
      "note": "Tap login button"
    },
    {
      "id": 3,
      "t": 2000,
      "type": "element",
      "action": "tap",
      "resource_id": "com.example:id/btn_next",
      "text": "Next",
      "timeout": 10,
      "note": "Navigate to next page"
    },
    {
      "id": 4,
      "t": 3000,
      "type": "app",
      "action": "open",    // open | close
      "package": "com.example.app",
      "activity": ""
    },
    {
      "id": 5,
      "t": 5000,
      "type": "screenshot",
      "stage": "home",
      "send": true
    },
    {
      "id": 6,
      "t": 5100,
      "type": "key",
      "keycode": 4,
      "key_name": "BACK"
    },
    {
      "id": 7,
      "t": 6000,
      "type": "wait",
      "duration_ms": 2000
    },
    {
      "id": 8,
      "t": 8000,
      "type": "shell",
      "command": "am broadcast -a MY_ACTION"
    },
    {
      "id": 9,
      "t": 9000,
      "type": "marker",
      "note": "User checked screen manually here"
    },
    {
      "id": 10,
      "t": 10000,
      "type": "extract",
      "extract_label": "station_status",     // label for this extraction point
      "extract_fields": [                     // list of fields to extract from UI
        {"name": "station_name", "resource_id": "com.example:id/name", "text_match": ""},
        {"name": "status",       "resource_id": "", "text_match": "พร้อมใช้งาน|ไม่พร้อม"},
        {"name": "connector",    "resource_id": "com.example:id/type", "text_match": ""}
      ],
      "extract_all_text": true,              // also capture all visible text on screen
      "extract_screenshot": true,            // take screenshot with extraction
      "note": "Capture EV station info"
    }
  ]
}
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


KEY_NAMES = {
    3: "HOME", 4: "BACK", 24: "VOLUME_UP", 25: "VOLUME_DOWN",
    26: "POWER", 82: "MENU", 187: "APP_SWITCH",
}


@dataclass
class Frame:
    id: int = 0
    t: int = 0                      # ms offset from start
    type: str = "touch"             # touch|gesture|element|app|key|screenshot|wait|shell|marker|extract
    action: str = ""                # depends on type
    # touch / gesture
    x: int = 0
    y: int = 0
    x2: int = 0                     # swipe end
    y2: int = 0
    pressure: float = 0.0
    duration_ms: int = 0
    # swipe waypoints: list of [x, y, t_offset_ms] for intermediate points
    waypoints: Optional[List[List[int]]] = None
    # element info (auto-annotated or explicit)
    element: Optional[Dict[str, Any]] = None
    resource_id: str = ""
    text: str = ""
    timeout: float = 10.0
    # app
    package: str = ""
    activity: str = ""
    # key
    keycode: int = 0
    key_name: str = ""
    # screenshot
    stage: str = ""
    send: bool = False
    # wait
    # (uses duration_ms)
    # shell
    command: str = ""
    # extract (data collection)
    extract_label: str = ""
    extract_fields: Optional[List[Dict[str, Any]]] = None  # [{name, resource_id, text_match}]
    extract_all_text: bool = False
    extract_screenshot: bool = False
    # common
    note: str = ""
    enabled: bool = True            # can disable frames without deleting

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"id": self.id, "t": self.t, "type": self.type}
        if self.type == "touch":
            d.update(action=self.action, x=self.x, y=self.y)
            if self.pressure:
                d["pressure"] = self.pressure
        elif self.type == "gesture":
            d.update(action=self.action, x=self.x, y=self.y, duration_ms=self.duration_ms)
            if self.action == "swipe":
                d.update(x2=self.x2, y2=self.y2)
                if self.waypoints:
                    d["waypoints"] = self.waypoints
            if self.element:
                d["element"] = self.element
        elif self.type == "element":
            d.update(action=self.action or "tap")
            if self.resource_id:
                d["resource_id"] = self.resource_id
            if self.text:
                d["text"] = self.text
            d["timeout"] = self.timeout
        elif self.type == "app":
            d.update(action=self.action, package=self.package)
            if self.activity:
                d["activity"] = self.activity
        elif self.type == "key":
            d.update(keycode=self.keycode, key_name=self.key_name or KEY_NAMES.get(self.keycode, ""))
        elif self.type == "screenshot":
            d.update(stage=self.stage, send=self.send)
        elif self.type == "wait":
            d["duration_ms"] = self.duration_ms
        elif self.type == "shell":
            d["command"] = self.command
        elif self.type == "extract":
            d["extract_label"] = self.extract_label
            if self.extract_fields:
                d["extract_fields"] = self.extract_fields
            d["extract_all_text"] = self.extract_all_text
            d["extract_screenshot"] = self.extract_screenshot
        elif self.type == "marker":
            pass

        if self.note:
            d["note"] = self.note
        if not self.enabled:
            d["enabled"] = False
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Frame":
        f = cls()
        for k, v in d.items():
            if hasattr(f, k):
                setattr(f, k, v)
        return f


@dataclass
class RecordingMeta:
    name: str = "Untitled Recording"
    created: str = ""
    device: str = ""
    screen_width: int = 1080
    screen_height: int = 2400
    app_package: str = ""
    recording_mode: str = "mixed"   # raw | annotated | script | mixed
    total_duration_ms: int = 0


@dataclass
class RecordingSettings:
    loop: bool = False
    loop_count: int = 0             # 0 = infinite
    loop_delay: float = 5.0
    speed: float = 1.0
    on_error: str = "retry"         # retry | skip | pause | stop
    max_retries: int = 3
    retry_delay: float = 2.0
    screenshot_dir: str = "/img/captures"
    send_to: Dict[str, Any] = field(default_factory=lambda: {"method": "cp", "path": "/img/sent"})
    notify: bool = True
    notify_method: str = "stdout"   # stdout | webhook | file
    notify_url: str = ""


@dataclass
class UniversalRecording:
    version: int = 2
    meta: RecordingMeta = field(default_factory=RecordingMeta)
    settings: RecordingSettings = field(default_factory=RecordingSettings)
    frames: List[Frame] = field(default_factory=list)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": self.version,
            "meta": {
                "name": self.meta.name,
                "created": self.meta.created or datetime.now().isoformat(),
                "device": self.meta.device,
                "screen_width": self.meta.screen_width,
                "screen_height": self.meta.screen_height,
                "app_package": self.meta.app_package,
                "recording_mode": self.meta.recording_mode,
                "total_duration_ms": self.meta.total_duration_ms,
            },
            "settings": {
                "loop": self.settings.loop,
                "loop_count": self.settings.loop_count,
                "loop_delay": self.settings.loop_delay,
                "speed": self.settings.speed,
                "on_error": self.settings.on_error,
                "max_retries": self.settings.max_retries,
                "retry_delay": self.settings.retry_delay,
                "screenshot_dir": self.settings.screenshot_dir,
                "send_to": self.settings.send_to,
                "notify": self.settings.notify,
                "notify_method": self.settings.notify_method,
                "notify_url": self.settings.notify_url,
            },
            "frames": [f.to_dict() for f in self.frames if f.enabled],
        }
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "UniversalRecording":
        raw = json.loads(path.read_text(encoding="utf-8"))
        rec = cls()
        rec.version = raw.get("version", 1)

        meta_raw = raw.get("meta", {})
        for k, v in meta_raw.items():
            if hasattr(rec.meta, k):
                setattr(rec.meta, k, v)

        settings_raw = raw.get("settings", {})
        for k, v in settings_raw.items():
            if hasattr(rec.settings, k):
                setattr(rec.settings, k, v)

        rec.frames = [Frame.from_dict(f) for f in raw.get("frames", [])]
        return rec

    def next_id(self) -> int:
        return max((f.id for f in self.frames), default=0) + 1

    def add_frame(self, frame: Frame) -> Frame:
        if frame.id == 0:
            frame.id = self.next_id()
        self.frames.append(frame)
        return frame

    def get_enabled_frames(self) -> List[Frame]:
        return [f for f in self.frames if f.enabled]

    def total_duration(self) -> int:
        if not self.frames:
            return 0
        return max(f.t for f in self.frames)

    def collapse_raw_to_gestures(self) -> List[Frame]:
        """Collapse raw touch frames into gesture frames (tap/swipe)."""
        gestures: List[Frame] = []
        buffer: List[Frame] = []

        def flush() -> None:
            if not buffer:
                return
            start = buffer[0]
            end = buffer[-1]

            # Use Euclidean distance for accurate gesture classification
            dx = end.x - start.x
            dy = end.y - start.y
            dist = (dx * dx + dy * dy) ** 0.5

            # Also check max displacement across all frames
            max_dist = dist
            for bf in buffer:
                bdx = bf.x - start.x
                bdy = bf.y - start.y
                bd = (bdx * bdx + bdy * bdy) ** 0.5
                if bd > max_dist:
                    max_dist = bd
            effective_dist = max(dist, max_dist)

            dur = end.t - start.t

            if effective_dist < 30 and dur < 500:
                action = "tap"
            elif effective_dist < 30 and dur >= 500:
                action = "long_press"
            else:
                action = "swipe"

            g = Frame(
                id=0, t=start.t, type="gesture", action=action,
                x=start.x, y=start.y,
                x2=end.x, y2=end.y,
                duration_ms=dur,
                element=start.element,
            )
            gestures.append(g)
            buffer.clear()

        for f in self.frames:
            if f.type != "touch":
                flush()
                gestures.append(f)
                continue
            if f.action == "down" and buffer:
                flush()
            buffer.append(f)
            if f.action == "up":
                flush()

        flush()

        # Reassign IDs
        for i, g in enumerate(gestures, 1):
            g.id = i

        return gestures
