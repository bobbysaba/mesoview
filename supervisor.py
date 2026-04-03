#!/usr/bin/env python3
"""
Simple process supervisor for meso360.

Starts mesoingest, mesoview, and mesosync (SSH reverse tunnel) processes,
restarts them if they exit, and forwards signals for clean shutdown.
Designed to be OS-agnostic (Windows/macOS/Linux).

All output from child processes is written to a daily log file:
  <log_dir>/mesoview.YYYYMMDD.log

The log file rolls over at midnight; all children are restarted so they
inherit the new file handle.

Defaults:
- mesoingest:  python mesoingest.py
- mesoview:    python mesoview.py
- mesosync:    ssh reverse tunnel to clamps@remote.bliss.science (port from config)

You can override mesoingest/mesoview commands with environment variables:
- MESO_INGEST_CMD
- MESO_VIEWER_CMD
Each should be a full command line string.
"""

from __future__ import annotations  # allows type hints like tuple[str, str] on Python < 3.10

import json
import os
import shlex      # safe command-line tokenization and quoting, used to split/escape shell commands
import signal     # used to catch SIGINT (Ctrl-C) and SIGTERM so we can shut down children cleanly
import subprocess
import sys
import time
import atexit
from datetime import date
from pathlib import Path
from typing import List, Optional

from common import CONFIG_PATH, DEFAULT_DATA_DIR, DEFAULT_LOG_DIR, REPO_DIR, load_config

RESTART_DELAY_SEC = 2   # seconds to wait between terminate() and kill() during restarts
UPDATE_EXIT_CODE  = 42  # mesoview exits with this code after a successful git pull to trigger a full restart
TUNNEL_CHECK_INTERVAL_SEC = 30


def _load_config() -> dict:
    def _startup_log(msg: str) -> None:
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        print(f'[{ts}] [supervisor] {msg}', flush=True)

    return load_config(_startup_log)


def _open_log(log_dir: Path) -> tuple[object, date]:
    """Open today's log file (append mode). Returns (file_handle, today_date)."""
    log_dir.mkdir(parents=True, exist_ok=True)
    today = date.today()
    log_path = log_dir / f'meso360.{today.strftime("%Y%m%d")}.log'
    try:
        fh = open(log_path, 'a', buffering=1)  # buffering=1 = line-buffered: each line is flushed immediately
    except OSError as e:
        print(f'[supervisor] WARNING: could not open log file {log_path}: {e}; falling back to stderr', flush=True)
        fh = sys.stderr
    return fh, today  # return today's date so the main loop can detect when midnight rolls over


def _log(fh, msg: str) -> None:
    # datetime imported locally to avoid a module-level import that would shadow the one in _load_config
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{ts}] [supervisor] {msg}', file=fh, flush=True)


def _split_cmd(cmd: str) -> List[str]:
    # on Windows, subprocess needs the raw command string with shell=True to resolve PATH and extensions
    # on Unix, shlex.split tokenizes safely (handles spaces in paths, quoted args, etc.)
    if os.name == 'nt':
        return [cmd]
    return shlex.split(cmd)


def _default_cmds() -> tuple[str, str]:
    # shlex.quote produces single-quoted strings which cmd.exe doesn't understand; use double quotes on Windows
    py = f'"{sys.executable}"' if os.name == 'nt' else shlex.quote(sys.executable)
    # allow the caller to override commands via environment variables (useful for testing or venvs)
    ingest = os.environ.get('MESO_INGEST_CMD') or f'{py} mesoingest.py'
    viewer  = os.environ.get('MESO_VIEWER_CMD') or f'{py} mesoview.py'
    return ingest, viewer


