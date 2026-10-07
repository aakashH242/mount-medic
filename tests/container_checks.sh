#!/bin/sh
# /source is read-only; all fixture writes stay inside the disposable container.
set -eu
python3 -c 'import shutil; shutil.copytree("/source", "/work", dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git", ".idea", ".consequences", "build", "artifacts", "images", "__pycache__"))'
cd /work
make clean
make test
python3 -m compileall -q mount_medic
dbus-run-session -- python3 tests/authorization_smoke.py
dbus-run-session -- python3 tests/notification_smoke.py
dbus-run-session -- xvfb-run -a -s '-screen 0 1280x1024x24' python3 tests/update_desktop_smoke.py
dbus-run-session -- xvfb-run -a -s '-screen 0 1280x1024x24' python3 tests/update_progress_smoke.py "${MM_ARTIFACTS:-/tmp}/update-progress"
if [ "${MM_UPDATE_INSTALL_SMOKE:-0}" = 1 ]; then
    python3 tests/update_install_smoke.py
fi
python3 -m mount_medic check --dry-run --json
python3 install.py --apply /work --destdir /tmp/staged
python3 install.py --apply /work --destdir /tmp/staged
# Some distributions default Xvfb to 640x480, clipping the GTK header controls.
MM_STAGED_ROOT=/tmp/staged dbus-run-session -- xvfb-run -a -s '-screen 0 1280x1024x24' python3 tests/ui_smoke.py "${MM_ARTIFACTS:-/tmp}/ui.png"
dbus-run-session -- xvfb-run -a -s '-screen 0 1280x1024x24' python3 tests/feedback_smoke.py "${MM_ARTIFACTS:-/tmp}/feedback"
python3 install.py --remove --destdir /tmp/staged
test ! -e /tmp/staged/usr/local/share/icons/hicolor/256x256/apps/io.github.aakashH242.MountMedic.png
if command -v shellcheck >/dev/null; then
    shellcheck install.sh tests/container_checks.sh tests/vm/setup.sh
fi
cat /etc/os-release
python3 --version
ntfsfix --version
