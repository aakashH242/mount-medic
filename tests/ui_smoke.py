"""Render the real GTK window with synthetic drives in an isolated display."""
import sys
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mount_medic.desktop import MedicApplication, checked_time
from mount_medic.appearance import ASSETS
from mount_medic.protocol import BUS_NAME
from mount_medic.model import Identity, Volume, diagnosis
from mount_medic.model import MedicError
from mount_medic.storage import Preferences
from mount_medic import updates, __version__
from update_fixture import TARGET_VERSION, metadata


class TrayWatcher:
    """A private native tray host; no user desktop or drive service is involved."""
    def __init__(self):
        self.items = []
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        interface = Gio.DBusNodeInfo.new_for_xml('''<node><interface name="org.kde.StatusNotifierWatcher">
          <method name="RegisterStatusNotifierItem"><arg type="s" direction="in"/></method>
          <property name="IsStatusNotifierHostRegistered" type="b" access="read"/>
          <property name="RegisteredStatusNotifierItems" type="as" access="read"/>
          <property name="ProtocolVersion" type="i" access="read"/>
        </interface></node>''').interfaces[0]
        self.registration = self.bus.register_object("/StatusNotifierWatcher", interface, self.register, self.property, None)
        self.bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "RequestName",
                          GLib.Variant("(su)", ("org.kde.StatusNotifierWatcher", 0)), None, Gio.DBusCallFlags.NONE, 1000, None)

    def register(self, connection, sender, path, interface, method, parameters, invocation):
        value = parameters.unpack()[0]
        self.items.append((sender, value) if value.startswith("/") else (value, "/StatusNotifierItem"))
        invocation.return_value(None)

    def property(self, connection, sender, path, interface, name):
        return {"IsStatusNotifierHostRegistered": GLib.Variant("b", True), "ProtocolVersion": GLib.Variant("i", 0),
                "RegisteredStatusNotifierItems": GLib.Variant("as", [service + path for service, path in self.items])}[name]


def update_flows(app, output, sent):
    release = {**metadata(), "sha256": "a" * 64, "size": 123}
    app.preferences.save_updates({"release": release, "last_check": 1790000000})
    app.update_settings()
    dialog = app.update_dialog
    assert dialog.install.get_sensitive() and dialog.ignore.get_sensitive()
    assert f"Installed: {__version__}" in dialog.summary.get_text() and f"Available: {TARGET_VERSION}" in dialog.summary.get_text()
    capture(dialog, output.with_stem(output.stem + "-updates"))
    dialog.ignore.clicked()
    assert app.preferences.updates()["ignored"] == TARGET_VERSION and not dialog.ignore.get_sensitive()
    assert dialog.install.get_sensitive(), "ignored versions must still be installable manually"
    with patch("mount_medic.updates.fetch_release", side_effect=MedicError("Offline fixture")):
        # The public check stores a truthful failure before returning it to GTK.
        with patch("mount_medic.updates.check", side_effect=lambda prefs: prefs.save_updates({"error": "Update check failed: offline"})):
            dialog.check.clicked()
            settle_until(lambda: not app.update_checking)
    assert "offline" in dialog.summary.get_text()
    app.preferences.save_updates({"error": None, "ignored": None, "notified": None, "last_attempt": 0})
    sent.reset_mock()
    with patch("mount_medic.updates.fetch_release", return_value=release):
        app.check_updates()
        settle_until(lambda: not app.update_checking)
    assert sent.call_count == 1
    assert sent.call_args.args == ("update:" + TARGET_VERSION, f"Mount Medic {TARGET_VERSION} is available", f"Installed: {__version__}")
    assert sent.call_args.kwargs["actions"] == updates.UPDATE_ACTIONS
    assert app.preferences.updates()["notified"] is None, "a failed delivery must remain eligible for retry"
    app.preferences.save_updates({"last_attempt": 0, "install_error": "Administrator authentication cancelled"})
    with patch("mount_medic.updates.fetch_release", return_value=release):
        app.check_updates()
        settle_until(lambda: not app.update_checking)
    assert sent.call_count == 2, "a previous install failure must not suppress successful check notifications"
    sent.call_args.kwargs["on_sent"]()
    assert app.preferences.updates()["notified"] == TARGET_VERSION
    app.preferences.save_updates({"last_attempt": 0})
    with patch("mount_medic.updates.fetch_release", return_value=release):
        app.check_updates()
        settle_until(lambda: not app.update_checking)
    assert sent.call_count == 2, "successful delivery must suppress repeated hourly alerts"
    with patch("mount_medic.updates.fetch_release", return_value=release):
        app.check_updates(manual=True)
        settle_until(lambda: not app.update_checking)
    assert sent.call_count == 2, "manual checks must not repeat a dismissed update alert"
    with patch.object(Path, "is_file", return_value=True), patch("mount_medic.desktop.subprocess.Popen") as spawn:
        spawn.return_value.poll.return_value = None
        dialog.install.clicked()
        assert spawn.call_args.args[0][-4:] == [TARGET_VERSION, "a" * 64, "--restart", "gui"]
        assert app.update_dialog is None
    app.update_candidate = release
    app.notification_action("update.ignore", "update:" + TARGET_VERSION)
    assert app.preferences.updates()["ignored"] == TARGET_VERSION
    command = type("Command", (), {"get_arguments": lambda unused: ["mount-medic", "--quit-for-update"]})()
    app.busy = True
    with patch.object(app, "quit", side_effect=AssertionError("quit during repair")):
        assert app.do_command_line(command) == 2
    app.busy = False


