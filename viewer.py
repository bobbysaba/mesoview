#!/usr/bin/env python3
"""
MM Viewer — real-time web dashboard for Mobile Mesonet data.
Serves three interactive uPlot charts over SSE.

Usage:  python viewer.py           # live mode
        python viewer.py --test    # replay test_data/test.txt at 1 Hz
Access: http://<host-ip>:8080  (any device on the network)
"""

from flask import Flask, Response, render_template, jsonify
from pathlib import Path
from datetime import datetime, timezone, timedelta
import math
import argparse, csv, time, json, socket, urllib.request, webbrowser
import subprocess, os, sys, threading, atexit

try:
    from zeroconf import ServiceInfo, Zeroconf
except Exception:
    ServiceInfo = None
    Zeroconf = None

parser = argparse.ArgumentParser(add_help=False)
parser.add_argument('--test', action='store_true', help='Run in test/replay mode')
parser.add_argument('--test-start-offset', type=int, default=0,
                    help='(test mode) seconds after first data point to start replay')
args, _ = parser.parse_known_args()
TEST_MODE = args.test
TEST_START_OFFSET = max(0, args.test_start_offset or 0)

TEST_FILE = Path(__file__).parent / 'test_data' / 'test.txt'

app = Flask(__name__)

# ── Version cache (populated by background thread) ───────────────────────────
_ver_cache: dict = {'commit': None, 'dirty': False, 'update_available': False}
_ver_lock = threading.Lock()

