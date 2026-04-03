#!/usr/bin/env python3
"""
analyze_gaps.py — scan a meso360 daily data file and report all timestamp gaps.

Usage:
    python analyze_gaps.py                  # today's file
    python analyze_gaps.py 20260403.txt     # specific file
    python analyze_gaps.py --all            # all files in data dir
"""

import csv
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from common import DEFAULT_DATA_DIR, HEADER

GAP_SEC = 2  # matches mesoview JS constant — anything ≥ this is a "gap"

_HDR = HEADER.split(',')
IDX_DATE = _HDR.index('gps_date')
IDX_TIME = _HDR.index('gps_time')


def parse_ts(row):
    """Return UTC unix timestamp from a data row, or None if unparseable."""
    if len(row) <= max(IDX_DATE, IDX_TIME):
        return None
    d, t = row[IDX_DATE].strip(), row[IDX_TIME].strip()
    if not d or not t or d.lower() == 'nan' or t.lower() == 'nan':
        return None
    try:
        return datetime.strptime(d + t, '%d%m%y%H%M%S').replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def analyze(path: Path):
    print(f'\n{"="*60}')
    print(f'File: {path.name}')
    print(f'{"="*60}')

    gaps = []
    total_rows = 0
    nan_rows = 0
    prev_ts = None
    prev_time_str = None

    with open(path) as fh:
        reader = csv.reader(fh)
        next(reader, None)  # skip header
        for row in reader:
            total_rows += 1
            ts = parse_ts(row)
            if ts is None:
                nan_rows += 1
                continue
            if prev_ts is not None:
                delta = ts - prev_ts
                if delta >= GAP_SEC:
                    gaps.append({
                        'start_ts': prev_ts,
                        'end_ts': ts,
                        'start_time': prev_time_str,
                        'end_time': row[IDX_TIME].strip(),
                        'delta_sec': delta,
                    })
            prev_ts = ts
            prev_time_str = row[IDX_TIME].strip()

    total_gap_sec = sum(g['delta_sec'] - 1 for g in gaps)  # -1 because 1s between points is normal
    day_sec = 86400
    pct_missing = total_gap_sec / day_sec * 100

    print(f'Total data rows:   {total_rows}')
    print(f'Rows with NaN GPS: {nan_rows}')
    print(f'Gaps (≥{GAP_SEC}s):      {len(gaps)}')
    print(f'Total missing sec: {total_gap_sec:.0f}s  ({pct_missing:.1f}% of a full day)')
    print()

    if not gaps:
        print('No gaps found.')
        return

    # Bucket gaps by size
    buckets = {'2-5s': [], '6-30s': [], '31-300s': [], '>300s': []}
    for g in gaps:
        d = g['delta_sec']
        if d <= 5:
            buckets['2-5s'].append(g)
        elif d <= 30:
            buckets['6-30s'].append(g)
        elif d <= 300:
            buckets['31-300s'].append(g)
        else:
            buckets['>300s'].append(g)

    print('Gap size breakdown:')
    for label, items in buckets.items():
        if items:
            total = sum(g['delta_sec'] - 1 for g in items)
            print(f'  {label:>8}:  {len(items):4d} gaps,  {total:6.0f}s missing')

    print()
    print(f'{"#":>4}  {"UTC start":>8}  {"UTC end":>8}  {"delta":>8}  note')
    print(f'{"─"*4}  {"─"*8}  {"─"*8}  {"─"*8}  {"─"*20}')
    for i, g in enumerate(gaps, 1):
        start_dt = datetime.fromtimestamp(g['start_ts'], tz=timezone.utc)
        note = ''
        if g['delta_sec'] > 3600:
            note = '← ops break?'
        elif g['delta_sec'] > 300:
            note = '← long outage'
        elif g['delta_sec'] > 30:
            note = '← moderate'
        print(f'{i:>4}  {start_dt.strftime("%H:%M:%S"):>8}  {g["end_time"]:>8}  {g["delta_sec"]:>7.0f}s  {note}')


def main():
    data_dir = DEFAULT_DATA_DIR
    args = sys.argv[1:]

    if '--all' in args:
        files = sorted(data_dir.glob('????????.txt'))
        if not files:
            print(f'No data files found in {data_dir}')
            sys.exit(1)
        for f in files:
            analyze(f)
    elif args:
        p = Path(args[0])
        if not p.is_absolute():
            p = data_dir / p
        if not p.exists():
            print(f'File not found: {p}')
            sys.exit(1)
        analyze(p)
    else:
        from datetime import date
        today = date.today().strftime('%Y%m%d')
        p = data_dir / f'{today}.txt'
        if not p.exists():
            print(f"Today's file not found: {p}")
            print(f'Available files: {[f.name for f in sorted(data_dir.glob("????????.txt"))]}')
            sys.exit(1)
        analyze(p)


if __name__ == '__main__':
    main()
