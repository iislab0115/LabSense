#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py --headless 를 감싸 크래시·비정상 종료 시 자동으로 다시 띄움.

장시간(수 주~한 달) 수집 시 GUI 대신 이 스크립트 사용을 권장합니다.
로그: config.json 의 LOG_FILE 과 같은 디렉터리에 launcher_persistent.log

환경 변수:
  PERSISTENT_RESTART_SEC — 재시작 대기 초 (기본 15)
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
        "영구 런처 시작 — %s %s --headless (재시작 대기 %ss, 로그 %s)",
        py,
        main_py,
        restart_sec,
        log_path,
    )

    while True:
        log.info("수집 프로세스 시작")
        try:
            proc = subprocess.run(
                [str(py), str(main_py), "--headless"],
                cwd=str(ROOT),
            )
            retcode = proc.returncode
        except KeyboardInterrupt:
            log.info("런처 Ctrl+C — 종료합니다.")
            break
        if retcode == 0:
            log.info("수집기 정상 종료(exit=0) — 런처를 종료합니다. 다시 켜려면 이 스크립트를 재실행하세요.")
            break
        log.warning(
            "수집 프로세스 비정상 종료 (exit=%s). %ss 후 재시작합니다.",
            retcode,
            restart_sec,
        )
        try:
            time.sleep(restart_sec)
        except KeyboardInterrupt:
            log.info("런처 Ctrl+C — 종료합니다.")
            break


if __name__ == "__main__":
    main()
