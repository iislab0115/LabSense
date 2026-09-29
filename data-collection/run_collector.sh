#!/bin/bash
# run_collector.sh
# Restart the collector indefinitely, recovering automatically from a crash

echo "Starting unified collection (Shelly + SmartThings, headless)"
echo "Press Ctrl+C to stop"
echo "======================================"

while true; do
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] collector started"
    
    # Run the Python program
    python3 main.py --headless
    
    exit_code=$?
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] program exited (code: $exit_code)"
    
    # Do not restart when the user stopped it with Ctrl+C (exit code 130)
    if [ $exit_code -eq 130 ]; then
        echo "Stopped by the user."
        break
    fi
    
    # Otherwise restart after 5 seconds
    echo "Restarting in 5 seconds..."
    sleep 5
done

echo "Collection finished"