def _build_rtun_cmd(port: int) -> str:
    """Build the SSH reverse tunnel command for the given port.

    Uses ~/.ssh/clamps_rsa and connects to clamps@remote.bliss.science.
    The key path is resolved via pathlib so it works on Windows, macOS,
    and Linux without modification.
    """
    key = Path.home() / '.ssh' / 'clamps_rsa'
    # Windows paths may contain spaces, so wrap in quotes; Unix uses shlex.quote for safety
    key_str = f'"{key}"' if os.name == 'nt' else shlex.quote(str(key))
    return (
        f'ssh -q -N '                        # -q = quiet (suppress banners), -N = no remote command (tunnel only)
        f'-L {port}:localhost:22 '           # local-forward: localhost:port on vehicle → localhost:22 on remote
        f'-R {port}:localhost:22 '           # reverse-tunnel: bind port on remote → forward to localhost:22 here
        f'-o ExitOnForwardFailure=yes '      # exit immediately if the remote port cannot be bound
        f'-o TCPKeepAlive=yes '              # send TCP keepalives so the connection isn't dropped by firewalls/NAT
        f'-o ServerAliveCountMax=3 '         # disconnect after 3 missed keepalive responses
        f'-o ServerAliveInterval=10 '        # send a keepalive every 10 seconds
        f'-i {key_str} '                     # use the NSSL/BLISS RSA key for authentication
        f'clamps@remote.bliss.science'       # remote server that acts as the tunnel endpoint
    )


def _probe_rtun(port: int, timeout_sec: int = 10) -> tuple[bool, str]:
    """Return True when the remote port accepts a localhost TCP connection."""
    key = Path.home() / '.ssh' / 'clamps_rsa'
    cmd = [
        'ssh',
        '-q',
        '-i',
        str(key),
        '-o',
        'BatchMode=yes',
        '-o',
        f'ConnectTimeout={timeout_sec}',
        'clamps@remote.bliss.science',
        (
            "("
            "python3 -c "
            f"\"import socket,sys; s=socket.create_connection(('127.0.0.1', {int(port)}), {timeout_sec}); s.close(); sys.exit(0)\""
            " || "
            "python -c "
            f"\"import socket,sys; s=socket.create_connection(('127.0.0.1', {int(port)}), {timeout_sec}); s.close(); sys.exit(0)\""
            ")"
        ),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec + 5,
        )
    except Exception as e:
        return False, str(e)

    detail = (result.stderr or result.stdout or '').strip()
    return result.returncode == 0, detail or f'rc={result.returncode}'


