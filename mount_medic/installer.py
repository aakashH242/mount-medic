import argparse
import base64
import configparser
from contextlib import nullcontext
import ctypes
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import stat

from .dependencies import PACKAGES, dependency_command, family, missing_packages, package_install_command
from .model import MedicError
from .protocol import BUS_NAME, authorization_hours
from .storage import Preferences, atomic_json, notification_seconds, read_json
from .releases import ReleaseError, create_public_directory, extract_archive, fetch_release, sync_directory, verify_archive, version, MAX_ARCHIVE, MAX_SOURCE

LIBRARY = Path("usr/local/lib/mount-medic")
MANIFEST = LIBRARY / "manifest.json"
TRANSACTION = LIBRARY.parent / ".mount-medic-transaction"
INTEGRATION = {
    "io.github.aakashH242.MountMedic.service": "usr/share/dbus-1/system-services",
    "io.github.aakashH242.MountMedic.conf": "usr/share/dbus-1/system.d",
    "io.github.aakashH242.mount-medic.policy": "usr/share/polkit-1/actions",
    "io.github.aakashH242.MountMedic.desktop": "usr/local/share/applications",
}
ICONS = {
    f"{BUS_NAME}.png": "usr/local/share/icons/hicolor/256x256/apps",
    f"{BUS_NAME}-symbolic.svg": "usr/local/share/icons/hicolor/scalable/apps",
}
WRAPPER = """#!/usr/bin/python3 -IB
import sys
sys.path.insert(0, '/usr/local/lib/mount-medic')
{guard}
from mount_medic.{module} import main
raise SystemExit(main())
"""
# Desktop users can stat the transaction directory but cannot read its private journal.
MAINTENANCE = """from pathlib import Path
if Path('/usr/local/lib/.mount-medic-transaction').exists():
    raise SystemExit('Mount Medic installation is incomplete. Re-run the guided installer, or run: sudo /usr/local/lib/mount-medic/installer --recover')"""


def ask(message: str) -> bool:
    return sys.stdin.isatty() and input(message + " [y/N] ").strip().lower() == "y"


def setup_notifications() -> None:
    preferences = Preferences()
    settings = {"notification_seconds": preferences.notification_seconds(), "toast_seconds": preferences.toast_seconds(),
                "notification_sound": preferences.notification_sound_enabled()}
    if sys.stdin.isatty():
        for key, title in (("notification_seconds", "Desktop notification"), ("toast_seconds", "In-app message")):
            while True:
                answer = input(f"{title} duration in seconds, 1–600 [{settings[key]}]: ").strip()
                try:
                    settings[key] = notification_seconds(int(answer) if answer else settings[key])
                    break
                except (ValueError, MedicError):
                    print("Enter a whole number from 1 to 600, or press Enter to keep the default.")
        while True:
            default = "Y/n" if settings["notification_sound"] else "y/N"
            answer = input(f"Play notification sounds? [{default}] ").strip().lower()
            if answer in {"", "y", "yes", "n", "no"}:
                if answer:
                    settings["notification_sound"] = answer in {"y", "yes"}
                break
            print("Enter yes or no, or press Enter to keep the current setting.")
    preferences.set_notification_settings(settings)


def setup_security() -> None:
    preferences = Preferences()
    hours = preferences.authorization_hours()
    if sys.stdin.isatty():
        while True:
            answer = input(f"Remember administrator approval in hours, 1–24 [{hours}]: ").strip()
            try:
                hours = authorization_hours(int(answer) if answer else hours)
                break
            except (ValueError, MedicError):
                print("Enter a whole number from 1 to 24, or press Enter to keep the current setting.")
    preferences.set_authorization_hours(hours)


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
    files["usr/local/bin/mount-medic"] = (WRAPPER.format(module="cli", guard=MAINTENANCE).encode(), 0o755)
    for name in ("worker", "installer"):
        files[str(LIBRARY / name)] = (WRAPPER.format(module=name, guard=MAINTENANCE if name == "worker" else "").encode(), 0o755)
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


