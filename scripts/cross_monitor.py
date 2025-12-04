#!/usr/bin/env python3
"""
Cross-monitoring and auto-remediation for AbstractFinance.
Each server monitors the other and automatically fixes failed services.

Escalation strategy:
1. First failure threshold: Restart Docker containers
2. If still failing after container restart: Reboot entire server
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
FAILURES_BEFORE_CONTAINER_RESTART = int(os.environ.get('FAILURES_BEFORE_RESTART', '3'))
FAILURES_BEFORE_SERVER_REBOOT = int(os.environ.get('FAILURES_BEFORE_REBOOT', '6'))
ACTION_COOLDOWN = int(os.environ.get('ACTION_COOLDOWN', '300'))  # 5 min between actions
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


def run_ssh_command(host: str, command: str, timeout: int = 120) -> tuple[bool, str]:
    """Run a command on remote host via SSH."""
    ssh_cmd = [
        "ssh",
        "-i", SSH_KEY_PATH,
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=30",
        f"root@{host}",
        command
    ]

    try:
        result = subprocess.run(
            ssh_cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        return result.returncode == 0, result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return False, "Command timed out"
    except Exception as e:
        return False, str(e)


def restart_containers(host: str) -> bool:
    """Restart Docker containers on remote host."""
    print(f"[RESTART] Restarting containers on {host}...")

    success, output = run_ssh_command(
        host,
        "cd /srv/abstractfinance && docker compose restart trading-engine ibgateway"
    )

    if success:
        print(f"[RESTART] Containers restarted successfully on {host}")
    else:
        print(f"[RESTART] Failed to restart containers: {output}")

    return success


def reboot_server(host: str) -> bool:
    """Reboot the entire remote server."""
    print(f"[REBOOT] Rebooting server {host}...")

    # Send reboot command - connection will be closed
    ssh_cmd = [
        "ssh",
        "-i", SSH_KEY_PATH,
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=30",
        f"root@{host}",
        "reboot"
    ]

    try:
        subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=30)
        print(f"[REBOOT] Reboot command sent to {host}")
        return True
    except:
        # Connection reset/timeout is expected during reboot
        print(f"[REBOOT] Server {host} is rebooting")
        return True


def run_monitor():
    """Main monitoring loop with graduated auto-remediation."""
    if not TARGET_URL:
        print("ERROR: MONITOR_TARGET_URL environment variable not set")
        sys.exit(1)

    if not TARGET_HOST:
        print("ERROR: MONITOR_TARGET_HOST environment variable not set")
        sys.exit(1)

    print(f"Cross-monitor with graduated auto-remediation started")
    print(f"  Target: {TARGET_NAME}")
    print(f"  Health URL: {TARGET_URL}")
    print(f"  SSH Host: {TARGET_HOST}")
    print(f"  Check interval: {CHECK_INTERVAL}s")
    print(f"  Failures before container restart: {FAILURES_BEFORE_CONTAINER_RESTART}")
    print(f"  Failures before server reboot: {FAILURES_BEFORE_SERVER_REBOOT}")
    print(f"  Action cooldown: {ACTION_COOLDOWN}s")
    print()

    consecutive_failures = 0
    last_action_time = 0
    container_restart_attempted = False
    last_status = None

    while True:
        try:
            result = check_health(TARGET_URL)
            now = time.time()

            if result["healthy"]:
                status_str = f"OK ({result['latency_ms']}ms)"
                consecutive_failures = 0
                container_restart_attempted = False  # Reset escalation
            else:
                consecutive_failures += 1
                status_str = f"FAIL #{consecutive_failures} ({result.get('error', 'Unknown')})"

                # Check cooldown
                time_since_last_action = now - last_action_time
                can_act = time_since_last_action >= ACTION_COOLDOWN

                # Escalation Level 2: Server reboot (if container restart didn't help)
                if consecutive_failures >= FAILURES_BEFORE_SERVER_REBOOT and container_restart_attempted:
                    if can_act:
                        print(f"[{result['timestamp']}] {consecutive_failures} failures after container restart - REBOOTING SERVER")
                        if reboot_server(TARGET_HOST):
                            last_action_time = now
                            consecutive_failures = 0
                            container_restart_attempted = False
                            print(f"[REBOOT] Waiting 180s for server to come back up...")
                            time.sleep(180)
                        else:
                            last_action_time = now
                    else:
                        remaining = int(ACTION_COOLDOWN - time_since_last_action)
                        print(f"[{result['timestamp']}] {status_str} (cooldown: {remaining}s)")
                        time.sleep(CHECK_INTERVAL)
                        continue

                # Escalation Level 1: Container restart
                elif consecutive_failures >= FAILURES_BEFORE_CONTAINER_RESTART and not container_restart_attempted:
                    if can_act:
                        print(f"[{result['timestamp']}] {consecutive_failures} failures - RESTARTING CONTAINERS")
                        if restart_containers(TARGET_HOST):
                            last_action_time = now
                            consecutive_failures = 0
                            container_restart_attempted = True  # Mark that we tried this
                            print(f"[RESTART] Waiting 90s for containers to start...")
                            time.sleep(90)
                        else:
                            # Container restart failed, escalate to reboot next time
                            container_restart_attempted = True
                            last_action_time = now
                    else:
                        remaining = int(ACTION_COOLDOWN - time_since_last_action)
                        print(f"[{result['timestamp']}] {status_str} (cooldown: {remaining}s)")
                        time.sleep(CHECK_INTERVAL)
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
