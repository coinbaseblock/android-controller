#!/usr/bin/env python3
"""
Smart element interaction for Android automation.

Provides:
- Find elements by resource-id, text, class with retry/timeout
- Detect obstructions (popups, notifications, overlays blocking target)
- Dismiss common obstructions automatically
- Report element visibility and clickability state
"""

from __future__ import annotations

import re
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BOUNDS_PATTERN = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")

# Common obstruction patterns (class names / resource-ids that indicate overlays)
OBSTRUCTION_CLASSES = {
    "android.widget.FrameLayout",
    "android.widget.RelativeLayout",
    "android.widget.LinearLayout",
}

DIALOG_INDICATORS = [
    "alertTitle",
    "dialog",
    "popup",
    "snackbar",
    "toast",
    "permission",
    "overlay",
    "com.android.systemui",
]

DISMISS_TEXTS = [
    "OK", "ตกลง", "DISMISS", "CANCEL", "ยกเลิก", "CLOSE", "ปิด",
    "GOT IT", "ALLOW", "อนุญาต", "DENY", "ปฏิเสธ", "NOT NOW",
    "SKIP", "ข้าม", "NO THANKS",
]


@dataclass
class ElementInfo:
    resource_id: str = ""
    text: str = ""
    content_desc: str = ""
    cls: str = ""
    bounds: Tuple[int, int, int, int] = (0, 0, 0, 0)
    center: Tuple[int, int] = (0, 0)
    clickable: bool = False
    enabled: bool = True
    focusable: bool = False
    visible: bool = True
    index: int = 0


@dataclass
class ObstructionReport:
    is_obstructed: bool = False
    obstruction_type: str = ""  # "dialog" | "notification" | "popup" | "system_ui" | ""
    obstruction_elements: List[ElementInfo] = field(default_factory=list)
    dismissible: bool = False
    dismiss_element: Optional[ElementInfo] = None
    message: str = ""


class AdbError(RuntimeError):
    pass


class ElementNotFoundError(RuntimeError):
    pass


class ElementObstructedError(RuntimeError):
    def __init__(self, message: str, report: ObstructionReport):
        super().__init__(message)
        self.report = report


def adb_cmd(serial: Optional[str]) -> List[str]:
    return ["adb", "-s", serial] if serial else ["adb"]


def run_adb(cmd: List[str], timeout: float = 30) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise AdbError(result.stderr.strip() or "adb command failed")
    return result.stdout


def get_ui_xml(serial: Optional[str]) -> str:
    """Dump current UI hierarchy as XML string."""
    prefix = adb_cmd(serial)
    remote = "/sdcard/window_dump.xml"
    run_adb(prefix + ["shell", "uiautomator", "dump", remote])
    return run_adb(prefix + ["exec-out", "cat", remote])


def parse_bounds(bounds_str: str) -> Tuple[int, int, int, int]:
    m = BOUNDS_PATTERN.match(bounds_str)
    if not m:
        return (0, 0, 0, 0)
    return tuple(int(x) for x in m.groups())  # type: ignore[return-value]


def parse_all_elements(xml_str: str) -> List[ElementInfo]:
    """Parse all elements from UI hierarchy XML."""
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return []

    elements: List[ElementInfo] = []
    idx = 0

    def walk(node: ET.Element) -> None:
        nonlocal idx
        if node.tag == "node":
            attrs = node.attrib
            bounds = parse_bounds(attrs.get("bounds", ""))
            cx = (bounds[0] + bounds[2]) // 2
            cy = (bounds[1] + bounds[3]) // 2
            elements.append(ElementInfo(
                resource_id=attrs.get("resource-id", ""),
                text=attrs.get("text", ""),
                content_desc=attrs.get("content-desc", ""),
                cls=attrs.get("class", ""),
                bounds=bounds,
                center=(cx, cy),
                clickable=attrs.get("clickable", "false") == "true",
                enabled=attrs.get("enabled", "true") == "true",
                focusable=attrs.get("focusable", "false") == "true",
                visible=bounds != (0, 0, 0, 0),
                index=idx,
            ))
            idx += 1
        for child in node:
            walk(child)

    walk(root)
    return elements


