"""Exercise explicit actions with real GTK messages and a private notification bus."""
import os
from pathlib import Path
import sys
import tempfile
from threading import Event, Thread
import time
from unittest.mock import Mock, patch
from types import SimpleNamespace

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gtk

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mount_medic import __version__, updates
from mount_medic.desktop import MedicApplication
from mount_medic.model import Identity, MedicError, Volume, diagnosis
from mount_medic.storage import Preferences, atomic_json
from notification_smoke import NotificationServer, wait_for
from ui_smoke import TrayWatcher, capture, widgets
from update_fixture import TARGET_VERSION, metadata


def respond(name, inspect=None):
    from gi.repository import GLib
    def ready():
        matches = [window for window in Gtk.Window.list_toplevels() if isinstance(window, Gtk.Dialog)
                   and (window.get_title() == name or isinstance(window, Gtk.MessageDialog) and window.get_property("text") == name)]
        if not matches:
            return True
        if inspect:
            inspect(matches[0])
        matches[0].response(Gtk.ResponseType.OK)
        return False
    GLib.timeout_add(40, ready)


def manual_checks(app, report, server, output):
    ready = Event()
    def inspect(request):
        assert request == {"op": "check", "id": report["volume"]["id"]}
        assert ready.wait(5)
        return [report]
    app.render([report])
    app.toast.dismiss()
    assert not app.toast.get_visible()
    calls = len(server.calls)
    with patch("mount_medic.client.call", side_effect=inspect):
        app.buttons["Check now"].clicked()
        try:
            assert app.busy and app.toast.get_visible()
            assert "Checking" in app.toast.message.get_text()
            assert not app.buttons["Check now"].get_sensitive()
            capture(app.window, output / "checking.png")
        finally:
            ready.set()
        wait_for(lambda: not app.busy)
    assert "Check complete" in app.toast.message.get_text() and "Dirty flag set" in app.toast.message.get_text()
    assert app.toast.get_message_type() == Gtk.MessageType.WARNING
    assert app.toast.icon.get_icon_name()[0] == "dialog-warning-symbolic"
    assert len(server.calls) == calls, "in-app checks should not also send desktop notifications"
    capture(app.window, output / "check-complete.png")
    with patch("mount_medic.client.list_volumes", return_value=[report]):
        app.refresh()
        wait_for(lambda: not app.busy)
    assert "Check complete" in app.toast.message.get_text(), "refresh overwrote explicit action feedback"
    app.toast.response(Gtk.ResponseType.CLOSE)
    app.window.show_all()
    assert not app.toast.get_visible(), "show_all resurrected a dismissed message"

    with patch("mount_medic.client.call", side_effect=MedicError("Synthetic inspection failure")):
        app.buttons["Check now"].clicked()
        wait_for(lambda: not app.busy)
    assert app.toast.get_message_type() == Gtk.MessageType.ERROR
    assert "Check failed" in app.toast.message.get_text() and "Synthetic inspection failure" in app.toast.message.get_text()
    capture(app.window, output / "check-error.png")

    ready.clear()
    calls = len(server.calls)
    with patch.object(app, "activate", side_effect=AssertionError("completion reopened a closed window")), patch("mount_medic.client.call", side_effect=inspect):
        app.buttons["Check now"].clicked()
        app.window.hide()
        ready.set()
        wait_for(lambda: not app.busy and len(server.calls) > calls)
    assert "Check complete" in server.calls[-1][4] and not app.window.get_visible()

    app.window.hide()
    tray_check = (next(item for item in app.indicator.get_menu().get_children() if item.get_label() == "Check now").activate
                  if app.indicator else lambda: app.check_from_tray(None))
    with patch.object(app, "activate", side_effect=AssertionError("tray check opened GUI")):
        other = {**report, "state": "mounted_rw", "volume": {**report["volume"], "id": "b" * 24, "label": "Work"}}
        for reports in ([report], [report], [], [report, other]):
            calls = len(server.calls)
            with patch("mount_medic.client.call", return_value=reports) as operation:
                tray_check()
                wait_for(lambda: not app.busy and len(server.calls) > calls)
                assert operation.call_args.args[0] == {"op": "check"}
            assert server.calls[-1][3] == "Check complete"
            assert not app.window.get_visible()
            if not reports:
                assert "No monitored drives" in server.calls[-1][4]
            elif len(reports) == 2:
                assert "2 drives" in server.calls[-1][4] and "dirty flag set" in server.calls[-1][4]
            if reports:
                assert server.calls[-1][2] == "dialog-warning"
        calls = len(server.calls)
        with patch("mount_medic.client.call", side_effect=MedicError("Synthetic tray failure")):
            tray_check()
            wait_for(lambda: not app.busy and len(server.calls) > calls)
        assert server.calls[-1][3] == "Check failed" and server.calls[-1][6]["urgency"] == 2
        assert server.calls[-1][2] == "dialog-error"
        app.busy = True
        calls = len(server.calls)
        with patch("mount_medic.client.call", side_effect=AssertionError("busy check executed")):
            tray_check()
            wait_for(lambda: len(server.calls) > calls)
        app.busy = False
        app.pending = False
        assert "already running" in server.calls[-1][4]

    unbuilt = MedicApplication()
    try:
        calls = len(server.calls)
        with patch.object(unbuilt, "activate", side_effect=AssertionError("background check built GUI")), patch("mount_medic.client.call", return_value=[report]):
            unbuilt.check_from_tray(None)
            wait_for(lambda: not unbuilt.busy and len(server.calls) > calls)
        assert unbuilt.window is None and server.calls[-1][3] == "Check complete"
    finally:
        if unbuilt.notifications:
            unbuilt.notifications.close_all()
        unbuilt.pool.shutdown()
    app.window.show_all()


