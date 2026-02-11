#!/usr/bin/env python3
"""
Flow configuration loader and validator for Android automation flows.

A flow is defined in YAML with the following structure:

    flow:
      name: "My App Automation"
      app_package: "com.example.myapp"
      app_activity: ".MainActivity"          # optional, auto-launches main
      device: "10.1.1.242:43849"             # optional, auto-detect
      loop: true                             # repeat the flow endlessly
      loop_count: 0                          # 0 = infinite when loop=true
      loop_delay: 5                          # seconds between loops
      screenshot_dir: "/img/captures"
      send_to:                               # where to send captured images
        method: "curl"                       # curl | adb-push | cp
        url: "https://example.com/upload"    # for curl
        path: "/img/sent"                    # for cp / adb-push
        headers:                             # optional HTTP headers
          Authorization: "Bearer xxx"

    on_error:
      notify: true                           # send notification on error
      notify_method: "webhook"               # webhook | file | stdout
      notify_url: "https://hooks.example.com/alert"
      screenshot_on_error: true              # capture screenshot when error
      max_retries: 3                         # retry failed step N times
      retry_delay: 2                         # seconds between retries
      pause_on_error: false                  # pause and wait for user

    steps:
      - action: "open_app"
        description: "Launch the app"

      - action: "wait"
        duration: 3

      - action: "tap_element"
        description: "Tap login button"
        resource_id: "com.example:id/btn_login"
        text: "Login"                        # fallback if resource_id fails
        timeout: 10                          # seconds to wait for element
        screenshot_after: true

      - action: "tap_coord"
        x: 540
        y: 1200
        description: "Tap at fixed coordinate"

      - action: "swipe"
        start_x: 540
        start_y: 1500
        end_x: 540
        end_y: 500
        duration_ms: 300
        description: "Scroll up"

      - action: "screenshot"
        stage: "after-login"
        send: true                           # send this screenshot to target

      - action: "key_event"
        keycode: 4                           # BACK key
        description: "Press back"

      - action: "close_app"
        description: "Force stop the app"

      - action: "shell"
        command: "am broadcast -a MY_ACTION"
        description: "Send custom broadcast"

      - action: "wait_element"
        resource_id: "com.example:id/home"
        timeout: 15
        description: "Wait for home screen"

      - action: "assert_element"
        resource_id: "com.example:id/title"
        text: "Welcome"
        description: "Verify welcome text"
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


@dataclass
class SendConfig:
    method: str = "cp"
    url: str = ""
    path: str = "/img/sent"
    headers: Dict[str, str] = field(default_factory=dict)


@dataclass
class ErrorConfig:
    notify: bool = True
    notify_method: str = "stdout"
    notify_url: str = ""
    screenshot_on_error: bool = True
    max_retries: int = 3
    retry_delay: float = 2.0
    pause_on_error: bool = False


@dataclass
class FlowStep:
    action: str
    description: str = ""
    # tap_element / wait_element / assert_element
    resource_id: Optional[str] = None
    text: Optional[str] = None
    timeout: float = 10.0
    screenshot_after: bool = False
    # tap_coord
    x: int = 0
    y: int = 0
    # swipe
    start_x: int = 0
    start_y: int = 0
    end_x: int = 0
    end_y: int = 0
    duration_ms: int = 300
    # wait
    duration: float = 1.0
    # screenshot
    stage: str = ""
    send: bool = False
    # key_event
    keycode: int = 0
    # shell
    command: str = ""


@dataclass
class FlowConfig:
    name: str = "Unnamed Flow"
    app_package: str = ""
    app_activity: str = ""
    device: str = ""
    loop: bool = False
    loop_count: int = 0
    loop_delay: float = 5.0
    screenshot_dir: str = "/img/captures"
    send_to: SendConfig = field(default_factory=SendConfig)
    on_error: ErrorConfig = field(default_factory=ErrorConfig)
    steps: List[FlowStep] = field(default_factory=list)


def _parse_step(raw: Dict[str, Any]) -> FlowStep:
    step = FlowStep(action=raw.get("action", ""))
    for k, v in raw.items():
        if hasattr(step, k):
            setattr(step, k, v)
    return step


def load_flow(path: Path) -> FlowConfig:
    """Load a flow configuration from a YAML file."""
    if yaml is None:
        print("ERROR: PyYAML is required. Install with: pip install pyyaml", file=sys.stderr)
        raise SystemExit(1)

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    flow_raw = raw.get("flow", {})
    send_raw = flow_raw.pop("send_to", {})
    error_raw = raw.get("on_error", {})
    steps_raw = raw.get("steps", [])

    config = FlowConfig()
    for k, v in flow_raw.items():
        if hasattr(config, k):
            setattr(config, k, v)

    config.send_to = SendConfig(**{k: v for k, v in send_raw.items() if hasattr(SendConfig, k) or k in SendConfig.__dataclass_fields__})
    config.on_error = ErrorConfig(**{k: v for k, v in error_raw.items() if k in ErrorConfig.__dataclass_fields__})
    config.steps = [_parse_step(s) for s in steps_raw]

    return config


def validate_flow(config: FlowConfig) -> List[str]:
    """Validate a flow configuration and return a list of warnings."""
    warnings: List[str] = []

    if not config.steps:
        warnings.append("Flow has no steps defined.")

    if not config.app_package:
        has_open = any(s.action == "open_app" for s in config.steps)
        if has_open:
            warnings.append("'open_app' step found but no app_package specified in flow config.")

    valid_actions = {
        "open_app", "close_app", "tap_element", "tap_coord", "swipe",
        "screenshot", "key_event", "wait", "wait_element", "assert_element",
        "shell",
    }
    for i, step in enumerate(config.steps):
        if step.action not in valid_actions:
            warnings.append(f"Step {i}: unknown action '{step.action}'")
        if step.action == "tap_element" and not step.resource_id and not step.text:
            warnings.append(f"Step {i}: tap_element needs resource_id or text")
        if step.action == "tap_coord" and step.x == 0 and step.y == 0:
            warnings.append(f"Step {i}: tap_coord has coordinates (0,0)")

    return warnings
