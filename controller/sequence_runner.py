#!/usr/bin/env python3
"""
Sequence Runner - Chain multiple .urf.json scripts and run them in sequence.

Supports:
  - Running a list of scripts in order (A -> B -> C -> D)
  - Looping the entire sequence (A B C D A B C D ...)
  - Configurable loop count (0 = infinite)
  - Delay between scripts
  - Delay between sequence loops
  - Speed control applied to all scripts
  - Error handling per-script (skip/stop)
  - Saving sequence definitions as .seq.json files

Usage:
    # Run scripts in sequence
    sequence_runner.py a.urf.json b.urf.json c.urf.json

    # Run with loop
    sequence_runner.py --loop --loop-count 5 a.urf.json b.urf.json

    # Run from sequence file
    sequence_runner.py --sequence my_sequence.seq.json

Sequence file format (.seq.json):
{
  "name": "EV Station Check All",
  "scripts": [
    {"path": "open_app.urf.json", "enabled": true},
    {"path": "check_station_1.urf.json", "enabled": true},
    {"path": "check_station_2.urf.json", "enabled": true},
    {"path": "close_app.urf.json", "enabled": true}
  ],
  "settings": {
    "loop": true,
    "loop_count": 0,
    "loop_delay": 10.0,
    "script_delay": 2.0,
    "speed": 1.0,
    "on_script_error": "skip",
    "replay_mode": "auto"
  }
}
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from universal_format import UniversalRecording
from universal_player import UniversalPlayer


class SequenceRunner:
    def __init__(
        self,
        scripts: List[Dict[str, Any]],
        loop: bool = False,
        loop_count: int = 0,
        loop_delay: float = 10.0,
        script_delay: float = 2.0,
        speed: float = 1.0,
        on_script_error: str = "skip",  # skip | stop
        replay_mode: str = "auto",
        serial: Optional[str] = None,
        dry_run: bool = False,
    ):
        self.scripts = scripts
        self.loop = loop
        self.loop_count = loop_count
        self.loop_delay = loop_delay
        self.script_delay = script_delay
        self.speed = speed
        self.on_script_error = on_script_error
        self.replay_mode = replay_mode
        self.serial = serial
        self.dry_run = dry_run
        self._running = False

        # Status tracking
        self.status = "idle"
        self.current_loop = 0
        self.current_script_idx = 0
        self.current_script_name = ""
        self.total_scripts = len([s for s in scripts if s.get("enabled", True)])
        self.results: List[Dict[str, Any]] = []

    def log(self, msg: str, level: str = "INFO") -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        icons = {"INFO": " ", "OK": "+", "WARN": "!", "ERROR": "X", "SEQ": "#"}
        icon = icons.get(level, " ")
        print(f"[{ts}] {icon} {msg}", flush=True)

    def get_status_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "current_loop": self.current_loop,
            "current_script_idx": self.current_script_idx,
            "current_script_name": self.current_script_name,
            "total_scripts": self.total_scripts,
            "results": self.results[-20:],
        }

    def run(self) -> int:
        """Run the sequence. Returns 0 on success."""
        enabled_scripts = [s for s in self.scripts if s.get("enabled", True)]
        if not enabled_scripts:
            self.log("No enabled scripts in sequence.", "WARN")
            return 1

        self.log(f"Sequence: {len(enabled_scripts)} scripts, loop={self.loop}, speed={self.speed}x", "SEQ")
        for i, s in enumerate(enabled_scripts):
            self.log(f"  [{i+1}] {Path(s['path']).name}", "SEQ")

        self._running = True
        self.status = "running"
        loop_idx = 0

        try:
            while self._running:
                loop_idx += 1
                self.current_loop = loop_idx

                if self.loop_count > 0 and loop_idx > self.loop_count:
                    break

                self.log(f"=== Sequence Loop {loop_idx} ===", "SEQ")

                for i, script_def in enumerate(enabled_scripts):
                    if not self._running:
                        break

                    self.current_script_idx = i + 1
                    script_path = Path(script_def["path"])
                    self.current_script_name = script_path.name

                    self.log(f"--- Script [{i+1}/{len(enabled_scripts)}]: {script_path.name} ---", "SEQ")

                    if not script_path.exists():
                        self.log(f"File not found: {script_path}", "ERROR")
                        self.results.append({
                            "loop": loop_idx, "script": script_path.name,
                            "status": "error", "error": "File not found",
                        })
                        if self.on_script_error == "stop":
                            self._running = False
                            break
                        continue

                    try:
                        recording = UniversalRecording.load(script_path)
                        if self.serial:
                            recording.meta.device = self.serial

                        player = UniversalPlayer(
                            recording=recording,
                            recording_path=script_path,
                            replay_mode=self.replay_mode,
                            speed=self.speed,
                            loop=False,  # individual scripts don't loop
                            loop_count=1,
                            dry_run=self.dry_run,
                        )
                        player.play()

                        self.results.append({
                            "loop": loop_idx, "script": script_path.name,
                            "status": "ok",
                        })
                        self.log(f"Script completed: {script_path.name}", "OK")

                    except Exception as e:
                        self.log(f"Script failed: {script_path.name} - {e}", "ERROR")
                        self.results.append({
                            "loop": loop_idx, "script": script_path.name,
                            "status": "error", "error": str(e),
                        })
                        if self.on_script_error == "stop":
                            self._running = False
                            break

                    # Delay between scripts
                    if self._running and i < len(enabled_scripts) - 1 and self.script_delay > 0:
                        self.log(f"Waiting {self.script_delay}s before next script...", "INFO")
                        time.sleep(self.script_delay)

                if not self.loop:
                    break

                if self._running and self.loop_delay > 0:
                    self.log(f"Waiting {self.loop_delay}s before next sequence loop...", "SEQ")
                    time.sleep(self.loop_delay)

        except KeyboardInterrupt:
            self.log("Stopped by user.", "WARN")
        finally:
            self._running = False
            self.status = "idle"
            self.log("Sequence runner stopped.", "SEQ")

        # Summary
        ok_count = sum(1 for r in self.results if r["status"] == "ok")
        err_count = sum(1 for r in self.results if r["status"] == "error")
        self.log(f"Summary: {ok_count} OK, {err_count} errors out of {len(self.results)} runs", "SEQ")

        return 0 if err_count == 0 else 1


def load_sequence_file(path: Path) -> Dict[str, Any]:
    """Load a .seq.json sequence definition file."""
    data = json.loads(path.read_text(encoding="utf-8"))
    # Resolve relative paths relative to the sequence file's directory
    base_dir = path.parent
    for script in data.get("scripts", []):
        sp = Path(script["path"])
        if not sp.is_absolute():
            script["path"] = str(base_dir / sp)
    return data


def save_sequence_file(path: Path, name: str, scripts: List[Dict[str, Any]],
                       settings: Dict[str, Any]) -> None:
    """Save a sequence definition to .seq.json."""
    data = {
        "name": name,
        "scripts": scripts,
        "settings": settings,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sequence Runner - Chain multiple .urf.json scripts"
    )
    parser.add_argument("scripts", nargs="*", type=Path,
                        help="Paths to .urf.json files to run in sequence")
    parser.add_argument("--sequence", type=Path,
                        help="Path to .seq.json sequence definition file")
    parser.add_argument("--loop", action="store_true", default=None,
                        help="Loop the entire sequence")
    parser.add_argument("--loop-count", type=int, default=None,
                        help="Number of sequence loops (0=infinite)")
    parser.add_argument("--loop-delay", type=float, default=None,
                        help="Delay between sequence loops (seconds)")
    parser.add_argument("--script-delay", type=float, default=None,
                        help="Delay between scripts (seconds)")
    parser.add_argument("--speed", type=float, default=None,
                        help="Speed multiplier for all scripts")
    parser.add_argument("--replay-mode", choices=["raw", "smart", "auto"], default=None,
                        help="Replay mode for all scripts")
    parser.add_argument("--on-error", choices=["skip", "stop"], default=None,
                        help="What to do when a script fails")
    parser.add_argument("-s", "--serial", help="Override device serial")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without executing")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    # Load from sequence file or CLI args
    if args.sequence:
        if not args.sequence.exists():
            print(f"Sequence file not found: {args.sequence}", file=sys.stderr)
            return 1
        seq_data = load_sequence_file(args.sequence)
        scripts = seq_data.get("scripts", [])
        settings = seq_data.get("settings", {})
    elif args.scripts:
        scripts = [{"path": str(p), "enabled": True} for p in args.scripts]
        settings = {}
    else:
        print("No scripts specified. Use positional args or --sequence.", file=sys.stderr)
        return 1

    # CLI overrides
    loop = args.loop if args.loop is not None else settings.get("loop", False)
    loop_count = args.loop_count if args.loop_count is not None else settings.get("loop_count", 0)
    loop_delay = args.loop_delay if args.loop_delay is not None else settings.get("loop_delay", 10.0)
    script_delay = args.script_delay if args.script_delay is not None else settings.get("script_delay", 2.0)
    speed = args.speed if args.speed is not None else settings.get("speed", 1.0)
    replay_mode = args.replay_mode if args.replay_mode is not None else settings.get("replay_mode", "auto")
    on_error = args.on_error if args.on_error is not None else settings.get("on_script_error", "skip")

    runner = SequenceRunner(
        scripts=scripts,
        loop=loop,
        loop_count=loop_count,
        loop_delay=loop_delay,
        script_delay=script_delay,
        speed=speed,
        on_script_error=on_error,
        replay_mode=replay_mode,
        serial=args.serial,
        dry_run=args.dry_run,
    )

    return runner.run()


if __name__ == "__main__":
    raise SystemExit(main())