def mutation_feedback(app, report, output):
    with patch.object(app, "cycle"):
        grants = {"monitor": True, "auto_repair": False, "auto_mount": False}
        with patch("mount_medic.client.call", return_value=grants):
            app.save_permissions(app.selected(), grants)
            wait_for(lambda: not app.busy)
        assert "permissions saved" in app.toast.message.get_text()
        with patch("mount_medic.client.call", return_value={"monitor": False}):
            app.save_permissions(app.selected(), {"monitor": False, "auto_repair": False, "auto_mount": False})
            wait_for(lambda: not app.busy)
        assert "removed from monitoring" in app.toast.message.get_text()
        app.ignore_selected(None)
        assert "added to Ignore List" in app.toast.message.get_text()
        with patch("mount_medic.dialogs.ignored_drives", return_value=report["volume"]["id"]):
            app.ignored_drives(None)
        assert "removed from Ignore List" in app.toast.message.get_text()

    with patch("mount_medic.installer.autostart_enabled", return_value=False), patch("mount_medic.installer.autostart") as save, patch.object(app, "watch_in_background"):
        respond("Startup settings", lambda dialog: next(item for item in widgets(dialog) if isinstance(item, Gtk.Switch)).set_active(True))
        app.startup_settings(None)
        assert save.call_args.args == (True,) and "login enabled" in app.toast.message.get_text()

    def durations(dialog):
        controls = [item for item in widgets(dialog) if isinstance(item, Gtk.SpinButton)]
        controls[0].set_value(11)
        controls[1].set_value(5)
        sound = next(item for item in widgets(dialog) if isinstance(item, Gtk.CheckButton))
        assert sound.get_active()
        sound.set_active(False)
    respond("Notification settings", durations)
    app.notification_settings(None)
    assert Preferences().toast_seconds() == 5 and Preferences().notification_seconds() == 11
    assert not Preferences().notification_sound_enabled()
    assert "settings saved" in app.toast.message.get_text()
    capture(app.window, output / "saved.png")

    with patch.object(app, "confirmation", return_value=True), patch.object(app, "refresh"), patch("mount_medic.client.udisks_operation", return_value={}) as operation:
        app.mount(None)
        wait_for(lambda: not app.busy)
        assert operation.call_args.args[1] == "Mount" and "Drive mounted" in app.toast.message.get_text()
        with patch("mount_medic.client.call", return_value=[report]):
            app.unmount(None)
            wait_for(lambda: not app.busy)
        assert "unmounted" in app.toast.message.get_text() and "Check complete" in app.toast.message.get_text()
        with patch("mount_medic.client.call", side_effect=MedicError("Synthetic check failure after unmount")):
            app.unmount(None)
            wait_for(lambda: not app.busy)
        assert "unmounted, but checking failed" in app.toast.message.get_text() and app.toast.get_message_type() == Gtk.MessageType.ERROR

    result = {**report, "state": "clean", "repair": {"status": "success"}, "next_steps": "Limited repair verified."}
    respond("Attempt limited repair?")
    with patch("mount_medic.client.repair", return_value=result):
        app.repair(None)
        wait_for(lambda: not app.busy)
    assert "Repair finished" in app.toast.message.get_text() and "verified" in app.toast.message.get_text()
    assert app.toast.get_message_type() == Gtk.MessageType.INFO


