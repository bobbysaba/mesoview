#!/usr/bin/env python3
"""
meso360 supervisor — process manager + web control panel.

Hosts the Flask web app (control panel at /, mesoview dashboard at /view),
manages mesoingest and mesosync as child subprocesses, and provides API
endpoints for status monitoring and component control.

Usage:  python supervisor.py           # live mode
        python supervisor.py --test    # replay test_data/test.txt at 1 Hz

All output is written to a daily log file:
  <log_dir>/meso360.YYYYMMDD.log
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
import atexit
import threading
import webbrowser
import urllib.request
from collections import deque
from datetime import date, datetime, timezone
from pathlib import Path
from typing import List, Optional

import psutil
from flask import Flask, Response, jsonify, request

from common import CONFIG_PATH, DEFAULT_DATA_DIR, DEFAULT_LOG_DIR, REPO_DIR, load_config

# ── Constants ────────────────────────────────────────────────────────────────
RESTART_DELAY_SEC = 2
TUNNEL_CHECK_INTERVAL_SEC = 30
LOG_RING_MAX = 200          # max lines in the in-memory log ring buffer
UPLOT_VERSION = '1.6.31'

# ── Argument parsing ─────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(prog='meso360')
parser.add_argument('--test', action='store_true', help='Replay test data at 1 Hz')
parser.add_argument('--no-browser', action='store_true', help='Skip opening browser on startup')
_args, _ = parser.parse_known_args()

# ── Shared state ─────────────────────────────────────────────────────────────
_supervisor_started_at: float = time.time()
_children_lock = threading.Lock()   # guards all child reads/mutations
_log_fh = sys.stderr                # current log file handle; swapped on midnight rotation
_log_fh_lock = threading.Lock()     # guards _log_fh swaps

# Log ring buffer for /api/log/stream SSE
_log_ring: deque = deque(maxlen=LOG_RING_MAX)
_log_ring_lock = threading.Lock()
_log_ring_cond = threading.Condition(_log_ring_lock)
_log_ring_seq = 0


def _load_config() -> dict:
    def _startup_log(msg: str) -> None:
        ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        print(f'[{ts}] [supervisor] {msg}', flush=True)
    return load_config(_startup_log)


def _open_log(log_dir: Path) -> tuple[object, date]:
    """Open today's log file (append mode). Returns (file_handle, today_date)."""
    log_dir.mkdir(parents=True, exist_ok=True)
    today = date.today()
    log_path = log_dir / f'meso360.{today.strftime("%Y%m%d")}.log'
    try:
        fh = open(log_path, 'a', buffering=1)
    except OSError as e:
        print(f'[supervisor] WARNING: could not open log file {log_path}: {e}; falling back to stderr', flush=True)
        fh = sys.stderr
    return fh, today


def _log(msg: str) -> None:
    """Write a timestamped log line to the current log file."""
    global _log_fh
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] [supervisor] {msg}'
    with _log_fh_lock:
        fh = _log_fh
    print(line, file=fh, flush=True)


# ── Log tail worker ──────────────────────────────────────────────────────────
def _log_tail_worker(log_dir: Path):
    """Tail the current log file and populate the ring buffer for SSE clients."""
    global _log_ring_seq
    path = log_dir / f'meso360.{date.today().strftime("%Y%m%d")}.log'
    pos = 0

    # Wait for log file to exist
    while not path.exists():
        time.sleep(1)
        path = log_dir / f'meso360.{date.today().strftime("%Y%m%d")}.log'

    # Start at end of existing file (don't replay old history)
    pos = path.stat().st_size

    while True:
        # Check for date rollover
        new_path = log_dir / f'meso360.{date.today().strftime("%Y%m%d")}.log'
        if new_path != path:
            path = new_path
            pos = 0

        if path.exists():
            try:
                with open(path) as fh:
                    end = fh.seek(0, 2)
                    if pos > end:
                        pos = 0
                    fh.seek(pos)
                    lines = fh.readlines()
                    pos = fh.tell()
                for line in lines:
                    line = line.rstrip('\n')
                    if line:
                        with _log_ring_cond:
                            _log_ring_seq += 1
                            _log_ring.append({'seq': _log_ring_seq, 'line': line})
                            _log_ring_cond.notify_all()
            except Exception:
                pass

        time.sleep(0.5)


