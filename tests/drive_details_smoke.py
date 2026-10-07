"""Exercise retained check times and drive details with real native GTK."""
import os
from pathlib import Path
from dataclasses import replace
from datetime import datetime
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
    assert app.store[0][3] == checked_time(repaired["checked_at"])
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
    monitored = {**row, "settings": {"monitor": True, "auto_repair": False, "auto_mount": False}}
    automatic = {**diagnosis(volume, "clean"), "checked_at": 1791277500}
    with patch("mount_medic.client.list_volumes", return_value=[monitored]), \
         patch("mount_medic.client.call", return_value=[automatic]):
        app.cycle()
        settle_until(lambda: not app.busy)
    assert app.store[0][3] == checked_time(automatic["checked_at"]), "Automatic checks must update Last check"
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
            assert all(column.get_resizable() for column in tree.get_columns())
            value_column = tree.get_column(1)
            value_column.set_fixed_width(300)
            settle()
            assert value_column.get_width() >= 300
            value_column.set_fixed_width(220)
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


def table_columns(app, volume, output):
    for hour, suffix in ((0, "12:05 AM"), (12, "12:05 PM"), (23, "11:05 PM")):
        assert checked_time(datetime(2026, 10, 7, hour, 5).timestamp()) == f"07 Oct {suffix}"
    displayed = replace(volume, mounts=["/fixture"], usage=filesystem_usage(500 * GB, 125 * GB))
    app.render([{**diagnosis(displayed, "mounted_rw"), "checked_at": datetime(2026, 10, 7, 13, 5).timestamp()}])
    tree = app.selection.get_tree_view()
    columns = tree.get_columns()
    assert all(column.get_resizable() for column in columns)
    status = columns[2]
    renderer = status.get_cells()[0]
    app.store[0][2] = "Available / mounted read-only"
    status.set_fixed_width(120)
    settle()
    status.cell_set_cell_data(app.store, app.store.get_iter_first(), False, False)
    assert status.get_width() == 120
    assert renderer.get_property("wrap-width") == 96
    assert renderer.get_preferred_height_for_width(tree, 120)[0] > renderer.get_preferred_height_for_width(tree, 1000)[0]
    assert columns[3].get_cells()[0].get_property("xpad") == 20
    columns[3].cell_set_cell_data(app.store, app.store.get_iter_first(), False, False)
    assert columns[3].get_width() >= columns[3].get_cells()[0].get_preferred_width(tree)[0]
    capture(app.window, output / "drives-resized-status.png")
    status.set_fixed_width(240)
    settle()
    assert status.get_width() == 240 and renderer.get_property("wrap-width") == 216
    print("PASS: resizable drive columns, Status wraps at resized width, Last check AM/PM and padding")


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
        table_columns(app, volume, output)
        permissions_view(app, volume, output)
    finally:
        app.window.destroy()
        app.pool.shutdown()


def permissions_view(app, volume, output):
    settings = {"monitor": True, "auto_repair": True, "auto_mount": True}
    displayed = replace(volume, label="Games_Ext", mounts=["/fixture"],
                        usage=filesystem_usage(500_100_000_000, 228_100_000_000)).as_dict()
    failures = []
    for response in (Gtk.ResponseType.CANCEL, Gtk.ResponseType.OK):
        if response == Gtk.ResponseType.OK:
            displayed["label"] = "<b>Games & backup</b> " * 4
            displayed["identity"]["hardware"] = "eui." + "0123456789abcdef" * 4
        def inspect():
            dialog = next((window for window in Gtk.Window.list_toplevels() if window.get_title() == "Drive permissions"), None)
            if dialog is None:
                return True
            try:
                grid = next(item for item in widgets(dialog) if isinstance(item, Gtk.Grid) and item.get_style_context().has_class("drive-details"))
                for index, (title, text) in enumerate(dialogs.drive_fields(displayed).items()):
                    value = grid.get_child_at(1, index)
                    assert value.get_text() == text and value.get_selectable()
                    assert value.get_accessible().get_name() == f"{title}: {text}"
                labels = [item for item in widgets(dialog) if isinstance(item, Gtk.Label)]
                notice = next(item for item in labels if item.get_text().startswith("Repair resets"))
                assert "interrupted writes may be lost" in notice.get_text()
                assert "Back up first" in notice.get_text() and "cannot replace Windows chkdsk" in notice.get_text()
                assert notice.get_accessible().get_description() == dialogs.REPAIR_NOTICE
                assert any(item.get_text() == "Back up before repair" for item in labels)
                auth = next(item for item in labels if item.get_text() == "Applying changes requires administrator authentication.")
                assert auth.get_style_context().has_class("permission-auth")
                capture(dialog, output / ("permissions.png" if response == Gtk.ResponseType.CANCEL else "permissions-long.png"))
                dialog.resize(520, dialog.get_size()[1])
                settle()
                monitor = dialog.get_display().get_monitor_at_window(dialog.get_window()).get_workarea()
                assert dialog.get_allocated_height() <= monitor.height, ("Permission dialog exceeds the screen", dialog.get_allocated_height(), monitor.height)
                scroll = grid.get_ancestor(Gtk.ScrolledWindow)
                for item in (grid, notice, auth):
                    x, y = item.translate_coordinates(dialog, 0, 0)
                    assert x >= 0 and x + item.get_allocated_width() <= dialog.get_allocated_width()
                adjustment = scroll.get_vadjustment()
                adjustment.set_value(adjustment.get_upper() - adjustment.get_page_size())
                settle()
                x, y = auth.translate_coordinates(dialog, 0, 0)
                assert y >= 0 and y + auth.get_allocated_height() <= dialog.get_allocated_height()
                capture(dialog, output / ("permissions-compact.png" if response == Gtk.ResponseType.CANCEL else "permissions-long-compact.png"))
                checks = {item.get_label(): item for item in widgets(dialog) if isinstance(item, Gtk.CheckButton)}
                assert all(item.get_active() for item in checks.values())
                checks["Background checks"].set_active(False)
                for name in ("Automatic repair", "Automatic mounting"):
                    assert not checks[name].get_active() and not checks[name].get_sensitive()
            except Exception as error:
                failures.append(error)
            finally:
                dialog.response(response)
            return False
        GLib.timeout_add(100, inspect)
        result = dialogs.drive_permissions(app.window, displayed, settings)
        assert not failures, failures
        expected = None if response == Gtk.ResponseType.CANCEL else dict.fromkeys(settings, False)
        assert result == expected
        assert settings == dict.fromkeys(settings, True), "Dialog mutated caller settings"
    assert not failures, failures
    print("PASS: styled copyable permissions summary/warnings, compact literal long values, cancel/apply and permission dependencies")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, XDG_CONFIG_HOME=directory, XDG_STATE_HOME=directory):
        main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(directory) / "screens")
