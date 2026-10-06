import argparse
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from .dependencies import PACKAGES, family, dependency_command
from .model import MedicError
from .protocol import BUS_NAME
from .storage import Preferences, atomic_json, notification_seconds, read_json

LIBRARY = Path("usr/local/lib/mount-medic")
MANIFEST = LIBRARY / "manifest.json"
INTEGRATION = {
    "io.github.aakashH242.MountMedic.service": "usr/share/dbus-1/system-services",
    "io.github.aakashH242.MountMedic.conf": "usr/share/dbus-1/system.d",
    "io.github.aakashH242.mount-medic.policy": "usr/share/polkit-1/actions",
    "io.github.aakashH242.MountMedic.desktop": "usr/local/share/applications",
}
ICONS = {
    f"{BUS_NAME}.png": "usr/local/share/icons/hicolor/256x256/apps",
    f"{BUS_NAME}-symbolic.svg": "usr/local/share/icons/hicolor/symbolic/apps",
}
WRAPPER = """#!/usr/bin/python3 -IB
import sys
sys.path.insert(0, '/usr/local/lib/mount-medic')
from mount_medic.{module} import main
raise SystemExit(main())
"""


def ask(message: str) -> bool:
    return sys.stdin.isatty() and input(message + " [y/N] ").strip().lower() == "y"


def setup_notifications() -> None:
    preferences = Preferences()
    seconds = preferences.notification_seconds()
    if sys.stdin.isatty():
        while True:
            answer = input(f"Notification duration in seconds, 1–600 [{seconds}]: ").strip()
            try:
                seconds = notification_seconds(int(answer) if answer else seconds)
                break
            except (ValueError, MedicError):
                print("Enter a whole number from 1 to 600, or press Enter to keep the default.")
    preferences.set_notification_seconds(seconds)


def elevated(arguments: list[str]) -> list[str]:
    if os.geteuid() == 0:
        return arguments
    for tool in ("pkexec", "sudo"):
        binary = shutil.which(tool, path="/usr/bin:/usr/sbin:/bin:/sbin")
        if binary:
            return [binary, *arguments]
    raise MedicError("Run the displayed installation command as an administrator; no authentication tool is available")


def payload(source: Path) -> dict:
    files = {}
    for path in sorted((source / "mount_medic").glob("*.py")):
        if path.is_symlink():
            raise MedicError("Refusing symbolic links in installation sources")
        files[str(LIBRARY / "mount_medic" / path.name)] = (path.read_bytes(), 0o644)
    for path in sorted((source / "mount_medic/assets").iterdir()):
        if path.is_symlink() or not path.is_file():
            raise MedicError("Refusing non-regular application assets")
        files[str(LIBRARY / "mount_medic/assets" / path.name)] = (path.read_bytes(), 0o644)
    for name, target in ICONS.items():
        files[str(Path(target) / name)] = ((source / "mount_medic/assets" / name).read_bytes(), 0o644)
    files[str(LIBRARY / "mount-medic-probe")] = ((source / "build/mount-medic-probe").read_bytes(), 0o755)
    for name, target in INTEGRATION.items():
        files[str(Path(target) / name)] = ((source / "integration" / name).read_bytes(), 0o644)
    files["usr/local/bin/mount-medic"] = (WRAPPER.format(module="cli").encode(), 0o755)
    for name in ("worker", "installer"):
        files[str(LIBRARY / name)] = (WRAPPER.format(module=name).encode(), 0o755)
    files[str(LIBRARY / "autostart.desktop")] = ((source / "integration/mount-medic-autostart.desktop").read_bytes(), 0o644)
    files[str(LIBRARY / "LICENSE")] = ((source / "LICENSE").read_bytes(), 0o644)
    return files


def safe_target(root: Path, relative: str) -> Path:
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise MedicError("Invalid installation manifest path")
    path = root / relative
    for parent in [path, *path.parents]:
        if parent == root.parent:
            break
        if parent.is_symlink():
            raise MedicError(f"Refusing installation symlink: {parent}")
        if parent.exists() and root == Path("/"):
            info = parent.stat()
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise MedicError(f"Untrusted installation path: {parent}")
    return path


