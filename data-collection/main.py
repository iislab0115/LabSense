#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Unified collection (single entry point)

- Shelly: smart plugs (SMP) -> CSV (shelly_collector)
- SmartThings: MS / DS / Stick Vacuum / Camera -> CSV (smartthings_worker)
- Dashboard: dashboard.py (tkinter)

Before running:
1. data-collection/config.json (CSV paths, SmartThings OAuth, etc.)
2. python smartthings_auth.py, once, to obtain a token

Usage:
  python main.py
  python main.py --headless      # console only
  python run_persistent.py       # headless, restarting automatically after an
                                 # abnormal exit (recommended for long runs)
"""

from __future__ import annotations

import os
import sys
import signal
import threading
import logging
import time

os.environ.setdefault("SMARTTHINGS_EMBEDDED", "1")

import smartthings_worker as st

import shelly_collector
from dashboard import run_dashboard


def run_shelly_worker():
    collector = shelly_collector.DataCollector()
    shelly_collector._unified_collector = collector
    collector.run()


def run_smartthings_worker():
    import asyncio

    if not st.load_token():
        logging.error(
            "No SmartThings token found. Run in this folder: python smartthings_auth.py"
        )
        return
    st.load_metadata()
    st.load_ban_list()
    try:
        asyncio.run(st.scheduler())
    except Exception as e:
        logging.error(f"SmartThings scheduler stopped: {e}")


def stop_all():
    try:
        st.request_stop()
    except Exception:
        pass
    c = getattr(shelly_collector, "_unified_collector", None)
    if c is not None:
        try:
            c.is_running = False
        except Exception:
            pass


def main():
    headless = "--headless" in sys.argv

    shelly_collector._stop_callback = stop_all

    t_st = threading.Thread(target=run_smartthings_worker, name="smartthings", daemon=True)
    t_sh = threading.Thread(target=run_shelly_worker, name="shelly", daemon=True)
    t_st.start()
    t_sh.start()

    if headless:
        shutdown = threading.Event()

        def _on_signal(_signum, _frame):
            shutdown.set()

        signal.signal(signal.SIGINT, _on_signal)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _on_signal)
        if sys.platform == "win32" and hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, _on_signal)

        print("Unified collector running (headless). Press Ctrl+C to stop.")
        try:
            while not shutdown.wait(timeout=1.0):
                pass
            print("\nShutting down...")
        finally:
            stop_all()
            t_sh.join(timeout=15)
            t_st.join(timeout=15)
        return

    try:
        run_dashboard(shelly_collector)
    finally:
        stop_all()
        t_sh.join(timeout=15)
        t_st.join(timeout=15)


if __name__ == "__main__":
    main()
