#!/usr/bin/env python3
"""
mesoview — real-time web dashboard for Mobile Mesonet data.
Serves three interactive uPlot charts over SSE.

Usage:  python mesoview.py           # live mode
        python mesoview.py --test    # replay test_data/test.txt at 1 Hz
Access: http://<host-ip>:8080  (any device on the network)
"""

from flask import Flask, Response, render_template, jsonify
from pathlib import Path
from datetime import datetime, timezone, timedelta
import math
import argparse, csv, time, json, socket, urllib.request, webbrowser
import subprocess, os, sys, threading, atexit
from collections import deque  # used for the fixed-size in-memory data cache

# zeroconf enables http://mesoview.local discovery on the LAN without knowing the host IP
# it's optional — if not installed, mDNS is skipped and the numeric IP still works
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
TEST_START_OFFSET = max(0, args.test_start_offset or 0)  # clamp to 0 so a negative offset is treated as 0

TEST_FILE = Path(__file__).parent / 'test_data' / 'test.txt'  # sample data file used in --test mode

app = Flask(__name__)

# ── Version cache (populated by background thread) ───────────────────────────
# Storing version info in a dict avoids blocking HTTP requests on slow git operations
_ver_cache: dict = {'commit': None, 'dirty': False, 'update_available': False}
_ver_lock = threading.Lock()  # protects _ver_cache from concurrent reads/writes across threads

