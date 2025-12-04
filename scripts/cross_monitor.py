#!/usr/bin/env python3
"""
Cross-monitoring and auto-remediation for AbstractFinance.
Each server monitors the other and automatically restarts failed services.
"""

import os
import sys
import time
import json
import subprocess
import urllib.request
import urllib.error
from datetime import datetime
from typing import Dict, Any


# Configuration from environment
TARGET_URL = os.environ.get('MONITOR_TARGET_URL')  # e.g., http://94.130.228.55:8080/health
TARGET_HOST = os.environ.get('MONITOR_TARGET_HOST')  # e.g., 94.130.228.55
TARGET_NAME = os.environ.get('MONITOR_TARGET_NAME', 'remote-server')
CHECK_INTERVAL = int(os.environ.get('MONITOR_INTERVAL', '60'))  # seconds
FAILURES_BEFORE_RESTART = int(os.environ.get('FAILURES_BEFORE_RESTART', '3'))
RESTART_COOLDOWN = int(os.environ.get('RESTART_COOLDOWN', '300'))  # 5 min between restarts
SSH_KEY_PATH = os.environ.get('SSH_KEY_PATH', '/root/.ssh/id_ed25519')


def check_health(url: str, timeout: int = 10) -> Dict[str, Any]:
    """Check health endpoint."""
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


def reboot_remote_server(host: str) -> bool:
    """
    SSH into remote server and reboot it.
    Returns True if reboot command was sent successfully.
    """
    print(f"[REBOOT] Attempting to reboot server {host}...")

    # Build SSH command - use 'reboot' to restart the entire server
    ssh_cmd = [
        "ssh",
        "-i", SSH_KEY_PATH,
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=30",
        f"root@{host}",
        "reboot"
    ]

    try:
        result = subprocess.run(
            ssh_cmd,
            capture_output=True,
            text=True,
            timeout=120  # 2 minute timeout for restart
        )

        # Reboot command may return non-zero or close connection - that's expected
        print(f"[REBOOT] Reboot command sent to {host}")
        return True

    except subprocess.TimeoutExpired:
        # Timeout is expected - server is rebooting
        print(f"[REBOOT] Server {host} is rebooting (connection closed)")
        return True
    except Exception as e:
        # Connection reset is expected during reboot
        if "Connection reset" in str(e) or "closed by remote" in str(e):
            print(f"[REBOOT] Server {host} is rebooting")
            return True
        print(f"[REBOOT] Exception while rebooting: {e}")
        return False


def run_monitor():
    """Main monitoring loop with auto-remediation."""
    if not TARGET_URL:
        print("ERROR: MONITOR_TARGET_URL environment variable not set")
        sys.exit(1)

    if not TARGET_HOST:
        print("ERROR: MONITOR_TARGET_HOST environment variable not set")
        sys.exit(1)

    print(f"Cross-monitor with auto-remediation started")
    print(f"  Target: {TARGET_NAME}")
    print(f"  Health URL: {TARGET_URL}")
    print(f"  SSH Host: {TARGET_HOST}")
    print(f"  Check interval: {CHECK_INTERVAL}s")
    print(f"  Failures before restart: {FAILURES_BEFORE_RESTART}")
    print(f"  Restart cooldown: {RESTART_COOLDOWN}s")
    print()

    consecutive_failures = 0
    last_restart_time = 0
    last_status = None

    while True:
        try:
            result = check_health(TARGET_URL)
            now = time.time()

            if result["healthy"]:
                status_str = f"OK ({result['latency_ms']}ms)"
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                status_str = f"FAIL #{consecutive_failures} ({result.get('error', 'Unknown')})"

                # Check if we should attempt a restart
                time_since_last_restart = now - last_restart_time
                can_restart = time_since_last_restart >= RESTART_COOLDOWN

                if consecutive_failures >= FAILURES_BEFORE_RESTART:
                    if can_restart:
                        print(f"[{result['timestamp']}] {consecutive_failures} consecutive failures - triggering SERVER REBOOT")

                        if reboot_remote_server(TARGET_HOST):
                            last_restart_time = now
                            consecutive_failures = 0  # Reset after reboot
                            # Wait for server to come back up (reboot takes longer)
                            print(f"[REBOOT] Waiting 180s for server to reboot and services to start...")
                            time.sleep(180)
                        else:
                            print(f"[REBOOT] Reboot failed, will retry after cooldown")
                            last_restart_time = now  # Still apply cooldown
                    else:
                        remaining = int(RESTART_COOLDOWN - time_since_last_restart)
                        print(f"[{result['timestamp']}] {status_str} (restart cooldown: {remaining}s remaining)")
                        continue

            # Log status
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
