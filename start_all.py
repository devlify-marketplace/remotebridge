#!/usr/bin/env python3
"""
RemoteBridge Master Launcher Module (start_all.py)

Launches all RemoteBridge system modules concurrently:
  1. Relay Server       (server/relay.py on port 6000)
  2. Admin Console       (admin/server.py on port 8443)
  3. Desktop Host        (desktop/host_p12.py)
  4. Desktop GUI         (desktop/gui.py)

Usage:
  python3 start_all.py [--headless]
"""

import sys
import os
import time
import subprocess
import signal
import argparse

ROOT = os.path.dirname(os.path.abspath(__file__))

processes = []

def cleanup(signum=None, frame=None):
    print("\n[*] Shutting down all RemoteBridge modules...")
    for proc, name in processes:
        print(f"[*] Stopping {name} (PID: {proc.pid})...")
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    print("[+] All modules stopped gracefully.")
    sys.exit(0)

def main():
    parser = argparse.ArgumentParser(description="RemoteBridge Master Launcher")
    parser.add_argument("--headless", action="store_true", help="Launch backend modules only (no desktop GUI)")
    parser.add_argument("--relay-port", type=int, default=6000, help="Relay server port (default: 6000)")
    parser.add_argument("--admin-port", type=int, default=8443, help="Admin console port (default: 8443)")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    print("==========================================================")
    print("           RemoteBridge System Master Launcher            ")
    print("==========================================================")

    # 1. Start Relay Server
    print(f"[*] Starting Relay Server on port {args.relay_port}...")
    relay_cmd = [sys.executable, os.path.join(ROOT, "server", "relay.py"), "--port", str(args.relay_port), "--stats-open"]
    p_relay = subprocess.Popen(relay_cmd)
    processes.append((p_relay, "Relay Server"))
    time.sleep(1)

    # 2. Start Admin Console
    print(f"[*] Starting Admin Console on port {args.admin_port}...")
    admin_cmd = [sys.executable, os.path.join(ROOT, "admin", "server.py"), "--port", str(args.admin_port)]
    p_admin = subprocess.Popen(admin_cmd)
    processes.append((p_admin, "Admin Console"))
    time.sleep(1)

    # 3. Start Desktop Host
    print("[*] Starting Desktop Host...")
    host_cmd = [sys.executable, os.path.join(ROOT, "desktop", "host_p12.py"), "--relay", f"127.0.0.1:{args.relay_port}"]
    p_host = subprocess.Popen(host_cmd)
    processes.append((p_host, "Desktop Host"))
    time.sleep(1)

    # 4. Start Desktop GUI (if not headless)
    if not args.headless and "DISPLAY" in os.environ:
        print("[*] Starting Desktop GUI Launcher...")
        gui_cmd = [sys.executable, os.path.join(ROOT, "desktop", "gui.py")]
        p_gui = subprocess.Popen(gui_cmd)
        processes.append((p_gui, "Desktop GUI"))
    else:
        print("[*] Headless mode enabled or no display attached; skipping GUI launcher.")

    print("\n==========================================================")
    print("  All RemoteBridge Modules Running Successfully!         ")
    print("==========================================================")
    print(f"  - Admin Web Console : http://localhost:{args.admin_port}")
    print(f"  - Admin Metrics     : http://localhost:{args.admin_port}/metrics")
    print(f"  - Relay TCP Port    : 127.0.0.1:{args.relay_port}")
    print("==========================================================")
    print("Press Ctrl+C to terminate all running modules.\n")

    while True:
        time.sleep(1)
        # Check if any child process died unexpectedly
        for proc, name in processes:
            if proc.poll() is not None:
                print(f"[!] Warning: {name} exited with code {proc.returncode}")

if __name__ == "__main__":
    main()