def find_element(
    elements: List[ElementInfo],
    resource_id: Optional[str] = None,
    text: Optional[str] = None,
    cls: Optional[str] = None,
    partial_text: bool = False,
) -> Optional[ElementInfo]:
    """Find first matching element."""
    for el in elements:
        if resource_id and el.resource_id == resource_id:
            return el
        if text:
            if partial_text and text.lower() in el.text.lower():
                return el
            elif not partial_text and el.text == text:
                return el
        if cls and el.cls == cls and not resource_id and not text:
            return el
    return None


def find_all_elements(
    elements: List[ElementInfo],
    resource_id: Optional[str] = None,
    text: Optional[str] = None,
) -> List[ElementInfo]:
    """Find all matching elements."""
    results: List[ElementInfo] = []
    for el in elements:
        if resource_id and el.resource_id == resource_id:
            results.append(el)
        elif text and el.text == text:
            results.append(el)
    return results


def _bounds_overlap(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> bool:
    """Check if bounds a and b overlap."""
    return not (a[2] <= b[0] or a[0] >= b[2] or a[3] <= b[1] or a[1] >= b[3])


def _bounds_contains_point(bounds: Tuple[int, int, int, int], point: Tuple[int, int]) -> bool:
    return bounds[0] <= point[0] <= bounds[2] and bounds[1] <= point[1] <= bounds[3]


def _is_fullscreen(bounds: Tuple[int, int, int, int], screen_w: int = 1080, screen_h: int = 2400) -> bool:
    """Check if bounds cover most of the screen (likely an overlay)."""
    w = bounds[2] - bounds[0]
    h = bounds[3] - bounds[1]
    return w > screen_w * 0.8 and h > screen_h * 0.5


def detect_obstruction(
    elements: List[ElementInfo],
    target: ElementInfo,
) -> ObstructionReport:
    """Detect if an element is obstructed by dialogs, popups, or notifications."""
    report = ObstructionReport()
    target_idx = target.index

    # Check elements that appear AFTER target in the tree (drawn on top)
    overlapping: List[ElementInfo] = []
    for el in elements:
        if el.index <= target_idx:
            continue
        if not el.visible:
            continue
        # Check if this element overlaps the target's center
        if _bounds_contains_point(el.bounds, target.center):
            overlapping.append(el)

    if not overlapping:
        return report

    # Classify the obstruction
    report.is_obstructed = True
    report.obstruction_elements = overlapping

    # Check for dialog indicators
    for el in overlapping:
        rid_lower = el.resource_id.lower()
        text_lower = el.text.lower()
        cls_lower = el.cls.lower()

        for indicator in DIALOG_INDICATORS:
            if indicator in rid_lower or indicator in text_lower or indicator in cls_lower:
                report.obstruction_type = "dialog"
                report.message = f"Dialog detected: resource={el.resource_id}, text={el.text}"
                break

        if "systemui" in rid_lower or "com.android.systemui" in rid_lower:
            report.obstruction_type = "system_ui"
            report.message = f"System UI overlay: {el.resource_id}"

    if not report.obstruction_type:
        report.obstruction_type = "popup"
        report.message = f"Unknown overlay with {len(overlapping)} elements on top of target"

    # Check if dismissible
    for el in elements:
        if el.index <= target_idx:
            continue
        if not el.clickable:
            continue
        for dismiss_text in DISMISS_TEXTS:
            if dismiss_text.lower() == el.text.lower():
                report.dismissible = True
                report.dismiss_element = el
                report.message += f" (dismissible via '{el.text}')"
                return report

    return report


def wait_for_element(
    serial: Optional[str],
    resource_id: Optional[str] = None,
    text: Optional[str] = None,
    timeout: float = 10.0,
    poll_interval: float = 1.0,
) -> Tuple[Optional[ElementInfo], List[ElementInfo], Optional[ObstructionReport]]:
    """
    Wait for an element to appear and be interactable.

    Returns:
        (element, all_elements, obstruction_report)
        - element is None if not found within timeout
        - obstruction_report is set if element is found but obstructed
    """
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            xml = get_ui_xml(serial)
            all_elements = parse_all_elements(xml)
            el = find_element(all_elements, resource_id=resource_id, text=text)

            if el is not None:
                obstruction = detect_obstruction(all_elements, el)
                return el, all_elements, obstruction

        except (AdbError, ET.ParseError):
            pass

        remaining = deadline - time.time()
        if remaining > 0:
            time.sleep(min(poll_interval, remaining))

    return None, [], None


def tap_element(
    serial: Optional[str],
    resource_id: Optional[str] = None,
    text: Optional[str] = None,
    timeout: float = 10.0,
    auto_dismiss: bool = True,
    max_dismiss_attempts: int = 3,
) -> Tuple[ElementInfo, Optional[ObstructionReport]]:
    """
    Find and tap an element, handling obstructions.

    If auto_dismiss is True and element is obstructed by a dismissible dialog,
    attempt to dismiss it first.

    Raises:
        ElementNotFoundError: if element not found within timeout
        ElementObstructedError: if element is obstructed and cannot be dismissed
    """
    prefix = adb_cmd(serial)
    dismiss_attempts = 0

    while dismiss_attempts <= max_dismiss_attempts:
        el, all_elements, obstruction = wait_for_element(
            serial, resource_id=resource_id, text=text, timeout=timeout
        )

        if el is None:
            raise ElementNotFoundError(
                f"Element not found: resource_id={resource_id}, text={text} "
                f"(waited {timeout}s)"
            )

        if obstruction and obstruction.is_obstructed:
            if auto_dismiss and obstruction.dismissible and obstruction.dismiss_element:
                dismiss_el = obstruction.dismiss_element
                print(f"  [auto-dismiss] Tapping '{dismiss_el.text}' at {dismiss_el.center}")
                run_adb(prefix + [
                    "shell", "input", "tap",
                    str(dismiss_el.center[0]), str(dismiss_el.center[1]),
                ])
                time.sleep(1.0)
                dismiss_attempts += 1
                continue
            else:
                raise ElementObstructedError(
                    f"Element obstructed: {obstruction.message}",
                    obstruction,
                )

        # Element found and not obstructed, tap it
        run_adb(prefix + [
            "shell", "input", "tap",
            str(el.center[0]), str(el.center[1]),
        ])
        return el, obstruction

    raise ElementObstructedError(
        f"Could not dismiss obstruction after {max_dismiss_attempts} attempts",
        obstruction or ObstructionReport(),
    )


def get_current_app(serial: Optional[str]) -> Dict[str, str]:
    """Get the currently focused app package and activity."""
    prefix = adb_cmd(serial)
    output = run_adb(prefix + ["shell", "dumpsys", "activity", "activities"])
    # Parse mResumedActivity or mFocusedActivity
    for line in output.splitlines():
        if "mResumedActivity" in line or "mFocusedActivity" in line:
            # Extract package/activity from the line
            match = re.search(r'(\S+)/(\S+)', line)
            if match:
                return {"package": match.group(1), "activity": match.group(2)}
    return {"package": "", "activity": ""}


def launch_app(serial: Optional[str], package: str, activity: str = "") -> None:
    """Launch an app by package name."""
    prefix = adb_cmd(serial)
    if activity:
        component = f"{package}/{activity}"
        run_adb(prefix + ["shell", "am", "start", "-n", component])
    else:
        run_adb(prefix + ["shell", "monkey", "-p", package, "-c",
                          "android.intent.category.LAUNCHER", "1"])


def force_stop_app(serial: Optional[str], package: str) -> None:
    """Force stop an app."""
    prefix = adb_cmd(serial)
    run_adb(prefix + ["shell", "am", "force-stop", package])


def take_screenshot(serial: Optional[str], output_path: Path) -> Path:
    """Take a screenshot and save to output_path."""
    prefix = adb_cmd(serial)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    remote = "/sdcard/automation_screen.png"
    run_adb(prefix + ["shell", "screencap", "-p", remote])
    run_adb(prefix + ["pull", remote, str(output_path)])
    return output_path


def send_key_event(serial: Optional[str], keycode: int) -> None:
    """Send a key event."""
    prefix = adb_cmd(serial)
    run_adb(prefix + ["shell", "input", "keyevent", str(keycode)])


def send_swipe(
    serial: Optional[str],
    start_x: int, start_y: int,
    end_x: int, end_y: int,
    duration_ms: int = 300,
) -> None:
    """Send a swipe gesture."""
    prefix = adb_cmd(serial)
    run_adb(prefix + [
        "shell", "input", "swipe",
        str(start_x), str(start_y),
        str(end_x), str(end_y),
        str(duration_ms),
    ])


def send_tap(serial: Optional[str], x: int, y: int) -> None:
    """Send a tap at coordinates."""
    prefix = adb_cmd(serial)
    run_adb(prefix + ["shell", "input", "tap", str(x), str(y)])