def _preflight(log_fh, cfg: dict) -> None:
    """Run startup sanity checks and log PASS/WARN results before children are launched."""
    _log(log_fh, '=== Preflight checks ===')

    # Check 1 — Config file present next to this script
    if CONFIG_PATH.exists():
        _log(log_fh, f'  PASS  config file found: {CONFIG_PATH}')
    else:
        _log(log_fh, f'  WARN  config file not found: {CONFIG_PATH}')
        _log(log_fh, '       Run: cp meso360.config.example.json meso360.config.json')

    # Check 2 — Data directory is reachable and writable
    data_dir = Path(cfg.get('data_dir', str(DEFAULT_DATA_DIR))).expanduser()
    probe = data_dir / '.preflight_probe'
    try:
        data_dir.mkdir(parents=True, exist_ok=True)  # create the directory if it doesn't already exist
        probe.write_text('ok')                        # confirm we can actually write inside it
        probe.unlink()                                # clean up the probe file immediately
        _log(log_fh, f'  PASS  data directory writable: {data_dir}')
    except Exception as e:
        _log(log_fh, f'  WARN  data directory not writable: {data_dir} ({e})')
        _log(log_fh, f'       Fix permissions or set "data_dir" in meso360.config.json')

    # Check 3 — SSH key (only relevant when a reverse tunnel is configured)
    if cfg.get('rtun_port') is not None:
        key = Path.home() / '.ssh' / 'clamps_rsa'
        if key.exists():
            _log(log_fh, f'  PASS  SSH key found: {key}')
        else:
            _log(log_fh, f'  WARN  SSH key not found: {key}')
            _log(log_fh, f'       Copy clamps_rsa to {key} — mesosync will not connect until this is done')

    # Check 4 — Git update check (fetch remote, pull if behind)
    try:
        dirty = bool(subprocess.run(
            ['git', 'status', '--porcelain'],
            cwd=REPO_DIR, capture_output=True, text=True, timeout=5,
        ).stdout.strip())
        if dirty:
            _log(log_fh, '  WARN  local git changes detected; skipping automatic git pull')
            _log(log_fh, '       Commit or discard local edits before updating from remote')
            _log(log_fh, '========================')
            return
        subprocess.run(
            ['git', 'fetch', '--quiet'],
            cwd=REPO_DIR, capture_output=True, text=True, timeout=15,
        )
        local_sha = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd=REPO_DIR, capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        remote_sha = subprocess.run(
            ['git', 'rev-parse', '@{u}'],
            cwd=REPO_DIR, capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        if local_sha and remote_sha and local_sha != remote_sha:
            _log(log_fh, f'  INFO  update available — running git pull...')
            result = subprocess.run(
                ['git', 'pull'],
                cwd=REPO_DIR, capture_output=True, text=True, timeout=30,
            )
            for line in (result.stdout + result.stderr).strip().splitlines():
                _log(log_fh, f'        {line}')
            if result.returncode == 0:
                _log(log_fh, f'  PASS  git pull succeeded')
            else:
                _log(log_fh, f'  WARN  git pull failed (rc={result.returncode})')
        else:
            _log(log_fh, f'  PASS  software up to date ({local_sha[:7] if local_sha else "unknown"})')
    except Exception as e:
        _log(log_fh, f'  WARN  could not check for updates: {e}')

    _log(log_fh, '========================')


class Child:
    def __init__(self, name: str, cmd: str, restart_arg: str = ''):
        self.name        = name                          # human-readable label used in log messages
        self.cmd         = cmd                           # full shell command string for this process
        self.restart_arg = restart_arg                   # optional extra arg appended only on restarts
        self.proc: Optional[subprocess.Popen] = None    # set when the process is running; None otherwise
        self._start_count = 0                            # incremented each time start() is called

    def start(self, log_fh) -> None:
        self._start_count += 1
        # On restarts tell mesoview not to open a new browser tab — the existing tab either
        # reloads itself (update flow) or auto-reconnects via SSE retry (crash flow).
        extra = f' {self.restart_arg}' if self._start_count > 1 and self.restart_arg else ''
        cmd = self.cmd + extra
        # Windows requires shell=True to resolve executables via PATH; Unix passes a pre-split arg list
        if os.name == 'nt':
            self.proc = subprocess.Popen(
                cmd,
                shell=True,
                stdout=log_fh,
                stderr=log_fh,
                cwd=REPO_DIR,
                creationflags=getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0),
            )
        else:
            self.proc = subprocess.Popen(
                _split_cmd(cmd),
                stdout=log_fh,
                stderr=log_fh,  # stdout+stderr both go to the shared log file
                cwd=REPO_DIR,
                start_new_session=True,  # put each child in its own process group so shutdown can target the whole tree
            )

    def poll(self) -> Optional[int]:
        # returns the exit code if the process has ended, or None if it's still running
        return self.proc.poll() if self.proc else None

    def _signal_group(self, sig: int) -> None:
        if not self.proc or self.proc.poll() is not None:
            return
        try:
            if os.name == 'nt':
                if sig == signal.SIGTERM and hasattr(signal, 'CTRL_BREAK_EVENT'):
                    self.proc.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    self.proc.kill()
            else:
                os.killpg(self.proc.pid, sig)
        except Exception:
            pass  # ignore errors if the process already exited or the platform doesn't support the signal

    def terminate(self) -> None:
        # terminate the whole child process group, not just the direct child, so helpers don't get orphaned
        self._signal_group(signal.SIGTERM)

    def kill(self) -> None:
        # force-kill the entire child process group after the grace period expires
        self._signal_group(signal.SIGKILL if os.name != 'nt' else signal.SIGTERM)


def _stop_children(children: List[Child], grace_sec: float = 1.0) -> None:
    for child in children:
        child.terminate()
    time.sleep(grace_sec)
    for child in children:
        child.kill()


