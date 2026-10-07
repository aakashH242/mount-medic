"""Run only in a disposable root container: protected staging and real UID-drop build."""
import os
from pathlib import Path
import shutil
import subprocess
import pwd
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mount_medic import installer
from mount_medic.releases import ReleaseError
from mount_medic.storage import atomic_json, read_json
from scripts.release import package
from update_fixture import TARGET_VERSION


def exercise_launcher_guard(root, uid):
    root.chmod(0o755)
    marker = root / "launcher-transaction"
    marker.mkdir(mode=0o700)
    atomic_json(marker / "status.json", {"committed": False})
    guard = installer.MAINTENANCE.replace("/usr/local/lib/.mount-medic-transaction", str(marker))
    command = [sys.executable, "-IB", "-c", guard + "\nprint('started')"]
    blocked = subprocess.run(command, user=uid, group=uid, extra_groups=[], cwd="/tmp", capture_output=True, text=True)
    assert blocked.returncode == 1 and "--recover" in blocked.stderr and "guided installer" in blocked.stderr and "Traceback" not in blocked.stderr, blocked
    (marker / "status.json").unlink()
    staging = subprocess.run(command, user=uid, group=uid, extra_groups=[], cwd="/tmp", capture_output=True, text=True)
    assert staging.returncode == 1 and "--recover" in staging.stderr, staging
    marker.rmdir()
    ready = subprocess.run(command, user=uid, group=uid, extra_groups=[], cwd="/tmp", capture_output=True, text=True)
    assert ready.returncode == 0 and ready.stdout.strip() == "started", ready


def exercise():
    assert os.geteuid() == 0
    checkout = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source"
        source.mkdir()
        for name in ("mount_medic", "native", "integration"):
            shutil.copytree(checkout / name, source / name, ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("Makefile", "install.py", "LICENSE", "pyproject.toml"):
            shutil.copy2(checkout / name, source / name)
        declaration = f'__version__ = "{TARGET_VERSION}"\n'
        (source / "mount_medic/__init__.py").write_text(declaration)
        tracked = "\0".join(str(path.relative_to(source)) for path in source.rglob("*") if path.is_file()).encode()
        with patch("scripts.release.subprocess.check_output", return_value=tracked):
            release = package(source, root / "assets")
        archive = root / "assets" / release["archive"]
        args = SimpleNamespace(upgrade=TARGET_VERSION, sha256=release["sha256"], archive=archive, build_user=1000, approved_packages=[])
        destination = root / "installed"
        destination.mkdir(mode=0o755)
        destination.chmod(0o755)
        settings = destination / "var/lib/mount-medic/1000.json"
        atomic_json(settings, {"drive": {"monitor": True, "auto_repair": True}})
        real_install = installer.install_tree
        with patch.object(installer, "fetch_release", return_value=release), patch.object(installer, "missing_packages", return_value=[]), patch.object(installer, "install_tree", side_effect=lambda tree, ignored: real_install(tree, destination)):
            with patch.object(installer.subprocess, "run", wraps=subprocess.run) as run:
                previous = os.umask(0o077)
                try:
                    result = installer.upgrade_release(args)
                finally:
                    os.umask(previous)
            build_call = next(call for call in run.call_args_list if call.args[0] == ["make", "all"])
            assert build_call.kwargs["user"] > 0 and build_call.kwargs["user"] != args.build_user
            assert build_call.kwargs["extra_groups"] == []
            try:
                pwd.getpwuid(build_call.kwargs["user"])
            except KeyError:
                pass
            else:
                raise AssertionError("Build identity belongs to an existing account")
            assert result["version"] == TARGET_VERSION
            assert read_json(settings) == {"drive": {"monitor": True, "auto_repair": True}}
            binary = destination / installer.LIBRARY / "mount-medic-probe"
            assert binary.stat().st_uid == 0 and binary.stat().st_mode & 0o111
            assert (destination / installer.LIBRARY / "mount_medic/__init__.py").read_text() == declaration
            assert not (destination / installer.TRANSACTION).exists()
            archive.write_bytes(b"corrupt")
            with patch.object(installer, "prepare_install", side_effect=AssertionError("built a corrupt archive")):
                try:
                    installer.upgrade_release(args)
                except ReleaseError:
                    pass
                else:
                    raise AssertionError("corrupt archive accepted by privileged helper")
        exercise_launcher_guard(root, build_call.kwargs["user"])
        library = destination / installer.LIBRARY
        code = f"import sys, os; from pathlib import Path; sys.path.insert(0, {str(library)!r}); from mount_medic import __version__; assert __version__ == {TARGET_VERSION!r}; assert os.access({str(binary)!r}, os.X_OK); Path({str(destination / 'usr/local/share/applications/io.github.aakashH242.MountMedic.desktop')!r}).read_bytes(); print('readable')"
        reader = subprocess.run([sys.executable, "-IB", "-c", code], user=build_call.kwargs["user"], group=build_call.kwargs["group"], extra_groups=[], cwd="/tmp", capture_output=True, text=True)
        assert reader.returncode == 0 and reader.stdout.strip() == "readable", reader
        print("PASS: protected archive verified, isolated build UID, complete root-owned install, settings preserved, corruption refused, normal-user recovery/staging guards and app access under umask 077")


if __name__ == "__main__":
    exercise()