def write_file(path: Path, contents: tuple) -> None:
    content, mode = contents
    create_public_directory(path.parent)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            os.fchmod(stream.fileno(), mode)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def exchange_directories(first: Path, second: Path) -> None:
    # Linux exchanges complete generations without a missing-library window.
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        exchange = libc.renameat2
    except AttributeError as error:
        raise MedicError("Atomic directory exchange is unavailable; installation left unchanged") from error
    exchange.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    exchange.restype = ctypes.c_int
    if exchange(-100, os.fsencode(first), -100, os.fsencode(second), 2):  # AT_FDCWD, RENAME_EXCHANGE
        raise OSError(ctypes.get_errno(), "Cannot atomically exchange installation directories")
    sync_directory(first.parent)
    sync_directory(second.parent)


def external_path(relative: str) -> bool:
    if relative == "usr/local/bin/mount-medic":
        return True
    path = Path(relative)
    name = path.name.lower()
    if not name.startswith((BUS_NAME.lower() + ".", BUS_NAME.lower() + "-", "io.github.aakashh242.mount-medic.")):
        return False
    suffixes = {"usr/share/dbus-1/system-services": ".service", "usr/share/dbus-1/system.d": ".conf",
                "usr/share/polkit-1/actions": ".policy", "usr/local/share/applications": ".desktop"}
    folder = path.parent.as_posix()
    if folder in suffixes:
        return name.endswith(suffixes[folder])
    return bool(re.fullmatch(r"usr/local/share/icons/hicolor/(?:[0-9]{1,4}x[0-9]{1,4}|scalable|symbolic)/apps", folder)) and path.suffix in {".png", ".svg"}


def validate_payload_path(relative: str) -> None:
    if not isinstance(relative, str) or Path(relative).as_posix() != relative or ".." in Path(relative).parts or "\0" in relative:
        raise MedicError("Invalid installation payload path")
    if relative == str(MANIFEST) or not (relative.startswith(str(LIBRARY) + "/") or external_path(relative)):
        raise MedicError("Unexpected installation path")


def decode_payload(data: bytes) -> dict:
    try:
        saved = json.loads(data)
        if not isinstance(saved, dict) or not 1 <= len(saved) <= 1000:
            raise ValueError("Invalid file count")
        files = {}
        total = 0
        for relative, entry in saved.items():
            validate_payload_path(relative)
            if not isinstance(entry, list) or len(entry) != 2 or type(entry[1]) is not int or entry[1] not in (0o644, 0o755):
                raise ValueError("Invalid file mode")
            content = base64.b64decode(entry[0], validate=True)
            total += len(content)
            if total > MAX_SOURCE + MAX_ARCHIVE:
                raise ValueError("Payload too large")
            files[relative] = (content, entry[1])
        return files
    except (ValueError, TypeError) as error:
        raise MedicError(f"Invalid target installation payload: {error}") from error


def recover_install(root: Path) -> dict:
    transaction = safe_target(root, str(TRANSACTION))
    saved = read_json(safe_target(root, str(TRANSACTION / "status.json")))
    if not saved:
        if transaction.exists():
            shutil.rmtree(transaction)
        return {"recovered": False}
    if saved.get("committed") is not True:
        library = safe_target(root, str(LIBRARY))
        staged = safe_target(root, str(TRANSACTION / "library"))
        if library.exists() and library.stat().st_ino == saved["new_inode"]:
            if saved["had_library"]:
                exchange_directories(library, staged)
            else:
                os.rename(library, staged)
                sync_directory(library.parent)
        for relative, old in saved["external"].items():
            validate_payload_path(relative)
            if not external_path(relative):
                raise MedicError("Unexpected recovery path")
            target = safe_target(root, relative)
            if old is None:
                target.unlink(missing_ok=True)
                if target.parent.exists():
                    sync_directory(target.parent)
            else:
                backup = safe_target(root, str(TRANSACTION / "backup" / relative))
                write_file(target, (backup.read_bytes(), old))
    shutil.rmtree(transaction)
    sync_directory(transaction.parent)
    refresh_icons(root)
    return {"recovered": True, "committed": saved.get("committed", False)}


def install_tree(source: Path, root: Path) -> dict:
    return install_payload(payload(source), root)


