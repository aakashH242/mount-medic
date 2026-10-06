"""Build/test in unprivileged distro containers without host device access."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ROOT / "tests/artifacts/matrix"
TARGETS = {
    "ubuntu24": ("ubuntu:24.04", "apt-get update && apt-get install -y python3 python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 build-essential pkg-config ntfs-3g ntfs-3g-dev util-linux dbus-daemon xvfb xauth"),
    "ubuntu26": ("ubuntu:26.04", "apt-get update && apt-get install -y python3 python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 build-essential pkg-config ntfs-3g ntfs-3g-dev util-linux dbus-daemon xvfb xauth"),
    "debian13": ("debian:13-slim", "apt-get update && apt-get install -y python3 python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 build-essential pkg-config ntfs-3g ntfs-3g-dev util-linux dbus-daemon xvfb xauth"),
    "fedora43": ("fedora:43", "dnf -y install python3 python3-gobject gtk3 libappindicator-gtk3 gcc glibc-devel make pkgconf-pkg-config ntfs-3g ntfsprogs ntfs-3g-devel util-linux dbus-daemon xorg-x11-server-Xvfb xorg-x11-xauth"),
    "fedora44": ("fedora:44", "dnf -y install python3 python3-gobject gtk3 libappindicator-gtk3 gcc glibc-devel make pkgconf-pkg-config ntfs-3g ntfsprogs ntfs-3g-devel util-linux dbus-daemon xorg-x11-server-Xvfb xorg-x11-xauth"),
    "arch": ("archlinux:latest", "pacman -Syu --noconfirm --needed python python-gobject gtk3 libayatana-appindicator gcc make pkgconf ntfs-3g ntfsprogs util-linux dbus xorg-server-xvfb xorg-xauth"),
    "opensuse": ("opensuse/tumbleweed:latest", "zypper --non-interactive install python3 python3-gobject python3-gobject-Gdk typelib-1_0-Gtk-3_0 gcc glibc-devel make pkgconf-pkg-config ntfs-3g ntfsprogs libntfs-3g-devel util-linux dbus-1-daemon xvfb-run xorg-x11-server-Xvfb xauth"),
    "alpine": ("alpine:3.24", "apk add python3 py3-gobject3 gtk+3.0 font-dejavu libayatana-appindicator build-base linux-headers pkgconf ntfs-3g ntfs-3g-progs ntfs-3g-dev util-linux dbus xvfb xvfb-run xauth"),
}


def test_target(name: str) -> dict:
    base, install = TARGETS[name]
    log = ARTIFACTS / (name + ".log")
    result = {"target": name, "base": base, "started": int(time.time()), "passed": False}
    with log.open("w") as stream:
        try:
            subprocess.run(["docker", "pull", base], stdout=stream, stderr=stream, check=True)
            inspected = json.loads(subprocess.check_output(["docker", "image", "inspect", base]))[0]
            digest = inspected["RepoDigests"][0]
            result["base_digest"] = digest
            with tempfile.TemporaryDirectory(prefix="mount-medic-container-") as folder:
                dockerfile = Path(folder) / "Dockerfile"
                dockerfile.write_text(f"FROM {digest}\nRUN {install}\nWORKDIR /work\n")
                tag = "mount-medic-test:" + name
                subprocess.run(["docker", "build", "-t", tag, folder], stdout=stream, stderr=stream, check=True)
            subprocess.run(["docker", "run", "--rm", "--network=none", "-v", str(ROOT) + ":/source:ro", tag,
                            "sh", "/source/tests/container_checks.sh"], stdout=stream, stderr=stream, check=True)
            result["passed"] = True
        except subprocess.CalledProcessError as error:
            result["error"] = f"command exited {error.returncode}; see {log.name}"
    result["finished"] = int(time.time())
    (ARTIFACTS / (name + ".json")).write_text(json.dumps(result, indent=2))
    print(json.dumps(result), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("targets", nargs="*", choices=sorted(TARGETS))
    parser.add_argument("--jobs", type=int, default=2)
    args = parser.parse_args()
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=max(1, min(args.jobs, 3))) as pool:
        results = list(pool.map(test_target, args.targets or TARGETS))
    (ARTIFACTS / "results.json").write_text(json.dumps(results, indent=2))
    return 0 if all(result["passed"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
