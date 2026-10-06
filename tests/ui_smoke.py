"""Render the real GTK window with synthetic drives in an isolated display."""
import sys
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mount_medic.desktop import MedicApplication, checked_time
from mount_medic.appearance import ASSETS
from mount_medic.protocol import BUS_NAME
from mount_medic.model import Identity, Volume, diagnosis
from mount_medic.model import MedicError
from mount_medic.storage import Preferences


def settle():
    loop = GLib.MainLoop()
    GLib.timeout_add(120, lambda: (loop.quit(), False)[1])
    loop.run()


def capture(widget, output):
    settle()
    window = widget.get_window()
    image = Gdk.pixbuf_get_from_window(window, 0, 0, window.get_width(), window.get_height())
    image.savev(str(output), "png", [], [])


def installed_icon():
    root = os.environ.get("MM_STAGED_ROOT")
    if not root:
        return
    theme = Gtk.IconTheme.new()
    theme.set_search_path([str(Path(root) / "usr/local/share/icons"), "/usr/share/icons"])
    theme.set_custom_theme("hicolor")
    icon = theme.lookup_icon(BUS_NAME, 32, Gtk.IconLookupFlags.FORCE_SIZE)
    assert icon and icon.get_filename().startswith(root), "installed launcher icon did not resolve"
    assert icon.load_icon().get_width() == 32
    entry = GLib.KeyFile()
    entry.load_from_file(str(Path(root) / f"usr/local/share/applications/{BUS_NAME}.desktop"), GLib.KeyFileFlags.NONE)
    assert entry.get_string("Desktop Entry", "Icon") == BUS_NAME
    assert entry.get_string("Desktop Entry", "StartupWMClass") == Gdk.get_program_class()


def widgets(widget):
    yield widget
    if isinstance(widget, Gtk.Container):
        for child in widget.get_children():
            yield from widgets(child)


