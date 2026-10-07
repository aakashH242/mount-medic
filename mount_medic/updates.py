"""Shared update checks and user-side installation coordination."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from . import __version__
from .model import MedicError
from .releases import (ReleaseError, REPOSITORY, download_archive, fetch_release,
                       validate_release, version)
from .storage import Preferences

CHECK_INTERVAL = 3600
UPDATE_ACTIONS = ("update.ignore", "Ignore this version", "update.install", "Install")
HELPER = Path("/usr/local/lib/mount-medic/installer")


def installed() -> bool:
    return HELPER.is_file() and (HELPER.parent / "mount_medic/updates.py").is_file()


def due(preferences: Preferences) -> bool:
    try:
        last = preferences.updates().get("last_attempt", 0)
    except (MedicError, OSError):
        return True
    return not isinstance(last, (int, float)) or not 0 <= time.time() - last < CHECK_INTERVAL


def status(preferences: Preferences) -> dict:
    try:
        return describe(preferences.updates())
    except (MedicError, OSError) as error:
        return describe({"error": str(error)})


def describe(saved: dict) -> dict:
    release = saved.get("release")
    if release is not None:
        try:
            release = validate_release(release)
        except ReleaseError:
            release = None
    available = release is not None and version(release["version"]) > version(__version__)
    return {"installed": __version__, "state": "update_available" if available else "current" if release else "unchecked",
            "available": release["version"] if available else None, "release": release,
            "last_check": saved.get("last_check"),
            "error": str(saved.get("error") or saved.get("install_error")) if saved.get("error") or saved.get("install_error") else None,
            "ignored": available and saved.get("ignored") == release["version"],
            "release_notes": f"{REPOSITORY}/releases/tag/v{release['version']}" if release else None}


def check(preferences: Preferences) -> dict:
    preferences.save_updates({"last_attempt": int(time.time())})
    try:
        release = fetch_release()
        preferences.save_updates({"release": release, "last_check": int(time.time()), "error": None})
    except (ReleaseError, OSError) as error:
        preferences.save_updates({"error": str(error)})
        raise MedicError(f"Update check failed: {error}") from error
    return status(preferences)


def ignore(preferences: Preferences, target: str) -> dict:
    version(target)
    preferences.save_updates({"ignored": target})
    return {"ignored_version": target}


def notification_due(preferences: Preferences, release: dict) -> bool:
    saved = preferences.updates()
    target = release["version"]
    return version(target) > version(__version__) and saved.get("ignored") != target and saved.get("notified") != target


def stop_desktop() -> int:
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return 0
    from gi.repository import Gio, GLib
    from .protocol import BUS_NAME
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        owned = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner",
                              GLib.Variant("(s)", (BUS_NAME,)), None, Gio.DBusCallFlags.NONE, 3000, None).unpack()[0]
        if not owned:
            return 0
        proxy = Gio.Application(application_id=BUS_NAME, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE | Gio.ApplicationFlags.IS_LAUNCHER)
        # GApplication.run is a process entry point; each update coordinator invokes it once.
        return proxy.run(["mount-medic", "--quit-for-update"])
    except GLib.Error as error:
        raise MedicError(f"Could not contact the desktop app: {error.message}") from error


def install(release: dict, restart: str | None = None) -> dict:
    from .installer import elevated
    if os.geteuid() == 0:
        raise MedicError("Start updates as your normal user; administrator access is requested separately")
    validate_release(release)
    if version(release["version"]) <= version(__version__):
        raise MedicError("This version is already installed")
    if not installed():
        raise MedicError("This checkout has no installed update helper. Install the first updater-enabled release manually.")
    if restart and not shutil.which("pkexec", path="/usr/bin:/usr/sbin:/bin:/sbin"):
        raise MedicError("Graphical administrator authentication is unavailable. Use mount-medic update install from an interactive terminal.")
    approved = confirm_dependencies(release, restart)
    with tempfile.TemporaryDirectory(prefix="mount-medic-download-") as directory:
        archive = download_archive(release, Path(directory))
        mode = stop_desktop()
        if mode == 2:
            raise MedicError("Finish the active operation or close its dialog before installing an update")
        if mode not in (0, 10, 11):
            raise MedicError("Could not stop the desktop app safely")
        resume = restart or ({10: "gui", 11: "watch"}.get(mode))
        try:
            # The activated worker exits after inactivity. Never terminate a repair.
            deadline = time.monotonic() + 35
            while any(worker_running(path) for path in Path("/proc").glob("[0-9]*/cmdline")):
                if time.monotonic() >= deadline:
                    raise MedicError("The worker is still busy; retry after checks or repairs finish")
                time.sleep(0.25)
            command = elevated([str(HELPER), "--upgrade", release["version"], "--sha256", release["sha256"],
                                "--archive", str(archive), "--build-user", str(os.getuid()), "--approved-packages", *approved])
            result = subprocess.run(command, stdout=sys.stderr)
            if result.returncode:
                raise MedicError("Update cancelled or failed. Check the installer message; interrupted updates can be recovered with the installed helper's --recover command.")
            Preferences().save_updates({"install_error": None})
        finally:
            if resume:
                subprocess.Popen(["/usr/local/bin/mount-medic", resume], start_new_session=True)
    return {"updated": release["version"]}


def confirm_dependencies(release: dict, restart: str | None) -> list:
    from .dependencies import family, missing_packages, package_install_command
    from .installer import ask
    platform = family()
    missing = missing_packages(platform, release["packages"].get(platform))
    if not missing:
        return []
    message = "This update requires additional packages:\n" + " ".join(package_install_command(platform, missing))
    if platform == "arch":
        message += "\nThis includes a full Arch system upgrade."
    if restart:
        from gi.repository import Gtk
        if not Gtk.init_check()[0]:
            raise MedicError("Review required packages from a graphical session or an interactive terminal")
        dialog = Gtk.MessageDialog(modal=True, text="Install required dependencies?", secondary_text=message)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Install", Gtk.ResponseType.YES)
        approved = dialog.run() == Gtk.ResponseType.YES
        dialog.destroy()
    else:
        approved = ask(message + "\nInstall these dependencies with the update?")
    if not approved:
        raise MedicError("Update cancelled; required packages were not approved")
    return missing


def worker_running(path: Path) -> bool:
    try:
        return b"/usr/local/lib/mount-medic/worker" in path.read_bytes().split(b"\0")
    except (FileNotFoundError, PermissionError):
        return False


def cli(args) -> dict:
    preferences = Preferences()
    if args.action == "ignore":
        if args.dry_run:
            version(args.version)
            return {"dry_run": True, "ignore_version": args.version}
        return ignore(preferences, args.version)
    result = (describe({**preferences.updates(), "release": fetch_release(), "error": None})
              if args.dry_run else check(preferences))
    if args.action == "check":
        return {**result, "dry_run": True} if args.dry_run else result
    if not result["available"]:
        return result
    if args.dry_run:
        return {**result, "dry_run": True, "effects": "No installation or authentication."}
    return install(result["release"])


def main() -> int:
    # Private coordinator launched by the GUI; target arguments pin its selected release.
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("version")
    parser.add_argument("sha256")
    parser.add_argument("--restart", choices=("gui", "watch"))
    args = parser.parse_args()
    try:
        release = fetch_release(args.version)
        if release["sha256"] != args.sha256:
            raise MedicError("The selected update changed; check again before installing")
        print(install(release, args.restart))
        return 0
    except (MedicError, ReleaseError, OSError, ImportError) as error:
        print(f"Mount Medic update: {error}", file=sys.stderr)
        if args.restart:
            try:
                Preferences().save_updates({"install_error": str(error)})
                from .notifications import Notifications
                Notifications(Preferences(), lambda action, key: None, print).message("Mount Medic update failed", str(error))
            except Exception as report_error:
                print(f"Could not report update failure: {report_error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