def _version_worker():
    """Fetch git status/remote once immediately, then every 5 minutes."""
    repo_dir = Path(__file__).parent
    while True:
        try:
            # get the short commit hash shown in the dashboard header
            commit = subprocess.run(
                ['git', 'rev-parse', '--short', 'HEAD'],
                cwd=repo_dir, capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            # porcelain output is non-empty if there are any uncommitted changes
            dirty = bool(subprocess.run(
                ['git', 'status', '--porcelain'],
                cwd=repo_dir, capture_output=True, text=True, timeout=5,
            ).stdout.strip())
            # fetch from remote so we can compare local vs remote SHA
            subprocess.run(
                ['git', 'fetch', '--quiet'],
                cwd=repo_dir, capture_output=True, text=True, timeout=15,
            )
            local_sha = subprocess.run(
                ['git', 'rev-parse', 'HEAD'],
                cwd=repo_dir, capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            # @{u} resolves to the upstream (remote tracking) branch of the current branch
            remote_sha = subprocess.run(
                ['git', 'rev-parse', '@{u}'],
                cwd=repo_dir, capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            # an update is available if remote SHA exists and differs from local SHA
            update_available = bool(local_sha and remote_sha and local_sha != remote_sha)
            with _ver_lock:
                _ver_cache.update(commit=commit, dirty=dirty, update_available=update_available)
        except Exception:
            pass  # silently skip if git isn't available or the repo has no remote
        time.sleep(300)  # re-check every 5 minutes; no need to poll faster for a version indicator

threading.Thread(target=_version_worker, daemon=True).start()  # daemon=True so it exits when the main process exits

# ── Config ───────────────────────────────────────────────────────────────────
def _log(msg):
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{ts}] [mesoview] {msg}', flush=True)  # flush=True ensures lines appear immediately in the supervisor log

def _load_config():
    cfg_path = Path(__file__).parent / 'mesoview.config.json'
    if not cfg_path.exists():
        return {}  # missing config is fine; all values fall back to defaults below
    try:
        with open(cfg_path) as f:
            return json.load(f) or {}
    except Exception as e:
        _log(f'Warning: could not read config {cfg_path}: {e}')
        return {}

_CFG = _load_config()  # loaded once at startup; restart viewer to pick up config changes

# Update DATA_DIR to match DATA_DIR in mesoingest.py
DATA_DIR      = Path(_CFG.get('data_dir', str(Path.home() / 'data' / 'raw' / 'mesonet'))).expanduser()
UPLOT_VERSION = '1.6.31'     # pinned version of the uPlot charting library; update here to upgrade
HTTP_PORT     = int(_CFG.get('http_port', 8080))
MDNS_HOSTNAME = _CFG.get('mdns_hostname', 'mesoview')  # advertised as <hostname>.local on the LAN

# Column indices — derived from HEADER so they can't drift if columns are added/removed.
# Must match HEADER in mesoingest.py.
HEADER = 'sfc_wspd,sfc_wdir,t_slow,rh_slow,t_fast,dewpoint,der_rh,pressure,compass_dir,gps_date,gps_time,lat,lon,gps_alt,gps_spd,gps_dir,panel_temp'
_HDR = HEADER.split(',')  # intermediate list used to look up column positions by name
IDX = {
    'wspd':        _HDR.index('sfc_wspd'),      # surface wind speed
    'wdir':        _HDR.index('sfc_wdir'),      # surface wind direction
    't':           _HDR.index('t_fast'),        # fast-response temperature
    'td':          _HDR.index('dewpoint'),      # dewpoint temperature
    'pressure':    _HDR.index('pressure'),      # station pressure
    'compass_dir': _HDR.index('compass_dir'),   # vehicle heading from compass
    'date':        _HDR.index('gps_date'),      # GPS date (DDMMYY format)
    'time_':       _HDR.index('gps_time'),      # GPS time (HHMMSS format); trailing underscore avoids shadowing Python's 'time'
    'lat':         _HDR.index('lat'),           # GPS latitude
    'lon':         _HDR.index('lon'),           # GPS longitude
}

# ── uPlot assets (auto-downloaded once for offline field use) ─────────────────
STATIC = Path(__file__).parent / 'static'

def ensure_uplot():
    STATIC.mkdir(exist_ok=True)
    # assets are served from the local static directory so the dashboard works with no internet connection
    base = f'https://unpkg.com/uplot@{UPLOT_VERSION}/dist'
    assets = [
        ('uplot.min.js',  f'{base}/uPlot.iife.min.js'),
        ('uplot.min.css', f'{base}/uPlot.min.css'),
    ]
    for fname, url in assets:
        dst = STATIC / fname
        if not dst.exists():  # only download if the file isn't already cached locally
            try:
                _log(f'Downloading {fname}...')
                with urllib.request.urlopen(url, timeout=10) as resp:
                    dst.write_bytes(resp.read())
                _log(f'Saved {dst}')
            except Exception as e:
                _log(f'Warning: could not fetch {fname}: {e}')  # non-fatal; dashboard will load from CDN instead

# ── Helpers ───────────────────────────────────────────────────────────────────
def today_file():
    # returns the path to today's data file based on UTC date, matching the naming convention in mesoingest.py
    return DATA_DIR / f"{datetime.now(timezone.utc).strftime('%Y%m%d')}.txt"

def parse_row(row):
    """Parse a CSV row into a data point dict, or return None on failure."""
    if len(row) <= max(IDX.values()):
        return None  # row is too short to contain all expected columns; skip it
    try:
        # combine GPS date (DDMMYY) and time (HHMMSS) into a single UTC unix timestamp
        ts = datetime.strptime(
            row[IDX['date']] + row[IDX['time_']], '%d%m%y%H%M%S'
        ).replace(tzinfo=timezone.utc).timestamp()

        def f(idx):
            # helper: convert a single column to float, returning None if missing or non-finite
            try:
                v = float(row[idx])
                return v if math.isfinite(v) else None  # treat inf/nan as missing data
            except (ValueError, IndexError):
                return None

        t           = f(IDX['t'])
        td          = f(IDX['td'])
        wspd        = f(IDX['wspd'])
        wdir        = f(IDX['wdir'])
        pressure    = f(IDX['pressure'])
        compass_dir = f(IDX['compass_dir'])
        lat         = f(IDX['lat'])
        lon         = f(IDX['lon'])

        # skip rows where all primary met fields are missing (e.g. sensor dropout or header line)
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
    except (ValueError, IndexError) as e:
        _log(f'WARNING: parse_row failed: {e}')  # log so repeated failures are visible in the supervisor log
        return None

def get_local_ip():
    """Best-effort LAN IP discovery (avoids 127.0.0.1 where possible)."""
    try:
        # open a UDP socket to a public address — no data is sent, but the OS picks the outbound interface
        # this gives us the LAN IP that other devices would use to reach this machine
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())  # fallback: resolve our own hostname
        except Exception:
            return '0.0.0.0'  # last resort: bind to all interfaces and let the user find the address

def advertise_mdns(hostname='mesoview', port=8080):
    """Advertise http://<hostname>.local:<port> via mDNS/Bonjour."""
    if Zeroconf is None or ServiceInfo is None:
        _log('mDNS: zeroconf not installed; skipping .local advertisement')
        return None

    ip = get_local_ip()
    service_type = '_http._tcp.local.'                # standard mDNS service type for HTTP
    instance     = f'{hostname}._http._tcp.local.'    # full instance name visible to browsers/devices
    server       = f'{hostname}.local.'               # the hostname that resolves to this machine on the LAN
    info = ServiceInfo(
        service_type,
        instance,
        addresses=[socket.inet_aton(ip)],  # inet_aton converts dotted-decimal IP to packed bytes
        port=port,
        server=server,
        properties={'path': '/'},          # advertise the root path so browsers can navigate directly
    )

    zc = Zeroconf()
    try:
        zc.register_service(info)  # broadcast the service on the LAN
    except Exception as e:
        _log(f'mDNS: failed to register service: {e}')
        try:
            zc.close()
        except Exception:
            pass
        return None

    def _cleanup():
        # unregister the mDNS service when the viewer exits so the name doesn't linger on the LAN
        try:
            zc.unregister_service(info)
            zc.close()
        except Exception:
            pass

    atexit.register(_cleanup)  # atexit runs _cleanup automatically on normal process exit
    _log(f'mDNS: advertised http://{hostname}.local:{port} -> {ip}:{port}')
    return zc

# ── In-memory data cache ──────────────────────────────────────────────────────
# Holds up to 2 hours of parsed points so /initial never reads from disk.
_data_buf: deque = deque(maxlen=7200)  # 7200 = 2 hours at 1 Hz; oldest points are auto-dropped when full
_data_lock = threading.Lock()          # guards _data_buf against concurrent access from Flask threads and _cache_worker

def _cache_worker():
    """Background thread: pre-populates _data_buf from existing files on
    startup, then tails the current daily file and appends new points as
    ingest writes them."""
    # ── Startup: load up to 2 hours of history so /initial has data immediately ──
    now    = datetime.now(timezone.utc)
    cutoff = now.timestamp() - 2 * 60 * 60  # unix timestamp 2 hours ago
    yesterday = DATA_DIR / f"{(now - timedelta(days=1)).strftime('%Y%m%d')}.txt"
    for f in (yesterday, today_file()):  # check yesterday first in case we're near midnight
        if f.exists():
            with open(f) as fh:
                reader = csv.reader(fh)
                next(reader, None)  # skip header row
                for row in reader:
                    p = parse_row(row)
                    if p and p['ts'] >= cutoff:  # only load points within the 2-hour window
                        with _data_lock:
                            _data_buf.append(p)

    # ── Tail loop: append new points as ingest writes them ───────────────────
    path = today_file()
    while not path.exists():  # wait for ingest to create today's file before starting to tail
        time.sleep(2)
        path = today_file()
    pos = path.stat().st_size  # start at the end of the file so we only pick up new lines

    while True:
        new_path = today_file()
        if new_path != path:
            # date has rolled over — switch to the new file
            path = new_path
            pos  = 0  # read the new file from the start (after skipping its header)
            if path.exists():
                with open(path) as fh:
                    fh.readline()   # skip header on the new file
                    pos = fh.tell() # advance pos past the header so we don't re-read it next loop

        if path.exists():
            with open(path) as fh:
                fh.seek(pos)          # jump to where we left off last iteration
                if pos == 0:
                    fh.readline()     # skip header when starting from the top of a newly-appeared file
                lines = fh.readlines()
                pos = fh.tell()       # save position so next iteration continues from here
            for line in lines:
                p = parse_row(next(csv.reader([line.strip()]), []))
                if p:
                    with _data_lock:
                        _data_buf.append(p)  # add new point to the rolling cache

        time.sleep(1)  # poll the file once per second, matching ingest's ~1 Hz write rate

if not TEST_MODE:
    # only start the cache worker in live mode; test mode serves from the test file directly
    threading.Thread(target=_cache_worker, daemon=True).start()

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
        start_ts = data[0]['ts'] + TEST_START_OFFSET  # replay starts this many seconds into the file
        if start_ts > data[-1]['ts']:
            start_ts = data[-1]['ts']  # clamp to end of file if offset overshoots
        cutoff = start_ts - 2 * 60 * 60  # show 2 hours of history before the replay start point
        result = {k: [] for k in empty}
        for p in data:
            if cutoff <= p['ts'] < start_ts:  # only include points in the 2-hour pre-replay window
                for k, v in p.items():
                    result[k].append(v)
        _log(f'TEST initial: start_ts={start_ts}, cutoff={cutoff}, points={len(result["ts"])}')
        return jsonify(result)
    # live mode: serve directly from the in-memory cache — no disk I/O on client connect
    cutoff = datetime.now(timezone.utc).timestamp() - 2 * 60 * 60  # 2 hours ago as a unix timestamp
    with _data_lock:
        snapshot = list(_data_buf)  # copy the deque while holding the lock to avoid mid-iteration modification
    result = {k: [] for k in empty}
    for p in snapshot:
        if p['ts'] >= cutoff:  # filter to the last 2 hours in case the cache holds older data
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
                    continue  # skip malformed rows before attempting to parse
                p = parse_row(r)
                if p:
                    data.append(p)
        if TEST_START_OFFSET and data:
            start_ts = data[0]['ts'] + TEST_START_OFFSET  # fast-forward into the file
            if start_ts > data[-1]['ts']:
                start_ts = data[-1]['ts']  # clamp so we don't skip past the end
            data = [p for p in data if p['ts'] >= start_ts]
        _log(f'TEST stream points={len(data)} (offset={TEST_START_OFFSET}s)')
        while True:
            for p in data:
                yield f'data: {json.dumps(p)}\n\n'  # SSE format: "data: <payload>\n\n"
                time.sleep(1)                        # emit one point per second to simulate live 1 Hz data

    def generate():
        path = today_file()

        # Wait for the data file to appear (ingest may not have started yet)
        _wait_secs = 0
        while not path.exists():
            yield ': keep-alive\n\n'   # SSE comment line; keeps the connection alive while waiting
            time.sleep(2)
            _wait_secs += 2
            if _wait_secs % 60 == 0:  # log a warning every minute so the user knows something is wrong
                _log(f'WARNING: still waiting for data file {path} ({_wait_secs}s elapsed)')
            path = today_file()        # re-evaluate in case the date rolled over while waiting

        pos = path.stat().st_size   # start at end of file (no replay) so clients only see new data

        while True:
            # Handle day rollover
            new_path = today_file()
            if new_path != path:
                # date has changed — switch to the new file
                path = new_path
                # Skip header row on day rollover
                if path.exists():
                    with open(path) as fh:
                        fh.readline()      # skip the header on the new file
                        pos = fh.tell()    # advance pos past the header
                else:
                    pos = 0  # new file doesn't exist yet; will wait for it on the next iteration

            if path.exists():
                with open(path) as fh:
                    fh.seek(pos)          # jump to where we last read
                    if pos == 0:
                        fh.readline()     # skip header when starting from top of a new file
                    lines = fh.readlines()
                    pos = fh.tell()       # save position for the next iteration
                for line in lines:
                    p = parse_row(next(csv.reader([line.strip()]), []))
                    if p:
                        yield f'data: {json.dumps(p)}\n\n'  # push the new point to the browser

            time.sleep(1)  # poll once per second, matching ingest's ~1 Hz write rate

    return Response(
        generate_test() if TEST_MODE else generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',       # prevent browsers/proxies from caching the SSE stream
            'X-Accel-Buffering': 'no',         # disable nginx proxy buffering so events reach the browser immediately
        },
    )

