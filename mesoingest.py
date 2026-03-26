#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Bobby Saba - Mobile Mesonet Ingest Script
# This script reads in mobile mesonet data from the NSSL LiDAR truck and stores it locally

import os
import sys
import time
import datetime as dt
import requests                      # HTTP client used to query the datalogger web interface
from pathlib import Path
from datetime import timezone
from html.parser import HTMLParser   # used to extract values from the datalogger's HTML table response
from common import DEFAULT_DATA_DIR, HEADER, load_config

def _log(msg):
    # prefix every log line with a UTC timestamp so log entries are unambiguous across time zones
    ts = dt.datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{ts}] [mesoingest] {msg}', flush=True)  # flush=True ensures output appears immediately in the supervisor log

_CFG = load_config(_log)
_SESSION = requests.Session()  # reuses HTTP connections across 1 Hz polls when the datalogger supports keep-alive

# set vehicle-specific variables
DATA_DIR = Path(_CFG.get('data_dir', str(DEFAULT_DATA_DIR))).expanduser()  # where daily .txt files are written
IP = _CFG.get('logger_ip', '192.168.4.6')  # LAN IP of the Campbell Scientific datalogger; default is the NSSL truck address

MAX_TRIES    = int(_CFG.get('ingest_retry_max',   100))  # how many consecutive failures before giving up and exiting
RETRY_DELAY  = int(_CFG.get('ingest_retry_delay',   5))  # seconds to wait between retry attempts

class TableParser(HTMLParser):
    """Lightweight HTML table parser — extracts all <td> text content."""
    def __init__(self):
        super().__init__()
        self.values = []       # collected text values, one per <td> cell
        self._in_td = False    # flag: True while the parser is inside a <td>...</td> element

    def handle_starttag(self, tag, attrs):
        if tag == 'td':
            self._in_td = True   # entering a table cell; start collecting text

    def handle_endtag(self, tag):
        if tag == 'td':
            self._in_td = False  # leaving a table cell; stop collecting text

    def handle_data(self, data):
        if self._in_td:
            self.values.append(data.strip())  # only capture text that is inside a <td>


def fetch_record(ip, max_tries=100, retry_delay=5, session=None):
    """Fetch the newest mesonet record from the datalogger web interface.
    Returns (values, record_num) where record_num is None if it can't be parsed."""
    # the datalogger serves its most recent observation via this URL pattern
    url = f'http://{ip}/command=NewestRecord&table=Obs'
    session = session or _SESSION

    for attempt in range(1, max_tries + 1):
        try:
            resp = session.get(url, timeout=5)  # reuse the same HTTP session so repeated polls can keep the TCP connection warm
            resp.raise_for_status()              # raise immediately on 4xx/5xx HTTP errors

            parser = TableParser()
            parser.feed(resp.text)  # parse the HTML response and extract all <td> values

            if not parser.values:
                raise ValueError('No table data found in response')  # treat an empty table as a failure

            # extract the sequential record number from the HTML for gap detection
            try:
                record_num = int(resp.text.split('Current Record: </b>')[1].split('<')[0].strip())
            except (IndexError, ValueError):
                record_num = None

            return parser.values, record_num

        except Exception as e:
            if attempt < max_tries:
                _log(f'Read attempt {attempt} failed ({e}). Retrying in {retry_delay}s...')
                time.sleep(retry_delay)
            else:
                # after exhausting all retries, log and exit so the supervisor can restart ingest
                _log(f'Failed to read data after {max_tries} attempts. Terminating.')
                sys.exit(1)


def fetch_lastrecords(ip, session=None):
    """Fetch the last 10 Obs records from lastrecords.html for gap backfill.
    Returns a list of (record_num, values) tuples sorted oldest-first.
    Values are the 19 raw Obs fields — compatible with parse_record() directly."""
    url = f'http://{ip}/lastrecords.html'
    session = session or _SESSION
    resp = session.get(url, timeout=5)
    resp.raise_for_status()
    records = []
    for line in resp.text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(',', 1)  # split on first comma only to isolate record_num
        if len(parts) != 2:
            continue
        try:
            rec_num = int(float(parts[0]))  # float() first in case logger outputs "63.0"
            values = parts[1].split(',')
            records.append((rec_num, values))
        except ValueError:
            continue  # skip non-numeric lines (e.g. <!DOCTYPE html>)
    return records


