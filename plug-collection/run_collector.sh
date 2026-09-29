#!/bin/bash
# run_collector.sh
# 수집 프로그램을 무한 재시작 (크래시 시 자동 복구)

echo "통합 수집(Shelly + SmartThings, headless) 시작"
echo "종료하려면 Ctrl+C 누르세요"
echo "======================================"

while true; do
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 수집 프로그램 시작"
    
    # Python 프로그램 실행
    python3 main.py --headless
    
    exit_code=$?
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 프로그램 종료 (코드: $exit_code)"
    
    # Ctrl+C로 종료한 경우 (exit code 130)는 재시작 안 함
    if [ $exit_code -eq 130 ]; then
        echo "사용자가 종료했습니다."
        break
    fi
    
    # 그 외의 경우 5초 후 재시작
    echo "5초 후 재시작..."
    sleep 5
done

echo "수집 종료"
