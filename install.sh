#!/bin/sh
set -eu
if [ "${1:-}" = "--download" ]; then
    shift
    install_tmp=$(mktemp -d)
    trap 'rm -rf -- "$install_tmp"' EXIT
    curl --proto '=https' --proto-redir '=https' --max-time 30 -fsSL https://github.com/aakashH242/mount-medic/releases/latest/download/bootstrap.py -o "$install_tmp/bootstrap.py"
    # The script may arrive through a pipe; consent must come from the terminal.
    python3 "$install_tmp/bootstrap.py" "$@" </dev/tty
    exit 0
fi
cd "$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
exec python3 install.py "$@"
