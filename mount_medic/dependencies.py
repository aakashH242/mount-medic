from pathlib import Path
import shlex

PACKAGES = {
    "debian": ["apt-get", "install", "python3", "python3-gi", "gir1.2-gtk-3.0", "gir1.2-ayatanaappindicator3-0.1",
               "build-essential", "pkg-config", "ntfs-3g", "ntfs-3g-dev", "util-linux", "udisks2", "polkitd"],
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
