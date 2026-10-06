"""Bounded diagnostics from the disposable guest on a failed integration run."""
from pathlib import Path
import os
import pwd
import subprocess

assert Path("/sys/class/dmi/id/product_name").read_text().strip() == "MountMedicTest"
medic = pwd.getpwnam("medic")
subprocess.run(["lsblk", "-J", "-o", "PATH,TYPE,FSTYPE,UUID,SERIAL,MAJ:MIN"],
               user=medic.pw_uid, group=medic.pw_gid, extra_groups=[])
subprocess.run(["udevadm", "info", "--query=property", "--name", "/dev/vdb"],
               user=medic.pw_uid, group=medic.pw_gid, extra_groups=[])
for descriptor in Path("/proc/1/fd").iterdir():
    try:
        descriptor.stat()
    except PermissionError:
        print("INACCESSIBLE FD", descriptor, os.readlink(descriptor))
        print(Path("/proc/1/fdinfo", descriptor.name).read_text())
for process in Path("/proc").glob("[0-9]*"):
    try:
        if b"/usr/local/lib/mount-medic/worker" in (process / "cmdline").read_bytes():
            print("WORKER", process.name, (process / "attr/current").read_text())
            print((process / "status").read_text())
    except OSError:
        pass
if Path("/usr/bin/journalctl").exists():
    subprocess.run(["journalctl", "-b", "--no-pager", "-p", "warning", "-n", "35"])
    subprocess.run(["journalctl", "-b", "--no-pager", "-u", "gdm", "-n", "40"])
    subprocess.run(["journalctl", "-b", "--no-pager", "_TRANSPORT=audit", "-n", "30"])
    subprocess.run(["journalctl", "-b", "--no-pager", "_COMM=gnome-shell", "-n", "35"])
    subprocess.run(["journalctl", "-b", "--no-pager", "_UID=1000", "-n", "65"])
    subprocess.run(["loginctl", "list-sessions"])