def main() -> int:
    cfg = _load_config()
    log_dir = Path(cfg.get('log_dir', str(DEFAULT_LOG_DIR))).expanduser()

    log_fh, log_date = _open_log(log_dir)
    _log(log_fh, f'starting — log: {log_fh.name}')
    _preflight(log_fh, cfg)  # run sanity checks and log results before any children are launched

    ingest_cmd, viewer_cmd = _default_cmds()
    children: List[Child] = [
        Child('mesoingest', ingest_cmd),
        Child('mesoview',   viewer_cmd, restart_arg='--no-browser'),
    ]

    rtun_port = cfg.get('rtun_port')
    if rtun_port is not None:
        try:
            rtun_port = int(rtun_port)
        except (ValueError, TypeError):
            _log(log_fh, f'WARNING: rtun_port "{rtun_port}" is not a valid integer — SSH tunnel disabled')
            rtun_port = None
    if rtun_port:
        children.append(Child('mesosync', _build_rtun_cmd(rtun_port)))
    else:
        _log(log_fh, 'rtun_port not set in config — SSH tunnel disabled')

    mesosync = next((child for child in children if child.name == 'mesosync'), None)
    next_tunnel_check = time.monotonic() + TUNNEL_CHECK_INTERVAL_SEC

    stop = False  # set to True by the signal handler to break the main loop
    shutting_down = False

    def _handle_signal(signum, _frame):
        nonlocal stop  # nonlocal lets the nested function write to the enclosing scope's 'stop' variable
        _log(log_fh, f'received signal {signum}, shutting down')
        stop = True  # main loop will exit on the next iteration

    def _cleanup() -> None:
        nonlocal shutting_down
        if shutting_down:
            return
        shutting_down = True
        try:
            _log(log_fh, 'shutting down children')
        except Exception:
            pass
        _stop_children(children)

    atexit.register(_cleanup)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_signal)  # register handler for Ctrl-C and systemd/kill signals
        except Exception:
            pass  # signal.signal can raise on some platforms (e.g. Windows doesn't support all signals)

    for child in children:
        _log(log_fh, f'starting {child.name}: {child.cmd}')
        child.start(log_fh)

    try:
        while not stop:
            # ── Midnight log rotation ─────────────────────────────────────────
            today = date.today()
            if today != log_date:
                # date has changed — close the old log file and open a new one for today
                _log(log_fh, f'date rolled over to {today} — rotating log and restarting children')
                log_fh.close()
                log_fh, log_date = _open_log(log_dir)
                _log(log_fh, f'new log: {log_fh.name}')
                _stop_children(children, grace_sec=RESTART_DELAY_SEC)
                for child in children:
                    # restart each child with the new log file handle so output goes to today's log
                    _log(log_fh, f'starting {child.name}: {child.cmd}')
                    child.start(log_fh)

            # ── Crash restart ─────────────────────────────────────────────────
            for child in children:
                rc = child.poll()     # None = still running; any integer = process has exited
                if rc is not None:
                    if rc == UPDATE_EXIT_CODE:
                        # mesoview pulled new code and signalled a full update — restart all children
                        _log(log_fh, f'{child.name} exited with update code ({rc}); restarting all children')
                        time.sleep(RESTART_DELAY_SEC)
                        _stop_children([c for c in children if c is not child], grace_sec=RESTART_DELAY_SEC)
                        for c in children:
                            _log(log_fh, f'starting {c.name}: {c.cmd}')
                            c.start(log_fh)
                    else:
                        # child exited unexpectedly — log the exit code and restart it
                        _log(log_fh, f'{child.name} exited with {rc}; restarting in {RESTART_DELAY_SEC}s')
                        time.sleep(RESTART_DELAY_SEC)
                        child.start(log_fh)

            if mesosync and mesosync.poll() is None and rtun_port and time.monotonic() >= next_tunnel_check:
                ok, detail = _probe_rtun(rtun_port)
                if ok:
                    _log(log_fh, f'mesosync probe passed on remote port {rtun_port}')
                else:
                    _log(log_fh, f'mesosync probe failed on remote port {rtun_port} ({detail}); restarting tunnel')
                    _stop_children([mesosync], grace_sec=RESTART_DELAY_SEC)
                    _log(log_fh, f'starting {mesosync.name}: {mesosync.cmd}')
                    mesosync.start(log_fh)
                next_tunnel_check = time.monotonic() + TUNNEL_CHECK_INTERVAL_SEC

            time.sleep(1)  # poll children once per second; low CPU overhead

    finally:
        # reached on clean shutdown (stop=True) or unhandled exception
        _cleanup()
        atexit.unregister(_cleanup)
        log_fh.close()

    return 0


if __name__ == '__main__':
    raise SystemExit(main())  # propagates the return code to the shell (0 = success)
