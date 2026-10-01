"""
Phase 10 - self-update.

Split deliberately into two halves with very different confidence levels:

  1. Decide whether to update at all - compare versions, download the new
     build, verify its checksum. Pure Python, stdlib only, works the same
     on every OS, and is exercised by tests the same way the rest of this
     project's non-GUI logic is.
  2. Actually replace the running program with the new one - inherently
     OS-specific (you can't overwrite an .exe Windows is currently
     executing; POSIX is more forgiving but the "restart into the new
     version" dance still differs by platform). Written carefully for
     both branches below, but - like the WiX/PyInstaller pieces in
     deploy/ - this half hasn't been run for real in the Linux sandbox
     this was developed in: there's no live installed host process here
     to replace out from under itself. Treat apply_update() as reviewed,
     not proven, until it's exercised on an actual install.

CURRENT_VERSION is what a running host reports to the admin console
(see admin_client.py's policy fetch) and compares against whatever
admin/server.py has published (see admin/store.py's set_release).
"""

import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request

CURRENT_VERSION = "12.0.0"


def _version_tuple(v: str) -> tuple:
    """'10.0.1' -> (10, 0, 1). Non-numeric parts sort as -1 so a garbled
    version string compares as older rather than raising - an update
    decision should never crash a running session over a typo'd version
    string in the admin console."""
    parts = []
    for piece in v.strip().lstrip("v").split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            parts.append(-1)
    return tuple(parts)


def is_newer(remote_version: str, current_version: str = CURRENT_VERSION) -> bool:
    if not remote_version:
        return False
    return _version_tuple(remote_version) > _version_tuple(current_version)


def download_file(url: str, dest_path: str, timeout: float = 60.0) -> None:
    """Streams the URL to dest_path. Raises on any failure - callers should
    treat a failed download as "no update this cycle", not fatal."""
    req = urllib.request.Request(url, headers={"User-Agent": "remotebridge-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest_path, "wb") as out:
        shutil.copyfileobj(resp, out)


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify(path: str, expected_sha256: str) -> bool:
    if not expected_sha256:
        return True  # nothing to check against - caller decides if that's acceptable
    return sha256_of(path).lower() == expected_sha256.strip().lower()


def fetch_and_verify(release: dict, dest_dir: str = None) -> str:
    """Downloads release["download_url"] to a temp file and verifies it
    against release["sha256"]. Returns the downloaded path on success;
    raises ValueError on a checksum mismatch, or lets the underlying
    urllib error propagate on a network failure - either way, the caller
    (host_p11.py) catches broadly and just tries again next cycle."""
    dest_dir = dest_dir or tempfile.gettempdir()
    suffix = os.path.splitext(release.get("download_url", ""))[1] or ".bin"
    fd, dest_path = tempfile.mkstemp(suffix=suffix, dir=dest_dir)
    os.close(fd)
    download_file(release["download_url"], dest_path)
    if not verify(dest_path, release.get("sha256", "")):
        os.remove(dest_path)
        raise ValueError(f"downloaded update failed checksum verification: {release['download_url']}")
    return dest_path


# --- Applying the update: OS-specific, see module docstring --------------

def apply_update(new_file_path: str, current_exe_path: str = None) -> None:
    """Replaces the currently-running program with new_file_path and
    restarts it, then exits this process. current_exe_path defaults to
    sys.executable's frozen location (what PyInstaller sets when this is
    running as a built .exe); pass it explicitly when running from source
    (there's nothing meaningful to "replace" for a `python3 host_p12.py`
    dev run - that path just isn't exercised outside a frozen build).
    Never returns on success; raises if it can't even get the relaunch
    mechanism started, so the caller can log and skip this cycle."""
    target = current_exe_path or (sys.executable if getattr(sys, "frozen", False) else None)
    if not target:
        raise RuntimeError("apply_update needs a frozen executable path - nothing to replace "
                            "when running from source (`python3 host_p12.py`)")

    if os.name == "nt":
        _apply_update_windows(new_file_path, target)
    else:
        _apply_update_posix(new_file_path, target)
    sys.exit(0)


def _apply_update_windows(new_file_path: str, target_exe: str) -> None:
    """Windows can't overwrite a .exe while it's running, so a tiny batch
    script waits for this process to exit, moves the new file over the
    old one, then relaunches it. `ping -n` is the traditional
    dependency-free way to sleep a couple seconds in a .bat file without
    needing PowerShell or a bundled `sleep`."""
    script_path = os.path.join(tempfile.gettempdir(), "remotebridge_update.bat")
    with open(script_path, "w") as f:
        f.write(f"""@echo off
ping -n 3 127.0.0.1 >nul
move /Y "{new_file_path}" "{target_exe}" >nul
start "" "{target_exe}"
del "%~f0"
""")
    subprocess.Popen(["cmd.exe", "/c", script_path],
                      creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)


def _apply_update_posix(new_file_path: str, target_exe: str) -> None:
    """POSIX allows replacing a file that's still running (the process
    keeps its already-open inode; the path just starts pointing at the
    new one), so this is simpler than Windows: a short shell script still
    waits a moment for a clean exit, then swaps the file in and relaunches."""
    script_path = os.path.join(tempfile.gettempdir(), "remotebridge_update.sh")
    with open(script_path, "w") as f:
        f.write(f"""#!/bin/sh
sleep 2
mv -f "{new_file_path}" "{target_exe}"
chmod +x "{target_exe}"
exec "{target_exe}" &
""")
    os.chmod(script_path, os.stat(script_path).st_mode | stat.S_IEXEC)
    subprocess.Popen(["/bin/sh", script_path], start_new_session=True)
