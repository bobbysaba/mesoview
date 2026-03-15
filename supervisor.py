#!/usr/bin/env python3
"""
Simple process supervisor for Mesoview.

Starts two commands, restarts them if they exit, and forwards signals for
clean shutdown. Designed to be OS-agnostic (Windows/macOS/Linux).

Defaults:
- ingest:  python ingest_mm.py
- viewer:  python viewer.py

You can override commands with environment variables:
- MESO_INGEST_CMD
- MESO_VIEWER_CMD
Each should be a full command line string.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import time
from typing import List, Optional

RESTART_DELAY_SEC = 2

def _split_cmd(cmd: str) -> List[str]:
    if os.name == "nt":
        # On Windows, let CreateProcess parse when shell=True.
        return [cmd]
    return shlex.split(cmd)

def _default_cmds() -> tuple[str, str]:
    py = shlex.quote(sys.executable)
    ingest = os.environ.get("MESO_INGEST_CMD") or f"{py} ingest_mm.py"
    viewer = os.environ.get("MESO_VIEWER_CMD") or f"{py} viewer.py"
    return ingest, viewer

class Child:
    def __init__(self, name: str, cmd: str):
        self.name = name
        self.cmd = cmd
        self.proc: Optional[subprocess.Popen] = None

    def start(self) -> None:
        print(f"[supervisor] starting {self.name}: {self.cmd}", flush=True)
        if os.name == "nt":
            # shell=True to allow command string parsing on Windows.
            self.proc = subprocess.Popen(self.cmd, shell=True)
        else:
            self.proc = subprocess.Popen(_split_cmd(self.cmd))

    def poll(self) -> Optional[int]:
        if not self.proc:
            return None
        return self.proc.poll()

    def terminate(self) -> None:
        if not self.proc:
            return
        try:
            self.proc.terminate()
        except Exception:
            pass

    def kill(self) -> None:
        if not self.proc:
            return
        try:
            self.proc.kill()
        except Exception:
            pass

def main() -> int:
    ingest_cmd, viewer_cmd = _default_cmds()
    ingest = Child("ingest", ingest_cmd)
    viewer = Child("viewer", viewer_cmd)

    stop = False

    def _handle_signal(signum, _frame):
        nonlocal stop
        print(f"[supervisor] received signal {signum}, shutting down", flush=True)
        stop = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_signal)
        except Exception:
            pass

    ingest.start()
    viewer.start()

    try:
        while not stop:
            for child in (ingest, viewer):
                rc = child.poll()
                if rc is not None:
                    print(
                        f"[supervisor] {child.name} exited with {rc}; restarting in {RESTART_DELAY_SEC}s",
                        flush=True,
                    )
                    time.sleep(RESTART_DELAY_SEC)
                    child.start()
            time.sleep(1)
    finally:
        for child in (ingest, viewer):
            child.terminate()
        time.sleep(1)
        for child in (ingest, viewer):
            child.kill()

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
