"""Run a checksum-pinned guest; only /dev/kvm is passed to the QEMU container."""
import argparse
import base64
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
IMAGES = {
    "ubuntu": ("https://cloud-images.ubuntu.com/releases/26.04/release/ubuntu-26.04-server-cloudimg-amd64.img",
               "sha256", "8800651811af9a85465ad1d552add729947bb16488dddb4a9b5305a3d97332b2"),
    "fedora": ("https://download.fedoraproject.org/pub/fedora/linux/releases/44/Cloud/x86_64/images/Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2",
               "sha256", "28680fe5b371a5a82ebf43a31926e086a168e59949d03969c5093e7071f90b7f"),
    "alpine": ("https://dl-cdn.alpinelinux.org/alpine/v3.24/releases/cloud/alpine-3.24.2-x86_64-cloudinit-r0.qcow2",
               "sha512", "c9504d23613f304e0cfb6f5fec872e29e5a5a62e2bc64796daf19c14fdddaa87dc252912fd8bdd17f7b8ebf0cd03305e4075993d54de175e6028d6b50414c67f"),
}


def source_archive() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in ROOT.rglob("*"):
            relative = path.relative_to(ROOT)
            if set(relative.parts) & {".git", ".idea", ".consequences", "build", "artifacts", "images", "__pycache__"}:
                continue
            if path.is_file() and not path.is_symlink():
                archive.add(path, arcname=str(relative))
    return buffer.getvalue()


def docker(directory: Path, arguments: list[str]) -> None:
    subprocess.run(["docker", "run", "--rm", "--network=none", "-v", f"{directory}:/data",
                    "mount-medic-vm-runner:local", *arguments], check=True, stdout=subprocess.DEVNULL)


def qmp(guest: Path, command: str, arguments: dict) -> dict:
    result = subprocess.check_output(["docker", "exec", guest.name, "python3", f"/data/{guest.name}/control.py",
                                      f"/data/{guest.name}/qmp.sock", command, json.dumps(arguments)], text=True)
    return json.loads(result)


def desktop_phase(guest: Path, phase: str) -> None:
    if phase in {"AUTH_CANCEL", "AUTH_ACCEPT", "AUTH_ENROLL", "WINDOW", "NOTIFICATION"}:
        time.sleep(1 if phase == "NOTIFICATION" else 3)
        qmp(guest, "screendump", {"filename": f"/data/{guest.name}/{phase.lower()}.png", "format": "png"})
        keys = ["esc"] if phase == "AUTH_CANCEL" else []
        if phase in {"AUTH_ACCEPT", "AUTH_ENROLL"}:
            keys = ["minus" if char == "-" else char for char in "disposable-vm-only"] + ["ret"]
        for key in keys:
            qmp(guest, "send-key", {"keys": [{"type": "qcode", "data": key}], "hold-time": 40})
            time.sleep(0.06)
    elif phase in {"REMOVE", "REPLACE"}:
        qmp(guest, "device_del", {"id": "testdevice"})
    elif phase in {"ADD", "ADD_REPLACEMENT"}:
        serial = "medic-test-disk" if phase == "ADD" else "medic-replacement"
        nodes = qmp(guest, "query-named-block-nodes", {})
        if not any(node.get("node-name") == "testdisk" for node in nodes):
            qmp(guest, "blockdev-add", {"node-name": "testdisk", "driver": "raw",
                                       "file": {"driver": "file", "filename": f"/data/{guest.name}/disposable.raw"}})
        qmp(guest, "device_add", {"driver": "virtio-blk-pci", "drive": "testdisk", "serial": serial, "id": "testdevice"})