@app.route('/version')
def version():
    """Return cached git status (populated by background thread)."""
    with _ver_lock:
        return jsonify(dict(_ver_cache))  # copy the dict while holding the lock to avoid partial reads

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
        # 'changed' is True only if pull succeeded AND new commits were actually downloaded
        changed = result.returncode == 0 and 'Already up to date.' not in output
    except Exception as e:
        return jsonify({'ok': False, 'output': str(e), 'changed': False})

    if changed:
        def _exit():
            time.sleep(0.5)  # brief delay so this HTTP response can finish before we exit
            os._exit(42)  # sentinel code — supervisor will restart all children with new code
        threading.Thread(target=_exit, daemon=True).start()

    return jsonify({'ok': result.returncode == 0, 'output': output, 'changed': changed})

# ─────────────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    ensure_uplot()  # download uPlot JS/CSS to static/ if not already present (needed for offline use)
    if TEST_MODE:
        _log(f'TEST MODE — replaying {TEST_FILE}')
    else:
        _log(f'Data directory: {DATA_DIR}')
    advertise_mdns(hostname=MDNS_HOSTNAME, port=HTTP_PORT)
    local_ip = get_local_ip()
    url = f'http://{local_ip}:{HTTP_PORT}'
    _log(f'Starting MM Viewer — open {url}')
    try:
        webbrowser.open(url, new=2)  # new=2 opens in a new browser tab rather than a new window
    except Exception as e:
        _log(f'Warning: could not open browser: {e}')  # non-fatal; user can open the URL manually
    # host='0.0.0.0' binds to all interfaces so devices on the LAN can connect, not just localhost
    # threaded=True allows Flask to handle multiple SSE clients simultaneously without blocking
    app.run(host='0.0.0.0', port=HTTP_PORT, debug=False, threaded=True)