def drive_flows(app, volume, output, sent):
    row = diagnosis(volume, "unmanaged")
    key = volume.key
    app.reports.clear()
    app.discovered.clear()
    sent.reset_mock()
    app.render([row])
    app.render([row])
    assert sent.call_count == 1 and app.store[0][5] == "Not added"
    app.render([])
    app.render([row])
    assert sent.call_count == 2, "reconnection should offer the dismissed drive again"
    app.notification_action("ignore", key)
    assert key in Preferences().ignored() and app.store[0][5] == "Ignored"
    app.render([])
    app.render([row])
    assert sent.call_count == 2, "ignore must survive reconnection"
    app.discovered.clear()
    app.render([row])
    assert sent.call_count == 2, "ignore must survive a new watcher session"

    failures = []

    def respond(title, inspect):
        def ready():
            matches = [window for window in Gtk.Window.list_toplevels() if window.get_title() == title]
            if not matches:
                return True
            dialog = matches[0]
            try:
                answer = inspect(dialog)
            except Exception as error:
                failures.append(error)
                answer = Gtk.ResponseType.CANCEL
            dialog.response(answer)
            return False
        GLib.timeout_add(40, ready)

    def finished():
        for unused in range(30):
            settle()
            if not app.busy:
                break
        assert not app.busy
        assert not failures, failures

    def monitor_cancel(dialog):
        controls = [item for item in widgets(dialog) if isinstance(item, Gtk.CheckButton)]
        assert [item.get_active() for item in controls] == [True, False, False]
        assert controls[1].get_sensitive() and controls[2].get_sensitive()
        controls[1].set_active(True)
        controls[2].set_active(True)
        controls[0].set_active(False)
        assert [item.get_active() for item in controls] == [False, False, False]
        assert not controls[1].get_sensitive() and not controls[2].get_sensitive()
        controls[0].set_active(True)
        capture(dialog, output.with_stem(output.stem + "-monitor"))
        return Gtk.ResponseType.CANCEL

    respond("Drive permissions", monitor_cancel)
    with patch("mount_medic.client.list_volumes", return_value=[row]), patch("mount_medic.client.call", side_effect=AssertionError("cancel must not configure")):
        app.notification_action("monitor", key)
        finished()
    assert key in app.preferences.ignored(), "cancel must preserve the ignore"

    def monitor_apply(dialog):
        for item in widgets(dialog):
            if isinstance(item, Gtk.CheckButton):
                item.set_active(True)
        return Gtk.ResponseType.OK

    respond("Drive permissions", monitor_apply)
    with patch("mount_medic.client.list_volumes", return_value=[row]), patch("mount_medic.client.call", side_effect=MedicError("Synthetic authentication cancellation")):
        app.notification_action("monitor", key)
        finished()
    assert key in app.preferences.ignored()
    assert "authentication cancellation" in app.progress.get_text()

    permissions = {"monitor": True, "auto_repair": True, "auto_mount": True}
    respond("Drive permissions", monitor_apply)
    with patch("mount_medic.client.list_volumes", return_value=[row]), patch("mount_medic.client.call", return_value=permissions) as call, patch.object(app, "cycle") as cycle:
        app.notification_action("monitor", key)
        finished()
        assert call.call_args.args[0] == {"op": "configure", "id": key, **permissions}
        assert cycle.call_count == 1
    assert key not in app.preferences.ignored()
    row["settings"] = permissions
    app.render([row])
    assert app.store[0][5] == "Added" and app.buttons["Monitor"].get_label() == "Remove"
    assert not app.buttons["Ignore This Drive"].get_sensitive()
    with patch.object(app, "confirmation", return_value=False), patch("mount_medic.client.call", side_effect=AssertionError("cancelled removal wrote settings")):
        app.manage_selected(None)
    with patch.object(app, "confirmation", return_value=True), patch("mount_medic.client.call", return_value={"monitor": False}) as call, patch.object(app, "cycle"):
        app.manage_selected(None)
        finished()
        assert call.call_args.args[0] == {"op": "configure", "id": key, "monitor": False, "auto_repair": False, "auto_mount": False}

    replacement = {**row, "volume": {**row["volume"], "id": "f" * 24}}
    with patch("mount_medic.client.list_volumes", return_value=[replacement]), patch.object(app, "settings", side_effect=AssertionError("stale action must not configure replacement")):
        app.notification_action("monitor", key)
        finished()
    assert "disconnected" in app.progress.get_text()

    row["settings"] = {}
    app.render([row])
    app.ignore_selected(None)
    other = {**row["volume"], "id": "b" * 24, "device": "", "label": "Disconnected backup"}
    app.preferences.ignore(other)

    def unignore(dialog):
        tree = next(item for item in widgets(dialog) if isinstance(item, Gtk.TreeView))
        assert len(tree.get_model()) == 2
        tree.get_selection().select_path(Gtk.TreePath.new_first())
        capture(dialog, output.with_stem(output.stem + "-ignored"))
        return Gtk.ResponseType.OK

    respond("Ignore List", unignore)
    app.ignored_drives(None)
    assert not failures, failures
    assert key not in app.preferences.ignored() and other["id"] in app.preferences.ignored()
    assert app.store[0][5] == "Not added"
    app.preferences.unignore(other["id"])

    def duration(dialog):
        spin = next(item for item in widgets(dialog) if isinstance(item, Gtk.SpinButton))
        assert spin.get_value_as_int() == 10
        spin.set_value(23)
        capture(dialog, output.with_stem(output.stem + "-settings"))
        return Gtk.ResponseType.OK

    respond("Notification settings", duration)
    app.notification_settings(None)
    assert not failures, failures
    assert Preferences().notification_seconds() == 23
    respond("Notification settings", lambda dialog: Gtk.ResponseType.CANCEL)
    app.notification_settings(None)
    assert Preferences().notification_seconds() == 23
    assert not failures, failures


