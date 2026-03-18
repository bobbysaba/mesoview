#!/usr/bin/env python3
"""
Shared config/schema helpers for meso360 components.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

REPO_DIR = Path(__file__).parent
CONFIG_PATH = REPO_DIR / 'meso360.config.json'
DEFAULT_DATA_DIR = Path.home() / 'data' / 'raw' / 'mesonet'
DEFAULT_LOG_DIR = Path.home() / 'mesoview_logs'

# Must stay aligned with the datalogger table layout.
HEADER = (
    'sfc_wspd,sfc_wdir,t_slow,rh_slow,t_fast,dewpoint,der_rh,pressure,'
    'compass_dir,gps_date,gps_time,lat,lon,gps_alt,gps_spd,gps_dir,panel_temp'
)


def load_config(log: Callable[[str], None] | None = None) -> dict:
    """Load meso360.config.json, returning {} on missing/invalid config."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f) or {}
    except json.JSONDecodeError as e:
        if log is not None:
            log(f'ERROR: config file {CONFIG_PATH} contains invalid JSON: {e} — using defaults')
        return {}
    except Exception as e:
        if log is not None:
            log(f'Warning: could not read config {CONFIG_PATH}: {e}')
        return {}