def install_payload(files: dict, root: Path) -> dict:
    recover_install(root)
    library = safe_target(root, str(LIBRARY))
    transaction = safe_target(root, str(TRANSACTION))
    create_public_directory(transaction.parent)
    transaction.mkdir(mode=0o700)
    staged = transaction / "library"
    manifest = {}
    try:
        if library.exists():
            for path in library.rglob("*"):
                safe_target(root, str(path.relative_to(root)))
                if not (path.is_dir() or path.is_file()):
                    raise MedicError("Unexpected installed file type")
        create_public_directory(staged)
        previous = {}
        for relative, contents in files.items():
            validate_payload_path(relative)
            target = safe_target(root, relative)
            if relative.startswith(str(LIBRARY) + "/"):
                write_file(staged / Path(relative).relative_to(LIBRARY), contents)
            else:
                previous[relative] = stat.S_IMODE(target.stat().st_mode) if target.exists() else None
                if target.exists():
                    write_file(transaction / "backup" / relative, (target.read_bytes(), previous[relative]))
            manifest[relative] = hashlib.sha256(contents[0]).hexdigest()
        # Only remove obsolete integration we still own; preserve local edits.
        for relative, checksum in read_json(safe_target(root, str(MANIFEST))).items():
            validate_payload_path(relative)
            if relative in files or not external_path(relative):
                continue
            target = safe_target(root, relative)
            if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == checksum:
                previous[relative] = stat.S_IMODE(target.stat().st_mode)
                write_file(transaction / "backup" / relative, (target.read_bytes(), previous[relative]))
        atomic_json(staged / "manifest.json", manifest)
        # Staged files and directory entries must survive a power loss before switching.
        for path in staged.rglob("*"):
            if path.is_file():
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
        for directory in sorted((path for path in staged.rglob("*") if path.is_dir()), reverse=True):
            sync_directory(directory)
        sync_directory(staged)
        journal = {"new_inode": staged.stat().st_ino, "had_library": library.exists(), "external": previous}
        for directory in sorted((path for path in transaction.rglob("*") if path.is_dir()), reverse=True):
            sync_directory(directory)
        sync_directory(transaction.parent)
        atomic_json(transaction / "status.json", journal)
        if root == Path("/"):
            require_idle()
        for relative in previous:
            target = safe_target(root, relative)
            if relative in files:
                write_file(target, files[relative])
            else:
                target.unlink()
                sync_directory(target.parent)
        if library.exists():
            exchange_directories(library, staged)
        else:
            os.rename(staged, library)
            sync_directory(library.parent)
        atomic_json(transaction / "status.json", {**journal, "committed": True})
    except BaseException:
        recover_install(root)
        raise
    recover_install(root)
    return {"installed": len(files), "root": str(root)}


def uninstall_tree(root: Path) -> dict:
    recover_install(root)
    manifest = read_json(safe_target(root, str(MANIFEST)))
    preserved = []
    for relative, checksum in manifest.items():
        validate_payload_path(relative)
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


def autostart_path() -> Path:
    config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config / f"autostart/{BUS_NAME}.desktop"


def autostart_enabled() -> bool:
    path = autostart_path()
    if not path.exists():
        return False
    entry = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        entry.read_string(path.read_text())
        return (entry.get("Desktop Entry", "Name", fallback="") == "Mount Medic"
                and not entry.getboolean("Desktop Entry", "Hidden", fallback=False)
                and entry.getboolean("Desktop Entry", "X-GNOME-Autostart-enabled", fallback=True))
    except (configparser.Error, ValueError) as error:
        raise MedicError(f"Cannot read startup settings: {error}") from error


