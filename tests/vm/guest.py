"""Destructive checks, guarded to run ONLY against our 128 MiB guest disk."""
import hashlib
import json
import os
import pwd
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, "/usr/local/lib/mount-medic")
from mount_medic.discovery import discover, resolve
from mount_medic.engine import Engine
from mount_medic.model import MedicError
from mount_medic.safety import assert_unused, claim, operation_lock
from interrupted import verify_interruption

DEVICE = "/dev/vdb"


def command(arguments: list[str], success: bool = True):
    result = subprocess.run(arguments, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if success and result.returncode:
        raise AssertionError(f"{arguments}: {result.stdout}")
    return result


def disk_hash():
    with open(DEVICE, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def wait_unused(volume_id):
    for _ in range(50):
        try:
            assert_unused(resolve(volume_id))
            return
        except MedicError:
            time.sleep(0.1)
    assert_unused(resolve(volume_id))


def verify_uninstall():
    from gi.repository import Gio, GLib
    from mount_medic.protocol import BUS_NAME
    assert Path("/sys/class/dmi/id/product_name").read_text().strip() == "MountMedicTest"
    bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    for _ in range(45):
        owned = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                              "NameHasOwner", GLib.Variant("(s)", (BUS_NAME,)), GLib.VariantType("(b)"),
                              Gio.DBusCallFlags.NONE, 5000, None).unpack()[0]
        if not owned:
            break
        time.sleep(1)
    assert not owned, "worker did not exit after idle"
    history = Path("/home/medic/.local/state/mount-medic/history.json")
    before = history.read_bytes()
    command([sys.executable, "install.py", "--remove"])
    assert not Path("/usr/local/bin/mount-medic").exists()
    assert not Path("/usr/share/polkit-1/actions/io.github.aakashH242.mount-medic.policy").exists()
    names = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                         "ListActivatableNames", None, GLib.VariantType("(as)"), Gio.DBusCallFlags.NONE, 5000, None).unpack()[0]
    assert BUS_NAME not in names
    settings = Engine().registry.load(pwd.getpwnam("medic").pw_uid)
    assert settings and all(not entry.get("auto_repair") and not entry.get("auto_mount") for entry in settings.values())
    assert history.read_bytes() == before
    print("PASS: idle worker exit, system uninstall, revoked permissions, unchanged user history", flush=True)


