#!/usr/bin/env python3
"""
RemoteBridge Desktop GUI (Phase 14)

Provides a graphical desktop interface for launching RemoteBridge Host and Viewer instances,
configuring session parameters, viewing live session status, and switching monitors.
Uses Python tkinter (built-in, cross-platform, zero extra dependencies).
"""

import sys
import os
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

class RemoteBridgeApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("RemoteBridge Desktop")
        self.geometry("560x480")
        self.resizable(False, False)

        # Style configuration
        style = ttk.Style(self)
        style.theme_use("clam")

        # Header
        header_frame = ttk.Frame(self, padding=15)
        header_frame.pack(fill="x")
        ttk.Label(header_frame, text="RemoteBridge", font=("Helvetica", 18, "bold")).pack(anchor="w")
        ttk.Label(header_frame, text="Secure Cross-Platform Remote Desktop & Control", font=("Helvetica", 10)).pack(anchor="w")

        # Tabs
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=15, pady=10)

        # Host Tab
        self.host_tab = ttk.Frame(self.notebook, padding=15)
        self.notebook.add(self.host_tab, text="Host Machine")
        self._build_host_tab()

        # Viewer Tab
        self.viewer_tab = ttk.Frame(self.notebook, padding=15)
        self.notebook.add(self.viewer_tab, text="Connect as Viewer")
        self._build_viewer_tab()

        # Status Bar
        self.status_var = tk.StringVar(value="Ready")
        status_bar = ttk.Label(self, textvariable=self.status_var, relief="sunken", anchor="w", padding=5)
        status_bar.pack(fill="x", side="bottom")

        self.host_process = None
        self.viewer_process = None

    def _build_host_tab(self):
        frame = self.host_tab
        
        ttk.Label(frame, text="Host Session Settings", font=("Helvetica", 11, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        ttk.Label(frame, text="Relay Address:").grid(row=1, column=0, sticky="w", pady=5)
        self.host_relay_entry = ttk.Entry(frame, width=35)
        self.host_relay_entry.insert(0, "127.0.0.1:6000")
        self.host_relay_entry.grid(row=1, column=1, sticky="w", pady=5)

        ttk.Label(frame, text="Device ID:").grid(row=2, column=0, sticky="w", pady=5)
        self.host_id_entry = ttk.Entry(frame, width=35)
        self.host_id_entry.insert(0, "my-desktop-host")
        self.host_id_entry.grid(row=2, column=1, sticky="w", pady=5)

        ttk.Label(frame, text="Unattended Password:").grid(row=3, column=0, sticky="w", pady=5)
        self.host_pwd_entry = ttk.Entry(frame, width=35, show="*")
        self.host_pwd_entry.grid(row=3, column=1, sticky="w", pady=5)

        ttk.Label(frame, text="Relay Token (Optional):").grid(row=4, column=0, sticky="w", pady=5)
        self.host_token_entry = ttk.Entry(frame, width=35)
        self.host_token_entry.grid(row=4, column=1, sticky="w", pady=5)

        self.start_host_btn = ttk.Button(frame, text="Start Host Session", command=self._toggle_host)
        self.start_host_btn.grid(row=5, column=0, columnspan=2, pady=20)

    def _build_viewer_tab(self):
        frame = self.viewer_tab

        ttk.Label(frame, text="Connect to Remote Host", font=("Helvetica", 11, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        ttk.Label(frame, text="Target (Address/Relay):").grid(row=1, column=0, sticky="w", pady=5)
        self.viewer_target_entry = ttk.Entry(frame, width=35)
        self.viewer_target_entry.insert(0, "127.0.0.1:6000")
        self.viewer_target_entry.grid(row=1, column=1, sticky="w", pady=5)

        ttk.Label(frame, text="Host Device ID:").grid(row=2, column=0, sticky="w", pady=5)
        self.viewer_id_entry = ttk.Entry(frame, width=35)
        self.viewer_id_entry.insert(0, "my-desktop-host")
        self.viewer_id_entry.grid(row=2, column=1, sticky="w", pady=5)

        ttk.Label(frame, text="Password:").grid(row=3, column=0, sticky="w", pady=5)
        self.viewer_pwd_entry = ttk.Entry(frame, width=35, show="*")
        self.viewer_pwd_entry.grid(row=3, column=1, sticky="w", pady=5)

        self.connect_viewer_btn = ttk.Button(frame, text="Connect as Viewer", command=self._toggle_viewer)
        self.connect_viewer_btn.grid(row=4, column=0, columnspan=2, pady=20)

    def _toggle_host(self):
        if self.host_process:
            self.host_process.terminate()
            self.host_process = None
            self.start_host_btn.config(text="Start Host Session")
            self.status_var.set("Host session stopped.")
            return

        relay = self.host_relay_entry.get().strip()
        dev_id = self.host_id_entry.get().strip()
        pwd = self.host_pwd_entry.get().strip()
        token = self.host_token_entry.get().strip()

        if not dev_id:
            messagebox.showerror("Error", "Device ID is required.")
            return

        cmd = [sys.executable, os.path.join(ROOT, "host_p12.py"), "--relay", relay, "--id", dev_id]
        if pwd:
            cmd.extend(["--password", pwd])
        if token:
            cmd.extend(["--relay-token", token])

        try:
            self.host_process = subprocess.Popen(cmd)
            self.start_host_btn.config(text="Stop Host Session")
            self.status_var.set(f"Host running as '{dev_id}' on {relay}")
        except Exception as err:
            messagebox.showerror("Host Error", str(err))

    def _toggle_viewer(self):
        if self.viewer_process:
            self.viewer_process.terminate()
            self.viewer_process = None
            self.connect_viewer_btn.config(text="Connect as Viewer")
            self.status_var.set("Viewer disconnected.")
            return

        target = self.viewer_target_entry.get().strip()
        dev_id = self.viewer_id_entry.get().strip()
        pwd = self.viewer_pwd_entry.get().strip()

        cmd = [sys.executable, os.path.join(ROOT, "viewer_p12.py"), f"--relay={target}", dev_id]
        if pwd:
            cmd.extend(["--password", pwd])

        try:
            self.viewer_process = subprocess.Popen(cmd)
            self.connect_viewer_btn.config(text="Disconnect Viewer")
            self.status_var.set(f"Connecting viewer to '{dev_id}'...")
        except Exception as err:
            messagebox.showerror("Viewer Error", str(err))

if __name__ == "__main__":
    app = RemoteBridgeApp()
    app.mainloop()