def settle():
    loop = GLib.MainLoop()
    GLib.timeout_add(120, lambda: (loop.quit(), False)[1])
    loop.run()


def settle_until(predicate):
    for unused in range(20):
        settle()
        if predicate():
            return
    raise AssertionError("The window manager did not apply the requested window state")


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
    tray = theme.lookup_icon(BUS_NAME + "-symbolic", 16, Gtk.IconLookupFlags.FORCE_SIZE)
    assert tray and tray.get_filename().startswith(root), "installed symbolic tray icon did not resolve"
    assert tray.load_icon().get_width() == 16
    entry = GLib.KeyFile()
    entry.load_from_file(str(Path(root) / f"usr/local/share/applications/{BUS_NAME}.desktop"), GLib.KeyFileFlags.NONE)
    assert entry.get_string("Desktop Entry", "Icon") == BUS_NAME
    assert entry.get_string("Desktop Entry", "StartupWMClass") == Gdk.get_program_class()


def widgets(widget):
    yield widget
    if isinstance(widget, Gtk.Container):
        children = []
        widget.forall(children.append)
        for child in children:
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
        assert all(item.get_tooltip_text() == item.get_accessible().get_description() for item in controls)
        labels = [item.get_text() for item in widgets(dialog) if isinstance(item, Gtk.Label)]
        assert not any(item.get_tooltip_text() in labels for item in controls), "checkbox explanations are repeated on screen"
        assert any("interrupted writes may be lost" in text and "chkdsk" in text for text in labels)
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
        selected = tree.get_model()[0]
        assert "Volume ID" not in selected[1] and selected[0] in selected[2]
        assert tree.get_accessible().get_description() == selected[2]
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
        assert "1–600 seconds" in spin.get_tooltip_text()
        assert spin.get_tooltip_text() == spin.get_accessible().get_description()
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

    app.selection.select_path(Gtk.TreePath.new_from_indices([0]))
    app.selected().update(checked_at=1791277200, complete=True,
                          probe={"read_only": True, "journal": {"clean": False}, "warnings": []})
    def diagnostics(dialog):
        assert dialog.get_resizable()
        assert not any(isinstance(item, Gtk.TextView) for item in widgets(dialog)), "diagnostics still render JSON text"
        table = next(item for item in widgets(dialog) if isinstance(item, Gtk.TreeView))
        assert [column.get_title() for column in table.get_columns()] == ["Field", "Value"]
        cells = []
        def collect(model, path, iterator):
            cells.append(tuple(model[iterator]))
            return False
        table.get_model().foreach(collect)
        assert ("Read only", "Yes") in cells and ("Clean", "No") in cells
        assert ("Warnings", "None") in cells
        assert ("Last checked", checked_time(1791277200)) in cells
        assert any(name == "Size" and "GiB" in value and "bytes" in value for name, value in cells)
        assert ("Filesystem UUID", volume.identity.uuid) in cells
        capture(dialog, output.with_stem(output.stem + "-diagnostics"))
        dialog.resize(520, 380)
        settle()
        scroll = table.get_ancestor(Gtk.ScrolledWindow)
        horizontal = scroll.get_hadjustment()
        assert horizontal.get_upper() <= horizontal.get_page_size() + 1, "diagnostic values overflow the compact viewport"
        capture(dialog, output.with_stem(output.stem + "-diagnostics-compact"))
        return Gtk.ResponseType.CLOSE
    respond("Drive diagnostics", diagnostics)
    app.details(None)
    assert not failures, failures


