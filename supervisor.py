#!/usr/bin/env python3
"""
Simple process supervisor for Mesoview.

Starts two commands, restarts them if they exit, and forwards signals for
clean shutdown. Designed to be OS-agnostic (Windows/macOS/Linux).

All output from both child processes is written to a daily log file:
  <log_dir>/mesoview.YYYYMMDD.log

The log file rolls over at midnight; both children are restarted so they
inherit the new file handle.

Defaults:
- ingest:  python ingest_mm.py
- viewer:  python viewer.py

You can override commands with environment variables:
- MESO_INGEST_CMD
- MESO_VIEWER_CMD
Each should be a full command line string.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from typing import List, Optional

RESTART_DELAY_SEC = 2


def _load_config() -> dict:
    cfg_path = Path(__file__).parent / 'mesoview.config.json'
    if not cfg_path.exists():
        return {}
    try:
        with open(cfg_path) as f:
            return json.load(f) or {}
    except Exception as e:
        print(f'Warning: could not read config {cfg_path}: {e}', flush=True)
        return {}


def _open_log(log_dir: Path) -> tuple[object, date]:
    """Open today's log file (append mode). Returns (file_handle, today_date)."""
    log_dir.mkdir(parents=True, exist_ok=True)
    today = date.today()
    log_path = log_dir / f'mesoview.{today.strftime("%Y%m%d")}.log'
    fh = open(log_path, 'a', buffering=1)  # line-buffered
    return fh, today


def _log(fh, msg: str) -> None:
    from datetime import datetime, timezone
    line = f'[{datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}] [supervisor] {msg}'
    print(line, file=fh, flush=True)
    # Also echo to the original stderr so interactive runs stay visible.
    print(line, file=sys.__stderr__, flush=True)


def _split_cmd(cmd: str) -> List[str]:
    if os.name == 'nt':
        return [cmd]
    return shlex.split(cmd)


def _default_cmds() -> tuple[str, str]:
    py = shlex.quote(sys.executable)
    ingest = os.environ.get('MESO_INGEST_CMD') or f'{py} ingest_mm.py'
    viewer  = os.environ.get('MESO_VIEWER_CMD') or f'{py} viewer.py'
    return ingest, viewer


class Child:
    def __init__(self, name: str, cmd: str):
        self.name = name
        self.cmd  = cmd
        self.proc: Optional[subprocess.Popen] = None

    def start(self, log_fh) -> None:
        if os.name == 'nt':
            self.proc = subprocess.Popen(
                self.cmd, shell=True, stdout=log_fh, stderr=log_fh,
            )
        else:
            self.proc = subprocess.Popen(
                _split_cmd(self.cmd), stdout=log_fh, stderr=log_fh,
            )

    def poll(self) -> Optional[int]:
        return self.proc.poll() if self.proc else None

    def terminate(self) -> None:
        if self.proc:
            try: self.proc.terminate()
            except Exception: pass

    def kill(self) -> None:
        if self.proc:
            try: self.proc.kill()
            except Exception: pass


def main() -> int:
    cfg = _load_config()
    default_log_dir = Path.home() / 'mesoview_logs'
    log_dir = Path(cfg.get('log_dir', str(default_log_dir)))

    log_fh, log_date = _open_log(log_dir)
    _log(log_fh, f'starting — log: {log_fh.name}')

    ingest_cmd, viewer_cmd = _default_cmds()
    ingest = Child('ingest', ingest_cmd)
    viewer = Child('viewer', viewer_cmd)

    stop = False

    def _handle_signal(signum, _frame):
        nonlocal stop
        _log(log_fh, f'received signal {signum}, shutting down')
        stop = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_signal)
        except Exception:
            pass

    _log(log_fh, f'starting ingest: {ingest_cmd}')
    ingest.start(log_fh)
    _log(log_fh, f'starting viewer: {viewer_cmd}')
    viewer.start(log_fh)

    try:
        while not stop:
            # ── Midnight log rotation ─────────────────────────────────────────
            today = date.today()
            if today != log_date:
                _log(log_fh, f'date rolled over to {today} — rotating log and restarting children')
                log_fh.close()
                log_fh, log_date = _open_log(log_dir)
                _log(log_fh, f'new log: {log_fh.name}')
                for child in (ingest, viewer):
                    child.terminate()
                time.sleep(RESTART_DELAY_SEC)
                for child in (ingest, viewer):
                    child.kill()
                _log(log_fh, f'starting ingest: {ingest_cmd}')
                ingest.start(log_fh)
                _log(log_fh, f'starting viewer: {viewer_cmd}')
                viewer.start(log_fh)

            # ── Crash restart ─────────────────────────────────────────────────
            for child in (ingest, viewer):
                rc = child.poll()
                if rc is not None:
                    _log(log_fh, f'{child.name} exited with {rc}; restarting in {RESTART_DELAY_SEC}s')
                    time.sleep(RESTART_DELAY_SEC)
                    child.start(log_fh)

            time.sleep(1)

    finally:
        _log(log_fh, 'shutting down children')
        for child in (ingest, viewer):
            child.terminate()
        time.sleep(1)
        for child in (ingest, viewer):
            child.kill()
        log_fh.close()

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
