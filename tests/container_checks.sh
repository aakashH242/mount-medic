#!/bin/sh
# /source is read-only; all fixture writes stay inside the disposable container.
set -eu
python3 -c 'import shutil; shutil.copytree("/source", "/work", dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git", ".idea", ".consequences", "build", "artifacts", "images", "__pycache__"))'
cd /work
make clean
make test
python3 -m compileall -q mount_medic
dbus-run-session -- python3 tests/notification_smoke.py
python3 -m mount_medic check --dry-run --json
python3 install.py --apply /work --destdir /tmp/staged
python3 install.py --apply /work --destdir /tmp/staged
MM_STAGED_ROOT=/tmp/staged dbus-run-session -- xvfb-run -a python3 tests/ui_smoke.py "${MM_ARTIFACTS:-/tmp}/ui.png"
python3 install.py --remove --destdir /tmp/staged
test ! -e /tmp/staged/usr/local/share/icons/hicolor/256x256/apps/io.github.aakashH242.MountMedic.png
if command -v shellcheck >/dev/null; then
    shellcheck install.sh tests/container_checks.sh tests/vm/setup.sh
fi
cat /etc/os-release
python3 --version
ntfsfix --version
