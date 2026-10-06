import json
import os
from pathlib import Path

from .commands import executable
from .discovery import discover, resolve
from .model import MedicError, diagnosis
from .protocol import BUS_NAME, BUS_PATH, INTERFACE, validate


def call(request: dict):
    validate(request)
    if os.geteuid() == 0:
        from .engine import Engine
        from .worker import secure_state
        secure_state()
        return Engine().dispatch(int(os.environ.get("SUDO_UID", "0")), request)
    try:
        from gi.repository import Gio, GLib
        connection = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        reply = connection.call_sync(BUS_NAME, BUS_PATH, INTERFACE, "Call",
                                     GLib.Variant("(s)", (json.dumps(request),)), GLib.VariantType("(s)"),
                                     Gio.DBusCallFlags.NONE, 2_147_483_647, None)
        return json.loads(reply.unpack()[0])
    except (ImportError, ValueError) as error:
        raise MedicError("The desktop bridge is unavailable; run mount-medic doctor") from error
    except Exception as error:
        raise MedicError(str(error)) from error


def list_volumes() -> list[dict]:
    try:
        return call({"op": "list"})
    except MedicError:
        return [diagnosis(volume, "unmanaged", "Worker unavailable; discovery only") for volume in discover()]


def repair(request: dict) -> dict:
    report = call(request)
    if report.get("mount_requested"):
        try:
            report["mount"] = udisks_operation(request["id"], "Mount", request["op"] == "automatic")
            report.update(volume=report["mount"]["volume"], state=report["mount"]["observed_state"], actions=[],
                          next_steps="Limited repair verified; mounted and directory access confirmed.")
        except Exception as error:
            report["mount"] = {"state": "failed", "evidence": str(error)}
            report["next_steps"] = "Limited repair completed, but mounting failed: " + str(error)
    return report


def udisks_operation(volume_id: str, operation: str, background: bool = False) -> dict:
    from gi.repository import Gio, GLib
    if operation == "Mount":
        call({"op": "prepare_mount", "id": volume_id, "background": background})
    volume = resolve(volume_id)
    bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    managed = bus.call_sync("org.freedesktop.UDisks2", "/org/freedesktop/UDisks2",
                           "org.freedesktop.DBus.ObjectManager", "GetManagedObjects", None,
                           GLib.VariantType("(a{oa{sa{sv}}})"), Gio.DBusCallFlags.NONE, 15000, None).unpack()[0]
    paths = []
    for path, interfaces in managed.items():
        block = interfaces.get("org.freedesktop.UDisks2.Block", {})
        device = bytes(block.get("Device", [])).rstrip(b"\0").decode(errors="replace")
        if device == volume.device and block.get("IdUUID", "") == volume.identity.uuid:
            paths.append(path)
    if len(paths) != 1:
        raise MedicError("UDisks identity changed or could not be verified")
    if resolve(volume_id).devnum != volume.devnum:
        raise MedicError("Device changed before the UDisks operation")
    options = {"auth.no_user_interaction": GLib.Variant("b", background)}
    result = bus.call_sync("org.freedesktop.UDisks2", paths[0], "org.freedesktop.UDisks2.Filesystem",
                          operation, GLib.Variant("(a{sv})", (options,)), None,
                          Gio.DBusCallFlags.NONE, 120000, None).unpack()
    current = resolve(volume_id)
    if operation == "Mount":
        mountpoint = result[0]
        if mountpoint not in current.mounts or not os.access(mountpoint, os.R_OK | os.X_OK):
            raise MedicError("Mount returned but directory access could not be verified")
        with os.scandir(mountpoint) as entries:
            next(entries, None)
        return {"state": "mounted", "mountpoint": mountpoint, "volume": current.as_dict(),
                "observed_state": "mounted_ro" if current.mount_readonly else "mounted_rw"}
    if current.mounts:
        raise MedicError("The volume is still mounted")
    return {"state": "unmounted"}


def doctor() -> dict:
    from .dependencies import dependency_command
    needed = ["lsblk", "findmnt", "ntfsfix", "udevadm"]
    missing = []
    for name in needed:
        try:
            executable(name)
        except MedicError:
            missing.append(name)
    capabilities = {}
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # noqa: F401
        capabilities["gtk"] = True
    except (ImportError, ValueError):
        capabilities["gtk"] = False
    capabilities["installed_probe"] = Path("/usr/local/lib/mount-medic/mount-medic-probe").is_file()
    try:
        from gi.repository import Gio, GLib
        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        names = set()
        for method in ("ListNames", "ListActivatableNames"):
            names.update(bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                      method, None, GLib.VariantType("(as)"), Gio.DBusCallFlags.NONE, 5000, None).unpack()[0])
        capabilities.update(system_bus=True, worker=BUS_NAME in names,
                            polkit="org.freedesktop.PolicyKit1" in names, udisks="org.freedesktop.UDisks2" in names)
    except Exception:
        capabilities.update(system_bus=False, worker=False, polkit=False, udisks=False)
    return {"state": "ready" if not missing and all(capabilities.values()) else "unavailable",
            "missing_tools": missing, "capabilities": capabilities,
            "dependency_command": dependency_command(),
            "note": "CLI discovery works without desktop services. Unattended repair requires the installed worker and polkit."}
