#!/usr/bin/env python3
"""
Overlay touch markers from a log file onto a screenshot.

Supports the JSON/CSV output produced by `touch_event_capture.py` and draws
numbered circles on a copy of the screenshot to visualize tap/drag paths.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Dict, Iterable, List

from PIL import Image, ImageDraw, ImageFont


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Draw touch markers from a log onto a screenshot image"
    )
    parser.add_argument("log", type=Path, help="Touch log in JSON or CSV format")
    parser.add_argument("screenshot", type=Path, help="Screenshot image to annotate")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output path (default: add -marked before the file extension)",
    )
    return parser.parse_args()


def load_events(log_path: Path) -> List[Dict[str, object]]:
    if log_path.suffix.lower() == ".csv":
        return list(_load_csv(log_path))
    return json.loads(log_path.read_text(encoding="utf-8"))


def _load_csv(log_path: Path) -> Iterable[Dict[str, object]]:
    with log_path.open(newline="", encoding="utf-8") as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            yield {
                "timestamp": float(row["timestamp"]),
                "x": int(row["x"]),
                "y": int(row["y"]),
                "action": row["action"],
            }


def derive_output_path(screenshot_path: Path, explicit: Path | None) -> Path:
    if explicit:
        return explicit
    stem = screenshot_path.stem
    return screenshot_path.with_name(f"{stem}-marked{screenshot_path.suffix}")


def draw_markers(events: List[Dict[str, object]], screenshot_path: Path, output_path: Path) -> None:
    image = Image.open(screenshot_path).convert("RGBA")
    overlay = Image.new("RGBA", image.size, (255, 255, 255, 0))
    draw = ImageDraw.Draw(overlay)

    font = ImageFont.load_default()
    radius = 18

    for idx, event in enumerate(events, start=1):
        x = int(event["x"])
        y = int(event["y"])
        bbox = [
            (x - radius, y - radius),
            (x + radius, y + radius),
        ]
        color = (255, 0, 0, 180) if event.get("action") == "down" else (0, 122, 255, 150)
        draw.ellipse(bbox, outline=color, width=3)
        text = str(idx)
        text_size = draw.textbbox((0, 0), text, font=font)
        text_width = text_size[2] - text_size[0]
        text_height = text_size[3] - text_size[1]
        text_pos = (x - text_width / 2, y - text_height / 2)
        draw.text(text_pos, text, font=font, fill=(0, 0, 0, 230))

    combined = Image.alpha_composite(image, overlay).convert("RGB")
    combined.save(output_path)


def main() -> int:
    args = parse_args()
    events = load_events(args.log)
    output_path = derive_output_path(args.screenshot, args.output)

    draw_markers(events, args.screenshot, output_path)
    print(f"Wrote annotated screenshot to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())