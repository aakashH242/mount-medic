from contextlib import contextmanager
import configparser
import fcntl
import os
from pathlib import Path
import re
import stat
import time

from .commands import executable, run
from .discovery import disk_sequence, resolve
from .model import MedicError, Volume


def unescape_mount(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def mounted_in_text(text: str, volume: Volume) -> bool:
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 10 or "-" not in fields:
            raise MedicError("Cannot interpret mount namespace information")
        separator = fields.index("-")
        source = unescape_mount(fields[separator + 2])
        if fields[2] == volume.devnum or source == volume.device:
            return True
        if source.startswith("/dev/"):
            try:
                info = os.stat(source)
                if stat.S_ISBLK(info.st_mode) and f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}" == volume.devnum:
                    return True
            except FileNotFoundError:
                continue
    return False


def descriptor_device(descriptor: Path) -> str:
    target = os.readlink(descriptor)
    # procfs labels these kernel handles; none can be a block device. SELinux
    # may deny getattr on unrelated sockets or anonymous inodes such as io_uring.
    if target.startswith("anon_inode:") or re.fullmatch(r"(?:socket|pipe):\[[0-9]+\]", target):
        return ""
    info = descriptor.stat()
    return f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}" if stat.S_ISBLK(info.st_mode) else ""


def assert_unused(volume: Volume) -> None:
    if volume.mounts:
        raise MedicError("Volume is mounted; explicitly unmount it first")
    if any((Path("/sys/dev/block") / volume.devnum / "holders").iterdir()):
        raise MedicError("Volume has active storage holders")
    seen_namespaces = set()
    deadline = time.monotonic() + 15
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        if time.monotonic() > deadline:
            raise MedicError("Active-use verification timed out")
        try:
            namespace = (process / "ns/mnt").stat().st_ino
            if namespace not in seen_namespaces:
                if mounted_in_text((process / "mountinfo").read_text(), volume):
                    raise MedicError("Volume is mounted in a process mount namespace")
                seen_namespaces.add(namespace)
            for descriptor in (process / "fd").iterdir():
                try:
                    number = descriptor_device(descriptor)
                except FileNotFoundError:
                    continue
                if number == volume.devnum:
                    raise MedicError("Volume is open by another process")
        except FileNotFoundError:
            continue  # A process or descriptor can disappear during enumeration.
        except PermissionError as error:
            target = ""
            try:
                target = os.readlink(error.filename)
            except OSError:
                pass
            raise MedicError(f"Cannot verify all mount namespaces and active users: {error.filename} {target}") from error


def require_supported(volume: Volume) -> None:
    if volume.duplicate or not volume.supported or not volume.identity.strong:
        raise MedicError("Ambiguous, weak, or unsupported volume identity")
    if volume.readonly:
        raise MedicError("Block device is read-only")


@contextmanager
def claim(volume: Volume):
    assert_unused(volume)
    sequence = disk_sequence(volume)
    descriptor = os.open(volume.device, os.O_RDONLY | os.O_EXCL | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        actual = f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}"
        if not stat.S_ISBLK(info.st_mode) or actual != volume.devnum:
            raise MedicError("Target is not the expected block device")
        current = resolve(volume.key)
        if current.device != volume.device or disk_sequence(current) != sequence or current.mounts:
            raise MedicError("Device changed while acquiring exclusive access")
        # Native tools reopen this inherited descriptor, never an old /dev name.
        yield descriptor
        if disk_sequence(resolve(volume.key)) != sequence:
            raise MedicError("Device changed during the operation")
    finally:
        os.close(descriptor)


@contextmanager
def operation_lock(directory: Path):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(directory / "operation.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            # ponytail: one global disk-operation lock; per-device locks only if needed.
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise MedicError("Another Mount Medic operation is running") from error
        yield
    finally:
        os.close(descriptor)


def prohibited_options(options: str) -> bool:
    return bool({"force", "remove_hiberfile"} & {part.strip().split("=", 1)[0] for part in options.split(",")})


def udisks_defaults(volume: Volume) -> dict:
    configured = {}
    options = Path("/etc/udisks2/mount_options.conf")
    if options.exists():
        parser = configparser.ConfigParser(interpolation=None, delimiters=("=",), strict=False)
        try:
            parser.read_string(options.read_text())
        except configparser.Error as error:
            raise MedicError("Cannot interpret UDisks mount configuration") from error
        if parser.has_section("defaults"):
            configured.update(parser["defaults"])
        for section in parser.sections():
            if section.startswith("/dev/") and os.path.realpath(section) == volume.device:
                configured.update(parser[section])
    properties = run([executable("udevadm"), "info", "--query=property", "--name", volume.device])
    if properties.returncode:
        raise MedicError("Cannot verify UDisks device-specific mount options")
    for line in properties.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.startswith("UDISKS_MOUNT_OPTIONS_"):
            configured[key.removeprefix("UDISKS_MOUNT_OPTIONS_").lower()] = value
    return configured


def assert_safe_mount_options(volume: Volume) -> None:
    import json
    result = run([executable("findmnt"), "--fstab", "--evaluate", "--json",
                  "--output", "SOURCE,OPTIONS"])
    if result.returncode not in (0, 1) or (result.returncode == 1 and result.stderr):
        raise MedicError("Cannot verify fstab mount options")
    table = json.loads(result.stdout) if result.stdout.strip() else {}
    matched = False
    for entry in table.get("filesystems", []):
        if os.path.realpath(entry.get("source", "")) == volume.device:
            matched = True
            if prohibited_options(entry.get("options", "")):
                raise MedicError("Existing fstab entry requests an unsafe mount option")
    if matched:
        return  # UDisks uses fstab options exclusively for these devices.
    for key, value in udisks_defaults(volume).items():
        if key == "defaults" or (key.startswith("ntfs") and key.endswith("_defaults")):
            if prohibited_options(value):
                raise MedicError("UDisks mount defaults request a prohibited option")