def _clean_gps_field(val):
    """Strip surrounding quotes/spaces and zero-pad to 6 digits.
    Keeps 'nan' as-is so the caller can detect and handle a missing GPS fix."""
    s = val.strip("'\" ")  # the datalogger sometimes wraps values in single or double quotes
    # GPS date is DDMMYY and GPS time is HHMMSS — both need to be exactly 6 digits
    # 'nan' means the GPS has no fix yet; pass it through so main_loop can log a warning
    return s if s.lower() == 'nan' else s.zfill(6)


def parse_record(raw_values):
    """Clean and format raw table values into a CSV data line."""
    values = list(raw_values)  # copy so we don't mutate the original list

    # fix GPS date/time formatting (strip surrounding chars, zero-pad to 6 digits)
    values[9]  = _clean_gps_field(values[9])   # gps_date: DDMMYY
    values[10] = _clean_gps_field(values[10])  # gps_time: HHMMSS

    # drop GPS status (raw index 11) and battery voltage (raw index 17) — not needed in output
    # remove higher index first so the lower index isn't shifted by the first pop
    for idx in sorted([11, 17], reverse=True):
        if idx < len(values):  # guard in case the datalogger sends a shorter-than-expected row
            values.pop(idx)

    return ','.join(values)  # rejoin as a comma-separated string ready to write to file


def _write_record(data_line):
    """Write a parsed CSV data line to the appropriate daily file."""
    # use GPS date to name the daily file — GPS timestamp is ground truth for the observation time
    # fall back to UTC wall clock only if GPS date is missing (no fix); logs a warning when this happens
    gps_date_str = data_line.split(',')[9]  # index 9 is gps_date after parse_record drops GPS status
    try:
        file_date = dt.datetime.strptime(gps_date_str, '%d%m%y').strftime('%Y%m%d')
    except ValueError:
        # GPS date is 'nan' or otherwise unparseable — use today's UTC date to avoid losing the record
        file_date = dt.datetime.now(timezone.utc).strftime('%Y%m%d')
        _log(f'WARNING: GPS date invalid ({gps_date_str!r}), using UTC date {file_date}')

    daily_file = DATA_DIR / f'{file_date}.txt'

    # atomic header write: open in exclusive-create mode ('x') so only the first caller writes the header
    # FileExistsError means another process (or a previous loop iteration) already created the file — safe to ignore
    daily_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(daily_file, 'x') as f:
            f.write(HEADER + '\n')
    except FileExistsError:
        pass  # file already exists with a header; nothing to do

    with open(daily_file, 'a') as f:
        f.write(data_line + '\n')  # append the new observation as a single CSV line


def main_loop():
    last_record_num = None  # tracks the last successfully written record number for gap detection

    while True:
        start = dt.datetime.now(timezone.utc)  # record loop start time to enforce 1 Hz cadence

        # fetch and check for gaps
        raw, record_num = fetch_record(IP, max_tries=MAX_TRIES, retry_delay=RETRY_DELAY, session=_SESSION)

        if last_record_num is not None and record_num is not None and record_num - last_record_num > 1:
            gap = record_num - last_record_num - 1
            _log(f'WARNING: gap detected — missed {gap} record(s) ({last_record_num + 1} to {record_num - 1}), attempting backfill')
            try:
                backfill = fetch_lastrecords(IP, session=_SESSION)
                missing = sorted(
                    [(n, v) for n, v in backfill if last_record_num < n < record_num],
                    key=lambda x: x[0]
                )
                for rec_num, rec_values in missing:
                    try:
                        _write_record(parse_record(rec_values))
                        _log(f'Backfilled record {rec_num}')
                    except Exception as e:
                        _log(f'WARNING: failed to backfill record {rec_num}: {e}')
                recovered = len(missing)
                if recovered < gap:
                    _log(f'WARNING: {gap - recovered} record(s) unrecoverable (not in lastrecords buffer)')
            except Exception as e:
                _log(f'WARNING: backfill fetch failed: {e}')

        if record_num is not None:
            last_record_num = record_num

        # parse and write current record
        try:
            data_line = parse_record(raw)
        except Exception as e:
            # log the failure so repeated errors are visible, then skip this record and keep running
            _log(f'WARNING: parse_record failed, skipping record: {e}')
            elapsed = (dt.datetime.now(timezone.utc) - start).total_seconds()
            if elapsed < 1:
                time.sleep(1 - elapsed)  # still maintain 1 Hz even when skipping
            continue

        _write_record(data_line)

        # sleep for the remainder of the second to maintain ~1 Hz output rate
        elapsed = (dt.datetime.now(timezone.utc) - start).total_seconds()
        if elapsed < 1:
            time.sleep(1 - elapsed)


if __name__ == '__main__':
    main_loop()