def window_lifecycle(app, tray):
    events = []
    failures = []
    tray_alive = []
    window = app.window
    deadline = GLib.get_monotonic_time() + 5_000_000

    def hide():
        header = window.get_titlebar()
        close = next(item for item in widgets(header) if isinstance(item, Gtk.Button) and item.get_style_context().has_class("close"))
        close.clicked()
        GLib.timeout_add(100, hidden)
        return False

    def hidden():
        try:
            assert app.background and not window.get_visible(), "closing exited or left the UI visible"
            if app.indicator:
                def received(connection, result, unused):
                    try:
                        assert connection.call_finish(result).unpack()[0] == "Active", "closing deactivated the native tray"
                        tray_alive.append(True)
                    except Exception as error:
                        failures.append(error)
                        app.quit()
                service, path = tray.items[-1]
                tray.bus.call(service, path, "org.freedesktop.DBus.Properties", "Get",
                              GLib.Variant("(ss)", ("org.kde.StatusNotifierItem", "Status")), None,
                              Gio.DBusCallFlags.NONE, 1000, None, received, None)
            app.show_error("Background check needs attention")
            app.notify.assert_called_with("application", "Mount Medic needs attention", "Background check needs attention")
            events.append("hidden")
            app.device_event()
            GLib.timeout_add(100, reopen)
        except Exception as error:
            failures.append(error)
            app.quit()
        return False

    def reopen():
        if ("background event" not in events or (app.indicator and not tray_alive)) and GLib.get_monotonic_time() < deadline:
            return True
        try:
            assert "background event" in events, "hotplug callback stopped with the window hidden"
            assert not app.indicator or tray_alive, "native tray did not respond while the window was hidden"
            app.activate()
            assert app.window is window and window.get_visible(), "reopen did not restore the existing window"
            events.append("reopened")
        except Exception as error:
            failures.append(error)
        settings = next(item for item in widgets(window.get_titlebar()) if isinstance(item, Gtk.MenuButton) and item.get_label() == "Settings")
        quit_item = next(item for item in settings.get_popup().get_children() if item.get_label() == "Quit Mount Medic")
        quit_item.activate()
        return False

    app.background = False
    def background_event():
        events.append("background event")
        return False
    with patch.object(app, "start_watching"), patch.object(app, "refresh"), patch.object(app, "cycle", side_effect=background_event):
        GLib.timeout_add(50, hide)
        assert app.run(["mount-medic", "gui"]) == 0
    assert not failures, failures
    assert events == ["hidden", "background event", "reopened"], events