# ── Helper functions ─────────────────────────────────────────────────────────
def _split_cmd(cmd: str) -> List[str]:
    if os.name == 'nt':
        return [cmd]
    return shlex.split(cmd)


def _default_ingest_cmd() -> str:
    py = f'"{sys.executable}"' if os.name == 'nt' else shlex.quote(sys.executable)
    return os.environ.get('MESO_INGEST_CMD') or f'{py} mesoingest.py'


def _build_rtun_cmd(port: int) -> str:
    """Build the SSH reverse tunnel command."""
    key = Path.home() / '.ssh' / 'clamps_rsa'
    key_str = f'"{key}"' if os.name == 'nt' else shlex.quote(str(key))
    return (
        f'ssh -q -N '
        f'-L {port}:localhost:22 '
        f'-R {port}:localhost:22 '
        f'-o ExitOnForwardFailure=yes '
        f'-o TCPKeepAlive=yes '
        f'-o ServerAliveCountMax=3 '
        f'-o ServerAliveInterval=10 '
        f'-i {key_str} '
        f'clamps@remote.bliss.science'
    )


def _probe_rtun(port: int, timeout_sec: int = 10) -> tuple[bool, str]:
    """Return True when the remote port accepts a localhost TCP connection."""
    key = Path.home() / '.ssh' / 'clamps_rsa'
    cmd = [
        'ssh', '-q', '-i', str(key),
        '-o', 'BatchMode=yes',
        '-o', f'ConnectTimeout={timeout_sec}',
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
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec + 5)
    except Exception as e:
        return False, str(e)
    detail = (result.stderr or result.stdout or '').strip()
    return result.returncode == 0, detail or f'rc={result.returncode}'


