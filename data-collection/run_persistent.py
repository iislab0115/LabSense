#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Wraps main.py --headless and relaunches it automatically after a crash or an
abnormal exit.

For long runs (weeks to a month) prefer this script over the GUI.
Log: launcher_persistent.log, in the same directory as LOG_FILE in config.json

Environment variables:
  PERSISTENT_RESTART_SEC - seconds to wait before restarting (default 15)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
_LOCAL_CONFIG = ROOT / "config.local.json"
CONFIG = _LOCAL_CONFIG if _LOCAL_CONFIG.exists() else ROOT / "config.json"


def _launcher_log_path() -> Path:
    if CONFIG.exists():
        try:
            data = json.loads(CONFIG.read_text(encoding="utf-8"))
            lf = data.get("LOG_FILE")
            if lf:
                p = Path(lf).expanduser()
                p.parent.mkdir(parents=True, exist_ok=True)
                return p.parent / "launcher_persistent.log"
        except Exception:
            pass
    fallback = ROOT / "logs"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback / "launcher_persistent.log"


def main() -> None:
    log_path = _launcher_log_path()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )
    log = logging.getLogger("persistent_launcher")

    try:
        restart_sec = max(5, int(os.environ.get("PERSISTENT_RESTART_SEC", "15")))
    except ValueError:
        restart_sec = 15

    main_py = ROOT / "main.py"
    py = sys.executable
    log.info(
        "Persistent launcher started: %s %s --headless (restart wait %ss, log %s)",
        py,
        main_py,
        restart_sec,
        log_path,
    )

    while True:
        log.info("Collector process started")
        try:
            proc = subprocess.run(
                [str(py), str(main_py), "--headless"],
                cwd=str(ROOT),
            )
            retcode = proc.returncode
        except KeyboardInterrupt:
            log.info("Launcher received Ctrl+C, exiting.")
            break
        if retcode == 0:
            log.info("Collector exited normally (exit=0); the launcher is stopping. Re-run this script to start again.")
            break
        log.warning(
            "Collector exited abnormally (exit=%s). Restarting in %ss.",
            retcode,
            restart_sec,
        )
        try:
            time.sleep(restart_sec)
        except KeyboardInterrupt:
            log.info("Launcher received Ctrl+C, exiting.")
            break


if __name__ == "__main__":
    main()
