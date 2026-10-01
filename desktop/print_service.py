"""
Phase 8 - Remote printing.

Prints a file that has already been pushed to the host's download
directory via the existing (Phase 3) file-transfer channel. This module
only ever prints files inside that directory: `filename` is required to
be a bare filename with no path separators, and the resolved path is
double-checked to still be inside download_dir before anything is
handed to the OS print command. That's what stops a viewer from
requesting something like "../../etc/passwd" or an absolute path.
"""

import os
import platform
import shutil
import subprocess


class PrintError(Exception):
    pass


def _is_within_directory(path: str, directory: str) -> bool:
    directory = os.path.realpath(directory)
    path = os.path.realpath(path)
    return os.path.commonpath([directory, path]) == directory


def resolve_print_path(filename: str, download_dir: str) -> str:
    """Validates filename and returns the absolute path to print, or raises PrintError."""
    if not filename or os.sep in filename or (os.altsep and os.altsep in filename):
        raise PrintError("Invalid filename (no path separators allowed)")
    if filename in (".", ".."):
        raise PrintError("Invalid filename")

    candidate = os.path.join(download_dir, filename)
    if not _is_within_directory(candidate, download_dir):
        raise PrintError("Resolved path escapes the download directory")
    if not os.path.isfile(candidate):
        raise PrintError(f"'{filename}' was not found in the download directory")
    return candidate


def print_file(filename: str, download_dir: str, printer: str = "", copies: int = 1) -> tuple:
    """
    Prints filename (must already be in download_dir) on the host machine.
    Returns (success: bool, message: str, job_id: str).
    """
    try:
        path = resolve_print_path(filename, download_dir)
    except PrintError as e:
        return False, str(e), ""

    system = platform.system()
    copies = max(1, int(copies))

    try:
        if system == "Windows":
            return _print_windows(path, printer, copies)
        else:
            return _print_posix(path, printer, copies)
    except PrintError as e:
        return False, str(e), ""
    except Exception as e:
        return False, f"Print failed: {e}", ""


def _print_posix(path: str, printer: str, copies: int) -> tuple:
    """Uses CUPS's `lp` (falls back to `lpr`) — standard on Linux and macOS."""
    lp = shutil.which("lp")
    lpr = shutil.which("lpr")
    if not lp and not lpr:
        raise PrintError("No print command found (lp/lpr) — is CUPS installed?")

    if lp:
        cmd = ["lp"]
        if printer:
            cmd += ["-d", printer]
        cmd += ["-n", str(copies), path]
    else:
        cmd = ["lpr"]
        if printer:
            cmd += ["-P", printer]
        cmd += ["-#", str(copies), path]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise PrintError(f"Print command failed: {result.stderr.strip() or result.stdout.strip()}")

    job_id = result.stdout.strip()
    return True, f"Sent to printer{' ' + printer if printer else ''}", job_id


def _print_windows(path: str, printer: str, copies: int) -> tuple:
    """Shells out to the file's default 'print' verb via the OS.
    Per-copy count and explicit printer selection need a print-capable
    app registered for the file type; this covers the common case of
    printing to the default printer."""
    for _ in range(copies):
        try:
            os.startfile(path, "print")  # type: ignore[attr-defined]
        except OSError as e:
            raise PrintError(f"Could not print: {e}")
    return True, "Sent to default printer", ""
