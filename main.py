#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
통합 수집 (단일 진입점)

- Shelly: 스마트 플러그(SMP) → CSV (shelly_collector)
- SmartThings: MS / DS / Stick Vacuum / Camera → CSV (smartthings_worker)
- 대시보드: dashboard.py (tkinter)

사전 준비:
1. plug-collection/config.json (CSV 경로, SmartThings OAuth 등)
2. python smartthings_auth.py 로 토큰 1회 발급

실행:
  python main.py
  python main.py --headless      # 콘솔만
  python run_persistent.py       # headless + 비정상 종료 시 자동 재시작(장기 수집 권장)
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
            "SmartThings 토큰이 없습니다. 이 폴더에서 실행: python smartthings_auth.py"
        )
        return
    st.load_metadata()
    st.load_ban_list()
    try:
        asyncio.run(st.scheduler())
    except Exception as e:
        logging.error(f"SmartThings scheduler 종료: {e}")


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

        print("통합 수집기(headless) 실행 중… 종료: Ctrl+C")
        try:
            while not shutdown.wait(timeout=1.0):
                pass
            print("\n종료 중…")
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