def run_guest(target: str, directory: Path, reuse: Path | None = None) -> dict:
    url, algorithm, expected = IMAGES[target]
    image = directory / f"{target}.img"
    if not image.exists():
        urllib.request.urlretrieve(url, image)
    with image.open("rb") as stream:
        checksum = hashlib.file_digest(stream, algorithm).hexdigest()
    if checksum != expected:
        raise ValueError(f"{target} image checksum mismatch; verify the official release before updating its pin")
    backing = image.relative_to(directory)
    if reuse is not None:
        reuse = reuse.resolve()
        backing = reuse.relative_to(directory)
        previous = json.loads((reuse.parent / "result.json").read_text())
        if reuse.name != "root.qcow2" or previous["target"] != target or previous["checksum"] != checksum:
            raise ValueError("Only a completed overlay from this harness and image can be reused")
    guest = directory / (target + "-run-" + str(int(time.time())))
    guest.mkdir()
    (guest / "control.py").write_bytes((ROOT / "tests/vm/control.py").read_bytes())
    data = {"users": [{"name": "medic", "uid": 2000, "shell": "/bin/sh", "lock_passwd": False,
                       "sudo": ["ALL=(ALL) NOPASSWD:ALL"],
                       "plain_text_passwd": "disposable-vm-only"}], "ssh_pwauth": False,
            "write_files": [{"path": "/var/lib/mount-medic-test/source.tar.gz", "encoding": "b64",
                              "content": base64.b64encode(source_archive()).decode()}],
            "runcmd": [["sh", "-c", "if command -v systemctl >/dev/null; then systemctl stop serial-getty@ttyS0.service display-manager.service; fi; exec > /dev/ttyS0 2>&1; mkdir -p /opt/mount-medic; tar -xzf /var/lib/mount-medic-test/source.tar.gz -C /opt/mount-medic; "
                        "sh /opt/mount-medic/tests/vm/setup.sh " + target]]}
    (guest / "user-data").write_text("#cloud-config\n" + json.dumps(data))
    (guest / "meta-data").write_text(json.dumps({"instance-id": guest.name, "local-hostname": "mount-medic-test"}))
    docker(guest, ["genisoimage", "-quiet", "-output", "/data/seed.iso", "-volid", "cidata", "-joliet", "-rock",
                   "/data/user-data", "/data/meta-data"])
    docker(directory, ["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", f"/data/{backing}",
                       f"/data/{guest.name}/root.qcow2", "32G"])
    with (guest / "disposable.raw").open("wb") as stream:
        stream.truncate(128 * 1024 * 1024)
    docker(guest, ["cp", "/usr/share/OVMF/OVMF_VARS_4M.fd", "/data/nvram.fd"])
    command = ["docker", "run", "--rm", "--name", guest.name, "--device", "/dev/kvm", "-v", f"{directory}:/data",
               "mount-medic-vm-runner:local", "qemu-system-x86_64", "-enable-kvm", "-cpu", "host", "-m", "4096", "-smp", "2",
               "-drive", "if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd",
               "-drive", f"if=pflash,format=raw,file=/data/{guest.name}/nvram.fd",
               "-smbios", "type=1,product=MountMedicTest", "-display", "none", "-device", "virtio-vga", "-device", "qemu-xhci", "-device", "usb-tablet",
               "-drive", f"file=/data/{guest.name}/root.qcow2,if=none,id=rootdisk,format=qcow2",
               "-device", "virtio-blk-pci,drive=rootdisk,serial=medic-root-disk",
               "-drive", f"file=/data/{guest.name}/disposable.raw,if=none,id=testdisk,format=raw",
               "-device", "virtio-blk-pci,drive=testdisk,serial=medic-test-disk,id=testdevice",
               "-drive", f"file=/data/{guest.name}/seed.iso,media=cdrom,readonly=on",
               "-nic", "user,model=virtio-net-pci", "-chardev", f"socket,id=console,path=/data/{guest.name}/console.sock,server=on,wait=off,logfile=/data/{guest.name}/serial.log",
               "-serial", "chardev:console", "-monitor", "none",
               "-chardev", f"socket,id=agent,path=/data/{guest.name}/agent.sock,server=on,wait=off",
               "-device", "virtio-serial-pci", "-device", "virtserialport,chardev=agent,name=org.qemu.guest_agent.0",
               "-qmp", f"unix:/data/{guest.name}/qmp.sock,server=on,wait=off"]
    log = guest / "serial.log"
    result = {"target": target, "image": url, "checksum": checksum, "passed": False, "log": str(log)}
    log.touch()
    with (guest / "qemu.log").open("w") as stream:
        process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 2400
        handled = set()
        try:
            while process.poll() is None and time.monotonic() < deadline:
                contents = log.read_text(errors="replace")
                for phase in ("AUTH_CANCEL", "AUTH_ACCEPT", "AUTH_ENROLL", "WINDOW", "NOTIFICATION", "REMOVE", "ADD", "REPLACE", "ADD_REPLACEMENT"):
                    if f"MOUNT_MEDIC_PHASE:{phase}\n" in contents and phase not in handled:
                        desktop_phase(guest, phase)
                        handled.add(phase)
                if "MOUNT_MEDIC_VM_PASS" in contents or "MOUNT_MEDIC_VM_FAIL" in contents:
                    result["passed"] = "MOUNT_MEDIC_VM_PASS" in contents
                    break
                time.sleep(2)
        except Exception as error:
            result["error"] = str(error)
        finally:
            try:
                subprocess.run(["docker", "exec", guest.name, "python3", f"/data/{guest.name}/control.py",
                                f"/data/{guest.name}/agent.sock", "guest-exec",
                                json.dumps({"path": "/bin/sync", "capture-output": True})],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
            except subprocess.TimeoutExpired:
                pass
            subprocess.run(["docker", "stop", "--time", "5", guest.name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            process.wait(timeout=30)
    (guest / "result.json").write_text(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("target", choices=IMAGES)
    parser.add_argument("--directory", type=Path, default=Path("/tmp/mount-medic-vms"))
    parser.add_argument("--reuse", type=Path, help="Completed guest root overlay; reuse package downloads in a new overlay")
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    result = run_guest(args.target, args.directory.resolve(), args.reuse)
    print(json.dumps(result), flush=True)
    raise SystemExit(0 if result["passed"] else 1)