def update_feedback(app, output):
    release = {**metadata(), "sha256": "a" * 64, "size": 123}
    app.preferences.save_updates({"release": release, "error": None, "install_error": None})
    app.update_settings()
    dialog = app.update_dialog
    dialog.ignore.clicked()
    assert "ignored" in dialog.toast.message.get_text() and dialog.toast.get_visible()
    with patch("mount_medic.updates.check", return_value={}):
        dialog.check.clicked()
        wait_for(lambda: not app.update_checking)
    assert "Update check complete" in dialog.toast.message.get_text()

    def offline(preferences):
        preferences.save_updates({"error": "Synthetic offline check"})
        raise MedicError("Synthetic offline check")
    with patch("mount_medic.updates.check", side_effect=offline):
        dialog.check.clicked()
        wait_for(lambda: not app.update_checking)
    assert dialog.toast.get_message_type() == Gtk.MessageType.ERROR and "offline" in dialog.toast.message.get_text()
    capture(dialog, output / "update-error.png")
    with patch("mount_medic.updates.installed", return_value=True), patch("mount_medic.desktop.subprocess.Popen", side_effect=OSError("Synthetic installer launch failure")):
        dialog.install.clicked()
    assert app.update_dialog is None and app.toast.get_message_type() == Gtk.MessageType.ERROR
    assert "installer launch failure" in app.toast.message.get_text()

    app.pending_update_result = {"state": "success", "message": "Earlier tray update succeeded"}
    app.window.hide()
    app.update_candidate = release
    process = Mock(returncode=2)
    process.poll.return_value = None
    with patch("mount_medic.updates.installed", return_value=True), patch("mount_medic.desktop.subprocess.Popen", return_value=process):
        app.install_update()
        assert app.pending_update_result is None
        latest = {"state": "failed", "message": "Newest update failed during download"}
        app.preferences.save_updates({"install_result": latest})
        process.poll.return_value = 2
        wait_for(lambda: not app.updating)
    app.window.show_all()
    assert app.show_update_result()
    assert app.toast.message.get_text() == latest["message"]
    assert app.preferences.updates()["install_result"] == {**latest, "seen": True}

    old = {"id": "old", "state": "success", "message": "Older update succeeded"}
    newer = {"id": "new", "state": "failed", "message": "Newer update failed"}
    app.preferences.save_updates({"install_result": old})
    app.pending_update_result = old
    process.poll.return_value = None
    with patch("mount_medic.updates.installed", return_value=True), patch("mount_medic.desktop.subprocess.Popen", return_value=process):
        app.install_update()
        with updates.update_lock():
            assert updates.run_update(SimpleNamespace(version=release["version"], restart="gui")) == 2
        process.poll.return_value = 2
        wait_for(lambda: not app.updating)
    assert app.preferences.updates()["install_result"] == old
    assert not app.show_update_result(), "A refused attempt reused the earlier success"
    assert "failed or was cancelled" in app.toast.message.get_text()
    app.preferences.save_updates({"install_result": newer})
    assert app.show_update_result(), "Suppression of the earlier result hid a new failure"
    assert app.toast.message.get_text() == newer["message"]

    app.stale_update_result = None
    app.pending_update_result = old
    app.preferences.save_updates({"install_result": newer})
    assert app.show_update_result()
    assert app.toast.message.get_text() == newer["message"], "Pending handoff hid a newer cached result"
    assert app.preferences.updates()["install_result"] == {**newer, "seen": True}
    assert not app.show_update_result()

    for invalid in ({**newer, "message": None}, {**newer, "id": 42}, {**newer, "state": "invalid"}):
        app.pending_update_result = old
        app.preferences.save_updates({"install_result": invalid})
        assert app.show_update_result(), "Malformed cache displaced a valid restart message"
        assert app.toast.message.get_text() == old["message"]
        assert app.preferences.updates()["install_result"] == invalid

    app.preferences.save_updates({"install_result": old})
    feedback = app.show_feedback
    saved = Event()
    def write_newer():
        assert saved.wait(5)
        Preferences().save_updates({"install_result": newer})
    writer = Thread(target=write_newer)
    def concurrent_feedback(message, level):
        feedback(message, level)
        saved.set()
        writer.join(timeout=5)
        assert not writer.is_alive()
    writer.start()
    with patch.object(app, "show_feedback", side_effect=concurrent_feedback):
        assert app.show_update_result()
    assert app.preferences.updates()["install_result"] == newer, "Acknowledgment replaced a newer result"
    assert app.show_update_result()
    assert app.toast.message.get_text() == newer["message"]

    app.pending_update_result = old
    app.window.hide()
    with patch("mount_medic.updates.fetch_release", return_value=release), patch("os.geteuid", return_value=1000), patch("mount_medic.updates.installed", return_value=True), patch("mount_medic.updates.confirm_dependencies", return_value=[]), patch("mount_medic.updates.download_archive", side_effect=OSError("Synthetic CLI download failure")), patch("mount_medic.updates.source_version", return_value=__version__), patch("mount_medic.updates.recovery_pending", return_value=False), patch("mount_medic.updates.stop_desktop", side_effect=AssertionError("Failed download closed the app")):
        try:
            updates.cli(SimpleNamespace(action="install", dry_run=False))
            raise AssertionError("CLI download failure was not reported")
        except OSError as error:
            assert "Synthetic CLI download failure" in str(error)
    app.window.show_all()
    assert app.show_update_result()
    assert "Synthetic CLI download failure" in app.toast.message.get_text()
    assert "Older update succeeded" not in app.toast.message.get_text()
    print("PASS: refused updates ignore old outcomes; pending/acknowledged results preserve newer failures")