def _preflight(cfg: dict) -> None:
    """Run startup sanity checks."""
    _log('=== Preflight checks ===')

    if CONFIG_PATH.exists():
        _log(f'  PASS  config file found: {CONFIG_PATH}')
    else:
        _log(f'  WARN  config file not found: {CONFIG_PATH}')
        _log('       Run: cp meso360.config.example.json meso360.config.json')

    data_dir = Path(cfg.get('data_dir', str(DEFAULT_DATA_DIR))).expanduser()
    probe = data_dir / '.preflight_probe'
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe.write_text('ok')
        probe.unlink()
        _log(f'  PASS  data directory writable: {data_dir}')
    except Exception as e:
        _log(f'  WARN  data directory not writable: {data_dir} ({e})')

    if cfg.get('rtun_port') is not None:
        key = Path.home() / '.ssh' / 'clamps_rsa'
        if key.exists():
            _log(f'  PASS  SSH key found: {key}')
        else:
            _log(f'  WARN  SSH key not found: {key}')

    try:
        dirty = bool(subprocess.run(
            ['git', 'status', '--porcelain'],
            cwd=REPO_DIR, capture_output=True, text=True, timeout=5,
        ).stdout.strip())
        if dirty:
            _log('  WARN  local git changes detected; skipping automatic git pull')
            _log('========================')
            return
        subprocess.run(['git', 'fetch', '--quiet'],
                       cwd=REPO_DIR, capture_output=True, text=True, timeout=15)
        local_sha = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd=REPO_DIR, capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        remote_sha = subprocess.run(
            ['git', 'rev-parse', '@{u}'],
            cwd=REPO_DIR, capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        if local_sha and remote_sha and local_sha != remote_sha:
            _log('  INFO  update available — running git pull...')
            result = subprocess.run(['git', 'pull'],
                                    cwd=REPO_DIR, capture_output=True, text=True, timeout=30)
            for line in (result.stdout + result.stderr).strip().splitlines():
                _log(f'        {line}')
            if result.returncode == 0:
                _log('  PASS  git pull succeeded')
            else:
                _log(f'  WARN  git pull failed (rc={result.returncode})')
        else:
            _log(f'  PASS  software up to date ({local_sha[:7] if local_sha else "unknown"})')
    except Exception as e:
        _log(f'  WARN  could not check for updates: {e}')

    _log('========================')


def _get_local_ip() -> str:
    """Best-effort LAN IP discovery."""
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return '0.0.0.0'
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass


def _advertise_mdns(hostname: str, port: int):
    """Advertise via mDNS/Bonjour if zeroconf is available."""
    try:
        from zeroconf import ServiceInfo, Zeroconf
    except Exception:
        _log('mDNS: zeroconf not installed; skipping .local advertisement')
        return None

    ip = _get_local_ip()
    service_type = '_http._tcp.local.'
    instance = f'{hostname}._http._tcp.local.'
    server = f'{hostname}.local.'
    info = ServiceInfo(
        service_type, instance,
        addresses=[socket.inet_aton(ip)],
        port=port, server=server, properties={'path': '/'},
    )
    zc = Zeroconf()
    try:
        zc.register_service(info)
    except Exception as e:
        _log(f'mDNS: failed to register service: {e}')
        try:
            zc.close()
        except Exception:
            pass
        return None

    def _cleanup():
        try:
            zc.unregister_service(info)
            zc.close()
        except Exception:
            pass

    atexit.register(_cleanup)
    _log(f'mDNS: advertised http://{hostname}.local:{port} -> {ip}:{port}')
    return zc


def _ensure_uplot():
    """Download uPlot JS/CSS to static/ if not already cached."""
    static = Path(__file__).parent / 'static'
    static.mkdir(exist_ok=True)
    base = f'https://unpkg.com/uplot@{UPLOT_VERSION}/dist'
    assets = [
        ('uplot.min.js', f'{base}/uPlot.iife.min.js'),
        ('uplot.min.css', f'{base}/uPlot.min.css'),
    ]
    for fname, url in assets:
        dst = static / fname
        if not dst.exists():
            try:
                _log(f'Downloading {fname}...')
                with urllib.request.urlopen(url, timeout=10) as resp:
                    dst.write_bytes(resp.read())
                _log(f'Saved {dst}')
            except Exception as e:
                _log(f'Warning: could not fetch {fname}: {e}')


# ── Version cache ────────────────────────────────────────────────────────────
_ver_cache: dict = {'commit': None, 'dirty': False, 'update_available': False}
_ver_lock = threading.Lock()


def _version_worker():
    """Fetch git status/remote once immediately, then every 5 minutes."""
    while True:
        try:
            commit = subprocess.run(
                ['git', 'rev-parse', '--short', 'HEAD'],
                cwd=REPO_DIR, capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            dirty = bool(subprocess.run(
                ['git', 'status', '--porcelain'],
                cwd=REPO_DIR, capture_output=True, text=True, timeout=5,
            ).stdout.strip())
            subprocess.run(['git', 'fetch', '--quiet'],
                           cwd=REPO_DIR, capture_output=True, text=True, timeout=15)
            local_sha = subprocess.run(
                ['git', 'rev-parse', 'HEAD'],
                cwd=REPO_DIR, capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            remote_sha = subprocess.run(
                ['git', 'rev-parse', '@{u}'],
                cwd=REPO_DIR, capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            update_available = bool(local_sha and remote_sha and local_sha != remote_sha)
            with _ver_lock:
                _ver_cache.update(commit=commit, dirty=dirty, update_available=update_available)
        except Exception as e:
            _log(f'WARNING: version check failed: {e}')
        time.sleep(300)


# ── Child class ──────────────────────────────────────────────────────────────
class Child:
    def __init__(self, name: str, cmd: str, enabled: bool = True):
        self.name = name
        self.cmd = cmd
        self.enabled = enabled
        self.proc: Optional[subprocess.Popen] = None
        self.started_at: Optional[float] = None
        self.restart_count = 0
        self.last_exit_code: Optional[int] = None
        self._start_count = 0

    @property
    def status(self) -> str:
        if not self.enabled:
            return 'disabled'
        if self.proc is None:
            return 'stopped'
        if self.proc.poll() is None:
            return 'running'
        return 'exited'

    @property
    def uptime_seconds(self) -> Optional[float]:
        if self.started_at and self.status == 'running':
            return time.monotonic() - self.started_at
        return None

    def start(self, log_fh) -> None:
        if not self.cmd:
            return  # no command configured (e.g. mesosync without rtun_port)
        self._start_count += 1
        if os.name == 'nt':
            self.proc = subprocess.Popen(
                self.cmd, shell=True,
                stdout=log_fh, stderr=log_fh, cwd=REPO_DIR,
                creationflags=getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0),
            )
        else:
            self.proc = subprocess.Popen(
                _split_cmd(self.cmd),
                stdout=log_fh, stderr=log_fh, cwd=REPO_DIR,
                start_new_session=True,
            )
        self.started_at = time.monotonic()
        self.last_exit_code = None

    def poll(self) -> Optional[int]:
        rc = self.proc.poll() if self.proc else None
        if rc is not None:
            self.last_exit_code = rc
        return rc

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
            pass

    def terminate(self) -> None:
        self._signal_group(signal.SIGTERM)

    def kill(self) -> None:
        self._signal_group(signal.SIGKILL if os.name != 'nt' else signal.SIGTERM)


def _stop_children(children: List[Child], grace_sec: float = 1.0) -> None:
    for child in children:
        child.terminate()
    time.sleep(grace_sec)
    for child in children:
        child.kill()


# ── Config persistence for enable/disable ────────────────────────────────────
def _persist_enabled(cfg: dict, children: List[Child]) -> None:
    """Write current enabled states back to meso360.config.json."""
    for child in children:
        cfg[f'{child.name}_enabled'] = child.enabled
    try:
        with open(CONFIG_PATH, 'w') as f:
            json.dump(cfg, f, indent=2)
            f.write('\n')
    except Exception as e:
        _log(f'WARNING: could not persist config: {e}')


# ── Flask app factory ────────────────────────────────────────────────────────
def _create_app(cfg: dict, children: List[Child]) -> Flask:
    """Create and configure the Flask app with all routes."""
    app = Flask(__name__, template_folder=str(REPO_DIR / 'templates'),
                static_folder=str(REPO_DIR / 'static'))

    # Suppress werkzeug request logs in production
    wlog = logging.getLogger('werkzeug')
    wlog.setLevel(logging.WARNING)

    # Import and register mesoview blueprint
    import mesoview
    if _args.test:
        mesoview.TEST_MODE = True
    app.register_blueprint(mesoview.mesoview_bp, url_prefix='/view')

    # ── Control panel ────────────────────────────────────────────────────
    from flask import render_template

    @app.route('/')
    def control_panel():
        return render_template('control.html')

    # ── Version endpoint ─────────────────────────────────────────────────
    @app.route('/version')
    def version():
        with _ver_lock:
            return jsonify(dict(_ver_cache))

    # ── Update endpoint ──────────────────────────────────────────────────
    @app.route('/update', methods=['POST'])
    def update():
        import ipaddress
        addr = request.remote_addr or ''
        is_local = False
        try:
            if ipaddress.ip_address(addr).is_loopback:
                is_local = True
        except ValueError:
            pass
        if not is_local and addr != _get_local_ip():
            return jsonify({'ok': False, 'output': 'Update is only allowed from the host machine.'}), 403

        dirty = bool(subprocess.run(
            ['git', 'status', '--porcelain'],
            cwd=REPO_DIR, capture_output=True, text=True, timeout=5,
        ).stdout.strip())
        if dirty:
            return jsonify({'ok': False, 'output': 'Working tree has local changes.'}), 409

        try:
            result = subprocess.run(['git', 'pull'],
                                    cwd=REPO_DIR, capture_output=True, text=True, timeout=30)
            output = (result.stdout + result.stderr).strip()
            changed = result.returncode == 0 and 'Already up to date.' not in output
        except Exception as e:
            return jsonify({'ok': False, 'output': str(e)})

        supervisor_changed = False
        if changed:
            # Check if supervisor.py itself changed in the pull
            try:
                diff = subprocess.run(
                    ['git', 'diff', '--name-only', 'HEAD~1', 'HEAD'],
                    cwd=REPO_DIR, capture_output=True, text=True, timeout=5,
                ).stdout
                supervisor_changed = 'supervisor.py' in diff
            except Exception:
                pass

            # Restart child subprocesses with new code
            with _children_lock:
                _stop_children(children, grace_sec=RESTART_DELAY_SEC)
                with _log_fh_lock:
                    fh = _log_fh
                for child in children:
                    if child.enabled:
                        _log(f'restarting {child.name} after update')
                        child.start(fh)

        return jsonify({
            'ok': result.returncode == 0,
            'output': output,
            'changed': changed,
            'supervisor_changed': supervisor_changed,
        })

    # ── API: status ──────────────────────────────────────────────────────
    @app.route('/api/status')
    def api_status():
        with _children_lock:
            components = []
            for child in children:
                components.append({
                    'name': child.name,
                    'status': child.status,
                    'enabled': child.enabled,
                    'configured': bool(child.cmd),
                    'uptime': round(child.uptime_seconds) if child.uptime_seconds else None,
                    'restart_count': child.restart_count,
                    'last_exit_code': child.last_exit_code,
                })
        cache = mesoview.cache_stats()
        return jsonify({
            'components': components,
            'cache': cache,
            'started_at': _supervisor_started_at,
            'test_mode': _args.test,
            'python_version': f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}',
            'cpu_percent': psutil.cpu_percent(interval=None),
            'mem_percent': psutil.virtual_memory().percent,
            'disk_percent': psutil.disk_usage(cfg.get('data_dir', str(DEFAULT_DATA_DIR))).percent,
            'config': {
                'logger_ip': cfg.get('logger_ip', '192.168.4.6'),
                'http_port': int(cfg.get('http_port', 8080)),
                'data_dir': cfg.get('data_dir', str(DEFAULT_DATA_DIR)),
                'log_dir': cfg.get('log_dir', str(DEFAULT_LOG_DIR)),
                'rtun_port': cfg.get('rtun_port'),
                'mdns_hostname': cfg.get('mdns_hostname', 'mesoview'),
            },
        })

    # ── API: control ─────────────────────────────────────────────────────
    @app.route('/api/control', methods=['POST'])
    def api_control():
        data = request.get_json(silent=True) or {}
        name = data.get('component')
        action = data.get('action')

        if action == 'restart-all':
            with _children_lock:
                with _log_fh_lock:
                    fh = _log_fh
                restarted = []
                for child in children:
                    if child.enabled and child.status == 'running':
                        child.terminate()
                        time.sleep(0.5)
                        child.kill()
                        child.start(fh)
                        child.restart_count += 1
                        restarted.append(child.name)
                _log(f'restart-all by user: {", ".join(restarted) or "none running"}')
            return jsonify({'ok': True, 'restarted': restarted})

        with _children_lock:
            child = next((c for c in children if c.name == name), None)
            if not child:
                return jsonify({'ok': False, 'error': f'unknown component: {name}'}), 404

            with _log_fh_lock:
                fh = _log_fh

            if action == 'stop':
                child.terminate()
                time.sleep(0.5)
                child.kill()
                child.proc = None
                _log(f'{name} stopped by user')
            elif action == 'start':
                if child.status != 'running':
                    child.enabled = True
                    child.start(fh)
                    _log(f'{name} started by user')
            elif action == 'restart':
                child.terminate()
                time.sleep(0.5)
                child.kill()
                child.enabled = True
                child.start(fh)
                child.restart_count += 1
                _log(f'{name} restarted by user')
            elif action == 'enable':
                child.enabled = True
                if child.status != 'running':
                    child.start(fh)
                _log(f'{name} enabled by user')
                _persist_enabled(cfg, children)
            elif action == 'disable':
                child.enabled = False
                child.terminate()
                time.sleep(0.5)
                child.kill()
                child.proc = None
                _log(f'{name} disabled by user')
                _persist_enabled(cfg, children)
            else:
                return jsonify({'ok': False, 'error': f'unknown action: {action}'}), 400

            return jsonify({'ok': True, 'component': name, 'status': child.status})

    # ── API: log stream (SSE) ────────────────────────────────────────────
    @app.route('/api/log/stream')
    def api_log_stream():
        def generate():
            with _log_ring_lock:
                # Burst: send all buffered lines on connect
                for entry in _log_ring:
                    yield f'event: log\ndata: {json.dumps(entry)}\n\n'
                last_seq = _log_ring_seq
            try:
                while True:
                    with _log_ring_cond:
                        changed = _log_ring_cond.wait_for(
                            lambda: _log_ring_seq != last_seq, timeout=2
                        )
                        if changed:
                            # Send all new entries since last_seq
                            new_entries = [e for e in _log_ring if e['seq'] > last_seq]
                            last_seq = _log_ring_seq
                        else:
                            new_entries = []
                    if not new_entries:
                        yield ': keep-alive\n\n'
                    else:
                        for entry in new_entries:
                            yield f'event: log\ndata: {json.dumps(entry)}\n\n'
            except GeneratorExit:
                pass

        return Response(
            generate(), mimetype='text/event-stream',
            headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
        )

    # ── API: log poll fallback ───────────────────────────────────────────
    @app.route('/api/log')
    def api_log():
        after = request.args.get('after', 0, type=int)
        limit = min(request.args.get('lines', 50, type=int), 500)
        with _log_ring_lock:
            entries = [e for e in _log_ring if e['seq'] > after]
        return jsonify(entries[-limit:])

    return app


# ── Main ─────────────────────────────────────────────────────────────────────
def main() -> int:
    global _log_fh

    cfg = _load_config()
    log_dir = Path(cfg.get('log_dir', str(DEFAULT_LOG_DIR))).expanduser()
    http_port = int(cfg.get('http_port', 8080))
    mdns_hostname = cfg.get('mdns_hostname', 'mesoview')

    _log_fh, log_date = _open_log(log_dir)
    _log(f'starting — log: {_log_fh.name}')
    _preflight(cfg)

    _ensure_uplot()

    # Build child list (no mesoview — it's in-process now)
    ingest_cmd = _default_ingest_cmd()
    ingest_enabled = cfg.get('mesoingest_enabled', True)
    children: List[Child] = [
        Child('mesoingest', ingest_cmd, enabled=ingest_enabled),
    ]

    rtun_port = cfg.get('rtun_port')
    if rtun_port is not None:
        try:
            rtun_port = int(rtun_port)
        except (ValueError, TypeError):
            _log(f'WARNING: rtun_port "{rtun_port}" is not a valid integer — SSH tunnel disabled')
            rtun_port = None
    if rtun_port:
        sync_enabled = cfg.get('mesosync_enabled', True)
        children.append(Child('mesosync', _build_rtun_cmd(rtun_port), enabled=sync_enabled))
    else:
        _log('rtun_port not set in config — SSH tunnel disabled')
        # Still show mesosync in the UI, just disabled with no command
        children.append(Child('mesosync', '', enabled=False))

    mesosync = next((c for c in children if c.name == 'mesosync'), None)
    next_tunnel_check = time.monotonic() + TUNNEL_CHECK_INTERVAL_SEC

    # Create Flask app
    app = _create_app(cfg, children)

    # Start background threads
    threading.Thread(target=_version_worker, daemon=True).start()
    threading.Thread(target=_log_tail_worker, args=(log_dir,), daemon=True).start()

    # Start mesoview cache worker
    import mesoview
    mesoview.start_cache_worker()

    # Start Flask in a daemon thread
    def _run_flask():
        app.run(host='0.0.0.0', port=http_port, debug=False, threaded=True,
                use_reloader=False)
    threading.Thread(target=_run_flask, daemon=True).start()

    # Give Flask a moment to bind
    time.sleep(0.5)

    # mDNS
    _advertise_mdns(hostname=mdns_hostname, port=http_port)

    # Open browser
    if not _args.no_browser:
        local_ip = _get_local_ip()
        url = f'http://{local_ip}:{http_port}'
        _log(f'Opening {url}')
        try:
            webbrowser.open(url, new=2)
        except Exception as e:
            _log(f'Warning: could not open browser: {e}')

    # Signal handling
    stop = False
    shutting_down = False

    def _handle_signal(signum, _frame):
        nonlocal stop
        _log(f'received signal {signum}, shutting down')
        stop = True

    def _cleanup():
        nonlocal shutting_down
        if shutting_down:
            return
        shutting_down = True
        try:
            _log('shutting down children')
        except Exception:
            pass
        with _children_lock:
            _stop_children(children)

    atexit.register(_cleanup)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_signal)
        except Exception:
            pass

    # Start enabled children
    with _children_lock:
        for child in children:
            if child.enabled:
                _log(f'starting {child.name}: {child.cmd}')
                child.start(_log_fh)
            else:
                _log(f'{child.name} is disabled — skipping')

    try:
        while not stop:
            # ── Midnight log rotation ────────────────────────────────────
            today = date.today()
            if today != log_date:
                _log(f'date rolled over to {today} — rotating log and restarting children')
                with _log_fh_lock:
                    _log_fh.close()
                    _log_fh, log_date = _open_log(log_dir)
                _log(f'new log: {_log_fh.name}')
                with _children_lock:
                    _stop_children(children, grace_sec=RESTART_DELAY_SEC)
                    for child in children:
                        if child.enabled:
                            _log(f'starting {child.name}: {child.cmd}')
                            child.start(_log_fh)

            # ── Crash restart ────────────────────────────────────────────
            with _children_lock:
                for child in children:
                    if not child.enabled:
                        continue
                    rc = child.poll()
                    if rc is not None:
                        child.restart_count += 1
                        _log(f'{child.name} exited with {rc}; restarting in {RESTART_DELAY_SEC}s')
                        time.sleep(RESTART_DELAY_SEC)
                        with _log_fh_lock:
                            fh = _log_fh
                        child.start(fh)

            # ── Tunnel health check ──────────────────────────────────────
            if (mesosync and mesosync.enabled and mesosync.status == 'running'
                    and rtun_port and time.monotonic() >= next_tunnel_check):
                ok, detail = _probe_rtun(rtun_port)
                if ok:
                    _log(f'mesosync probe passed on remote port {rtun_port}')
                else:
                    _log(f'mesosync probe failed on remote port {rtun_port} ({detail}); restarting tunnel')
                    with _children_lock:
                        _stop_children([mesosync], grace_sec=RESTART_DELAY_SEC)
                        _log(f'starting {mesosync.name}: {mesosync.cmd}')
                        with _log_fh_lock:
                            fh = _log_fh
                        mesosync.start(fh)
                next_tunnel_check = time.monotonic() + TUNNEL_CHECK_INTERVAL_SEC

            time.sleep(1)

    finally:
        _cleanup()
        atexit.unregister(_cleanup)
        with _log_fh_lock:
            _log_fh.close()

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