def main():
    output = Path(sys.argv[1])
    output.parent.mkdir(parents=True, exist_ok=True)
    tray_host = TrayWatcher()
    app = MedicApplication()
    notifier = patch.object(app, "notify")
    sent = notifier.start()
    Gtk.Settings.get_default().set_property("gtk-decoration-layout", "menu:")
    app.register(None)
    with patch.object(app, "start_watching") as watching, patch.object(app, "refresh"):
        app.activate()
        watching.assert_called_once_with()
        assert not app.background
    with patch.object(app, "start_watching"), patch.object(app, "hold") as hold:
        app.watch_in_background()
        app.watch_in_background()
        assert hold.call_count == 1 and app.close_window(app.window, None)
        app.background = False
    app.window.show_all()
    settle()
    header = app.window.get_titlebar()
    assert header.get_decoration_layout() == ":minimize,maximize,close"
    assert app.window.get_resizable()
    assert len([item for item in widgets(header) if isinstance(item, Gtk.Image) and item.get_icon_name()[0] == BUS_NAME]) == 1
    assert all(item.get_layout().get_unknown_glyphs_count() == 0 for item in widgets(header)
               if isinstance(item, Gtk.Label)), "the test display is missing fonts"
    controls = [item for item in widgets(header) if isinstance(item, Gtk.Button) and item.get_style_context().has_class("titlebutton")]
    assert len(controls) == 3, "minimize, maximize/restore and close controls are not all visible"
    assert all(control.get_visible() and control.get_mapped() for control in controls)
    for control in controls:
        x, y = control.translate_coordinates(app.window, 0, 0)
        rendered = Gdk.pixbuf_get_from_window(app.window.get_window(), x, y,
                                             control.get_allocated_width(), control.get_allocated_height())
        pixels = rendered.get_pixels()
        stride, channels = rendered.get_rowstride(), rendered.get_n_channels()
        colors = {pixels[row * stride + column * channels:row * stride + column * channels + 3]
                  for row in range(rendered.get_height()) for column in range(rendered.get_width())}
        assert len(colors) > 1, "the theme's window-control icon was erased by application CSS"
    if os.environ.get("MM_UI_WINDOW_MANAGER"):
        maximize = next(item for item in controls if item.get_style_context().has_class("maximize"))
        maximize.clicked()
        settle_until(app.window.is_maximized)
        assert app.window.is_maximized()
        maximize = next(item for item in widgets(header) if isinstance(item, Gtk.Button) and item.get_style_context().has_class("maximize"))
        maximize.clicked()
        settle_until(lambda: not app.window.is_maximized())
        assert not app.window.is_maximized()
        minimize = next(item for item in widgets(header) if isinstance(item, Gtk.Button) and item.get_style_context().has_class("minimize"))
        minimize.clicked()
        settle_until(lambda: app.window.get_window().get_state() & Gdk.WindowState.ICONIFIED)
        assert app.window.get_window().get_state() & Gdk.WindowState.ICONIFIED
        app.window.deiconify()
        app.window.present()
        app.window.resize(900, 700)
        settle_until(lambda: app.window.get_size().width >= 900)
        assert app.window.get_size().width >= 900
    settings = Gtk.Settings.get_default()
    settings.set_property("gtk-enable-animations", False)
    if os.environ.get("MM_UI_FONT"):
        settings.set_property("gtk-font-name", os.environ["MM_UI_FONT"])
    installed_icon()
    icon = Gtk.IconTheme.get_default().lookup_icon(BUS_NAME, 32, Gtk.IconLookupFlags.FORCE_SIZE)
    assert icon.load_icon().get_width() == 32
    resolved_icon = Path(icon.get_filename())
    assert resolved_icon.read_bytes() == (ASSETS / resolved_icon.name).read_bytes()
    assert app.window.get_icon_name() == BUS_NAME
    app.setup_indicator()
    if app.indicator:
        settle_until(lambda: bool(tray_host.items))
        assert tray_host.items, "indicator did not register with the native tray host"
        assert app.indicator.get_icon() == BUS_NAME + "-symbolic"
        assert app.indicator.get_icon_theme_path() == str(ASSETS)
    tray = Gtk.IconTheme.get_default().lookup_icon(BUS_NAME + "-symbolic", 16, Gtk.IconLookupFlags.FORCE_SIZE)
    for color in ("white", "black"):
        foreground = Gdk.RGBA()
        foreground.parse(color)
        pixbuf, symbolic = tray.load_symbolic(foreground)
        assert symbolic
        pixels = pixbuf.get_pixels()
        opaque = [pixels[index:index + 3] for index in range(0, len(pixels), 4) if pixels[index + 3] > 200]
        expected = 255 if color == "white" else 0
        assert opaque and all(abs(channel - expected) < 2 for pixel in opaque for channel in pixel), f"tray foreground {color}: {len(opaque)} opaque pixels, colors {set(opaque)}"
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
    assert app.detail.get_text() == "Back up before attempting repair."
    assert app.detail.get_tooltip_text() == problem["next_steps"]
    assert app.detail.get_accessible().get_description() == problem["next_steps"]
    for state, guidance in (("clean", "Saved result. Run a fresh check before acting."),
                            ("dirty", "Synthetic safety refusal: identity changed."),
                            ("io_failure", diagnosis(games, "io_failure")["next_steps"])):
        warning = {**problem, "state": state, "next_steps": guidance}
        app.render([warning])
        assert app.detail.get_text() == guidance, "critical or saved-result guidance was hidden in a tooltip"
    app.render(rows)
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
    update_flows(app, output, sent)
    window_lifecycle(app, tray_host)
    notifier.stop()
    app.window.destroy()
    app.pool.shutdown()
    print("PASS: single logo, native window controls, resizing, diagnostics table, close/background/reopen, GTK focus/gates, discovery/ignore/permissions/duration, screenshots")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XDG_CONFIG_HOME": directory, "XDG_STATE_HOME": directory}):
        main()