def main():
    global DEVICE
    assert Path("/sys/class/dmi/id/product_name").read_text().strip() == "MountMedicTest"
    targets = [path.parent for path in Path("/sys/class/block").glob("*/serial") if path.read_text().strip() == "medic-test-disk"]
    assert len(targets) == 1, targets
    assert int((targets[0] / "size").read_text()) * 512 == 128 * 1024 * 1024
    DEVICE = "/dev/" + targets[0].name
    Path("/etc/udev/rules.d/99-mount-medic-test.rules").write_text(
        'SUBSYSTEM=="block", ATTRS{serial}=="medic-test-disk", ENV{UDISKS_SYSTEM}="0"\n')
    command(["udevadm", "control", "--reload"])
    if sys.argv[1] != "alpine":
        subprocess.run(["sfdisk", DEVICE], input="label: dos\nstart=2048, type=7\n", text=True,
                       stdout=subprocess.DEVNULL, check=True)
        command(["udevadm", "settle"])
        DEVICE += "1"
    command(["mkntfs", "-F", "-Q", "-L", "Disposable VM test", DEVICE])
    command(["udevadm", "trigger", "--action=change", "/sys/class/block/" + Path(DEVICE).name])
    command(["udevadm", "settle"])
    volume = next(item for item in discover() if item.device == DEVICE)
    assert volume.identity.strong, volume
    if sys.argv[1] != "alpine":
        assert volume.identity.partition and volume.identity.start == 2048, volume
    engine = Engine()
    uid = pwd.getpwnam("medic").pw_uid
    (engine.registry.directory / f"{uid}.json").unlink(missing_ok=True)
    before = disk_hash()
    assert engine.inspect(volume.key)["state"] == "clean"
    assert disk_hash() == before
    print("PASS: block-device diagnosis did not write", flush=True)
    command(["dmsetup", "create", "medic-io-error", "--table", "0 262144 error"])
    try:
        failed_read = command(["/usr/local/lib/mount-medic/mount-medic-probe", "/dev/mapper/medic-io-error"])
        assert json.loads(failed_read.stdout.splitlines()[-1])["state"] == "io_failure", failed_read.stdout
    finally:
        command(["dmsetup", "remove", "medic-io-error"])
    print("PASS: kernel device-mapper read errors classified as actual I/O failure", flush=True)
    Path("/mnt/medic-test").mkdir(exist_ok=True)
    drivers = []
    for driver in ("ntfs3", "ntfs-3g"):
        mounted = command(["mount", "-t", driver, DEVICE, "/mnt/medic-test"], False)
        if mounted.returncode:
            print("UNAVAILABLE driver:", driver, mounted.stdout, flush=True)
            continue
        drivers.append(driver)
        Path("/mnt/medic-test/sentinel.txt").write_text("keep these bytes\n")
        assert engine.inspect(volume.key)["state"].startswith("mounted_")
        command(["umount", "/mnt/medic-test"])
        wait_unused(volume.key)
        with claim(resolve(volume.key)):
            competing = command(["mount", "-t", driver, DEVICE, "/mnt/medic-test"], False)
            assert competing.returncode, "exclusive claim failed to prevent " + driver
        wait_unused(volume.key)
        print("PASS: mounted exclusion and competing mount blocked:", driver, flush=True)
    assert drivers, "No NTFS driver available"
    engine.configure(uid, {"op": "configure", "id": volume.key, "monitor": True, "auto_repair": True, "auto_mount": False})
    try:
        engine.repair(uid + 1, {"op": "automatic", "id": volume.key})
        raise AssertionError("another user used the approval")
    except MedicError:
        pass
    command(["ntfsfix", DEVICE])
    command(["udevadm", "settle"])
    wait_unused(volume.key)
    checked = engine.inspect(volume.key)
    assert checked["state"] == "dirty", checked
    with operation_lock(engine.registry.directory):
        try:
            engine.dispatch(uid, {"op": "check", "id": volume.key})
            raise AssertionError("concurrent operation was permitted")
        except MedicError:
            pass
    result = engine.dispatch(uid, {"op": "automatic", "id": volume.key})
    assert result.get("repair", {}).get("status") == "success", result
    command(["mount", "-t", drivers[0], DEVICE, "/mnt/medic-test"])
    assert Path("/mnt/medic-test/sentinel.txt").read_text() == "keep these bytes\n"
    command(["umount", "/mnt/medic-test"])
    wait_unused(volume.key)
    print("PASS: authorized repair, independent verification, unchanged file contents", flush=True)
    verify_interruption(volume, engine, uid)
    nobody = pwd.getpwnam("nobody")
    denied = subprocess.run(["/usr/local/bin/mount-medic", "check", volume.key, "--json"],
                            user=nobody.pw_uid, group=nobody.pw_gid, extra_groups=[],
                            capture_output=True, text=True, timeout=30)
    assert denied.returncode == 2 and "denied or cancelled" in denied.stderr, denied
    print("PASS: inactive user denied by real polkit without an authentication prompt", flush=True)
    child = subprocess.Popen(["unshare", "--mount", "sh", "-c",
                              'mount --make-rprivate /; mount -t ntfs-3g "$1" /mnt/medic-test && touch /tmp/namespace-ready; read done; umount /mnt/medic-test', "sh", DEVICE], stdin=subprocess.PIPE)
    try:
        for _ in range(50):
            if Path("/tmp/namespace-ready").exists():
                break
            time.sleep(0.1)
        assert Path("/tmp/namespace-ready").exists()
        assert engine.inspect(volume.key)["state"] == "busy"
    finally:
        child.communicate(b"done\n", timeout=10)
    print("PASS: mount in a different namespace blocked inspection", flush=True)
    engine.configure(uid, {"op": "configure", "id": volume.key, "monitor": True, "auto_repair": True, "auto_mount": True})
    Path("/home/medic/volume-id").write_text(volume.key)
    os.chmod("/home/medic/volume-id", 0o644)


if __name__ == "__main__":
    if sys.argv[1] == "--uninstall":
        verify_uninstall()
    elif sys.argv[1] == "--dirty":
        assert Path("/sys/class/dmi/id/product_name").read_text().strip() == "MountMedicTest"
        selected = resolve(Path("/home/medic/volume-id").read_text())
        assert selected.identity.hardware == "medic-test-disk" and not selected.mounts
        command(["ntfsfix", selected.device])
    else:
        main()
