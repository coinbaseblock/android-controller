#!/usr/bin/env python3
"""
Flow Editor - Edit, adjust timing, insert/delete frames, configure loops.

Interactive editor for Universal Recording Format (.urf.json) files.

Features:
- View all frames with timing
- Insert new frames (tap, swipe, wait, screenshot, app, key, shell)
- Delete / disable / enable frames
- Adjust timing: shift frames, scale speed, add delays
- Configure loop and error settings
- Collapse raw touches to gestures
- Merge multiple recordings
- Export as simplified script (gestures only)

Usage:
    flow-editor.py /work/recording.urf.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from universal_format import Frame, UniversalRecording, KEY_NAMES


class FlowEditor:
    def __init__(self, path: Path):
        self.path = path
        self.recording = UniversalRecording.load(path)
        self._modified = False
        self._undo_stack: List[str] = []  # JSON snapshots for undo

    def _save_undo(self) -> None:
        """Save current state for undo."""
        snapshot = json.dumps([f.to_dict() for f in self.recording.frames], ensure_ascii=False)
        self._undo_stack.append(snapshot)
        if len(self._undo_stack) > 50:
            self._undo_stack.pop(0)

    def run(self) -> None:
        meta = self.recording.meta
        total = len(self.recording.frames)
        dur = meta.total_duration_ms / 1000

        print(f"\n=== Flow Editor ===")
        print(f"File: {self.path}")
        print(f"Name: {meta.name}")
        print(f"Frames: {total}, Duration: {dur:.1f}s, Mode: {meta.recording_mode}")
        print()
        self._show_help()

        try:
            while True:
                try:
                    raw = input("\nedit> ").strip()
                except EOFError:
                    break

                if not raw:
                    continue

                parts = raw.split()
                cmd = parts[0].lower()
                args = parts[1:]

                if cmd in ("q", "quit", "exit"):
                    if self._modified:
                        save = input("Save changes? (y/n): ").strip().lower()
                        if save == "y":
                            self._save()
                    break
                elif cmd in ("h", "help"):
                    self._show_help()
                elif cmd in ("l", "list"):
                    self._list(args)
                elif cmd in ("d", "detail"):
                    self._detail(args)
                elif cmd in ("del", "delete"):
                    self._delete(args)
                elif cmd in ("dis", "disable"):
                    self._toggle_enable(args, False)
                elif cmd in ("en", "enable"):
                    self._toggle_enable(args, True)
                elif cmd in ("i", "insert"):
                    self._insert(args)
                elif cmd in ("m", "move"):
                    self._move(args)
                elif cmd in ("t", "time"):
                    self._adjust_time(args)
                elif cmd in ("n", "note"):
                    self._set_note(args)
                elif cmd in ("speed",):
                    self._set_speed(args)
                elif cmd in ("loop",):
                    self._set_loop(args)
                elif cmd in ("error",):
                    self._set_error(args)
                elif cmd == "collapse":
                    self._collapse()
                elif cmd == "compact":
                    self._compact()
                elif cmd == "merge":
                    self._merge(args)
                elif cmd in ("s", "save"):
                    self._save()
                elif cmd in ("sa", "saveas"):
                    self._save_as(args)
                elif cmd == "undo":
                    self._undo()
                elif cmd == "info":
                    self._show_info()
                elif cmd == "export":
                    self._export(args)
                else:
                    print(f"  Unknown command: {cmd}. Type 'h' for help.")

        except KeyboardInterrupt:
            print()
            if self._modified:
                save = input("\nSave changes? (y/n): ").strip().lower()
                if save == "y":
                    self._save()

    def _show_help(self) -> None:
        print("Commands:")
        print("  l [N]          - List frames (last N, default 30)")
        print("  d <#id>        - Show frame detail")
        print("  del <#id>      - Delete frame")
        print("  dis <#id>      - Disable frame (skip during playback)")
        print("  en <#id>       - Enable frame")
        print("  i <pos> <type> - Insert frame (tap/swipe/wait/screenshot/app/key/shell)")
        print("  m <#id> <pos>  - Move frame to position")
        print("  t <#id> <+/-ms>- Adjust frame timing (e.g. t 5 +500, t 5 -200)")
        print("  t all <scale>  - Scale all timing (e.g. t all 2.0 = double delays)")
        print("  t shift <ms>   - Shift all frames by ms")
        print("  n <#id> <text> - Set note on frame")
        print("  speed <val>    - Set playback speed (e.g. speed 1.5)")
        print("  loop [on|off|N]- Set loop (on/off/count)")
        print("  error <mode>   - Set error mode (retry/skip/pause/stop)")
        print("  collapse       - Collapse raw touches → gestures")
        print("  compact        - Remove disabled frames & renumber")
        print("  merge <file>   - Append frames from another recording")
        print("  export <file>  - Export as simplified script (gestures only)")
        print("  info           - Show recording metadata & settings")
        print("  s              - Save")
        print("  sa <file>      - Save as")
        print("  undo           - Undo last change")
        print("  q              - Quit")

    def _list(self, args: List[str]) -> None:
        limit = int(args[0]) if args else 30
        frames = self.recording.frames
        start = max(0, len(frames) - limit) if limit < len(frames) else 0

        if not frames:
            print("  (no frames)")
            return

        # Show summary counts
        types = {}
        for f in frames:
            key = f.type
            types[key] = types.get(key, 0) + 1
        disabled = len([f for f in frames if not f.enabled])
        summary = ", ".join(f"{k}={v}" for k, v in sorted(types.items()))
        print(f"\n  Total: {len(frames)} frames ({summary})")
        if disabled:
            print(f"  Disabled: {disabled}")
        print()

        for f in frames[start:]:
            self._print_frame_line(f)

    def _print_frame_line(self, f: Frame) -> None:
        status = "" if f.enabled else " [OFF]"
        t_sec = f.t / 1000.0

        if f.type == "touch":
            print(f"  #{f.id:3d} [{t_sec:7.1f}s] touch {f.action:4s} ({f.x},{f.y}){status}")
        elif f.type == "gesture":
            el = ""
            if f.element and (f.element.get("text") or f.element.get("resource_id")):
                el = f" [{f.element.get('text') or f.element.get('resource_id','').split('/')[-1]}]"
            extra = ""
            if f.action == "swipe":
                extra = f" →({f.x2},{f.y2})"
            print(f"  #{f.id:3d} [{t_sec:7.1f}s] {f.action:10s} ({f.x},{f.y}){extra}{el}{status}")
        elif f.type == "element":
            label = f.text or f.resource_id.split("/")[-1]
            print(f"  #{f.id:3d} [{t_sec:7.1f}s] element    {f.action}: {label}{status}")
        elif f.type == "app":
            print(f"  #{f.id:3d} [{t_sec:7.1f}s] app        {f.action}: {f.package}{status}")
        elif f.type == "key":
            print(f"  #{f.id:3d} [{t_sec:7.1f}s] key        {f.key_name or f.keycode}{status}")
        elif f.type == "screenshot":
            send = " +send" if f.send else ""
            print(f"  #{f.id:3d} [{t_sec:7.1f}s] screenshot {f.stage}{send}{status}")
        elif f.type == "wait":
            print(f"  #{f.id:3d} [{t_sec:7.1f}s] wait       {f.duration_ms}ms{status}")
        elif f.type == "shell":
            print(f"  #{f.id:3d} [{t_sec:7.1f}s] shell      {f.command}{status}")
        elif f.type == "marker":
            print(f"  #{f.id:3d} [{t_sec:7.1f}s] marker     {f.note}{status}")

    def _find_frame(self, id_str: str) -> Optional[Frame]:
        try:
            fid = int(id_str.lstrip("#"))
        except ValueError:
            print(f"  Invalid ID: {id_str}")
            return None
        for f in self.recording.frames:
            if f.id == fid:
                return f
        print(f"  Frame #{fid} not found")
        return None

    def _detail(self, args: List[str]) -> None:
        if not args:
            print("  Usage: d <#id>")
            return
        f = self._find_frame(args[0])
        if f:
            print(json.dumps(f.to_dict(), indent=2, ensure_ascii=False))

    def _delete(self, args: List[str]) -> None:
        if not args:
            print("  Usage: del <#id> or del <#from>-<#to>")
            return

        self._save_undo()

        if "-" in args[0]:
            # Range delete
            parts = args[0].split("-")
            start_id = int(parts[0].lstrip("#"))
            end_id = int(parts[1].lstrip("#"))
            before = len(self.recording.frames)
            self.recording.frames = [f for f in self.recording.frames
                                      if not (start_id <= f.id <= end_id)]
            after = len(self.recording.frames)
            print(f"  Deleted {before - after} frames (#{start_id}-#{end_id})")
        else:
            f = self._find_frame(args[0])
            if f:
                self.recording.frames.remove(f)
                print(f"  Deleted frame #{f.id}")

        self._modified = True

    def _toggle_enable(self, args: List[str], enable: bool) -> None:
        if not args:
            print(f"  Usage: {'en' if enable else 'dis'} <#id>")
            return
        self._save_undo()
        f = self._find_frame(args[0])
        if f:
            f.enabled = enable
            print(f"  Frame #{f.id} {'enabled' if enable else 'disabled'}")
            self._modified = True

    def _insert(self, args: List[str]) -> None:
        if len(args) < 2:
            print("  Usage: i <position> <type>")
            print("  Types: tap, swipe, wait, screenshot, app, key, shell, element, marker")
            return

        self._save_undo()

        pos = int(args[0])
        ftype = args[1]

        # Calculate time offset based on position
        if pos > 0 and pos <= len(self.recording.frames):
            t = self.recording.frames[pos - 1].t + 500
        elif self.recording.frames:
            t = self.recording.frames[-1].t + 500
        else:
            t = 0

        frame = Frame(id=self.recording.next_id(), t=t, type=ftype)

        if ftype in ("tap", "gesture"):
            frame.type = "gesture"
            frame.action = "tap"
            coord = input("  Coordinates (x,y): ").strip()
            if coord and "," in coord:
                x, y = coord.split(",", 1)
                frame.x, frame.y = int(x.strip()), int(y.strip())

        elif ftype == "swipe":
            frame.type = "gesture"
            frame.action = "swipe"
            start = input("  Start (x,y): ").strip()
            end = input("  End (x,y): ").strip()
            dur = input("  Duration ms [300]: ").strip() or "300"
            if start and "," in start:
                x, y = start.split(",", 1)
                frame.x, frame.y = int(x.strip()), int(y.strip())
            if end and "," in end:
                x, y = end.split(",", 1)
                frame.x2, frame.y2 = int(x.strip()), int(y.strip())
            frame.duration_ms = int(dur)

        elif ftype == "element":
            frame.type = "element"
            frame.action = "tap"
            rid = input("  resource_id (or empty): ").strip()
            txt = input("  text (or empty): ").strip()
            if rid:
                frame.resource_id = rid
            if txt:
                frame.text = txt

        elif ftype == "wait":
            ms = input("  Duration ms [2000]: ").strip() or "2000"
            frame.duration_ms = int(ms)

        elif ftype == "screenshot":
            frame.stage = input("  Stage name: ").strip() or "capture"
            send = input("  Send? (y/n) [y]: ").strip().lower()
            frame.send = send != "n"

        elif ftype == "app":
            frame.action = input("  Action (open/close): ").strip() or "open"
            frame.package = input(f"  Package [{self.recording.meta.app_package}]: ").strip()
            if not frame.package:
                frame.package = self.recording.meta.app_package

        elif ftype == "key":
            code = input("  Keycode (4=BACK, 3=HOME): ").strip() or "4"
            frame.keycode = int(code)
            frame.key_name = KEY_NAMES.get(frame.keycode, "")

        elif ftype == "shell":
            frame.command = input("  Command: ").strip()

        elif ftype == "marker":
            frame.note = input("  Note: ").strip()

        # Insert at position
        if pos >= len(self.recording.frames):
            self.recording.frames.append(frame)
        else:
            self.recording.frames.insert(pos, frame)

        self._modified = True
        print(f"  Inserted #{frame.id} at position {pos}")

    def _move(self, args: List[str]) -> None:
        if len(args) < 2:
            print("  Usage: m <#id> <new_position>")
            return
        self._save_undo()
        f = self._find_frame(args[0])
        if not f:
            return
        new_pos = int(args[1])
        self.recording.frames.remove(f)
        if new_pos >= len(self.recording.frames):
            self.recording.frames.append(f)
        else:
            self.recording.frames.insert(new_pos, f)
        self._modified = True
        print(f"  Moved #{f.id} to position {new_pos}")

    def _adjust_time(self, args: List[str]) -> None:
        if not args:
            print("  Usage: t <#id> <+/-ms>  |  t all <scale>  |  t shift <ms>")
            return

        self._save_undo()

        if args[0] == "all" and len(args) > 1:
            scale = float(args[1])
            for f in self.recording.frames:
                f.t = int(f.t * scale)
                if f.type == "wait":
                    f.duration_ms = int(f.duration_ms * scale)
            print(f"  Scaled all timing by {scale}x")
            self._modified = True
            return

        if args[0] == "shift" and len(args) > 1:
            shift = int(args[1])
            for f in self.recording.frames:
                f.t = max(0, f.t + shift)
            print(f"  Shifted all frames by {shift}ms")
            self._modified = True
            return

        if len(args) < 2:
            print("  Usage: t <#id> <+/-ms>")
            return

        f = self._find_frame(args[0])
        if not f:
            return

        offset_str = args[1]
        if offset_str.startswith("+") or offset_str.startswith("-"):
            offset = int(offset_str)
            f.t = max(0, f.t + offset)
        else:
            f.t = max(0, int(offset_str))

        self._modified = True
        print(f"  Frame #{f.id} time → {f.t}ms ({f.t/1000:.1f}s)")

    def _set_note(self, args: List[str]) -> None:
        if len(args) < 2:
            print("  Usage: n <#id> <text>")
            return
        f = self._find_frame(args[0])
        if f:
            f.note = " ".join(args[1:])
            self._modified = True
            print(f"  Note set on #{f.id}")

    def _set_speed(self, args: List[str]) -> None:
        if not args:
            print(f"  Current speed: {self.recording.settings.speed}x")
            return
        self.recording.settings.speed = float(args[0])
        self._modified = True
        print(f"  Speed set to {self.recording.settings.speed}x")

    def _set_loop(self, args: List[str]) -> None:
        if not args:
            s = self.recording.settings
            print(f"  Loop: {'on' if s.loop else 'off'}, count: {s.loop_count}, delay: {s.loop_delay}s")
            return

        s = self.recording.settings
        if args[0] == "on":
            s.loop = True
        elif args[0] == "off":
            s.loop = False
        elif args[0] == "delay" and len(args) > 1:
            s.loop_delay = float(args[1])
        else:
            try:
                s.loop_count = int(args[0])
                s.loop = True
            except ValueError:
                print(f"  Usage: loop [on|off|N|delay N]")
                return

        self._modified = True
        print(f"  Loop: {'on' if s.loop else 'off'}, count: {s.loop_count}, delay: {s.loop_delay}s")

    def _set_error(self, args: List[str]) -> None:
        if not args:
            s = self.recording.settings
            print(f"  Error mode: {s.on_error}, retries: {s.max_retries}, delay: {s.retry_delay}s")
            return

        valid = {"retry", "skip", "pause", "stop"}
        if args[0] in valid:
            self.recording.settings.on_error = args[0]
            self._modified = True
            print(f"  Error mode: {args[0]}")
        else:
            print(f"  Valid modes: {', '.join(valid)}")

    def _collapse(self) -> None:
        """Collapse raw touches into gestures."""
        self._save_undo()
        gestures = self.recording.collapse_raw_to_gestures()
        raw_count = len([f for f in self.recording.frames if f.type == "touch"])
        self.recording.frames = gestures
        gesture_count = len([f for f in gestures if f.type == "gesture"])
        self._modified = True
        print(f"  Collapsed {raw_count} raw touches → {gesture_count} gestures")
        print(f"  Total frames: {len(self.recording.frames)}")

    def _compact(self) -> None:
        """Remove disabled frames and renumber."""
        self._save_undo()
        before = len(self.recording.frames)
        self.recording.frames = [f for f in self.recording.frames if f.enabled]
        for i, f in enumerate(self.recording.frames, 1):
            f.id = i
        after = len(self.recording.frames)
        self._modified = True
        print(f"  Removed {before - after} disabled frames. {after} remaining.")

    def _merge(self, args: List[str]) -> None:
        if not args:
            print("  Usage: merge <file.urf.json>")
            return

        merge_path = Path(args[0])
        if not merge_path.exists():
            print(f"  File not found: {merge_path}")
            return

        self._save_undo()
        other = UniversalRecording.load(merge_path)

        # Offset other frames by current last time
        time_offset = self.recording.total_duration() + 1000  # 1s gap
        base_id = self.recording.next_id()

        for i, f in enumerate(other.frames):
            f.t += time_offset
            f.id = base_id + i
            self.recording.frames.append(f)

        self._modified = True
        print(f"  Merged {len(other.frames)} frames from {merge_path}")
        print(f"  Total frames: {len(self.recording.frames)}")

    def _export(self, args: List[str]) -> None:
        if not args:
            print("  Usage: export <output.urf.json>")
            return

        out = Path(args[0])
        # Create a copy with only gesture/element/control frames (no raw touches)
        exported = UniversalRecording()
        exported.meta = self.recording.meta
        exported.settings = self.recording.settings
        exported.meta.recording_mode = "script"

        for f in self.recording.frames:
            if f.type != "touch" and f.enabled:
                exported.frames.append(f)

        # Renumber
        for i, f in enumerate(exported.frames, 1):
            f.id = i

        exported.save(out)
        print(f"  Exported {len(exported.frames)} frames to {out}")

    def _save(self) -> None:
        self.recording.meta.total_duration_ms = self.recording.total_duration()
        self.recording.save(self.path)
        self._modified = False
        print(f"  Saved: {self.path}")

    def _save_as(self, args: List[str]) -> None:
        if not args:
            print("  Usage: sa <filename>")
            return
        new_path = Path(args[0])
        self.recording.meta.total_duration_ms = self.recording.total_duration()
        self.recording.save(new_path)
        self.path = new_path
        self._modified = False
        print(f"  Saved as: {new_path}")

    def _undo(self) -> None:
        if not self._undo_stack:
            print("  Nothing to undo")
            return
        snapshot = self._undo_stack.pop()
        frames_data = json.loads(snapshot)
        self.recording.frames = [Frame.from_dict(d) for d in frames_data]
        self._modified = True
        print(f"  Undone. Frames: {len(self.recording.frames)}")

    def _show_info(self) -> None:
        m = self.recording.meta
        s = self.recording.settings
        print(f"\n  Recording Info:")
        print(f"    Name:     {m.name}")
        print(f"    Created:  {m.created}")
        print(f"    Device:   {m.device}")
        print(f"    Screen:   {m.screen_width}x{m.screen_height}")
        print(f"    Package:  {m.app_package}")
        print(f"    Mode:     {m.recording_mode}")
        print(f"    Duration: {m.total_duration_ms/1000:.1f}s")
        print(f"    Frames:   {len(self.recording.frames)}")
        print(f"\n  Settings:")
        print(f"    Speed:    {s.speed}x")
        print(f"    Loop:     {'on' if s.loop else 'off'} (count={s.loop_count}, delay={s.loop_delay}s)")
        print(f"    On error: {s.on_error} (retries={s.max_retries}, delay={s.retry_delay}s)")
        print(f"    Screenshots: {s.screenshot_dir}")
        print(f"    Send to:  {s.send_to}")


# ---------- CLI ----------

def main() -> int:
    parser = argparse.ArgumentParser(description="Flow Editor - Edit Universal Recording Format files")
    parser.add_argument("recording", type=Path, help="Path to .urf.json file")
    args = parser.parse_args()

    if not args.recording.exists():
        print(f"File not found: {args.recording}", file=sys.stderr)
        return 1

    editor = FlowEditor(args.recording)
    editor.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
