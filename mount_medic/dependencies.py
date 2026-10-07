from pathlib import Path
import shlex
import subprocess

from .model import MedicError

PACKAGES = {
    "debian": ["apt-get", "install", "python3", "python3-gi", "gir1.2-gtk-3.0", "gir1.2-ayatanaappindicator3-0.1",
               "build-essential", "pkg-config", "ntfs-3g", "ntfs-3g-dev", "util-linux", "udisks2", "polkitd", "pkexec"],
    "fedora": ["dnf", "install", "python3", "python3-gobject", "gtk3", "libappindicator-gtk3",
               "gcc", "glibc-devel", "make", "pkgconf-pkg-config", "ntfs-3g", "ntfsprogs", "ntfs-3g-devel", "util-linux", "udisks2", "polkit"],
    "arch": ["pacman", "-Syu", "--needed", "python", "python-gobject", "gtk3", "libayatana-appindicator",
             "base-devel", "pkgconf", "ntfs-3g", "ntfsprogs", "util-linux", "udisks2", "polkit"],
    "opensuse": ["zypper", "install", "python3", "python3-gobject", "python3-gobject-Gdk", "typelib-1_0-Gtk-3_0",
                 "libayatana-appindicator3-1", "gcc", "glibc-devel", "make", "pkgconf-pkg-config", "ntfs-3g", "ntfsprogs", "libntfs-3g-devel", "util-linux", "udisks2", "polkit"],
    "alpine": ["apk", "add", "python3", "py3-gobject3", "gtk+3.0", "libayatana-appindicator",
               "build-base", "linux-headers", "pkgconf", "ntfs-3g", "ntfs-3g-progs", "ntfs-3g-dev", "util-linux", "udisks2", "polkit-elogind", "elogind", "eudev"],
}


def family(path: Path = Path("/etc/os-release")) -> str:
    values = {}
    for line in path.read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip('"\'')
    ids = [values.get("ID", ""), *values.get("ID_LIKE", "").split()]
    for name in ids:
        if name in {"ubuntu", "debian", "linuxmint"}:
            return "debian"
        if name in {"fedora", "rhel", "centos"}:
            return "fedora"
        if name in {"arch", "manjaro"}:
            return "arch"
        if name.startswith("opensuse") or name == "suse":
            return "opensuse"
        if name == "alpine":
            return "alpine"
    return "unknown"


def dependency_command() -> str:
    command = PACKAGES.get(family())
    return shlex.join(command) if command else "Install Python 3.11+, GTK3/PyGObject, libntfs-3g development headers, ntfsfix, util-linux, UDisks2, polkit and a C compiler."


def required_packages(platform: str) -> list[str]:
    return PACKAGES[platform][3 if platform == "arch" else 2:]


def missing_packages(platform: str, packages: list[str] | None = None) -> list[str]:
    if platform not in PACKAGES:
        return []
    queries = {"debian": ["dpkg-query", "-W", "-f=${Status}"],
               "fedora": ["rpm", "-q", "--whatprovides"], "opensuse": ["rpm", "-q", "--whatprovides"],
               "arch": ["pacman", "-Q"], "alpine": ["apk", "info", "-e"]}
    if packages is None:
        packages = required_packages(platform)
    missing = []
    for package in packages:
        result = subprocess.run([*queries[platform], package], capture_output=True, text=True, timeout=30,
                                env={"PATH": "/usr/bin:/usr/sbin:/bin:/sbin", "LC_ALL": "C"})
        if result.returncode not in (0, 1):
            raise MedicError(f"Cannot query installed package {package}: {result.stderr.strip()}")
        if result.returncode or (platform == "debian" and result.stdout.strip() != "install ok installed"):
            missing.append(package)
    return missing


def package_install_command(platform: str, packages: list[str]) -> list[str]:
    command = PACKAGES[platform][:3 if platform == "arch" else 2]
    if platform == "opensuse":
        command.insert(1, "--non-interactive")
    elif platform in {"debian", "fedora"}:
        command.append("-y")
    elif platform == "arch":
        command.append("--noconfirm")
    return [*command, *packages]
