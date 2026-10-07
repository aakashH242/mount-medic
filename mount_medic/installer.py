import argparse
from contextlib import nullcontext
import ctypes
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import tempfile
import stat

from .dependencies import PACKAGES, dependency_command, family, missing_packages, package_install_command
from .model import MedicError
from .protocol import BUS_NAME
from .storage import Preferences, atomic_json, notification_seconds, read_json
from .releases import ReleaseError, extract_archive, fetch_release, verify_archive, version, MAX_ARCHIVE

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
    f"{BUS_NAME}-symbolic.svg": "usr/local/share/icons/hicolor/symbolic/apps",
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
    raise SystemExit('Mount Medic update was interrupted. Run: sudo /usr/local/lib/mount-medic/installer --recover')"""


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


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_file(path: Path, contents: tuple) -> None:
    content, mode = contents
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
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


def external_paths() -> set:
    return {str(Path(folder) / name) for name, folder in (INTEGRATION | ICONS).items()} | {"usr/local/bin/mount-medic"}


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
            if relative not in external_paths():
                raise MedicError("Unexpected recovery path")
            target = safe_target(root, relative)
            if old is None:
                target.unlink(missing_ok=True)
                sync_directory(target.parent)
            else:
                backup = safe_target(root, str(TRANSACTION / "backup" / relative))
                write_file(target, (backup.read_bytes(), old))
    shutil.rmtree(transaction)
    sync_directory(transaction.parent)
    refresh_icons(root)
    return {"recovered": True, "committed": saved.get("committed", False)}


def install_tree(source: Path, root: Path) -> dict:
    recover_install(root)
    files = payload(source)
    library = safe_target(root, str(LIBRARY))
    transaction = safe_target(root, str(TRANSACTION))
    transaction.mkdir(parents=True, mode=0o700)
    staged = transaction / "library"
    manifest = {}
    try:
        if library.exists():
            for path in library.rglob("*"):
                safe_target(root, str(path.relative_to(root)))
                if not (path.is_dir() or path.is_file()):
                    raise MedicError("Unexpected installed file type")
        staged.mkdir(mode=0o755)
        previous = {}
        for relative, contents in files.items():
            target = safe_target(root, relative)
            if relative.startswith(str(LIBRARY) + "/"):
                write_file(staged / Path(relative).relative_to(LIBRARY), contents)
            elif relative in external_paths():
                previous[relative] = stat.S_IMODE(target.stat().st_mode) if target.exists() else None
                if target.exists():
                    write_file(transaction / "backup" / relative, (target.read_bytes(), previous[relative]))
            else:
                raise MedicError("Unexpected installation path")
            manifest[relative] = hashlib.sha256(contents[0]).hexdigest()
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
            write_file(safe_target(root, relative), files[relative])
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
    allowed = external_paths()
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
    setup_notifications()
    if ask("Start Mount Medic automatically after graphical login?"):
        autostart(True)
    print("Installed. Open Mount Medic from the application menu to choose drives. No disks were checked or repaired.")


def prepare_install(source: Path, uid: int, requirements: dict | None = None) -> None:
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
    print("Files to install:\n" + "\n".join("/" + name for name in payload(source)))


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
        prepare_install(source, builder, {**release, "approved_packages": args.approved_packages})
        os.chown(build, 0, 0)
        build.chmod(0o700)
        binary = build / "mount-medic-probe"
        descriptor = os.open(binary, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= MAX_ARCHIVE:
                raise MedicError("Invalid native build output")
            compiled = stream.read(MAX_ARCHIVE + 1)
        write_file(binary, (compiled, 0o755))
        result = install_tree(source, Path("/"))
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
