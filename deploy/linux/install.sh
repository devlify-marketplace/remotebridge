#!/bin/sh
# Phase 10 - scripted Linux install for the host.
#
# Unlike deploy/wix (which needs a Windows machine and WiX Toolset this
# project's sandbox has neither of), this script is plain POSIX shell
# and Python venv/pip - both available in Linux, so - modulo actually
# reaching PyPI, which the sandbox this was authored in has no network
# for - this one really has been run end to end here: directory layout,
# file placement, and the systemd unit it generates are all exercised,
# not just reviewed. See ../README.md.
#
# Usage:
#   sudo ./install.sh                                   # system-wide, /opt
#   ./install.sh --user                                 # just this user, ~/.local/share
#   ./install.sh --deploy-config /path/to/acme.json      # bundle an org config
#   ./install.sh --no-service                            # copy files only, no systemd unit
#
# What this does NOT do: assume the host is meant to run unattended by
# default. A freshly installed host still uses whatever host_config.json
# defaults to (a console confirmation prompt, no unattended password) -
# see desktop/README.md and desktop/auth.py. Installing the software and
# configuring it to accept connections without a person present are
# deliberately separate steps.

set -e

MODE="system"
DEPLOY_CONFIG=""
WITH_SERVICE=1

while [ $# -gt 0 ]; do
    case "$1" in
        --user) MODE="user"; shift ;;
        --deploy-config) DEPLOY_CONFIG="$2"; shift 2 ;;
        --no-service) WITH_SERVICE=0; shift ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)

if [ "$MODE" = "system" ]; then
    INSTALL_ROOT="${REMOTEBRIDGE_INSTALL_ROOT:-/opt/remotebridge}"
    SERVICE_DIR="${REMOTEBRIDGE_SYSTEMD_DIR:-/etc/systemd/system}"
    SYSTEMCTL_FLAGS=""
else
    INSTALL_ROOT="${REMOTEBRIDGE_INSTALL_ROOT:-$HOME/.local/share/remotebridge}"
    SERVICE_DIR="${REMOTEBRIDGE_SYSTEMD_DIR:-$HOME/.config/systemd/user}"
    SYSTEMCTL_FLAGS="--user"
fi

echo "==> Installing to $INSTALL_ROOT"
mkdir -p "$INSTALL_ROOT"
cp -r "$REPO_ROOT/desktop/." "$INSTALL_ROOT/app"

echo "==> Setting up a virtualenv"
python3 -m venv "$INSTALL_ROOT/venv"
# Not run in this sandbox (no network to PyPI here) - on a real machine
# this installs mss/pillow/opencv-python/numpy/pynput/pyotp/pyperclip/
# sounddevice per desktop/requirements.txt.
"$INSTALL_ROOT/venv/bin/pip" install -r "$INSTALL_ROOT/app/requirements.txt" || \
    echo "    (pip install failed or skipped - fine for a dry run; a real install needs network)"

echo "==> Writing deploy_config.json"
if [ -n "$DEPLOY_CONFIG" ] && [ -f "$DEPLOY_CONFIG" ]; then
    cp "$DEPLOY_CONFIG" "$INSTALL_ROOT/app/deploy_config.json"
    echo "    Using org config: $DEPLOY_CONFIG"
else
    echo "{}" > "$INSTALL_ROOT/app/deploy_config.json"
    echo "    No org config given - host starts unconfigured (same as Phase 9) until"
    echo "    someone runs it once with --admin-url, or this file is edited in place."
fi

if [ "$WITH_SERVICE" = "1" ]; then
    echo "==> Installing systemd unit ($SYSTEMCTL_FLAGS)"
    mkdir -p "$SERVICE_DIR"
    cat > "$SERVICE_DIR/remotebridge-host.service" <<EOF
[Unit]
Description=RemoteBridge Host
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_ROOT/app
ExecStart=$INSTALL_ROOT/venv/bin/python3 $INSTALL_ROOT/app/host_p12.py
Restart=on-failure
RestartSec=5
# No TTY under systemd, so console.py's interactive file-manager prompt
# hits EOF on its first read and exits immediately by design (see
# console.py's console_loop) - the video/input/control/audio channels
# and admin-policy enforcement don't need a TTY at all and keep running.
StandardInput=null

[Install]
WantedBy=$([ "$MODE" = "system" ] && echo "multi-user.target" || echo "default.target")
EOF
    echo "    Wrote $SERVICE_DIR/remotebridge-host.service"
    echo "    Enable it with: systemctl $SYSTEMCTL_FLAGS enable --now remotebridge-host"
else
    echo "==> Skipping systemd unit (--no-service). Run it directly with:"
    echo "    $INSTALL_ROOT/venv/bin/python3 $INSTALL_ROOT/app/host_p12.py"
fi

echo "==> Done."
