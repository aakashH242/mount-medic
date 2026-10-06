#!/bin/sh
set -eu
if [ "${1:-}" = "--download" ]; then
    shift
    install_tmp=$(mktemp -d)
    trap 'rm -rf -- "$install_tmp"' EXIT
    curl -fsSL https://codeload.github.com/aakashH242/mount-medic/tar.gz/refs/heads/main -o "$install_tmp/source.tar.gz"
    tar -xzf "$install_tmp/source.tar.gz" -C "$install_tmp"
    # The script may arrive through a pipe; consent must come from the terminal.
    python3 "$install_tmp/mount-medic-main/install.py" "$@" </dev/tty
    exit 0
fi
cd "$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
exec python3 install.py "$@"
