#!/usr/bin/env python3
"""
Capture an Android UI hierarchy via `uiautomator dump`, pull the XML locally,
parse key fields, and store a lookup-friendly JSON structure.

Features:
- Runs `adb shell uiautomator dump /sdcard/window_dump.xml` followed by
  `adb pull` to retrieve the hierarchy.
- Extracts `resource-id`, `text`, `class`, and `bounds` attributes and computes
  the center point of each node's bounds.
- Stores data for quick lookup by resource-id or text alongside the center
  coordinates for replaying touch events.
- Tags every dump with a timestamp and an optional stage identifier.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BOUNDS_PATTERN = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
DEFAULT_OUTPUT = "/work/ui-dump.json"
REMOTE_XML_PATH = "/sdcard/window_dump.xml"


class AdbError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture and parse an Android UI hierarchy for replay/lookup",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Output JSON file path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--stage",
        help="Optional stage identifier to tag the dump (e.g., login, checkout)",
    )
    parser.add_argument(
        "-s",
        "--serial",
        help="ADB serial to target a specific device",
    )
    parser.add_argument(
        "--keep-xml",
        action="store_true",
        help=(
            "Keep the pulled XML next to the output file instead of removing the "
            "temporary copy"
        ),
    )
    return parser.parse_args()


def adb_base(serial: Optional[str]) -> List[str]:
    return ["adb", "-s", serial] if serial else ["adb"]


def run_adb(cmd: List[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise AdbError(result.stderr.strip() or "adb command failed")


def capture_xml(serial: Optional[str], destination: Path) -> Path:
    base = adb_base(serial)
    run_adb(base + ["shell", "uiautomator", "dump", REMOTE_XML_PATH])
    run_adb(base + ["pull", REMOTE_XML_PATH, str(destination)])
    return destination


def parse_bounds(bounds: str) -> Tuple[int, int, int, int]:
    match = BOUNDS_PATTERN.match(bounds)
    if not match:
        raise ValueError(f"Invalid bounds format: {bounds}")
    x1, y1, x2, y2 = map(int, match.groups())
    return x1, y1, x2, y2


def compute_center(bounds: Tuple[int, int, int, int]) -> Tuple[int, int]:
    x1, y1, x2, y2 = bounds
    return (x1 + x2) // 2, (y1 + y2) // 2


def parse_nodes(element: ET.Element) -> List[Dict[str, object]]:
    nodes: List[Dict[str, object]] = []

    def walk(node: ET.Element) -> None:
        if node.tag == "node":
            attrs = node.attrib
            bounds_raw = attrs.get("bounds")
            if bounds_raw:
                try:
                    bounds = parse_bounds(bounds_raw)
                    center_x, center_y = compute_center(bounds)
                except ValueError:
                    bounds = None
                    center_x = center_y = None
            else:
                bounds = None
                center_x = center_y = None

            nodes.append(
                {
                    "resource_id": attrs.get("resource-id", ""),
                    "text": attrs.get("text", ""),
                    "class": attrs.get("class", ""),
                    "bounds": {
                        "x1": bounds[0] if bounds else None,
                        "y1": bounds[1] if bounds else None,
                        "x2": bounds[2] if bounds else None,
                        "y2": bounds[3] if bounds else None,
                    },
                    "center": {"x": center_x, "y": center_y},
                }
            )

        for child in node:
            walk(child)

    walk(element)
    return nodes


def build_lookup(nodes: List[Dict[str, object]]) -> Dict[str, Dict[str, List[int]]]:
    by_resource_id: Dict[str, List[int]] = {}
    by_text: Dict[str, List[int]] = {}

    for idx, node in enumerate(nodes):
        res_id = str(node.get("resource_id", ""))
        text = str(node.get("text", ""))
        if res_id:
            by_resource_id.setdefault(res_id, []).append(idx)
        if text:
            by_text.setdefault(text, []).append(idx)

    return {"by_resource_id": by_resource_id, "by_text": by_text}


def load_xml(path: Path) -> ET.Element:
    tree = ET.parse(path)
    return tree.getroot()


def build_output(
    nodes: List[Dict[str, object]],
    stage: Optional[str],
    pulled_xml: Path,
) -> Dict[str, object]:
    payload: Dict[str, object] = {
        "captured_at": dt.datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
        "stage": stage,
        "source_xml": str(pulled_xml),
        "nodes": nodes,
        "lookup": build_lookup(nodes),
    }
    return payload


def main() -> int:
    args = parse_args()
    output_path = Path(args.output).expanduser()

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            xml_path = Path(tmpdir) / "window_dump.xml"
            pulled_path = capture_xml(args.serial, xml_path)
            root = load_xml(pulled_path)
            nodes = parse_nodes(root)
            payload = build_output(nodes, args.stage, pulled_path)

            if args.keep_xml:
                xml_target = output_path.with_suffix(".xml")
                xml_target.write_text(pulled_path.read_text(encoding="utf-8"), encoding="utf-8")
                payload["source_xml"] = str(xml_target)

            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except FileNotFoundError:
        print("adb not found. Ensure Android platform tools are available.", file=sys.stderr)
        return 1
    except AdbError as exc:
        print(f"ADB error: {exc}", file=sys.stderr)
        return 1

    print(f"Saved {len(payload['nodes'])} nodes to {output_path}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())