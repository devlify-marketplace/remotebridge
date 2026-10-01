# Phase 10 - PyInstaller spec for the host.
#
# Freezes desktop/host_p12.py and everything it imports into one
# executable, RemoteBridgeHost.exe, so it can run on a machine with no
# Python installed - what the WiX installer (../wix/Product.wxs) then
# packages into an MSI.
#
# NOT RUN in the environment this was written in (no PyInstaller
# available, no network to install it - see ../README.md). Written
# correctly against PyInstaller's documented spec-file API and reviewed
# by hand; treat it as reviewed; not proven, until it's actually run.
#
# Usage (on a machine with PyInstaller installed):
#   pip install pyinstaller
#   pyinstaller deploy/pyinstaller/host.spec --distpath deploy/pyinstaller/dist
#
# The scoped hiddenimports below are the modules Phase 6-10's host code
# reaches only via a lazy `import` *inside* a function body (mss inside
# broadcast_video, pynput inside handle_viewer_input, pyotp inside
# auth.py's TOTP functions) - PyInstaller's static analysis doesn't see
# those, so without listing them here the frozen exe would import
# cleanly but fail the first time one of those code paths actually runs.

# -*- mode: python ; coding: utf-8 -*-

import os

DESKTOP_DIR = os.path.join(os.path.dirname(os.path.abspath(SPEC)), "..", "..", "desktop")

a = Analysis(
    [os.path.join(DESKTOP_DIR, "host_p12.py")],
    pathex=[DESKTOP_DIR],
    binaries=[],
    datas=[],
    hiddenimports=[
        "mss", "mss.windows",
        "pynput.mouse", "pynput.keyboard",
        "pyotp",
        "sounddevice",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="RemoteBridgeHost",
    debug=False,
    strip=False,
    upx=False,          # UPX-packed exes get flagged by some AV/EDR products; skip it
    console=True,       # this host is an interactive console app (console.py's file-manager
                         # prompt), not a background service - see ../README.md's Known
                         # Limitations for what a true Windows-service mode would need
    icon=None,           # branding is fetched at runtime from the admin console (see
                         # admin_client.fetch_branding), not baked into the exe at build time;
                         # point this at a .ico if a static taskbar/exe icon is wanted too
)
