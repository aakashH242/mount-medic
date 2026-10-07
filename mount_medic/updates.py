"""Shared update checks and user-side installation coordination."""
import os
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from . import __version__
from .model import MedicError
from .releases import (ReleaseError, REPOSITORY, download_archive, fetch_release, source_version,
                       validate_release, version)
from .storage import Preferences

CHECK_INTERVAL = 3600
UPDATE_ACTIONS = ("update.ignore", "Ignore this version", "update.install", "Install")
HELPER = Path("/usr/local/lib/mount-medic/installer")
INSTALL_STAGES = {"verify": "Verifying the update…", "build": "Preparing the update…",
                  "install": "Installing update…"}


@contextmanager
def update_lock():
    state = Preferences().state
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / "update-install.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise MedicError("An update is already running. Wait for it to finish.") from error
        yield


def recovery_pending():
    from .installer import TRANSACTION
    return Path("/", TRANSACTION).exists()


def report_progress(progress, message):
    if progress:
        progress.stage(message)


def installation_result(target, error=None):
    if error is None:
        message = f"Update installed successfully. Version {target}."
        state = "success"
    else:
        state = "failed"
        try:
            current = source_version(HELPER.parent)
            if current == target and not recovery_pending():
                state = "success"
                message = f"Version {target} is installed. The installer reported a problem; see the update log."
            elif recovery_pending():
                message = "Update interrupted. Recover the installation before opening Mount Medic:\nsudo /usr/local/lib/mount-medic/installer --recover"
            else:
                message = (f"Update failed. Kept version {current}." if current == __version__
                           else "Update failed. Check Settings → Updates before trying again.")
        except (OSError, ReleaseError):
            message = "Update failed. Check Settings → Updates before trying again."
        message += "\n" + str(error)
    return {"state": state, "message": message, "id": uuid.uuid4().hex}


def save_result(target, error=None):
    result = installation_result(target, error)
    store_result(result)
    return result


def store_result(result):
    Preferences().save_updates({"install_error": None if result["state"] == "success" else result["message"],
                               "install_result": result})


def restart_result():
    value = os.environ.pop("MOUNT_MEDIC_UPDATE_RESULT", "")
    try:
        result = json.loads(value) if len(value) <= 16384 else None
    except ValueError:
        return None
    if isinstance(result, dict) and result.get("state") in ("success", "failed", "restart_failed") and isinstance(result.get("message"), str):
        return result
    return None


def installer_command(command, progress):
    if not progress:
        return subprocess.run(command, stdout=sys.stderr).returncode
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=sys.stderr, text=True,
                          errors="replace", bufsize=1) as process:
        while chunk := process.stdout.readline(4096):
            print(chunk, end="", file=sys.stderr, flush=True)
            stage = INSTALL_STAGES.get(chunk.strip().removeprefix("MM_UPDATE_STAGE:")) if chunk.startswith("MM_UPDATE_STAGE:") else None
            if stage:
                progress.stage(stage)
        return process.wait()


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
        owner = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "GetNameOwner",
                              GLib.Variant("(s)", (BUS_NAME,)), None, Gio.DBusCallFlags.NONE, 3000, None).unpack()[0]
        proxy = Gio.Application(application_id=BUS_NAME, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE | Gio.ApplicationFlags.IS_LAUNCHER)
        # GApplication.run is a process entry point; each update coordinator invokes it once.
        mode = proxy.run(["mount-medic", "--quit-for-update", "--update-owner=" + owner])
        if mode in (10, 11):
            # Track this connection so a newly opened app cannot prolong the handoff.
            while bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner",
                                GLib.Variant("(s)", (owner,)), None, Gio.DBusCallFlags.NONE, 1000, None).unpack()[0]:
                time.sleep(0.05)
            if bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner",
                             GLib.Variant("(s)", (BUS_NAME,)), None, Gio.DBusCallFlags.NONE, 1000, None).unpack()[0]:
                return 3
        return mode
    except GLib.Error as error:
        raise MedicError(f"Could not contact the desktop app: {error.message}") from error


def desktop_ready(mode):
    from gi.repository import Gio, GLib
    from .protocol import BUS_NAME
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    owned = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner",
                          GLib.Variant("(s)", (BUS_NAME,)), None, Gio.DBusCallFlags.NONE, 1000, None).unpack()[0]
    if not owned:
        return False
    proxy = Gio.Application(application_id=BUS_NAME, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE | Gio.ApplicationFlags.IS_LAUNCHER)
    return proxy.run(["mount-medic", "--update-ready-" + mode]) == {"gui": 12, "watch": 13}[mode]