def timers_and_levels(app, output):
    app.preferences.set_notification_settings({"toast_seconds": 3})
    accents = set()
    for level, kind in (("info", Gtk.MessageType.INFO), ("warn", Gtk.MessageType.WARNING), ("error", Gtk.MessageType.ERROR)):
        app.show_feedback(f"{level.capitalize()} message", level)
        assert app.toast.get_message_type() == kind
        capture(app.window, output / f"level-{level}.png")
        color = app.toast.get_style_context().get_property("border-left-color", Gtk.StateFlags.NORMAL)
        x, y = app.toast.translate_coordinates(app.window, 2, 10)
        pixel = Gdk.pixbuf_get_from_window(app.window.get_window(), x, y, 1, 1)
        expected = tuple(round(channel * 255) for channel in (color.red, color.green, color.blue))
        rendered = tuple(pixel.get_pixels()[:3])
        assert all(abs(actual - target) <= 2 for actual, target in zip(rendered, expected)), (level, rendered, expected)
        accents.add(rendered)
    assert len(accents) == 3, "message levels must have different visible accents"
    started = time.monotonic()
    app.show_feedback("Default duration")
    wait_for(lambda: not app.toast.get_visible(), timeout=6)
    assert time.monotonic() - started >= 2.8
    app.preferences.set_notification_settings({"toast_seconds": 1})
    app.show_feedback("Old result")
    pause = time.monotonic()
    wait_for(lambda: time.monotonic() - pause >= 0.7)
    app.show_feedback("New result")
    pause = time.monotonic()
    wait_for(lambda: time.monotonic() - pause >= 0.5)
    assert app.toast.get_visible() and app.toast.message.get_text() == "New result", "old timer hid the replacement"
    wait_for(lambda: not app.toast.get_visible())
    app.show_feedback("Dismiss me")
    app.toast.response(Gtk.ResponseType.CLOSE)
    assert app.toast.timeout is None and not app.toast.get_visible()
    atomic_json(app.preferences.config / "preferences.json", {"toast_seconds": 0})
    app.show_error("Invalid preferences must not prevent error feedback")
    assert app.toast.get_visible() and app.toast.get_message_type() == Gtk.MessageType.ERROR
    app.preferences.set_notification_settings({"toast_seconds": 3})
    with patch.object(app.preferences, "toast_seconds", side_effect=PermissionError("Synthetic preferences access failure")):
        app.show_error("Unreadable preferences must not prevent error feedback")
    assert app.toast.get_visible() and app.toast.get_message_type() == Gtk.MessageType.ERROR