def install_tree(source: Path, root: Path) -> dict:
    files = payload(source)
    manifest = read_json(safe_target(root, str(MANIFEST)))
    for relative, (content, mode) in files.items():
        target = safe_target(root, relative)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        descriptor, temporary = tempfile.mkstemp(dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        manifest[relative] = hashlib.sha256(content).hexdigest()
        # Update after each file so an interrupted installation can be removed.
        atomic_json(root / MANIFEST, manifest)
    refresh_icons(root)
    return {"installed": len(files), "root": str(root)}


def uninstall_tree(root: Path) -> dict:
    manifest = read_json(safe_target(root, str(MANIFEST)))
    preserved = []
    allowed = {str(Path(folder) / name) for name, folder in (INTEGRATION | ICONS).items()} | {"usr/local/bin/mount-medic"}
    for relative, checksum in manifest.items():
        if not relative.startswith(str(LIBRARY) + "/") and relative not in allowed:
            raise MedicError("Manifest includes an unexpected installation path")
        path = safe_target(root, relative)
        if not path.exists():
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
            preserved.append(str(path))
            continue
        path.unlink()
    for path in (root / "var/lib/mount-medic").glob("*.json"):
        settings = read_json(path)
        for entry in settings.values():
            entry.update(auto_repair=False, auto_mount=False)
        atomic_json(path, settings)
    if not preserved:
        (root / MANIFEST).unlink(missing_ok=True)
    refresh_icons(root)
    return {"removed": len(manifest) - len(preserved), "preserved_modified_files": preserved,
            "note": "Write authorizations revoked; preferences and diagnostic history retained."}


def refresh_icons(root: Path) -> None:
    # Icon themes watch this directory's mtime, not individual nested SVG files.
    directory = safe_target(root, "usr/local/share/icons/hicolor")
    if directory.is_dir():
        os.utime(directory, None)


def autostart(enable: bool) -> None:
    config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    path = config / "autostart/io.github.aakashH242.MountMedic.desktop"
    if enable:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(Path("/usr/local/lib/mount-medic/autostart.desktop").read_text())
    elif path.exists() and "Name=Mount Medic\n" in path.read_text():
        path.unlink()


def uninstall_interactive() -> dict:
    if not ask("Remove Mount Medic's installed files and revoke its automatic repair approvals?"):
        raise MedicError("Uninstall cancelled")
    command = elevated(["/usr/local/lib/mount-medic/installer", "--remove"])
    subprocess.run(command, check=True)
    autostart(False)
    return {"uninstalled": True, "history": "Preserved"}


def guided(source: Path) -> None:
    if os.geteuid() == 0:
        raise MedicError("Start install.sh as your normal user. It requests administrator access only when needed.")
    print("Mount Medic installs a native app, a read-only NTFS probe, an on-demand root worker,\n"
          "and narrowly scoped D-Bus/polkit policies. No drive is enrolled automatically.\n"
          "Dependencies: " + dependency_command())
    print("On Arch, the dependency command performs a full system upgrade.")
    if ask("Install the listed dependencies using your package manager?"):
        package_command = PACKAGES.get(family())
        if not package_command:
            raise MedicError("Install the dependencies manually on this distribution")
        subprocess.run(elevated(package_command), check=True)
    subprocess.run(["make", "all"], cwd=source, check=True)
    print("Files to install:\n" + "\n".join("/" + name for name in payload(source)))
    if not ask("Install these files and the disclosed privileged integration?"):
        raise MedicError("Installation cancelled")
    subprocess.run(elevated([sys.executable, str(source / "install.py"), "--apply", str(source)]), check=True)
    setup_notifications()
    if ask("Start Mount Medic automatically after graphical login?"):
        autostart(True)
    print("Installed. Open Mount Medic from the application menu to choose drives. No disks were checked or repaired.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Mount Medic guided installation")
    parser.add_argument("--apply", type=Path)
    parser.add_argument("--remove", action="store_true")
    parser.add_argument("--destdir", type=Path, default=Path("/"), help="Disposable staging directory for installer tests")
    args = parser.parse_args()
    try:
        root = args.destdir.resolve()
        if (args.apply or args.remove) and root == Path("/") and os.geteuid() != 0:
            raise MedicError("System installation requires administrator authentication")
        if not (args.apply or args.remove):
            guided(Path(__file__).resolve().parent.parent)
            return 0
        from .safety import operation_lock
        from .worker import secure_state
        if root == Path("/"):
            secure_state()
        with operation_lock(Path("/var/lib/mount-medic")) if root == Path("/") else nullcontext():
            if root == Path("/"):
                for process in Path("/proc").glob("[0-9]*/cmdline"):
                    try:
                        if b"/usr/local/lib/mount-medic/worker" in process.read_bytes().split(b"\0"):
                            raise MedicError("The worker is still running. Close Mount Medic, wait 30 seconds, and retry installation.")
                    except FileNotFoundError:
                        continue
            result = install_tree(args.apply.resolve(), root) if args.apply else uninstall_tree(root)
            print(json.dumps(result))
        return 0
    except (MedicError, OSError, subprocess.CalledProcessError) as error:
        print(f"Mount Medic installer: {error}", file=sys.stderr)
        return 2
