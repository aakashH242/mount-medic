"""Exercise retained check times and drive details with real native GTK."""
import os
from pathlib import Path
from dataclasses import replace
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk
from mount_medic import dialogs
from mount_medic.appearance import checked_time
from mount_medic.desktop import MedicApplication
from mount_medic.model import Identity, Volume, diagnosis
from mount_medic.usage import GB, filesystem_usage
from ui_smoke import capture, settle, settle_until, widgets


class App(MedicApplication):
    def discover_notifications(self, rows):
        pass

    def notify(self, *args, **kwargs):
        pass


def history(app, volume):
    timestamp = 1791277200
    checked = {**diagnosis(volume, "dirty"), "checked_at": timestamp, "complete": True, "actions": ["repair"]}
    app.checked([checked])
    for state, monitored in (("mounted_rw", True), ("mounted_ro", False), ("absent", False), ("unmanaged", False)):
        row = diagnosis(volume, state)
        row["settings"] = {"monitor": monitored}
        app.render([row])
        assert app.rows[volume.key].get("checked_at") == timestamp, (state, "Last check was lost", app.rows[volume.key])
        if state in {"mounted_rw", "mounted_ro", "absent"}:
            assert app.rows[volume.key]["state"] == state
            assert app.rows[volume.key]["actions"] == [], "A historical repair became eligible"
        assert app.store[0][3] == checked_time(timestamp)
        app.ignore_drive(volume.as_dict())
        assert app.store[0][3] == checked_time(timestamp)
    other = Volume(Identity("other", "other-disk", "partition-b", 500 * GB, 2048), "/dev/example2", "Work")
    app.render([diagnosis(other, "unmanaged"), diagnosis(volume, "mounted_rw")])
    assert app.rows[volume.key]["checked_at"] == timestamp
    app.render([diagnosis(volume, "mounted_rw")])
    assert app.rows[volume.key]["checked_at"] == timestamp
    restarted = App()
    try:
        restarted.render([diagnosis(volume, "mounted_rw")])
        assert restarted.rows[volume.key]["checked_at"] == timestamp, "Restart/update lost the saved check"
        assert restarted.rows[volume.key]["actions"] == []
    finally:
        restarted.pool.shutdown()
    print("PASS: Last check survives mounted/disconnected/monitor changes and restart")


def cycle_results(app, volume):
    row = diagnosis(volume, "unknown")
    row["settings"] = {"monitor": True, "auto_repair": True, "auto_mount": True}
    mounted = replace(volume, mounts=["/fixture"], usage=filesystem_usage(500 * GB, 125 * GB))
    repaired = {**diagnosis(mounted, "mounted_rw"), "checked_at": 1791277300,
                "repair": {"status": "success"}}
    with patch("mount_medic.client.list_volumes", return_value=[row]), \
         patch("mount_medic.client.call", return_value=[{**diagnosis(volume, "dirty"), "actions": ["repair"], "complete": True}]), \
         patch("mount_medic.client.repair", return_value=repaired):
        app.cycle()
        settle_until(lambda: not app.busy)
    current = app.rows[volume.key]
    assert current["volume"]["mounts"] == ["/fixture"], "Pre-repair discovery replaced the mounted result"
    assert current["volume"]["usage"] == mounted.usage
    assert current["checked_at"] == repaired["checked_at"]
    assert current["settings"] == row["settings"]
    absent = {"volume": {"id": volume.key}, "state": "absent", "actions": [],
              "checked_at": 1791277400, "next_steps": "Reconnect this enrolled volume."}
    with patch("mount_medic.client.list_volumes", return_value=[{**row, "volume": mounted.as_dict()}]), \
         patch("mount_medic.client.call", return_value=[absent]):
        app.cycle()
        settle_until(lambda: not app.busy)
    current = app.rows[volume.key]
    assert current["state"] == "absent"
    assert current["volume"]["identity"] == volume.as_dict()["identity"]
    assert current["volume"]["label"] == volume.label
    assert not current["volume"]["mounts"] and current["volume"]["usage"] is None
    assert current["checked_at"] == absent["checked_at"]
    print("PASS: automatic recovery keeps fresh mounted usage; disconnect during check retains drive identity")


def usage_views(app, volume, output):
    volume.mounts = ["/fixture"]
    volume.usage = filesystem_usage(500 * GB, 125 * GB)
    report = diagnosis(volume, "mounted_rw")
    app.render([report])
    app.window.show_all()
    text = "125.0 / 500.0 GB (75.0% free)"
    assert text in app.store[0][1]
    assert text == app.usage_summary.get_text()
    assert text == app.identity_fields["Storage"].get_text()
    assert all(label.get_selectable() for label in app.identity_fields.values())
    assert app.identity_fields["Filesystem UUID"].get_accessible().get_name() == "Filesystem UUID: ABCD1234"
    assert app.storage_meter.get_visible() and app.storage_meter.get_fraction() == 0.25
    app.identity_expander.set_expanded(True)
    settle()
    capture(app.window, output / "drives.png")
    failures = []
    def inspect():
        dialog = next((window for window in Gtk.Window.list_toplevels() if isinstance(window, Gtk.Dialog)), None)
        if dialog is None:
            return True
        try:
            tree = next(widget for widget in widgets(dialog) if isinstance(widget, Gtk.TreeView))
            model = tree.get_model()
            assert model[0][0] == "Next steps" and model[0][1] == report["next_steps"]
            values = []
            model.foreach(lambda model, path, row: values.append((model[row][0], model[row][1])) and False)
            assert ("Free space", "75.0%") in values
            assert ("Used", "125.0 GB (125,000,000,000 bytes)") in values
            assert ("Free", "375.0 GB (375,000,000,000 bytes)") in values
            assert any(isinstance(widget, Gtk.Label) and widget.get_text() == text for widget in widgets(dialog))
            capture(dialog, output / "diagnostics.png")
            dialog.resize(520, 380)
            settle()
            scroll = tree.get_ancestor(Gtk.ScrolledWindow)
            horizontal = scroll.get_hadjustment()
            assert horizontal.get_upper() <= horizontal.get_page_size() + 1
            capture(dialog, output / "diagnostics-compact.png")
        except Exception as error:
            failures.append(error)
        finally:
            dialog.response(Gtk.ResponseType.CLOSE)
        return False
    GLib.timeout_add(100, inspect)
    dialogs.drive_diagnostics(app.window, report)
    assert not failures, failures
    volume.mounts = []
    volume.usage = None
    app.reports.clear()
    app.render([diagnosis(volume, "unmanaged")])
    assert "Usage unavailable" in app.store[0][1]
    assert not app.storage_meter.get_visible()
    app.render([])
    assert all(not label.get_text() for label in app.identity_fields.values())
    print("PASS: shared GB/percentage in table, details and diagnostics; Next steps first; unknown usage explicit")


def main(output):
    output.mkdir(parents=True, exist_ok=True)
    if os.environ.get("MM_UI_FONT"):
        Gtk.Settings.get_default().set_property("gtk-font-name", os.environ["MM_UI_FONT"])
    app = App()
    app.register(None)
    app.build_window()
    volume = Volume(Identity("ABCD1234", "fixture-disk", "partition-a", 500_000_000_000, 2048), "/dev/example1", "Games")
    try:
        history(app, volume)
        cycle_results(app, volume)
        usage_views(app, volume, output)
    finally:
        app.window.destroy()
        app.pool.shutdown()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, XDG_CONFIG_HOME=directory, XDG_STATE_HOME=directory):
        main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(directory) / "screens")