def _version_worker():
    """Fetch git status/remote once immediately, then every 5 minutes."""
    repo_dir = Path(__file__).parent
    while True:
        try:
            commit = subprocess.run(
                ['git', 'rev-parse', '--short', 'HEAD'],
                cwd=repo_dir, capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            dirty = bool(subprocess.run(
                ['git', 'status', '--porcelain'],
                cwd=repo_dir, capture_output=True, text=True, timeout=5,
            ).stdout.strip())
            subprocess.run(
                ['git', 'fetch', '--quiet'],
                cwd=repo_dir, capture_output=True, text=True, timeout=15,
            )
            local_sha = subprocess.run(
                ['git', 'rev-parse', 'HEAD'],
                cwd=repo_dir, capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            remote_sha = subprocess.run(
                ['git', 'rev-parse', '@{u}'],
                cwd=repo_dir, capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            update_available = bool(local_sha and remote_sha and local_sha != remote_sha)
            with _ver_lock:
                _ver_cache.update(commit=commit, dirty=dirty, update_available=update_available)
        except Exception:
            pass
        time.sleep(300)

threading.Thread(target=_version_worker, daemon=True).start()

# ── Config ───────────────────────────────────────────────────────────────────
def _load_config():
    cfg_path = Path(__file__).parent / 'mesoview.config.json'
    if not cfg_path.exists():
        return {}
    try:
        with open(cfg_path) as f:
            return json.load(f) or {}
    except Exception as e:
        print(f'Warning: could not read config {cfg_path}: {e}')
        return {}

_CFG = _load_config()

# Update DATA_DIR to match DATA_DIR in ingest_mm.py
DATA_DIR      = Path(_CFG.get('data_dir', str(Path.home() / 'data' / 'raw' / 'mesonet')))
WINDOW        = 600          # records of history served on /initial (~10 min)
UPLOT_VERSION = '1.6.31'
HTTP_PORT     = int(_CFG.get('http_port', 8080))
MDNS_HOSTNAME = _CFG.get('mdns_hostname', 'mesoview')

# Column indices — matches HEADER in ingest_mm.py
IDX = {'wspd': 0, 'wdir': 1, 't': 4, 'td': 5, 'pressure': 7, 'compass_dir': 8, 'date': 9, 'time_': 10, 'lat': 11, 'lon': 12}

# ── uPlot assets (auto-downloaded once for offline field use) ─────────────────
STATIC = Path(__file__).parent / 'static'

def ensure_uplot():
    STATIC.mkdir(exist_ok=True)
    base = f'https://unpkg.com/uplot@{UPLOT_VERSION}/dist'
    assets = [
        ('uplot.min.js',  f'{base}/uPlot.iife.min.js'),
        ('uplot.min.css', f'{base}/uPlot.min.css'),
    ]
    for fname, url in assets:
        dst = STATIC / fname
        if not dst.exists():
            try:
                print(f'Downloading {fname}...')
                urllib.request.urlretrieve(url, dst)
                print(f'  saved to {dst}')
            except Exception as e:
                print(f'  Warning: could not fetch {fname}: {e}')

# ── Helpers ───────────────────────────────────────────────────────────────────
def today_file():
    return DATA_DIR / f"{datetime.now(timezone.utc).strftime('%Y%m%d')}.txt"

def parse_row(row):
    """Parse a CSV row into a data point dict, or return None on failure."""
    if len(row) <= max(IDX.values()):
        return None
    try:
        ts = datetime.strptime(
            row[IDX['date']] + row[IDX['time_']], '%d%m%y%H%M%S'
        ).replace(tzinfo=timezone.utc).timestamp()
        def f(idx):
            try:
                v = float(row[idx])
                return v if math.isfinite(v) else None
            except (ValueError, IndexError):
                return None

        t = f(IDX['t'])
        td = f(IDX['td'])
        wspd = f(IDX['wspd'])
        wdir = f(IDX['wdir'])
        pressure = f(IDX['pressure'])
        compass_dir = f(IDX['compass_dir'])
        lat = f(IDX['lat'])
        lon = f(IDX['lon'])

        if all(v is None for v in (t, td, wspd, wdir, pressure, compass_dir)):
            return None

        return dict(
            ts          = ts,
            t           = t,
            td          = td,
            wspd        = wspd,
            wdir        = wdir,
            pressure    = pressure,
            compass_dir = compass_dir,
            lat         = lat,
            lon         = lon,
        )
    except (ValueError, IndexError):
        return None

def get_local_ip():
    """Best-effort LAN IP discovery (avoids 127.0.0.1 where possible)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return '0.0.0.0'

def advertise_mdns(hostname='mesoview', port=8080):
    """Advertise http://<hostname>.local:<port> via mDNS/Bonjour."""
    if Zeroconf is None or ServiceInfo is None:
        print('mDNS: zeroconf not installed; skipping .local advertisement')
        return None

    ip = get_local_ip()
    service_type = '_http._tcp.local.'
    instance = f'{hostname}._http._tcp.local.'
    server = f'{hostname}.local.'
    info = ServiceInfo(
        service_type,
        instance,
        addresses=[socket.inet_aton(ip)],
        port=port,
        server=server,
        properties={'path': '/'},
    )

    zc = Zeroconf()
    try:
        zc.register_service(info)
    except Exception as e:
        print(f'mDNS: failed to register service: {e}')
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
    print(f'mDNS: advertised http://{hostname}.local:{port} -> {ip}:{port}')
    return zc

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/initial')
def initial():
    """Return the last 2 hours of records as JSON for chart initialization."""
    empty = {k: [] for k in ('ts', 't', 'td', 'wspd', 'wdir', 'pressure', 'compass_dir', 'lat', 'lon')}
    if TEST_MODE:
        # In test mode, preload the 2 hours before the replay start point
        with open(TEST_FILE) as fh:
            reader = csv.reader(fh)
            next(reader, None)  # skip header
            data = []
            for r in reader:
                p = parse_row(r)
                if p:
                    data.append(p)
        if not data:
            return jsonify(empty)
        start_ts = data[0]['ts'] + TEST_START_OFFSET
        if start_ts > data[-1]['ts']:
            start_ts = data[-1]['ts']
        cutoff = start_ts - 2 * 60 * 60
        result = {k: [] for k in empty}
        for p in data:
            if cutoff <= p['ts'] < start_ts:
                for k, v in p.items():
                    result[k].append(v)
        print(f'TEST initial: start_ts={start_ts}, cutoff={cutoff}, points={len(result["ts"])}')
        return jsonify(result)
    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - 2 * 60 * 60

    today = today_file()
    yesterday = DATA_DIR / f"{(now - timedelta(days=1)).strftime('%Y%m%d')}.txt"
    files = [f for f in (yesterday, today) if f.exists()]
    if not files:
        return jsonify(empty)

    result = {k: [] for k in empty}
    for f in files:
        with open(f) as fh:
            reader = csv.reader(fh)
            next(reader, None)  # skip header
            for row in reader:
                p = parse_row(row)
                if p and p['ts'] >= cutoff:
                    for k, v in p.items():
                        result[k].append(v)
    return jsonify(result)

@app.route('/stream')
def stream():
    """SSE endpoint — pushes one JSON point per second to all connected clients."""
    def generate_test():
        """Replay test.txt at 1 Hz using timestamps from file, looping forever."""
        with open(TEST_FILE) as fh:
            reader = csv.reader(fh)
            next(reader, None)   # skip header
            data = []
            for r in reader:
                if len(r) <= max(IDX.values()):
                    continue
                p = parse_row(r)
                if p:
                    data.append(p)
        if TEST_START_OFFSET and data:
            start_ts = data[0]['ts'] + TEST_START_OFFSET
            if start_ts > data[-1]['ts']:
                start_ts = data[-1]['ts']
            data = [p for p in data if p['ts'] >= start_ts]
        print(f'TEST stream points={len(data)} (offset={TEST_START_OFFSET}s)')
        while True:
            for p in data:
                yield f'data: {json.dumps(p)}\n\n'
                time.sleep(1)

    def generate():
        path = today_file()

        # Wait for the data file to appear (ingest may not have started yet)
        while not path.exists():
            yield ': keep-alive\n\n'
            time.sleep(2)
            path = today_file()

        pos = path.stat().st_size   # start at end of file (no replay)

        while True:
            # Handle day rollover
            new_path = today_file()
            if new_path != path:
                path = new_path
                # Skip header row on day rollover
                if path.exists():
                    with open(path) as fh:
                        fh.readline()
                        pos = fh.tell()
                else:
                    pos = 0

            if path.exists():
                with open(path) as fh:
                    fh.seek(pos)
                    lines = fh.readlines()
                    pos = fh.tell()
                for line in lines:
                    p = parse_row(line.strip().split(','))
                    if p:
                        yield f'data: {json.dumps(p)}\n\n'

            time.sleep(1)

    return Response(
        generate_test() if TEST_MODE else generate(),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )

@app.route('/version')
def version():
    """Return cached git status (populated by background thread)."""
    with _ver_lock:
        return jsonify(dict(_ver_cache))

@app.route('/update', methods=['POST'])
def update():
    """Run git pull in the repo directory, then restart the server process."""
    repo_dir = Path(__file__).parent
    try:
        result = subprocess.run(
            ['git', 'pull'],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = (result.stdout + result.stderr).strip()
        changed = result.returncode == 0 and 'Already up to date.' not in output
    except Exception as e:
        return jsonify({'ok': False, 'output': str(e), 'changed': False})

    if changed:
        def restart():
            time.sleep(0.5)
            # Spawn a shell that waits for this process to fully exit and release
            # the port, then starts a fresh Python process with the same args.
            subprocess.Popen(
                ['sh', '-c', 'sleep 1 && exec "$@"', '--', sys.executable] + sys.argv,
                close_fds=True,
            )
            os._exit(0)
        threading.Thread(target=restart, daemon=True).start()

    return jsonify({'ok': result.returncode == 0, 'output': output, 'changed': changed})

# ─────────────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    ensure_uplot()
    if TEST_MODE:
        print(f'TEST MODE — replaying {TEST_FILE}')
    else:
        print(f'Data directory: {DATA_DIR}')
    advertise_mdns(hostname=MDNS_HOSTNAME, port=HTTP_PORT)
    local_ip = get_local_ip()
    url = f'http://{local_ip}:{HTTP_PORT}'
    print(f'Starting MM Viewer — open {url}')
    try:
        webbrowser.open(url, new=2)
    except Exception as e:
        print(f'Warning: could not open browser: {e}')
    app.run(host='0.0.0.0', port=HTTP_PORT, debug=False, threaded=True)
