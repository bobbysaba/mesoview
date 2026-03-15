#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Bobby Saba - Mobile Mesonet Ingest Script
# This script reads in mobile mesonet data from the NSSL LiDAR truck and stores it locally

import os
import sys
import time
import datetime as dt
import requests
from pathlib import Path
from datetime import timezone
from html.parser import HTMLParser
import json

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

# set vehicle-specific variables
DATA_DIR = Path(_CFG.get('data_dir', str(Path.home() / 'data' / 'raw' / 'mesonet')))
IP = _CFG.get('logger_ip', '192.168.4.6')

MAX_TRIES = int(_CFG.get('ingest_retry_max', 100))
RETRY_DELAY = int(_CFG.get('ingest_retry_delay', 5))

# set headers for the files
HEADER = 'sfc_wspd,sfc_wdir,t_slow,rh_slow,t_fast,dewpoint,der_rh,pressure,compass_dir,gps_date,gps_time,lat,lon,gps_alt,gps_spd,gps_dir,panel_temp'


class TableParser(HTMLParser):
    """Lightweight HTML table parser — extracts all <td> text content."""
    def __init__(self):
        super().__init__()
        self.values = []
        self._in_td = False

    def handle_starttag(self, tag, attrs):
        if tag == 'td':
            self._in_td = True

    def handle_endtag(self, tag):
        if tag == 'td':
            self._in_td = False

    def handle_data(self, data):
        if self._in_td:
            self.values.append(data.strip())


def fetch_record(ip, max_tries=100, retry_delay=5):
    """Fetch the newest mesonet record from the datalogger web interface."""
    url = f'http://{ip}/command=NewestRecord&table=Obs'

    for attempt in range(1, max_tries + 1):
        try:
            resp = requests.get(url, timeout=5)
            resp.raise_for_status()

            parser = TableParser()
            parser.feed(resp.text)

            if not parser.values:
                raise ValueError('No table data found in response')

            return parser.values

        except Exception as e:
            if attempt < max_tries:
                print(f'Read attempt {attempt} failed ({e}). Retrying in {retry_delay}s...', flush=True)
                time.sleep(retry_delay)
            else:
                print(f'Failed to read data after {max_tries} attempts. Terminating.')
                sys.exit(1)


def parse_record(raw_values):
    """Clean and format raw table values into a CSV data line."""
    values = list(raw_values)

    # fix GPS date/time formatting (strip surrounding chars, zero-pad to 6 digits)
    values[9]  = values[9].strip("'\" ").zfill(6)
    values[10] = values[10].strip("'\" ").zfill(6)

    # drop GPS status (index 11) and battery voltage (index 17)
    # remove higher index first to avoid shifting
    for idx in sorted([11, 17], reverse=True):
        if idx < len(values):
            values.pop(idx)

    return ','.join(values)



def main_loop():
    while True:
        start = dt.datetime.now(timezone.utc)

        # fetch and parse
        raw = fetch_record(IP, max_tries=MAX_TRIES, retry_delay=RETRY_DELAY)
        data_line = parse_record(raw)

        # resolve daily output file
        file_date = dt.datetime.strptime(data_line.split(',')[9], '%d%m%y').strftime('%Y%m%d')
        daily_file = DATA_DIR / f'{file_date}.txt'

        if not daily_file.exists():
            daily_file.parent.mkdir(parents=True, exist_ok=True)
            with open(daily_file, 'w') as f:
                f.write(HEADER + '\n')

        with open(daily_file, 'a') as f:
            f.write(data_line + '\n')

        elapsed = (dt.datetime.now(timezone.utc) - start).total_seconds()
        if elapsed < 1:
            time.sleep(1 - elapsed)


if __name__ == '__main__':
    main_loop()