def install(release: dict, restart: str | None = None, progress=None) -> dict:
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
    report_progress(progress, "Checking required packages…")
    approved = confirm_dependencies(release, restart, progress)
    with tempfile.TemporaryDirectory(prefix="mount-medic-download-") as directory:
        report_progress(progress, "Downloading the update…")
        archive = download_archive(release, Path(directory))
        report_progress(progress, "Closing Mount Medic safely…")
        # A GTK coordinator must keep its main loop on the UI thread. GApplication.run
        # has a separate process lifecycle, so the shutdown request uses a short child.
        if progress:
            script = ("import sys; sys.path.insert(0, sys.argv[1]); "
                      "from mount_medic.updates import stop_desktop; raise SystemExit(stop_desktop())")
            mode = subprocess.run([sys.executable, "-IB", "-c", script, str(Path(__file__).resolve().parent.parent)]).returncode
        else:
            mode = stop_desktop()
        if mode == 2:
            raise MedicError("Finish the active operation or close its dialog before installing an update")
        if mode == 3:
            raise MedicError("Mount Medic was reopened during the update. Try the update again.")
        if mode not in (0, 10, 11):
            raise MedicError("Could not stop the desktop app safely")
        resume = restart or ({10: "gui", 11: "watch"}.get(mode))
        outcome = None
        try:
            report_progress(progress, "Waiting for background work to finish…")
            # The activated worker exits after inactivity. Never terminate a repair.
            deadline = time.monotonic() + 35
            while any(worker_running(path) for path in Path("/proc").glob("[0-9]*/cmdline")):
                if time.monotonic() >= deadline:
                    raise MedicError("The worker is still busy; retry after checks or repairs finish")
                time.sleep(0.25)
            command = elevated([str(HELPER), "--upgrade", release["version"], "--sha256", release["sha256"],
                                "--archive", str(archive), "--build-user", str(os.getuid()), "--approved-packages", *approved])
            report_progress(progress, "Waiting for administrator approval…")
            code = installer_command(command, progress)
            if code:
                raise MedicError("Update cancelled or failed. Check the installer message; interrupted updates can be recovered with the installed helper's --recover command.")
            if source_version(HELPER.parent) != release["version"] or recovery_pending():
                raise MedicError("The installer finished without the selected version. Check Settings → Updates.")
            outcome = installation_result(release["version"])
        except (MedicError, ReleaseError, OSError) as error:
            outcome = installation_result(release["version"], error)
            if outcome["state"] != "success":
                raise
        finally:
            if outcome:
                try:
                    store_result(outcome)
                except (MedicError, OSError) as error:
                    outcome = {**outcome, "message": outcome["message"] + "\nCould not save the update result: " + str(error)}
                    print(outcome["message"], file=sys.stderr)
                if progress:
                    progress.result(outcome["message"])
            if resume and not recovery_pending():
                report_progress(progress, "Restarting Mount Medic…")
                try:
                    environment = {**os.environ}
                    if outcome:
                        environment["MOUNT_MEDIC_UPDATE_RESULT"] = json.dumps(outcome)
                    process = subprocess.Popen(["/usr/local/bin/mount-medic", resume], start_new_session=True, env=environment)
                    if progress:
                        progress.wait_for_restart(process, resume)
                except (MedicError, OSError) as error:
                    message = (outcome or {}).get("message", "The update result could not be confirmed.") + "\n" + str(error)
                    if progress:
                        progress.result(message)
                    try:
                        store_result({"state": "restart_failed", "message": message, "id": uuid.uuid4().hex})
                    except (MedicError, OSError) as report_error:
                        print(f"Could not save restart failure: {report_error}", file=sys.stderr)
                    raise MedicError(message) from error
    return {"updated": release["version"], "message": outcome["message"]}


def confirm_dependencies(release: dict, restart: str | None, progress=None) -> list:
    from .dependencies import family, missing_packages, package_install_command
    from .installer import ask
    platform = family()
    missing = missing_packages(platform, release["packages"].get(platform))
    if not missing:
        return []
    message = "This update requires additional packages:\n" + " ".join(package_install_command(platform, missing))
    if platform == "arch":
        message += "\nThis includes a full Arch system upgrade."
    if progress:
        approved = progress.confirm(message)
    elif restart:
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
    with update_lock():
        preferences.save_updates({"install_result": None})
        return install(result["release"])


def perform_update(args, progress=None) -> int:
    try:
        Preferences().save_updates({"install_result": None})
        report_progress(progress, "Checking the selected update…")
        release = fetch_release(args.version)
        if release["sha256"] != args.sha256:
            raise MedicError("The selected update changed; check again before installing")
        result = install(release, args.restart, progress)
        if progress:
            progress.result(result["message"])
        print(result)
        if args.restart:
            from .notifications import desktop_message
            desktop_message("Mount Medic updated", result.get("message", f"Version {result['updated']} installed."))
        return 0
    except (MedicError, ReleaseError, OSError, ImportError) as error:
        print(f"Mount Medic update: {error}", file=sys.stderr)
        if args.restart:
            try:
                # Install failures are recorded before reopening the app. Earlier failures
                # leave the existing app running and need the same in-app result.
                if not Preferences().updates().get("install_result"):
                    save_result(args.version, error)
            except Exception as report_error:
                print(f"Could not report update failure: {report_error}", file=sys.stderr)
            if progress:
                if not getattr(progress, "result_message", None):
                    progress.result(installation_result(args.version, error)["message"])
            from .notifications import desktop_message
            desktop_message("Mount Medic update failed", str(error), "error")
        return 2


def run_update(args, progress=None) -> int:
    try:
        with update_lock():
            return perform_update(args, progress)
    except (MedicError, OSError) as error:
        print(f"Mount Medic update: {error}", file=sys.stderr)
        if progress:
            progress.result(str(error))
        return 2


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("version")
    parser.add_argument("sha256")
    parser.add_argument("--restart", choices=("gui", "watch"))
    args = parser.parse_args()
    if args.restart:
        from .update_progress import run
        return run(args)
    return run_update(args)


if __name__ == "__main__":
    raise SystemExit(main())
