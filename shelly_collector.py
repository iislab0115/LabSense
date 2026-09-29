#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Shelly Plug 12개 데이터 수집 프로그램
- 1초 간격 수집
- 일별 파일 자동 생성
- 에러 복구 기능
- 중단 후 재시작 가능
"""

import requests
import time
import csv
from datetime import datetime, timedelta
import os
import logging
from pathlib import Path
import signal
import sys
import threading

# ============================================
# 설정
# ============================================

# 스마트플러그 정보
DEVICES = [
    {"label": "SMP01", "id": "9070694ae97c", "ip": "192.168.0.172"},
    {"label": "SMP02", "id": "907069496a08", "ip": "192.168.0.144"},
    {"label": "SMP03", "id": "9070694a7d50", "ip": "192.168.0.180"},
    {"label": "SMP04", "id": "907069416dc0", "ip": "192.168.0.196"},
    {"label": "SMP05", "id": "90706949659c", "ip": "192.168.0.112"},
    {"label": "SMP06", "id": "9070694969fc", "ip": "192.168.0.132"},
    {"label": "SMP07", "id": "90706942f8f8", "ip": "192.168.0.136"},
    {"label": "SMP08", "id": "9070694a7c94", "ip": "192.168.0.193"},
    {"label": "SMP09", "id": "907069496a9c", "ip": "192.168.0.192"},
    {"label": "SMP10", "id": "9070694361dc", "ip": "192.168.0.152"},
    {"label": "SMP11", "id": "907069495ed4", "ip": "192.168.0.176"},
    {"label": "SMP12", "id": "9070694b1cfc", "ip": "192.168.0.120"},
]

# 수집 설정
INTERVAL = 1.0  # 1초 간격
TIMEOUT = 4.0   # HTTP 요청 타임아웃 (Wi-Fi 지연 대비)
MAX_RETRIES = 3  # 재시도 횟수

# GUI(또는 프로그램 출력)용 주기
UI_UPDATE_EVERY_SECONDS = 5.0
# 파일 flush 주기(너무 잦으면 I/O 부담이 커짐)
FLUSH_EVERY_SAMPLES = 10

CSV_BASE_DIR = Path(r"D:\smartthings_data\csv_data")
CSV_BASE_DIR.mkdir(parents=True, exist_ok=True)

# logs 저장 경로
LOG_DIR = CSV_BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# dashboard 상태(launcher에서 읽어 UI 갱신)
dashboard_state = {
    "status": "Initializing",
    "last_cycle": None,
    "total": len(DEVICES),
    "success": 0,
    "fail": 0,
    "devices": [],  # [{label, power, energy, status, updated}, ...]
}

# launcher(또는 dashboard)가 주입하는 콜백
on_data_updated = None

# ============================================
# 로깅 설정
# ============================================

def setup_logging():
    """로깅 설정 (통합 실행 시 SmartThings와 root 핸들러 공유, 중복 방지)"""
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
# CSV 파일 관리
# ============================================

class PlugCSVManager:
    """SMP##별 plug CSV를 일별 폴더에 저장.

    dashboard는 아래 컬럼을 기대합니다:
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
        # smartthings_collector 스타일과 비슷하게: SMPxx_<device_id>_<YYYYMMDD>.csv
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
            """측정 실패 시 0을 쓰면 그래프가 실제 부하↔0 으로 지그재그로 보인다 → 빈 칸."""
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
# 데이터 수집
# ============================================

