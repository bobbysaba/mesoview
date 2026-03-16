# meso360

Real-time data system for NSSL Mobile Mesonet vehicles. Three components work together — **mesoingest**, **mesoview**, and **mesosync** — orchestrated by a single process supervisor.

---

## Components

### mesoingest — `mesoingest.py`

Fetches the newest observation from the Campbell Scientific datalogger at 1 Hz via its HTTP interface, formats it as a CSV row, and appends it to a daily `.txt` file. Writes a header on the first record of each day. Retries automatically on network errors and exits after a configurable number of consecutive failures so the supervisor can restart it.

### mesoview — `mesoview.py`

Flask web server that streams live data to any browser on the local network. Reads the daily `.txt` files written by mesoingest and pushes one JSON record per second to connected browsers over SSE (Server-Sent Events). Serves an interactive dashboard with real-time wind, temperature, pressure, and map charts built with uPlot. Also exposes `/initial` to preload the last 2 hours of history from an in-memory cache on page load.

Run in `--test` mode to replay `test_data/test.txt` at 1 Hz with no datalogger required.

### mesosync — SSH reverse tunnel (managed by `supervisor.py`)

Maintains a persistent SSH reverse tunnel to `clamps@remote.bliss.science` so operators can reach the vehicle machine from outside the vehicle network. Uses `~/.ssh/clamps_rsa` for authentication. Each vehicle uses a unique `rtun_port` to avoid conflicts. The supervisor restarts the tunnel automatically if it drops.

### supervisor — `supervisor.py`

Starts mesoingest, mesoview, and mesosync as child processes, restarts any that exit unexpectedly, rotates the log file at midnight, and handles SIGINT/SIGTERM for clean shutdown. This is the only script you need to run. Works cross-platform (Windows/macOS/Linux).

---

## Requirements

