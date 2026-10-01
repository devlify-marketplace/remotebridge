"""
RemoteBridge PyInstaller Build Script

Compiles standalone desktop host, viewer, and GUI executables into the dist/ directory.
Usage:
    python3 deploy/build_executables.py
"""

import os
import sys
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def build():
    print("=== Building RemoteBridge Desktop Executables ===")

    try:
        import PyInstaller
    except ImportError:
        print("[*] PyInstaller not installed. Installing PyInstaller...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])

    dist_dir = os.path.join(ROOT, "dist")
    build_dir = os.path.join(ROOT, "build_pyinstaller")

    targets = [
        ("remotebridge-gui", os.path.join(ROOT, "desktop", "gui.py")),
        ("remotebridge-host", os.path.join(ROOT, "desktop", "host_p12.py")),
        ("remotebridge-viewer", os.path.join(ROOT, "desktop", "viewer_p12.py")),
    ]

    for name, script in targets:
        print(f"\n[*] Compiling {name} ({script})...")
        cmd = [
            sys.executable, "-m", "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onefile",
            "--name", name,
            "--distpath", dist_dir,
            "--workpath", build_dir,
            script
        ]
        res = subprocess.run(cmd, cwd=ROOT)
        if res.returncode == 0:
            print(f"[+] Successfully compiled {name}")
        else:
            print(f"[-] Failed to compile {name}")

if __name__ == "__main__":
    build()