def autostart(enable: bool) -> None:
    path = autostart_path()
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
    platform = family()
    missing = missing_packages(platform)
    print("Mount Medic installs a native app and read-only probe in /usr/local/lib/mount-medic,\n"
          "the /usr/local/bin/mount-medic launcher, desktop icons, and narrowly scoped\n"
          "D-Bus/polkit integration. No drive is enrolled automatically.")
    if platform in PACKAGES:
        print("Missing packages: " + (", ".join(missing) if missing else "none"))
    else:
        print("Install dependencies manually on this distribution: " + dependency_command())
    if missing:
        print("Automatic dependency installation: " + " ".join(package_install_command(platform, missing)))
        if platform == "arch":
            print("On Arch, this includes a full system upgrade.")
    if not ask("Install Mount Medic and any listed missing dependencies with one administrator authentication?"):
        raise MedicError("Installation cancelled")
    # Container tests can leave root-owned build files; never build in the checkout.
    with tempfile.TemporaryDirectory(prefix="mount-medic-install-") as directory:
        build_source = Path(directory)
        for name in ("mount_medic", "native", "integration"):
            shutil.copytree(source / name, build_source / name, symlinks=True,
                            ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("Makefile", "install.py", "LICENSE"):
            shutil.copy2(source / name, build_source / name)
        subprocess.run(elevated([sys.executable, str(build_source / "install.py"), "--setup", str(build_source),
                                "--build-user", str(os.getuid())]), check=True)
    Preferences().save_updates({"install_error": None})
    setup_notifications()
    setup_security()
    if ask("Start Mount Medic automatically after graphical login?"):
        autostart(True)
    print("Installed. Open Mount Medic from the application menu to choose drives. No disks were checked or repaired.")


def prepare_install(source: Path, uid: int, requirements: dict | None = None) -> dict:
    if uid <= 0:
        raise MedicError("The native probe must be built as a normal user")
    if requirements:
        group, groups, home = uid, [], str(source / "build")
    else:
        try:
            user = pwd.getpwuid(uid)
        except KeyError as error:
            raise MedicError("The build user does not exist") from error
        group, groups, home = user.pw_gid, os.getgrouplist(user.pw_name, user.pw_gid), user.pw_dir
    environment = {"PATH": "/usr/bin:/usr/sbin:/bin:/sbin", "HOME": home, "LC_ALL": "C"}
    platform = family()
    required = requirements["packages"].get(platform) if requirements else None
    missing = missing_packages(platform, required) if required else missing_packages(platform)
    if requirements and set(missing) - set(requirements["approved_packages"]):
        raise MedicError("Required dependencies changed. Check the update again and review its package changes.")
    if missing:
        subprocess.run(package_install_command(platform, missing), check=True,
                       env={**environment, "HOME": "/root"})
        if (missing_packages(platform, required) if required else missing_packages(platform)):
            raise MedicError("Required packages are still missing after dependency installation")
    subprocess.run(["make", "all"], cwd=source, check=True, env=environment,
                   user=uid, group=group, extra_groups=groups)
    if requirements:
        # Keep payload(source) as the unprivileged handoff across updater versions.
        code = ("import base64,json,sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
                "from mount_medic.installer import payload; files=payload(Path(sys.argv[1])); "
                "Path(sys.argv[1],'build','payload.json').write_text(json.dumps("
                "{name:[base64.b64encode(content).decode('ascii'),mode] for name,(content,mode) in files.items()}))")
        subprocess.run([sys.executable, "-IB", "-c", code, str(source)], check=True, env=environment,
                       user=uid, group=group, extra_groups=groups)
        files = decode_payload(read_build_output(source / "build/payload.json", MAX_SOURCE * 2))
    else:
        files = payload(source)
    print("Files to install:\n" + "\n".join("/" + name for name in files))
    return files


def read_build_output(path: Path, limit: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= limit:
            raise MedicError("Invalid build output")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise MedicError("Build output exceeds its size limit")
    return data


def build_uid() -> int:
    occupied = {account.pw_uid for account in pwd.getpwall()} | {group.gr_gid for group in grp.getgrall()}
    for process in Path("/proc").glob("[0-9]*/status"):
        try:
            for line in process.read_text().splitlines():
                if line.startswith(("Uid:", "Gid:", "Groups:")):
                    occupied.update(int(number) for number in line.split()[1:])
        except FileNotFoundError:
            continue
    # No account is created, and the operation lock serializes our transient build identities.
    # ponytail: local high UID range; use a distro build sandbox if its UID policy reserves this range.
    for uid in range(65533, 60000, -1):
        if uid in occupied:
            continue
        try:
            pwd.getpwuid(uid)
        except KeyError:
            try:
                grp.getgrgid(uid)
            except KeyError:
                return uid
    raise MedicError("No unused unprivileged build identity is available")


def upgrade_release(args) -> dict:
    from . import __version__
    release = fetch_release(args.upgrade)
    if release["sha256"] != args.sha256 or version(release["version"]) <= version(__version__):
        raise MedicError("The selected release changed or is not newer than the installed version")
    # No downloaded Python is executed as root. Only this installed helper owns installation.
    with tempfile.TemporaryDirectory(prefix="mount-medic-upgrade-", dir="/var/tmp") as directory:
        protected = Path(directory)
        descriptor = os.open(args.archive, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise MedicError("The update archive must be a regular file")
            data = stream.read(MAX_ARCHIVE + 1)
        verify_archive(data, release)
        archive = protected / "source.tar.gz"
        archive.write_bytes(data)
        verify_archive(archive.read_bytes(), release)
        source = extract_archive(archive, protected / "source", release)
        # Existing desktop users and services cannot write the build's private output.
        builder = build_uid()
        if builder == args.build_user:
            raise MedicError("A separate unprivileged build identity is required")
        protected.chmod(0o755)
        build = source / "build"
        if build.exists():
            raise MedicError("A release must contain source, not build output")
        build.mkdir(mode=0o700)
        os.chown(build, builder, builder)
        files = prepare_install(source, builder, {**release, "approved_packages": args.approved_packages})
        launchers = ("usr/local/bin/mount-medic", str(LIBRARY / "worker"), str(LIBRARY / "installer"))
        declaration = files.get(str(LIBRARY / "mount_medic/__init__.py"))
        if any(files.get(name, (None, None))[1] != 0o755 for name in launchers) or not declaration or declaration[0] != (source / "mount_medic/__init__.py").read_bytes():
            raise MedicError("Target payload is missing executable launchers or its matching version")
        os.chown(build, 0, 0)
        build.chmod(0o700)
        binary = build / "mount-medic-probe"
        compiled = read_build_output(binary, MAX_ARCHIVE)
        write_file(binary, (compiled, 0o755))
        files[str(LIBRARY / "mount-medic-probe")] = (compiled, 0o755)
        result = install_payload(files, Path("/"))
        return {**result, "version": release["version"]}


def require_idle() -> None:
    for process in Path("/proc").glob("[0-9]*/cmdline"):
        if process.parent.name == str(os.getpid()):
            continue
        try:
            arguments = process.read_bytes().split(b"\0")
            if b"/usr/local/lib/mount-medic/worker" in arguments or (
                    b"/usr/local/bin/mount-medic" in arguments and not {b"update", b"uninstall"}.intersection(arguments)):
                raise MedicError("Mount Medic is still running. Finish checks or repairs, quit the app, wait 30 seconds, and retry.")
        except FileNotFoundError:
            continue


def main() -> int:
    parser = argparse.ArgumentParser(description="Mount Medic guided installation")
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--apply", type=Path)
    operation.add_argument("--setup", type=Path)
    operation.add_argument("--remove", action="store_true")
    operation.add_argument("--upgrade", metavar="VERSION")
    operation.add_argument("--recover", action="store_true")
    parser.add_argument("--build-user", type=int)
    parser.add_argument("--sha256")
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--approved-packages", nargs="*", default=[])
    parser.add_argument("--destdir", type=Path, default=Path("/"), help="Disposable staging directory for installer tests")
    args = parser.parse_args()
    try:
        root = args.destdir.resolve()
        if (args.apply or args.setup or args.remove or args.upgrade or args.recover) and root == Path("/") and os.geteuid() != 0:
            raise MedicError("System installation requires administrator authentication")
        if args.build_user is not None and not (args.setup or args.upgrade):
            raise MedicError("--build-user is only valid with --setup or --upgrade")
        if args.upgrade and (root != Path("/") or not args.archive or not args.sha256 or not args.build_user or args.build_user <= 0):
            raise MedicError("--upgrade requires an archive, checksum and normal build user for a system installation")
        if (args.archive or args.sha256 or args.approved_packages) and not args.upgrade:
            raise MedicError("--archive and --sha256 require --upgrade")
        if args.setup and (root != Path("/") or args.build_user is None):
            raise MedicError("--setup requires a system installation and a normal build user")
        if not (args.apply or args.setup or args.remove or args.upgrade or args.recover):
            guided(Path(__file__).resolve().parent.parent)
            return 0
        from .safety import operation_lock
        from .worker import secure_state
        if root == Path("/"):
            secure_state()
        with operation_lock(Path("/var/lib/mount-medic")) if root == Path("/") else nullcontext():
            if root == Path("/"):
                require_idle()
            recover_install(root)
            if args.setup:
                prepare_install(args.setup.resolve(), args.build_user)
            source = args.setup or args.apply
            if args.upgrade:
                result = upgrade_release(args)
            elif args.recover:
                result = {"recovered": True}
            else:
                result = install_tree(source.resolve(), root) if source else uninstall_tree(root)
            print(json.dumps(result))
        return 0
    except (MedicError, ReleaseError, OSError, subprocess.SubprocessError, KeyError) as error:
        print(f"Mount Medic installer: {error}", file=sys.stderr)
        return 2
