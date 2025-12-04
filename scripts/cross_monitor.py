#!/usr/bin/env python3
"""
Cross-monitoring script for AbstractFinance.
Each server monitors the other and sends alerts via Telegram.
"""

import os
import sys
import time
import json
import urllib.request
import urllib.error
from datetime import datetime
from typing import Optional, Dict, Any


# Configuration from environment
TARGET_URL = os.environ.get('MONITOR_TARGET_URL')  # e.g., http://94.130.228.55:8080/health
TARGET_NAME = os.environ.get('MONITOR_TARGET_NAME', 'remote-server')
CHECK_INTERVAL = int(os.environ.get('MONITOR_INTERVAL', '60'))  # seconds
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
CONSECUTIVE_FAILURES_THRESHOLD = 3  # Alert after N consecutive failures


def send_telegram_alert(message: str) -> bool:
    """Send alert via Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[ALERT] {message}")
        return False

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = json.dumps({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }).encode('utf-8')

        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"}
        )

        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status == 200
    except Exception as e:
        print(f"Failed to send Telegram alert: {e}")
        return False


def check_health(url: str, timeout: int = 10) -> Dict[str, Any]:
    """
    Check health endpoint.

    Returns:
        Dict with 'healthy', 'status_code', 'response', 'error', 'latency_ms'
    """
    result = {
        "healthy": False,
        "status_code": None,
        "response": None,
        "error": None,
        "latency_ms": None,
        "timestamp": datetime.utcnow().isoformat()
    }

    start = time.time()

    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            result["latency_ms"] = int((time.time() - start) * 1000)
            result["status_code"] = response.status
            body = response.read().decode('utf-8')
            result["response"] = json.loads(body)
            result["healthy"] = response.status == 200
    except urllib.error.HTTPError as e:
        result["latency_ms"] = int((time.time() - start) * 1000)
        result["status_code"] = e.code
        result["error"] = f"HTTP {e.code}: {e.reason}"
    except urllib.error.URLError as e:
        result["latency_ms"] = int((time.time() - start) * 1000)
        result["error"] = f"Connection failed: {e.reason}"
    except Exception as e:
        result["latency_ms"] = int((time.time() - start) * 1000)
        result["error"] = str(e)

    return result


def run_monitor():
    """Main monitoring loop."""
    if not TARGET_URL:
        print("ERROR: MONITOR_TARGET_URL environment variable not set")
        sys.exit(1)

    print(f"Starting cross-monitor")
    print(f"  Target: {TARGET_NAME} ({TARGET_URL})")
    print(f"  Check interval: {CHECK_INTERVAL}s")
    print(f"  Telegram alerts: {'enabled' if TELEGRAM_BOT_TOKEN else 'disabled'}")
    print()

    consecutive_failures = 0
    last_status = None
    alert_sent = False

    while True:
        try:
            result = check_health(TARGET_URL)

            if result["healthy"]:
                status_str = f"OK ({result['latency_ms']}ms)"

                # If we were in failure state, send recovery alert
                if alert_sent:
                    msg = (
                        f"<b>RECOVERED</b> {TARGET_NAME}\n"
                        f"Service is back online\n"
                        f"Latency: {result['latency_ms']}ms\n"
                        f"Time: {result['timestamp']}"
                    )
                    send_telegram_alert(msg)
                    alert_sent = False

                consecutive_failures = 0
            else:
                consecutive_failures += 1
                status_str = f"FAIL ({result.get('error', 'Unknown error')})"

                # Send alert after threshold consecutive failures
                if consecutive_failures >= CONSECUTIVE_FAILURES_THRESHOLD and not alert_sent:
                    msg = (
                        f"<b>ALERT</b> {TARGET_NAME} is DOWN!\n"
                        f"Error: {result.get('error', 'No response')}\n"
                        f"Consecutive failures: {consecutive_failures}\n"
                        f"Time: {result['timestamp']}"
                    )
                    send_telegram_alert(msg)
                    alert_sent = True

            # Log status change or periodic status
            if result["healthy"] != last_status:
                print(f"[{result['timestamp']}] Status changed: {status_str}")
            else:
                print(f"[{result['timestamp']}] {status_str}")

            last_status = result["healthy"]

        except Exception as e:
            print(f"Monitor error: {e}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    run_monitor()
