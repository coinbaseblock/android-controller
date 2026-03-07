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
import shutil
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

    def _compress_captures(self) -> int:
        """Keep PNG captures as-is (no JPEG conversion).

        PNG format is preserved per configuration. Space is managed by
        archiving completed loop captures to HDD and periodic cleanup.
        Returns 0 (no conversion performed).
        """
        return 0

    @staticmethod
    def _is_station_data_file(name: str) -> bool:
        """Check if file contains station/extract data that must not be deleted or compressed."""
        return (name.endswith("-extract.jsonl") or name.startswith("extract-")
                or name.endswith("-playback.json") or name.endswith("-playback.html"))

    def _compress_old_logs(self) -> int:
        """Gzip log files older than 1 hour. Returns bytes saved.

        NEVER compresses station data files (*-extract.jsonl, *-playback.json)
        because they are actively appended to during playback and contain
        station counting data needed for accurate loop tracking.
        """
        import gzip as _gzip
        saved = 0
        cutoff = time.time() - 3600
        for log_dir in [Path("/work/logs"), Path("/img/logs")]:
            if not log_dir.exists():
                continue
            for f in list(log_dir.iterdir()):
                if not f.is_file() or f.suffix == ".gz":
                    continue
                if f.suffix not in (".jsonl", ".json", ".log", ".txt"):
                    continue
                # Never compress station/extract data – it's actively used
                if self._is_station_data_file(f.name):
                    continue
                try:
                    st = f.stat()
                    if st.st_mtime >= cutoff or st.st_size < 1024:
                        continue
                    data = f.read_bytes()
                    gz = f.with_suffix(f.suffix + ".gz")
                    with _gzip.open(gz, "wb", compresslevel=6) as gf:
                        gf.write(data)
                    saved += st.st_size - gz.stat().st_size
                    f.unlink()
                except Exception:
                    pass
        return saved

    @staticmethod
    def _get_overflow_root() -> Optional[Path]:
        """Return the overflow root directory, or None if not configured/available."""
        # Check config file first
        config_path = Path("/work/.overflow_config")
        if config_path.exists():
            try:
                import json as _json
                cfg = _json.loads(config_path.read_text(encoding="utf-8"))
                p = Path(cfg.get("path", ""))
                if p.exists():
                    return p
            except Exception:
                pass
        # Fall back to default mount
        p = Path("/overflow")
        if p.exists():
            return p
        return None

    def _archive_loop_captures(self, loop_idx: int) -> int:
        """Archive captures from a completed loop to HDD organized by date/loop.

        Moves all capture images matching the loop index to:
          /overflow/archive/YYYY-MM-DD/loop-NNNN/

        This preserves ALL captures for every station in the loop so that
        rounds remain complete and consistent across stations.
        Returns bytes moved (freed from /img/captures).
        """
        overflow = self._get_overflow_root()
        if overflow is None:
            return 0

        today = datetime.now().strftime("%Y-%m-%d")
        archive_dir = overflow / "archive" / today / f"loop-{loop_idx:04d}"
        archive_dir.mkdir(parents=True, exist_ok=True)

        loop_prefix = f"loop{loop_idx:04d}-"
        freed = 0

        for src_dir in [Path("/img/captures"), Path("/img/sent")]:
            if not src_dir.exists():
                continue
            for f in list(src_dir.iterdir()):
                if not f.is_file():
                    continue
                if f.suffix.lower() not in (".png", ".jpg", ".jpeg"):
                    continue
                # Only archive captures from this specific loop
                if not f.name.startswith(loop_prefix) and not f.name.startswith(f"extract-loop{loop_idx:04d}-"):
                    continue
                try:
                    sz = f.stat().st_size
                    shutil.move(str(f), str(archive_dir / f.name))
                    freed += sz
                except Exception:
                    pass

        if freed > 0:
            self.log(
                f"Archived loop {loop_idx} captures to {archive_dir} "
                f"({freed / (1024*1024):.1f} MB)",
                "OK",
            )
        return freed

    def _move_to_overflow(self, move_all: bool = False) -> int:
        """Move captures/sent to overflow dir if configured. Returns bytes freed.

        Args:
            move_all: If True, move ALL image files regardless of age.
                      If False, only move files older than 30 min.
        """
        overflow = self._get_overflow_root()
        if overflow is None:
            return 0

        freed = 0
        cutoff = time.time() - 1800  # 30 min (ignored when move_all=True)
        for sub, src in [("captures", Path("/img/captures")), ("sent", Path("/img/sent"))]:
            dst_dir = overflow / sub
            dst_dir.mkdir(exist_ok=True)
            if not src.exists():
                continue
            for f in list(src.iterdir()):
                if not f.is_file():
                    continue
                if f.suffix.lower() not in (".png", ".jpg", ".jpeg"):
                    continue
                try:
                    if not move_all and f.stat().st_mtime >= cutoff:
                        continue
                    sz = f.stat().st_size
                    shutil.move(str(f), str(dst_dir / f.name))
                    freed += sz
                except Exception:
                    pass
        return freed

    @staticmethod
    def _disk_usage_pct() -> float:
        """Return highest disk usage percentage across data mounts."""
        data_pct = 0.0
        has_data = False
        root_pct = 0.0
        for mount in ["/", "/work", "/img"]:
            try:
                u = shutil.disk_usage(mount)
                pct = u.used / u.total * 100 if u.total else 0
                if mount == "/":
                    root_pct = pct
                else:
                    has_data = True
                    data_pct = max(data_pct, pct)
            except OSError:
                pass
        return data_pct if has_data else root_pct

    def _delete_old_captures(self, keep_minutes: int = 30) -> int:
        """Delete capture images older than keep_minutes. Returns bytes freed.

        Only deletes image files (PNG/JPEG) from capture directories.
        Never deletes extract logs (*-extract.jsonl, *-playback.json/html).
        """
        freed = 0
        cutoff = time.time() - (keep_minutes * 60)
        for cap_dir in [Path("/img/captures"), Path("/img/sent")]:
            if not cap_dir.exists():
                continue
            for f in list(cap_dir.iterdir()):
                if not f.is_file():
                    continue
                if f.suffix.lower() not in (".png", ".jpg", ".jpeg"):
                    continue
                try:
                    st = f.stat()
                    if st.st_mtime < cutoff:
                        freed += st.st_size
                        f.unlink()
                except Exception:
                    pass
        return freed

    def _delete_all_captures(self) -> int:
        """Delete ALL capture images to free space in critical situations. Returns bytes freed.

        Extract data is already saved in JSONL logs, so capture images
        are only for visual debugging and can be safely removed.
        """
        freed = 0
        for cap_dir in [Path("/img/captures"), Path("/img/sent")]:
            if not cap_dir.exists():
                continue
            for f in list(cap_dir.iterdir()):
                if not f.is_file():
                    continue
                if f.suffix.lower() not in (".png", ".jpg", ".jpeg"):
                    continue
                try:
                    freed += f.stat().st_size
                    f.unlink()
                except Exception:
                    pass
        # Also clean temp/cache
        for tmp_dir in [Path("/img/cache"), Path("/img/temp")]:
            if not tmp_dir.exists():
                continue
            for f in list(tmp_dir.iterdir()):
                if not f.is_file():
                    continue
                try:
                    freed += f.stat().st_size
                    f.unlink()
                except Exception:
                    pass
        return freed

    def _cleanup_old_archives(self, keep_days: int = 7) -> int:
        """Delete archived captures older than keep_days. Returns bytes freed."""
        overflow = self._get_overflow_root()
        if overflow is None:
            return 0
        archive_root = overflow / "archive"
        if not archive_root.exists():
            return 0
        freed = 0
        cutoff = time.time() - (keep_days * 86400)
        for day_dir in list(archive_root.iterdir()):
            if not day_dir.is_dir():
                continue
            try:
                if day_dir.stat().st_mtime < cutoff:
                    sz = sum(f.stat().st_size for f in day_dir.rglob("*") if f.is_file())
                    shutil.rmtree(str(day_dir), ignore_errors=True)
                    freed += sz
            except Exception:
                pass
        return freed

    def _cleanup_between_scripts(self) -> None:
        """Cleanup between scripts WITHIN a loop.

        Primary strategy: MOVE captures to overflow/HDD (preserves all data).
        Deletion is only a last resort when overflow is not configured.

        Escalation based on disk usage:
          <80%  — no action
          80-90% — compress logs, move old captures (>30 min) to overflow
          90%+  — move ALL captures to overflow immediately
          95%+  — if overflow unavailable, delete as last resort
        """
        freed = 0
        actions = []
        usage = self._disk_usage_pct()

        if usage < 80:
            return

        has_overflow = self._get_overflow_root() is not None

        # Step 1: Compress old logs (non-destructive)
        log_saved = self._compress_old_logs()
        if log_saved > 0:
            freed += log_saved
            actions.append(f"compressed logs ({log_saved / (1024*1024):.1f} MB)")

        # Step 2: Clean temp/cache
        for tmp_dir in [Path("/img/cache"), Path("/img/temp")]:
            if not tmp_dir.exists():
                continue
            for f in list(tmp_dir.iterdir()):
                if not f.is_file():
                    continue
                try:
                    freed += f.stat().st_size
                    f.unlink()
                except Exception:
                    pass

        # Step 3: Move captures to overflow/HDD
        if has_overflow:
            if usage >= 90:
                # Move ALL captures immediately — HDD has plenty of space
                overflow_freed = self._move_to_overflow(move_all=True)
            else:
                # Move only old files (>30 min)
                overflow_freed = self._move_to_overflow(move_all=False)
            if overflow_freed > 0:
                freed += overflow_freed
                actions.append(f"moved to overflow ({overflow_freed / (1024*1024):.1f} MB)")

        # Step 4: If still high after move, archive current loop captures too
        if has_overflow and self._disk_usage_pct() >= 85 and self.current_loop > 0:
            arc_freed = self._archive_loop_captures(self.current_loop)
            if arc_freed > 0:
                freed += arc_freed
                actions.append(f"archived loop {self.current_loop} ({arc_freed / (1024*1024):.1f} MB)")

        # Step 5: Only delete if overflow is NOT available (last resort)
        if not has_overflow and self._disk_usage_pct() >= 90:
            prev_freed = self._delete_old_captures(keep_minutes=2)
            if prev_freed > 0:
                freed += prev_freed
                actions.append(f"deleted old captures ({prev_freed / (1024*1024):.1f} MB)")

        # Step 6: Emergency — only if still critical and no overflow
        if not has_overflow and self._disk_usage_pct() >= 95:
            em_freed = self._delete_all_captures()
            if em_freed > 0:
                freed += em_freed
                actions.append(f"emergency delete all ({em_freed / (1024*1024):.1f} MB)")

        if freed > 0:
            new_usage = self._disk_usage_pct()
            self.log(
                f"Cleanup (between scripts): {freed / (1024*1024):.1f} MB freed "
                f"({', '.join(actions)}) [disk: {usage:.0f}% -> {new_usage:.0f}%]",
                "OK",
            )
        elif usage >= 85:
            self.log(
                f"Disk usage high ({usage:.0f}%) - will archive after loop completes",
                "WARN",
            )

    def _cleanup_after_loop(self, loop_idx: int) -> None:
        """Archive and clean up after a COMPLETE loop.

        This runs after ALL stations in a loop have finished, so it is safe
        to move/delete captures. The flow is:
          1. Archive this loop's captures to HDD (/overflow/archive/YYYY-MM-DD/loop-NNNN/)
          2. Compress old logs
          3. Move any remaining old files to overflow
          4. If disk still high, clean old archives (>7 days)
        """
        freed = 0
        actions = []
        usage = self._disk_usage_pct()

        # Step 1: Archive this loop's captures to organized HDD directory
        archive_freed = self._archive_loop_captures(loop_idx)
        if archive_freed > 0:
            freed += archive_freed
            actions.append(f"archived loop {loop_idx} ({archive_freed / (1024*1024):.1f} MB)")

        # Step 2: Compress old logs
        log_saved = self._compress_old_logs()
        if log_saved > 0:
            freed += log_saved
            actions.append(f"compressed logs ({log_saved / (1024*1024):.1f} MB)")

        # Step 3: Move any remaining old files to overflow
        overflow_freed = self._move_to_overflow()
        if overflow_freed > 0:
            freed += overflow_freed
            actions.append(f"moved old to overflow ({overflow_freed / (1024*1024):.1f} MB)")

        # Step 4: If disk still high, also clean old archives
        new_usage = self._disk_usage_pct()
        if new_usage >= 90:
            old_freed = self._cleanup_old_archives(keep_days=3)
            if old_freed > 0:
                freed += old_freed
                actions.append(f"cleaned old archives ({old_freed / (1024*1024):.1f} MB)")
        elif new_usage >= 80:
            old_freed = self._cleanup_old_archives(keep_days=7)
            if old_freed > 0:
                freed += old_freed
                actions.append(f"cleaned old archives ({old_freed / (1024*1024):.1f} MB)")

        if freed > 0:
            final_usage = self._disk_usage_pct()
            self.log(
                f"Cleanup (after loop {loop_idx}): {freed / (1024*1024):.1f} MB freed "
                f"({', '.join(actions)}) [disk: {usage:.0f}% -> {final_usage:.0f}%]",
                "OK",
            )

    def _cleanup_captures(self) -> None:
        """Legacy entry point — delegates to _cleanup_between_scripts().

        Kept for backward compatibility with code that calls _cleanup_captures()
        directly (e.g. flow_editor_web.py).
        """
        self._cleanup_between_scripts()

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

                    # Pre-flight disk check: clean proactively before script runs
                    pre_usage = self._disk_usage_pct()
                    if pre_usage >= 80:
                        self.log(f"Disk at {pre_usage:.0f}% before script — running cleanup...", "WARN")
                        self._cleanup_between_scripts()

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
                            loop_offset=loop_idx - 1,
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

                    # Non-destructive cleanup between scripts (NEVER deletes current loop captures)
                    self._cleanup_between_scripts()

                    # Delay between scripts
                    if self._running and i < len(enabled_scripts) - 1 and self.script_delay > 0:
                        self.log(f"Waiting {self.script_delay}s before next script...", "INFO")
                        time.sleep(self.script_delay)

                # Archive captures and clean up after the COMPLETE loop
                # All stations have finished, so it's safe to move/delete captures
                self._cleanup_after_loop(loop_idx)

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