def _parse_gen2_switch_status(data: dict):
    """Shelly Plus 등 Gen2: /rpc/Switch.GetStatus 응답."""
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
    """Shelly Plug 등 Gen1: GET /status (meters[].power 등)."""
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
    """개별 플러그: Gen2 RPC 우선, 실패 시 Gen1 /status."""
    ip = device["ip"]
    label = device["label"]
    last_err = None

    # Gen2 (Plus 등): Switch.GetStatus
    try:
        r = requests.get(f"http://{ip}/rpc/Switch.GetStatus?id=0", timeout=TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict) and ("apower" in data or "aenergy" in data):
                return _parse_gen2_switch_status(data)
    except Exception as e:
        last_err = e

    # Gen1 (Plug 등): /status
    try:
        r = requests.get(f"http://{ip}/status", timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        parsed = _parse_gen1_http_status(data)
        if parsed is not None:
            return parsed
        last_err = last_err or ValueError("/status: meters 없음")
    except Exception as e:
        last_err = e

    if retry_count < MAX_RETRIES:
        time.sleep(0.12)
        return get_device_data(device, retry_count + 1)

    logger.error(
        f"{label} 연결 실패 (IP={ip}). PC와 Shelly가 같은 LAN인지, IP가 맞는지 확인. "
        f"오류: {last_err}"
    )
    return None

def collect_all_devices():
    """12개 디바이스 데이터 한 번에 수집"""
    results = {}
    
    for device in DEVICES:
        data = get_device_data(device)
        results[device['label']] = data
    
    return results

# ============================================
# 메인 수집 루프
# ============================================

class DataCollector:
    """데이터 수집 메인 클래스"""
    
    def __init__(self):
        self.plug_csv_manager = PlugCSVManager(CSV_BASE_DIR)
        self.is_running = True
        self.total_samples = 0
        self.error_count = 0
        self.start_time = None
        self.last_ui_update_time = 0.0
        self.current_date = datetime.now().date()

        # 일 단위 에너지 누적(Wh)
        self.daily_energy_wh = {device["label"]: 0.0 for device in DEVICES}
        # 루프 구간 증분 에너지(Wh) - NILM/윈도우 학습에 유용
        self.daily_energy_inc_wh = {device["label"]: 0.0 for device in DEVICES}
        # device의 누적 에너지(aenergy.total, Wh) 기준 마지막 값
        self.last_energy_total = {device["label"]: None for device in DEVICES}
        self.last_sample_epoch = time.time()
        
        # 시그널 핸들러는 메인 스레드에서만 등록 가능
        # (main.py 등에서 Shelly를 워커 스레드로 돌릴 때는 생략 → 종료는 _stop_callback / is_running)
        if threading.current_thread() is threading.main_thread():
            try:
                signal.signal(signal.SIGINT, self.signal_handler)
                if hasattr(signal, "SIGTERM"):
                    signal.signal(signal.SIGTERM, self.signal_handler)
            except ValueError:
                pass
    
    def signal_handler(self, signum, frame):
        """Ctrl+C 등 종료 시그널 처리"""
        logger.info("\n종료 신호 수신. 안전하게 종료 중...")
        self.is_running = False
    
    def update_dashboard(self, device_data, energy_by_label, now_str):
        """GUI 테이블에 필요한 상태를 업데이트(요청 UI 주기에 맞춰 호출)."""
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
        """메인 실행"""
        logger.info("=" * 60)
        logger.info("12개 Shelly Plug data collection started")
        logger.info(f"interval: {INTERVAL}s")
        logger.info(f"CSV_BASE_DIR: {CSV_BASE_DIR}")
        logger.info("=" * 60)
        
        # CSV 파일 열기
        self.plug_csv_manager.ensure_open(self.current_date)
        self.start_time = time.time()
        self.daily_energy_wh = {device["label"]: 0.0 for device in DEVICES}
        self.daily_energy_inc_wh = {device["label"]: 0.0 for device in DEVICES}
        self.last_sample_epoch = time.time()
        
        try:
            while self.is_running:
                loop_start = time.time()
                
                # 데이터 수집
                device_data = collect_all_devices()
                
                # 타임스탬프
                now = datetime.now()
                timestamp_sec = now.strftime('%Y-%m-%d %H:%M:%S')

                # 날짜 바뀌면 에너지 누적 리셋 + 파일 보장
                if now.date() != self.current_date:
                    self.current_date = now.date()
                    self.daily_energy_wh = {device["label"]: 0.0 for device in DEVICES}
                    self.daily_energy_inc_wh = {device["label"]: 0.0 for device in DEVICES}
                    self.last_energy_total = {device["label"]: None for device in DEVICES}
                    self.plug_csv_manager.ensure_open(self.current_date)
                    self.last_sample_epoch = time.time()

                # 실제 루프 간격(dt) 기반 에너지 누적 (Wh)
                now_epoch = time.time()
                dt = now_epoch - self.last_sample_epoch
                if dt <= 0:
                    dt = INTERVAL
                self.last_sample_epoch = now_epoch

                # 에너지(Wh)는 Shelly의 aenergy.total로 "증분"을 누적
                # (에너지 카운터는 누적값이라, 이전 값과 차이를 취해 해당 루프 기간 소비량으로 변환)
                for dev in DEVICES:
                    label = dev["label"]
                    data = device_data.get(label)
                    if data and data.get("energy_total") is not None:
                        energy_total = float(data["energy_total"])
                        prev_total = self.last_energy_total.get(label)
                        if prev_total is None:
                            # 첫 샘플: 기준점만 잡기
                            self.last_energy_total[label] = energy_total
                            self.daily_energy_inc_wh[label] = 0.0
                        else:
                            delta = energy_total - prev_total
                            if delta >= 0:
                                self.daily_energy_inc_wh[label] = delta
                                self.daily_energy_wh[label] = self.daily_energy_wh.get(label, 0.0) + delta
                            else:
                                # 장치 리셋/누적 에너지 롤오버 등으로 음수가 나오면 누적 기준만 갱신
                                self.daily_energy_inc_wh[label] = 0.0
                            self.last_energy_total[label] = energy_total
                    else:
                        # 에너지 필드가 누락된 경우에만 전력 적분으로 fallback
                        power_w = float(data["power"]) if data else 0.0
                        energy_inc_wh = power_w * dt / 3600.0
                        self.daily_energy_inc_wh[label] = energy_inc_wh
                        self.daily_energy_wh[label] = self.daily_energy_wh.get(label, 0.0) + energy_inc_wh
                
                # 첫 루프 직후 즉시 SMP 대시보드 갱신(5초 대기 없이 플러그 행이 보이도록)
                if self.total_samples == 0:
                    self.last_ui_update_time = time.time()
                    self.update_dashboard(device_data, self.daily_energy_wh, timestamp_sec)

                # Plug CSV(대시보드 그래프용) 쓰기
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
                
                # 통계
                self.total_samples += 1
                
                # 에러 카운트
                error_devices = [label for label, data in device_data.items() if data is None]
                if error_devices:
                    self.error_count += len(error_devices)
                    logger.warning(f"데이터 수집 실패: {', '.join(error_devices)}")

                # GUI 상태 업데이트(주기 제한)
                now_ts = time.time()
                if now_ts - self.last_ui_update_time >= UI_UPDATE_EVERY_SECONDS:
                    self.last_ui_update_time = now_ts
                    self.update_dashboard(device_data, self.daily_energy_wh, timestamp_sec)
                
                # 정확한 주기 유지
                elapsed = time.time() - loop_start
                sleep_time = max(0, INTERVAL - elapsed)
                
                if sleep_time == 0:
                    logger.warning(f"collection overrun: {elapsed:.3f}s (target: {INTERVAL}s)")
                
                time.sleep(sleep_time)
        
        except Exception as e:
            logger.error(f"예상치 못한 오류: {e}", exc_info=True)
        
        finally:
            self.cleanup()
    
    def cleanup(self):
        """종료 처리"""
        try:
            self.plug_csv_manager.flush()
        except Exception:
            pass
        self.plug_csv_manager.close()
        
        elapsed = time.time() - self.start_time
        logger.info("=" * 60)
        logger.info("수집 종료")
        logger.info(f"총 샘플: {self.total_samples}개")
        logger.info(f"총 시간: {elapsed/3600:.2f}시간")
        logger.info(f"오류 횟수: {self.error_count}회")
        logger.info("=" * 60)

# ============================================
# 실행
# ============================================

if __name__ == "__main__":
    collector = DataCollector()
    collector.run()
