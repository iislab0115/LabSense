#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Shelly plug data collection program
- 1-second polling interval
- daily files created automatically
- error recovery
- can be restarted after an interruption
"""

import requests
import time
import csv
import json
from datetime import datetime, timedelta
import os
import logging
from pathlib import Path
import signal
import sys
import threading

# ============================================
# Settings
# ============================================


def _load_config():
    """Load config.local.json if present, otherwise the config.json template.

    Device addresses differ per deployment, so they are not kept in the source.
    This follows the same rule as smartthings_auth.py: config.json is the
    committed template and the real values live in config.local.json, which is
    git-ignored.
    """
    base = Path(__file__).resolve().parent
    local = base / "config.local.json"
    path = local if local.exists() else base / "config.json"
    if not path.exists():
        raise SystemExit(
            "config.json / config.local.json not found.\n"
            "Copy config.json to config.local.json and fill in your own values."
        )
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f), path


CONFIG, CONFIG_PATH = _load_config()


def _devices_from_config(cfg, path):
    devices = cfg.get("SHELLY_DEVICES") or []
    bad = [d for d in devices
           if "Please enter" in str(d.get("ip", "")) or "Please enter" in str(d.get("id", ""))]
    if not devices or bad:
        raise SystemExit(
            "SHELLY_DEVICES is not configured (%s).\n"
            "Copy config.json to config.local.json and fill in the label, id and ip\n"
            "of each plug with your own values." % path.name
        )
    return devices


# Smart plug inventory, loaded from config.local.json
DEVICES = _devices_from_config(CONFIG, CONFIG_PATH)

# Collection settings
INTERVAL = float(CONFIG.get("SHELLY_INTERVAL", 1.0))  # polling interval, default 1 s
TIMEOUT = float(CONFIG.get("SHELLY_TIMEOUT", 4.0))    # HTTP timeout, allows for Wi-Fi latency
MAX_RETRIES = 3  # retry count

# Refresh period for the GUI (or console output)
UI_UPDATE_EVERY_SECONDS = 5.0
# Flush period; flushing too often costs I/O
FLUSH_EVERY_SAMPLES = 10

_csv_base = CONFIG.get("CSV_BASE_DIR", "")
if not _csv_base or "Please enter" in str(_csv_base):
    raise SystemExit(
        "CSV_BASE_DIR is not configured (%s).\n"
        "Set it to the absolute path of the directory for CSV output." % CONFIG_PATH.name
    )
CSV_BASE_DIR = Path(_csv_base)
CSV_BASE_DIR.mkdir(parents=True, exist_ok=True)

# Log directory
LOG_DIR = CSV_BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Dashboard state, read by the launcher to refresh the UI
dashboard_state = {
    "status": "Initializing",
    "last_cycle": None,
    "total": len(DEVICES),
    "success": 0,
    "fail": 0,
    "devices": [],  # [{label, power, energy, status, updated}, ...]
}

# Callback injected by the launcher (or the dashboard)
on_data_updated = None

# ============================================
# Logging setup
# ============================================

def setup_logging():
    """Logging setup. Under unified execution the root handler is shared with
    SmartThings, which avoids duplicate records."""
    log_file = LOG_DIR / f"collector_{datetime.now().strftime('%Y%m%d')}.log"
    log_path_abs = str(log_file.resolve())

    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    def _has_this_file():
        for h in root.handlers:
            if isinstance(h, logging.FileHandler):
                try:
                    if getattr(h, "baseFilename", None) == log_path_abs:
                        return True
                except Exception:
                    pass
        return False

    if not _has_this_file():
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)

    if os.environ.get("SHELLY_CONSOLE_LOG", "0") == "1":
        root.addHandler(logging.StreamHandler(sys.stdout))

    return logging.getLogger(__name__)

logger = setup_logging()

# ============================================
# CSV file handling
# ============================================

class PlugCSVManager:
    """Write one plug CSV per SMP## into the daily folder.

    The dashboard expects the columns below:
    - Timestamp (format: %Y-%m-%d %H:%M:%S)
    - Power (W)
    - Energy (Wh)
    """

    def __init__(self, base_dir):
        self.base_dir = Path(base_dir)
        self.current_date = None
        self.files = {}   # label -> handle
        self.writers = {} # label -> csv.writer
        self._samples_since_flush = 0

    def _date_folder(self, date):
        return self.base_dir / date.strftime("%Y%m%d")

    def _filename(self, date, device):
        # Same shape as smartthings_collector: SMPxx_<device_id>_<YYYYMMDD>.csv
        return f"{device['label']}_{device['id']}_{date.strftime('%Y%m%d')}.csv"

    def ensure_open(self, date):
        if self.current_date == date and self.files:
            return

        self.close()
        folder = self._date_folder(date)
        folder.mkdir(parents=True, exist_ok=True)

        for device in DEVICES:
            label = device["label"]
            filepath = folder / self._filename(date, device)
            file_exists = filepath.exists()

            f = open(filepath, "a", newline="", encoding="utf-8")
            self.files[label] = f
            self.writers[label] = csv.writer(f)

            if not file_exists:
                self.writers[label].writerow([
                    "Timestamp",
                    "Power (W)",
                    "Energy (Wh)",
                    "Energy Inc (Wh)",
                    "Voltage (V)",
                    "Current (A)",
                    "Temp (C)",
                ])

            logger.info(f"Plug CSV ready: {filepath}")

        self.current_date = date
        self._samples_since_flush = 0

    def write_row(self, label, timestamp_sec, power_w, energy_wh, energy_inc_wh, voltage_v, current_a, temp_c):
        w = self.writers.get(label)
        if not w:
            return

        def _cell_meas(x):
            """Write an empty cell, not 0, when a reading fails: a 0 would make the
            graph zig-zag between the real load and zero."""
            if x is None:
                return ""
            return float(x)

        w.writerow([
            timestamp_sec,
            _cell_meas(power_w),
            float(energy_wh),
            float(energy_inc_wh),
            _cell_meas(voltage_v),
            _cell_meas(current_a),
            _cell_meas(temp_c),
        ])
        self._samples_since_flush += 1
        if self._samples_since_flush >= FLUSH_EVERY_SAMPLES:
            self.flush()

    def flush(self):
        for f in self.files.values():
            f.flush()
        self._samples_since_flush = 0

    def close(self):
        for f in self.files.values():
            try:
                f.close()
            except Exception:
                pass
        self.files = {}
        self.writers = {}
        self._samples_since_flush = 0

# ============================================
# Data collection
# ============================================

def _parse_gen2_switch_status(data: dict):
    """Gen2 devices such as Shelly Plus: /rpc/Switch.GetStatus response."""
    aenergy = data.get("aenergy") or {}
    aenergy_total = aenergy.get("total")
    try:
        energy_total = float(aenergy_total) if aenergy_total is not None else None
    except (TypeError, ValueError):
        energy_total = None
    t = data.get("temperature")
    if isinstance(t, dict):
        tc = t.get("tC", 0)
    else:
        try:
            tc = float(t) if t is not None else 0.0
        except (TypeError, ValueError):
            tc = 0.0
    return {
        "power": float(data.get("apower", 0) or 0),
        "voltage": float(data.get("voltage", 0) or 0),
        "current": float(data.get("current", 0) or 0),
        "temp": float(tc) if tc is not None else 0.0,
        "energy_total": energy_total,
    }


def _parse_gen1_http_status(data: dict):
    """Gen1 devices such as Shelly Plug: GET /status (meters[].power etc.)."""
    meters = data.get("meters") or []
    if not meters:
        return None
    m0 = meters[0]
    power = float(m0.get("power", 0) or 0)
    voltage = float(m0.get("voltage", 0) or 0) or float(data.get("voltage", 0) or 0)
    total = m0.get("total")
    try:
        energy_total = float(total) if total is not None else None
    except (TypeError, ValueError):
        energy_total = None
    cur = float(m0.get("current", 0) or 0)
    if cur == 0 and voltage > 1:
        cur = power / voltage
    tmp = data.get("temperature", data.get("tmp"))
    try:
        if isinstance(tmp, dict):
            tc = float(tmp.get("tC", 0) or 0)
        else:
            tc = float(tmp) if tmp is not None else 0.0
    except (TypeError, ValueError):
        tc = 0.0
    return {
        "power": power,
        "voltage": voltage,
        "current": cur,
        "temp": tc,
        "energy_total": energy_total,
    }


def get_device_data(device, retry_count=0):
    """One plug: try the Gen2 RPC first, fall back to the Gen1 /status."""
    ip = device["ip"]
    label = device["label"]
    last_err = None

    # Gen2 (Plus and similar): Switch.GetStatus
    try:
        r = requests.get(f"http://{ip}/rpc/Switch.GetStatus?id=0", timeout=TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict) and ("apower" in data or "aenergy" in data):
                return _parse_gen2_switch_status(data)
    except Exception as e:
        last_err = e

    # Gen1 (Plug and similar): /status
    try:
        r = requests.get(f"http://{ip}/status", timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        parsed = _parse_gen1_http_status(data)
        if parsed is not None:
            return parsed
        last_err = last_err or ValueError("/status: no meters field")
    except Exception as e:
        last_err = e

    if retry_count < MAX_RETRIES:
        time.sleep(0.12)
        return get_device_data(device, retry_count + 1)

    logger.error(
        f"{label} unreachable (IP={ip}). Check that this machine and the Shelly are "
        f"on the same LAN and that the address is correct. "
        f"Error: {last_err}"
    )
    return None

def collect_all_devices():
    """Collect from every configured device in one pass."""
    results = {}
    
    for device in DEVICES:
        data = get_device_data(device)
        results[device['label']] = data
    
    return results

# ============================================
# Main collection loop
# ============================================

class DataCollector:
    """Main data collection class."""
    
    def __init__(self):
        self.plug_csv_manager = PlugCSVManager(CSV_BASE_DIR)
        self.is_running = True
        self.total_samples = 0
        self.error_count = 0
        self.start_time = None
        self.last_ui_update_time = 0.0
        self.current_date = datetime.now().date()

        # Per-day cumulative energy (Wh)
        self.daily_energy_wh = {device["label"]: 0.0 for device in DEVICES}
        # Per-loop incremental energy (Wh); useful for NILM and windowed learning
        self.daily_energy_inc_wh = {device["label"]: 0.0 for device in DEVICES}
        # Last value of the device's cumulative energy (aenergy.total, Wh)
        self.last_energy_total = {device["label"]: None for device in DEVICES}
        self.last_sample_epoch = time.time()
        
        # Signal handlers can only be registered on the main thread
        # (skipped when main.py runs Shelly on a worker thread; shutdown then
        #  goes through _stop_callback / is_running)
        if threading.current_thread() is threading.main_thread():
            try:
                signal.signal(signal.SIGINT, self.signal_handler)
                if hasattr(signal, "SIGTERM"):
                    signal.signal(signal.SIGTERM, self.signal_handler)
            except ValueError:
                pass
    
    def signal_handler(self, signum, frame):
        """Handle Ctrl+C and other shutdown signals."""
        logger.info("\nShutdown signal received, stopping safely...")
        self.is_running = False
    
    def update_dashboard(self, device_data, energy_by_label, now_str):
        """Update the state the GUI table needs, on the requested UI period."""
        global dashboard_state
        ok_devices = 0
        fail_devices = 0

        devices = []
        for dev in DEVICES:
            label = dev["label"]
            data = device_data.get(label)
            if data:
                ok_devices += 1
                devices.append({
                    "label": label,
                    "power": float(data["power"]),
                    "energy": float(energy_by_label.get(label, 0.0)),
                    "status": "OK",
                    "updated": now_str,
                })
            else:
                fail_devices += 1
                devices.append({
                    "label": label,
                    "power": None,
                    "energy": float(energy_by_label.get(label, 0.0)),
                    "status": "Fail",
                    "updated": now_str,
                })

        dashboard_state.update({
            "status": "Collecting",
            "last_cycle": now_str,
            "total": len(DEVICES),
            "success": ok_devices,
            "fail": fail_devices,
            "devices": devices,
        })

        cb = globals().get("on_data_updated")
        if cb:
            try:
                cb()
            except Exception:
                pass
    
    def run(self):
        """Main entry point."""
        logger.info("=" * 60)
        logger.info("Shelly plug data collection started")
        logger.info(f"interval: {INTERVAL}s")
        logger.info(f"CSV_BASE_DIR: {CSV_BASE_DIR}")
        logger.info("=" * 60)
        
        # Open the CSV files
        self.plug_csv_manager.ensure_open(self.current_date)
        self.start_time = time.time()
        self.daily_energy_wh = {device["label"]: 0.0 for device in DEVICES}
        self.daily_energy_inc_wh = {device["label"]: 0.0 for device in DEVICES}
        self.last_sample_epoch = time.time()
        
        try:
            while self.is_running:
                loop_start = time.time()
                
                # Data collection
                device_data = collect_all_devices()
                
                # Timestamp
                now = datetime.now()
                timestamp_sec = now.strftime('%Y-%m-%d %H:%M:%S')

                # On a date change, reset the energy accumulators and ensure the files exist
                if now.date() != self.current_date:
                    self.current_date = now.date()
                    self.daily_energy_wh = {device["label"]: 0.0 for device in DEVICES}
                    self.daily_energy_inc_wh = {device["label"]: 0.0 for device in DEVICES}
                    self.last_energy_total = {device["label"]: None for device in DEVICES}
                    self.plug_csv_manager.ensure_open(self.current_date)
                    self.last_sample_epoch = time.time()

                # Accumulate energy (Wh) from the real loop interval (dt)
                now_epoch = time.time()
                dt = now_epoch - self.last_sample_epoch
                if dt <= 0:
                    dt = INTERVAL
                self.last_sample_epoch = now_epoch

                # Energy (Wh) accumulates the increment of the Shelly aenergy.total counter
                # (the counter is cumulative, so the difference from the previous
                #  value gives the consumption over this loop)
                for dev in DEVICES:
                    label = dev["label"]
                    data = device_data.get(label)
                    if data and data.get("energy_total") is not None:
                        energy_total = float(data["energy_total"])
                        prev_total = self.last_energy_total.get(label)
                        if prev_total is None:
                            # First sample: only record the baseline
                            self.last_energy_total[label] = energy_total
                            self.daily_energy_inc_wh[label] = 0.0
                        else:
                            delta = energy_total - prev_total
                            if delta >= 0:
                                self.daily_energy_inc_wh[label] = delta
                                self.daily_energy_wh[label] = self.daily_energy_wh.get(label, 0.0) + delta
                            else:
                                # A negative difference means a device reset or a counter
                                # rollover, so only move the baseline
                                self.daily_energy_inc_wh[label] = 0.0
                            self.last_energy_total[label] = energy_total
                    else:
                        # Fall back to integrating power only when the energy field is missing
                        power_w = float(data["power"]) if data else 0.0
                        energy_inc_wh = power_w * dt / 3600.0
                        self.daily_energy_inc_wh[label] = energy_inc_wh
                        self.daily_energy_wh[label] = self.daily_energy_wh.get(label, 0.0) + energy_inc_wh
                
                # Refresh the SMP dashboard right after the first loop so the plug rows
                # appear without waiting 5 s
                if self.total_samples == 0:
                    self.last_ui_update_time = time.time()
                    self.update_dashboard(device_data, self.daily_energy_wh, timestamp_sec)

                # Write the plug CSV used by the dashboard graph
                for dev in DEVICES:
                    label = dev["label"]
                    data = device_data.get(label)
                    if data:
                        power_w = data["power"]
                        voltage_v = data["voltage"]
                        current_a = data["current"]
                        temp_c = data["temp"]
                    else:
                        power_w = None
                        voltage_v = None
                        current_a = None
                        temp_c = None

                    self.plug_csv_manager.write_row(
                        label=label,
                        timestamp_sec=timestamp_sec,
                        power_w=power_w,
                        energy_wh=self.daily_energy_wh.get(label, 0.0),
                        energy_inc_wh=self.daily_energy_inc_wh.get(label, 0.0),
                        voltage_v=voltage_v,
                        current_a=current_a,
                        temp_c=temp_c,
                    )
                self.plug_csv_manager.flush()
                
                # Statistics
                self.total_samples += 1
                
                # Error count
                error_devices = [label for label, data in device_data.items() if data is None]
                if error_devices:
                    self.error_count += len(error_devices)
                    logger.warning(f"collection failed: {', '.join(error_devices)}")

                # Update the GUI state, rate-limited
                now_ts = time.time()
                if now_ts - self.last_ui_update_time >= UI_UPDATE_EVERY_SECONDS:
                    self.last_ui_update_time = now_ts
                    self.update_dashboard(device_data, self.daily_energy_wh, timestamp_sec)
                
                # Keep the interval accurate
                elapsed = time.time() - loop_start
                sleep_time = max(0, INTERVAL - elapsed)
                
                if sleep_time == 0:
                    logger.warning(f"collection overrun: {elapsed:.3f}s (target: {INTERVAL}s)")
                
                time.sleep(sleep_time)
        
        except Exception as e:
            logger.error(f"unexpected error: {e}", exc_info=True)
        
        finally:
            self.cleanup()
    
    def cleanup(self):
        """Shutdown handling."""
        try:
            self.plug_csv_manager.flush()
        except Exception:
            pass
        self.plug_csv_manager.close()
        
        elapsed = time.time() - self.start_time
        logger.info("=" * 60)
        logger.info("collection finished")
        logger.info(f"total samples: {self.total_samples}")
        logger.info(f"total time: {elapsed/3600:.2f} h")
        logger.info(f"error count: {self.error_count}")
        logger.info("=" * 60)

# ============================================
# Entry point
# ============================================

if __name__ == "__main__":
    collector = DataCollector()
    collector.run()
