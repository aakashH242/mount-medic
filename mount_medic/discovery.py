from collections import Counter
import os
from pathlib import Path
import re

from .commands import executable, json_command, run
from .model import Identity, MedicError, Volume

LSBLK_COLUMNS = "PATH,TYPE,FSTYPE,UUID,PARTUUID,SIZE,START,WWN,SERIAL,MODEL,TRAN,MAJ:MIN,MOUNTPOINTS,RO"


class VolumeUnavailable(MedicError):
    pass


def hardware_identity(disk: dict) -> str:
    known = disk.get("wwn") or disk.get("serial")
    if known:
        return str(known)
    number = disk.get("maj:min", "")
    if not re.fullmatch(r"[0-9]+:[0-9]+", number):
        return ""
    # Some util-linux builds omit udev serials; use the kernel's disk identity.
    for suffix in ("serial", "device/serial"):
        try:
            serial = (Path("/sys/dev/block") / number / suffix).read_text().strip()
            if serial:
                return serial
        except FileNotFoundError:
            continue
    return ""


def udev_filesystem_metadata(node: dict) -> dict:
    node = dict(node)
    if (not node.get("fstype") or not node.get("uuid")) and node.get("type") in {"disk", "part"}:
        result = run([executable("udevadm"), "info", "--query=property", "--name", node["path"]])
        if result.returncode:
            raise MedicError("Cannot read device metadata from udev")
        properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        for field, key in (("fstype", "ID_FS_TYPE"), ("uuid", "ID_FS_UUID"),
                           ("partuuid", "ID_PART_ENTRY_UUID"), ("label", "ID_FS_LABEL")):
            if not node.get(field):
                node[field] = properties.get(key)
    node["children"] = [udev_filesystem_metadata(child) for child in node.get("children", [])]
    return node


def parse_devices(tree: list[dict]) -> list[Volume]:
    found = {}

    def visit(node: dict, ancestors: list[dict]) -> None:
        lineage = [*ancestors, node]
        if (node.get("fstype") or "").lower() in {"ntfs", "ntfs3"}:
            disks = [item for item in lineage if item.get("type") == "disk"]
            disk = disks[0] if len(disks) == 1 else {}
            identity = Identity(str(node.get("uuid") or ""),
                                hardware_identity(disk),
                                str(node.get("partuuid") or ""),
                                int(node.get("size") or 0), int(node.get("start") or 0))
            supported = bool(disk) and all(item.get("type") in {"disk", "part"} for item in lineage)
            volume = Volume(identity, str(node["path"]), str(node.get("label") or ""),
                            str(disk.get("model") or ""), str(disk.get("tran") or ""),
                            str(node.get("maj:min") or ""),
                            [value for value in node.get("mountpoints", []) or [] if value],
                            bool(node.get("ro")), supported)
            found[volume.device] = volume
        for child in node.get("children", []):
            visit(child, lineage)

    for node in tree:
        visit(node, [])
    counts = Counter(volume.identity.uuid for volume in found.values())
    for volume in found.values():
        volume.duplicate = counts[volume.identity.uuid] > 1
    return list(found.values())


def discover() -> list[Volume]:
    payload = json_command([executable("lsblk"), "--json", "--tree", "--bytes", "--output", LSBLK_COLUMNS + ",LABEL"])
    devices = payload.get("blockdevices", [])
    if os.geteuid() != 0:
        # Alpine's util-linux can lack udev linkage. Query the existing udev
        # database rather than opening devices or requiring discovery privilege.
        devices = [udev_filesystem_metadata(node) for node in devices]
    volumes = parse_devices(devices)
    mounts = json_command([executable("findmnt"), "--json", "--output", "SOURCE,TARGET,OPTIONS,MAJ:MIN"])

    mount_modes = {volume.key: [] for volume in volumes}

    def visit(entries):
        for entry in entries:
            source = os.path.realpath(entry.get("source", ""))
            for volume in volumes:
                if source == volume.device or entry.get("maj:min") == volume.devnum:
                    target = entry.get("target")
                    if target and target not in volume.mounts:
                        volume.mounts.append(target)
                    mount_modes[volume.key].append("ro" in entry.get("options", "").split(","))
            visit(entry.get("children", []))

    visit(mounts.get("filesystems", []))
    for volume in volumes:
        volume.mount_readonly = bool(mount_modes[volume.key]) and all(mount_modes[volume.key])
    return volumes


def resolve(volume_id: str) -> Volume:
    matches = [volume for volume in discover() if volume.key == volume_id]
    if len(matches) != 1:
        raise VolumeUnavailable("Volume is absent or its identity is ambiguous; select it again")
    return matches[0]


def disk_sequence(volume: Volume) -> str:
    path = Path("/sys/dev/block") / volume.devnum
    resolved = path.resolve(strict=True)
    if (resolved / "partition").exists():
        resolved = resolved.parent
    return (resolved / "diskseq").read_text().strip()
