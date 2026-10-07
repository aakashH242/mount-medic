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
        settings = destination / "var/lib/mount-medic/1000.json"
        atomic_json(settings, {"drive": {"monitor": True, "auto_repair": True}})
        real_install = installer.install_tree
        with patch.object(installer, "fetch_release", return_value=release), patch.object(installer, "missing_packages", return_value=[]), patch.object(installer, "install_tree", side_effect=lambda tree, ignored: real_install(tree, destination)):
            with patch.object(installer.subprocess, "run", wraps=subprocess.run) as run:
                result = installer.upgrade_release(args)
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
        print("PASS: protected archive verified, source built with an isolated UID, complete root-owned install, settings preserved, corruption refused")


if __name__ == "__main__":
    exercise()
