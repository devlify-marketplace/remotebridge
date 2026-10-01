#!/usr/bin/env bash
# RemoteBridge Master Start Shell Script
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
python3 "$DIR/start_all.py" "$@"