- Python 3.10 or later
- [Miniforge](https://github.com/conda-forge/miniforge/releases) (recommended) **or** any Python installation with pip
- OpenSSH client — built into macOS, Linux, and Windows 10/11 (no WSL required)
- `~/.ssh/clamps_rsa` — the NSSL/BLISS RSA key must be present on each vehicle machine before mesosync will connect

---

## Setup

### 1 — Clone the repo

```bash
git clone <repo-url>
cd meso360
```

---

### 2 — Set up the Python environment

#### Option A — conda (recommended)

```bash
conda env create -f environment.yml
conda activate mesoview
```

To update later after a `git pull`:

```bash
conda env update -f environment.yml --prune
```

#### Option B — pip / venv

```bash
python -m venv .venv

# macOS / Linux
source .venv/bin/activate

# Windows
.venv\Scripts\activate

pip install -r requirements.txt
```

---

### 3 — Configure

Copy the example config and edit it:

```bash
cp mesoview.config.example.json mesoview.config.json
```

`mesoview.config.json` is git-ignored so your local settings won't be overwritten by `git pull`.

```json
{
  "data_dir":           "~/data/raw/mesonet",
  "log_dir":            "~/mesoview_logs",
  "logger_ip":          "192.168.4.6",
  "http_port":          8080,
  "mdns_hostname":      "mesoview",
  "ingest_retry_max":   100,
  "ingest_retry_delay": 5,
  "rtun_port":          2222
}
```

| Key | Component | Description | Default |
|-----|-----------|-------------|---------|
| `data_dir` | mesoingest / mesoview | Where daily `.txt` data files are written and read | `~/data/raw/mesonet` |
| `log_dir` | supervisor | Where daily log files are written (`mesoview.YYYYMMDD.log`) | `~/mesoview_logs` |
| `logger_ip` | mesoingest | IP address of the Campbell datalogger on the vehicle network | `192.168.4.6` |
| `http_port` | mesoview | Port the web dashboard listens on | `8080` |
| `mdns_hostname` | mesoview | mDNS hostname advertised as `<hostname>.local` on the LAN | `mesoview` |
| `ingest_retry_max` | mesoingest | Max consecutive fetch failures before mesoingest exits | `100` |
| `ingest_retry_delay` | mesoingest | Seconds between retry attempts | `5` |
| `rtun_port` | mesosync | Port for the SSH reverse tunnel — **unique per vehicle** to avoid conflicts | *(required)* |

---

### 4 — Run

```bash
# Make sure your environment is active first
conda activate mesoview   # or: source .venv/bin/activate

python supervisor.py
```

On startup, supervisor runs a set of preflight checks before launching any child processes and logs the results:

```
[supervisor] === Preflight checks ===
[supervisor]   PASS  config file found: .../mesoview.config.json
[supervisor]   PASS  data directory writable: ~/data/raw/mesonet
[supervisor]   PASS  SSH key found: ~/.ssh/clamps_rsa
[supervisor]   PASS  software up to date (a1b2c3d)
[supervisor] ========================
```

The fourth check fetches the remote and pulls any new commits automatically before children start — so the system always boots on the latest code. If an update is pulled, the output shows the git log instead of "up to date".

Any issue that needs user action appears as `WARN` with an explanation and the exact command or step to resolve it. All warnings are non-blocking — supervisor still starts, and each component handles its own retry logic.

Open a browser on any device connected to the same network and go to:

```
http://<host-machine-ip>:8080
```

Or, if your network supports mDNS/Bonjour:

```
http://mesoview.local:8080
```

---

## Dashboard

### Status indicator

The colored dot in the top-left of the dashboard shows the current data feed health:

| Color | Meaning |
|-------|---------|
| Green | Connected and receiving data normally |
| Orange | Connected but no data received in the last 30 seconds — mesoingest may be down, the datalogger may be unreachable, or the rack may not be powered on |
| Red | SSE connection to mesoview lost — the mesoview process may be down or the network between your browser and the host machine is interrupted |

If the dot is orange, check the log file for `WARNING` messages from mesoingest.

### Status card

The **Status** card at the bottom of the right sidebar shows additional system health details:

| Field | Description |
|-------|-------------|
| Last update | Time of the most recently received data point |
| Gaps (UTC day) | Number of data gaps detected since UTC midnight |
| Completeness | Percentage of expected 1 Hz readings received in the last 10 minutes |
| 3‑min ΔP | Pressure change over the last 3 minutes (hPa) — useful for detecting rapid pressure falls |
| GPS fix | **Fix** (green) = valid GPS position; **No Fix** (orange) = GPS reporting `nan`, position unavailable |
| Record age | Seconds since the latest GPS timestamp — turns orange when >30 s, matching the connection dot |

### Logs

All output from all three components is written to `~/mesoview_logs/mesoview.YYYYMMDD.log` (configurable via `log_dir`). To watch it live:

```bash
tail -f ~/mesoview_logs/mesoview.$(date +%Y%m%d).log
```

### Test / replay mode

To run with no datalogger (replays `test_data/test.txt` at 1 Hz):

```bash
python mesoview.py --test
```

---

## Run on startup

The goal is to have `supervisor.py` launch automatically when the host machine boots, using the Python executable from your environment. Find that path first:

```bash
# conda
conda activate mesoview
which python          # macOS / Linux
where python          # Windows — copy the full path shown

# venv
# macOS / Linux: /path/to/meso360/.venv/bin/python
# Windows:       C:\path\to\meso360\.venv\Scripts\python.exe
```

---

### macOS — launchd

Create `~/Library/LaunchAgents/com.mesoview.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>             <string>com.mesoview</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOUR_USER/miniforge3/envs/mesoview/bin/python</string>
    <string>/path/to/meso360/supervisor.py</string>
  </array>
  <key>WorkingDirectory</key>  <string>/path/to/meso360</string>
  <key>RunAtLoad</key>         <true/>
  <key>KeepAlive</key>         <true/>
</dict>
</plist>
```

Replace the Python path and repo path, then load it:

```bash
launchctl load ~/Library/LaunchAgents/com.mesoview.plist
```

To stop / unload:

```bash
launchctl unload ~/Library/LaunchAgents/com.mesoview.plist
```

---

### Linux — systemd (recommended)

Create `~/.config/systemd/user/mesoview.service`:

```ini
[Unit]
Description=meso360 supervisor
After=network.target

[Service]
WorkingDirectory=/path/to/meso360
ExecStart=/path/to/conda/envs/mesoview/bin/python supervisor.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

Enable and start:

```bash
systemctl --user enable mesoview
systemctl --user start mesoview
systemctl --user status mesoview   # check logs
```

#### Linux — cron (alternative)

```bash
crontab -e
```

Add this line (replace paths):

```
@reboot cd /path/to/meso360 && /path/to/conda/envs/mesoview/bin/python supervisor.py
```

---

### Windows — Task Scheduler

1. Open **Task Scheduler** → **Create Task**
2. **General** tab: give it a name (e.g. `Mesoview`); check *Run whether user is logged on or not*
3. **Triggers** tab: New → *At startup*
4. **Actions** tab: New →
   - **Program/script**: full path to your env's Python, e.g.
     `C:\Users\YOU\miniforge3\envs\mesoview\python.exe`
   - **Add arguments**: `supervisor.py`
   - **Start in**: full path to the repo directory, e.g.
     `C:\path\to\meso360`
5. **Settings** tab: check *If the task fails, restart every 1 minute*
6. Click OK and enter your Windows password when prompted

To test without rebooting: right-click the task → **Run**.

---

## Updating

**At startup:** supervisor automatically runs `git pull` as part of the preflight checks, so the system always boots on the latest code. No manual action needed.

**Mid-operations:** if an update is available while the system is running, an `↑ Update · <commit>` button appears in the bottom-right corner of the dashboard. Clicking it pulls the latest code and restarts all three components (mesoingest, mesoview, mesosync) within a few seconds. The dashboard reconnects automatically.

**New dependencies:** if a pull adds new Python packages, update the environment manually:

```bash
conda env update -f environment.yml --prune
```

The config file (`mesoview.config.json`) is never modified by a git update.

---

## File layout

```
meso360/
├── supervisor.py          # start here — runs mesoingest, mesoview, and mesosync; auto-restarts all
├── mesoingest.py          # fetches data from the datalogger at 1 Hz; writes daily .txt files
├── mesoview.py            # Flask SSE server + web dashboard
├── mesoview.config.example.json   # copy to mesoview.config.json and edit
├── environment.yml        # conda environment spec
├── requirements.txt       # pip fallback
├── templates/
│   └── index.html         # single-page dashboard
├── static/                # uPlot chart library (auto-downloaded on first run)
├── test_data/
│   └── test.txt           # sample data for --test mode
└── docs/
    └── meso360_field_guide.md     # full reference: installation, dashboard guide, variables, troubleshooting
```