def main():
    output = Path(sys.argv[1])
    output.parent.mkdir(parents=True, exist_ok=True)
    app = MedicApplication()
    notifier = patch.object(app, "notify")
    sent = notifier.start()
    app.register(None)
    with patch.object(app, "start_watching") as watching, patch.object(app, "refresh"):
        app.activate()
        watching.assert_called_once_with()
        assert not app.background and not app.close_window(app.window, None)
    with patch.object(app, "start_watching"), patch.object(app, "hold") as hold:
        app.watch_in_background()
        app.watch_in_background()
        assert hold.call_count == 1 and app.close_window(app.window, None)
        app.background = False
    settings = Gtk.Settings.get_default()
    settings.set_property("gtk-enable-animations", False)
    if os.environ.get("MM_UI_FONT"):
        settings.set_property("gtk-font-name", os.environ["MM_UI_FONT"])
    installed_icon()
    icon = Gtk.IconTheme.get_default().lookup_icon(BUS_NAME, 32, Gtk.IconLookupFlags.FORCE_SIZE)
    assert icon.load_icon().get_width() == 32
    assert Path(icon.get_filename()).parent == ASSETS
    assert app.window.get_icon_name() == BUS_NAME
    app.setup_indicator()
    if app.indicator:
        assert app.indicator.get_icon() == BUS_NAME
        assert app.indicator.get_icon_theme_path() == str(ASSETS)
    work = Volume(Identity("ABCD1234", "sample-ssd", "part-a", 500_000_000_000, 2048),
                  "/dev/example1", "Work", mounts=["/media/Work"])
    games = Volume(Identity("BCD23456", "sample-ssd", "part-b", 1_000_000_000_000, 2048),
                   "/dev/example2", "Games")
    removable = Volume(Identity("CDE34567", "sample-usb", "part-c", 64_000_000_000, 2048),
                       "/dev/example3", "New USB drive")
    problem = diagnosis(games, "dirty")
    problem.update(actions=["repair"], checked_at=1791277200,
                   settings={"monitor": True, "auto_repair": False, "auto_mount": False})
    rows = [diagnosis(work, "mounted_rw"), problem, diagnosis(removable, "unmanaged")]
    app.reports[games.key] = {"settings": {"auto_repair": True}}
    app.render(rows)
    assert app.rows[games.key]["settings"]["auto_repair"] is False
    assert checked_time(10 ** 100) == "—"
    app.selection.select_path(Gtk.TreePath.new_from_indices([1]))
    app.window.show_all()
    while Gtk.events_pending():
        Gtk.main_iteration_do(False)
    assert app.buttons["Repair"].get_sensitive()
    assert not app.buttons["Mount"].get_sensitive()
    app.buttons["Check now"].grab_focus()
    assert app.window.get_focus() is app.buttons["Check now"]
    assert app.window.child_focus(Gtk.DirectionType.TAB_FORWARD)
    app.buttons["Permissions"].grab_focus()
    app.render(rows)
    assert app.selected()["volume"]["id"] == games.key
    assert app.window.get_focus() is app.buttons["Permissions"], "refresh lost keyboard focus"
    app.busy = True
    app.selection_changed(app.selection)
    assert not app.buttons["Check now"].get_sensitive()
    assert not app.refresh_button.get_sensitive()
    assert not app.more_button.get_sensitive()
    app.busy = False
    app.selection.select_path(Gtk.TreePath.new_from_indices([2]))
    assert not app.buttons["Repair"].get_sensitive()
    assert app.buttons["Repair"].get_tooltip_text()
    app.selection.select_path(Gtk.TreePath.new_from_indices([1]))
    capture(app.window, output)
    assert app.more_button.get_popup().get_children() == [app.buttons[name] for name in ("Mount", "Unmount and inspect", "Ignore This Drive", "Diagnostics")]
    app.identity_expander.set_expanded(True)
    assert games.key in app.identity_details.get_text()
    app.identity_expander.set_expanded(False)
    app.window.resize(640, 640)
    capture(app.window, output.with_stem(output.stem + "-compact"))
    assert app.buttons["Permissions"].get_allocation().width > 0
    failures = []

    def cancel_permissions():
        dialog = next(window for window in Gtk.Window.list_toplevels() if window.get_title() == "Drive permissions")
        try:
            capture(dialog, output.with_stem(output.stem + "-permissions"))
        except Exception as error:
            failures.append(error)
        dialog.response(Gtk.ResponseType.CANCEL)
        return False

    GLib.timeout_add(150, cancel_permissions)
    with patch("mount_medic.client.call", side_effect=AssertionError("cancelled permission dialog wrote settings")):
        app.settings()
    assert not failures, failures
    app.render([])
    assert app.drive_stack.get_visible_child_name() == "empty"
    app.window.show_all()
    assert not app.inspector.get_visible()
    assert not app.more_button.get_sensitive()
    capture(app.window, output.with_stem(output.stem + "-empty"))
    long_name = diagnosis(removable, "unmanaged")
    long_name["volume"]["label"] = "<b>Untrusted & long name</b> " * 12
    app.render([long_name])
    assert "&lt;b&gt;" in app.store[0][1]
    assert long_name["volume"]["label"] in app.identity_details.get_text()
    capture(app.window, output.with_stem(output.stem + "-long-label"))
    drive_flows(app, removable, output, sent)
    notifier.stop()
    app.window.destroy()
    app.pool.shutdown()
    print("PASS: GTK/icons, focus, gates, discovery/reconnect, ignore persistence/list, cancelled and saved permissions, removal/revocation, stale action, duration, screenshots")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XDG_CONFIG_HOME": directory, "XDG_STATE_HOME": directory}):
        main()