def coordinator_feedback(server):
    release = {**metadata(), "sha256": "a" * 64}
    for result in ({"updated": TARGET_VERSION}, MedicError("Synthetic cancelled install")):
        calls, finished = len(server.calls), []
        with patch("sys.argv", ["updater", TARGET_VERSION, "a" * 64, "--restart", "watch"]), patch("mount_medic.updates.fetch_release", return_value=release), patch("mount_medic.updates.install", side_effect=result if isinstance(result, Exception) else None, return_value=result):
            args = SimpleNamespace(version=TARGET_VERSION, sha256="a" * 64, restart="watch")
            thread = Thread(target=lambda: finished.append(updates.run_update(args)))
            thread.start()
            wait_for(lambda: bool(finished), timeout=5)
            thread.join()
        assert len(server.calls) == calls + 1
        assert finished == ([2] if isinstance(result, Exception) else [0])
        assert server.calls[-1][3] == ("Mount Medic update failed" if isinstance(result, Exception) else "Mount Medic updated")


def main(output):
    output.mkdir(parents=True, exist_ok=True)
    if os.environ.get("MM_UI_FONT"):
        Gtk.Settings.get_default().set_property("gtk-font-name", os.environ["MM_UI_FONT"])
    tray = TrayWatcher()
    server = NotificationServer()
    app = MedicApplication()
    app.register(None)
    app.build_window()
    app.window.show_all()
    app.setup_indicator()
    volume = Volume(Identity("ABCD1234", "sample-disk", "part-a", 500_000_000_000, 2048), "/dev/example1", "Games")
    report = diagnosis(volume, "dirty")
    report.update(settings={"monitor": True}, checked_at=1791277200)
    manual_checks(app, report, server, output)
    mutation_feedback(app, report, output)
    update_feedback(app, output)
    timers_and_levels(app, output)
    coordinator_feedback(server)
    if app.notifications:
        app.notifications.close_all()
    app.window.destroy()
    assert app.toast.timeout is None
    app.pool.shutdown()
    server.release()
    print("PASS: native tray/no-GUI success/repeat/empty/mixed/error/busy notifications; GUI check and mutation feedback; levels, 3s/configured expiry, timer reset/dismiss/destroy; install completion messages")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XDG_CONFIG_HOME": directory, "XDG_STATE_HOME": directory, "XDG_CACHE_HOME": directory}):
        main(Path(sys.argv[1]))
