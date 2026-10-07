"""Native dialogs shared by discovery actions and the drive manager."""
from gi.repository import Gtk, Pango

from . import appearance
from .engine import REPAIR_NOTICE
from .feedback import Toast
from .storage import MAX_NOTIFICATION_SECONDS, Preferences
from .usage import GB, storage_summary


class UpdatesDialog(Gtk.Dialog):
    def __init__(self, parent, on_check, on_ignore, on_install):
        super().__init__(title="Updates", transient_for=parent, use_header_bar=True)
        self.get_style_context().add_class("mount-medic")
        self.set_default_size(460, 300)
        self.set_resizable(True)
        self.add_button("Close", Gtk.ResponseType.CLOSE)
        self.connect("response", lambda dialog, response: dialog.destroy())
        area = self.get_content_area()
        area.set_border_width(16)
        area.set_spacing(12)
        self.toast = Toast(Preferences())
        area.pack_start(self.toast, False, False, 0)
        area.pack_start(appearance.label("Application updates", "section-title"), False, False, 0)
        self.summary = appearance.label("", "muted")
        self.summary.set_max_width_chars(56)
        self.summary.get_accessible().set_name("Update status")
        area.pack_start(self.summary, False, False, 0)
        self.notes = Gtk.LinkButton.new_with_label("https://github.com/aakashH242/mount-medic/releases", "Release notes")
        self.notes.set_halign(Gtk.Align.START)
        area.pack_start(self.notes, False, False, 0)
        actions = Gtk.Box(spacing=8)
        self.check = Gtk.Button(label="Check now")
        self.ignore = Gtk.Button(label="Ignore this version")
        self.install = Gtk.Button(label="Install")
        self.install.get_style_context().add_class("primary-action")
        for button, callback in ((self.check, on_check), (self.ignore, on_ignore), (self.install, on_install)):
            button.connect("clicked", callback)
            actions.pack_start(button, False, False, 0)
        area.pack_start(actions, False, False, 0)
        hint = appearance.label("Checks hourly while Mount Medic is running.", "muted")
        hint.set_tooltip_text("Closing the main window keeps checks running. Quitting stops them until next startup. "
                              "Install requests administrator access once, then restarts the app. Your drive permissions and history are retained.")
        hint.get_accessible().set_description(hint.get_tooltip_text())
        area.pack_start(hint, False, False, 0)

    def set_status(self, status, checking=False):
        self.release = status["release"]
        text = f"Installed: {status['installed']}"
        if status["available"]:
            text += f"\nAvailable: {status['available']}" + (" · ignored" if status["ignored"] else "")
        elif status["state"] == "current" and not status.get("error"):
            text += "\nYou’re up to date."
        else:
            text += "\nNo successful check yet."
        checked = status.get("last_check")
        if isinstance(checked, int):
            text += f"\nLast successful check: {appearance.checked_time(checked)}"
        if checking:
            text += "\nChecking GitHub…"
        elif status.get("error"):
            text += "\n" + status["error"]
        self.summary.set_text(text)
        self.check.set_sensitive(not checking)
        self.ignore.set_sensitive(bool(status["available"]) and not checking and not status["ignored"])
        self.install.set_sensitive(bool(status["available"]) and not checking)
        self.notes.set_uri(status.get("release_notes") or "https://github.com/aakashH242/mount-medic/releases")


def drive_fields(volume: dict) -> dict:
    identity = volume.get("identity", {})
    return {"Drive": volume.get("label") or "NTFS drive", "Storage": storage_summary(volume),
            "Device": volume.get("device") or "Disconnected", "Filesystem UUID": identity.get("uuid") or "Unknown",
            "Disk identity": identity.get("hardware") or "Unavailable"}


def drive_description(volume: dict) -> str:
    fields = drive_fields(volume)
    return f"{fields['Drive']}\n" + "\n".join(f"{name}: {value}" for name, value in fields.items() if name != "Drive")


def drive_diagnostics(parent, report):
    dialog = Gtk.Dialog(title="Drive diagnostics", transient_for=parent, modal=True, use_header_bar=True)
    dialog.get_style_context().add_class("mount-medic")
    dialog.set_default_size(680, 500)
    dialog.set_resizable(True)
    dialog.add_button("Close", Gtk.ResponseType.CLOSE)
    area = dialog.get_content_area()
    area.set_border_width(16)
    area.set_spacing(12)
    header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    header.get_style_context().add_class("diagnostic-heading")
    heading = appearance.label(report["volume"].get("label") or "NTFS drive", "section-title")
    heading.set_selectable(True)
    header.pack_start(heading, False, False, 0)
    summary = appearance.label(storage_summary(report["volume"]), "usage-summary")
    summary.set_selectable(True)
    header.pack_start(summary, False, False, 0)
    meter = appearance.storage_meter()
    appearance.update_storage_meter(meter, report["volume"])
    header.pack_start(meter, False, False, 0)
    area.pack_start(header, False, False, 0)
    model = Gtk.TreeStore(str, str, int)
    names = {"id": "Volume ID", "uuid": "Filesystem UUID", "hardware": "Disk identity",
             "readonly": "Read-only", "checked_at": "Last checked", "monitor": "Background checks",
             "auto_repair": "Automatic repair", "auto_mount": "Automatic mounting",
             "size": "Capacity", "start": "Partition start (sectors)", "devnum": "Device number",
             "usage": "Storage usage", "total": "Total", "used": "Used", "free": "Free", "free_percent": "Free space"}

    def add_fields(container, value):
        entries = value.items() if isinstance(value, dict) else enumerate(value, 1)
        for key, item in entries:
            title = names.get(key, str(key).replace("_", " ").capitalize())
            if isinstance(item, (dict, list)) and item:
                group = model.append(container, [title, "", Pango.Weight.SEMIBOLD])
                add_fields(group, item)
            else:
                model.append(container, [title, diagnostic_value(key, item), Pango.Weight.NORMAL])

    model.append(None, ["Next steps", diagnostic_value("next_steps", report.get("next_steps")), Pango.Weight.SEMIBOLD])
    add_fields(None, {key: value for key, value in report.items() if key != "next_steps"})
    tree = Gtk.TreeView(model=model, enable_search=True)
    tree.set_grid_lines(Gtk.TreeViewGridLines.HORIZONTAL)
    tree.get_accessible().set_name("Drive diagnostic fields and values")
    tree.set_tooltip_column(1)
    field = Gtk.CellRendererText(wrap_width=180, wrap_mode=Pango.WrapMode.WORD_CHAR, xpad=12, ypad=8)
    column = Gtk.TreeViewColumn("Field", field, text=0, weight=2)
    column.set_min_width(220)
    column.set_resizable(True)
    tree.append_column(column)
    value = Gtk.CellRendererText(wrap_width=360, wrap_mode=Pango.WrapMode.WORD_CHAR, xpad=12, ypad=8)
    column = Gtk.TreeViewColumn("Value", value, text=1)
    column.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
    column.set_fixed_width(220)
    column.set_resizable(True)
    column.set_expand(True)
    tree.append_column(column)
    def resize_values(widget, size):
        width = max(1, tree.get_column(1).get_width() - 24)
        if value.get_property("wrap-width") != width:
            value.set_property("wrap-width", width)
            tree.get_column(1).queue_resize()
    tree.connect("size-allocate", resize_values)
    tree.expand_all()
    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    scroll.add(tree)
    area.pack_start(scroll, True, True, 0)
    dialog.show_all()
    dialog.run()
    dialog.destroy()


def diagnostic_value(field, value):
    if value is True:
        return "Yes"
    if value is False:
        return "No"
    if field == "usage" and not value:
        return "Unavailable"
    if value in (None, "", [], {}):
        return "None"
    if field == "checked_at":
        return appearance.checked_time(value)
    if field in {"size", "total", "used", "free"} and isinstance(value, (int, float)):
        return f"{value / GB:.1f} GB ({value:,} bytes)"
    if field == "free_percent" and isinstance(value, (int, float)):
        return f"{value:.1f}%"
    if field == "state":
        return str(value).replace("_", " ").capitalize()
    return str(value)


def drive_permissions(parent, volume, settings):
    dialog = Gtk.Dialog(title="Drive permissions", transient_for=parent, modal=True, use_header_bar=True)
    dialog.set_default_size(580, -1)
    dialog.get_style_context().add_class("mount-medic")
    dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Apply", Gtk.ResponseType.OK)
    dialog.get_widget_for_response(Gtk.ResponseType.OK).get_style_context().add_class("primary-action")
    area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, border_width=16)
    scroll = Gtk.ScrolledWindow(propagate_natural_height=True)
    scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    # Keep the header actions reachable on a 720px screen, even with large text.
    scroll.set_max_content_height(560)
    scroll.add(area)
    dialog.get_content_area().pack_start(scroll, True, True, 0)
    summary, unused = appearance.detail_grid(drive_fields(volume))
    area.pack_start(summary, False, False, 0)
    controls = {}
    for key, title, hint in (
            ("monitor", "Background checks", "Read-only checks at login and when this drive connects."),
            ("auto_repair", "Automatic repair", "One eligible limited repair attempt, including dirty-flag clearing."),
            ("auto_mount", "Automatic mounting", "Mount normally after successful repair. No forced mounts.")):
        control = Gtk.CheckButton(label=title)
        control.set_active(bool(settings.get(key)))
        control.set_tooltip_text(hint)
        control.get_accessible().set_description(hint)
        area.pack_start(control, False, False, 0)
        controls[key] = control
    def monitoring_changed(control):
        for key in ("auto_repair", "auto_mount"):
            if not control.get_active():
                controls[key].set_active(False)
            controls[key].set_sensitive(control.get_active())
    controls["monitor"].connect("toggled", monitoring_changed)
    monitoring_changed(controls["monitor"])
    warning = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    warning.get_style_context().add_class("permission-notice")
    warning.pack_start(appearance.label("Back up before repair", "detail-label"), False, False, 0)
    notice = appearance.label("Repair resets the journal and clears the dirty flag; interrupted writes may be lost. "
                              "Back up first. It cannot replace Windows chkdsk.")
    notice.set_selectable(True)
    notice.set_tooltip_text(REPAIR_NOTICE)
    notice.get_accessible().set_description(REPAIR_NOTICE)
    notice.set_max_width_chars(65)
    warning.pack_start(notice, False, False, 0)
    area.pack_start(warning, False, False, 0)
    authentication = appearance.label("Applying changes requires administrator authentication.", "permission-auth")
    authentication.set_selectable(True)
    area.pack_start(authentication, False, False, 0)
    dialog.show_all()
    result = None
    if dialog.run() == Gtk.ResponseType.OK:
        result = {key: control.get_active() for key, control in controls.items()}
    dialog.destroy()
    return result


def startup_settings(parent, enabled):
    dialog = Gtk.Dialog(title="Startup settings", transient_for=parent, modal=True, use_header_bar=True)
    dialog.get_style_context().add_class("mount-medic")
    dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK)
    area = dialog.get_content_area()
    area.set_border_width(16)
    area.set_spacing(12)
    row = Gtk.Box(spacing=24)
    toggle = Gtk.Switch(active=enabled, valign=Gtk.Align.CENTER)
    toggle.get_accessible().set_name("Start after login")
    label = Gtk.Label(label="Start after _login", use_underline=True, xalign=0)
    label.set_mnemonic_widget(toggle)
    row.pack_start(label, True, True, 0)
    row.pack_end(toggle, False, False, 0)
    area.pack_start(row, False, False, 0)
    area.pack_start(appearance.label("Keep Mount Medic in the tray after you sign in.", "muted"), False, False, 0)
    dialog.show_all()
    result = toggle.get_active() if dialog.run() == Gtk.ResponseType.OK else None
    dialog.destroy()
    return result


def security_settings(parent, hours):
    dialog = Gtk.Dialog(title="Security settings", transient_for=parent, modal=True, use_header_bar=True)
    dialog.get_style_context().add_class("mount-medic")
    dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK)
    area = dialog.get_content_area()
    area.set_border_width(16)
    area.set_spacing(12)
    spin = Gtk.SpinButton.new_with_range(1, 24, 1)
    spin.set_value(hours)
    label = Gtk.Label.new_with_mnemonic("_Remember administrator approval (hours)")
    label.set_mnemonic_widget(spin)
    label.set_xalign(0)
    spin.get_accessible().set_name("Remember administrator approval")
    spin.set_tooltip_text("1–24 hours. Ends when you quit Mount Medic. Your password is never saved.")
    spin.get_accessible().set_description(spin.get_tooltip_text())
    area.pack_start(label, False, False, 0)
    area.pack_start(spin, False, False, 0)
    help_text = Gtk.Label(label="Closing the window keeps approval while the tray app runs.\nSaving clears current approval; the next password prompt uses this duration.")
    help_text.set_xalign(0)
    help_text.set_line_wrap(True)
    area.pack_start(help_text, False, False, 0)
    dialog.show_all()
    result = spin.get_value_as_int() if dialog.run() == Gtk.ResponseType.OK else None
    dialog.destroy()
    return result


def notification_settings(parent, settings):
    dialog = Gtk.Dialog(title="Notification settings", transient_for=parent, modal=True, use_header_bar=True)
    dialog.get_style_context().add_class("mount-medic")
    dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK)
    area = dialog.get_content_area()
    area.set_border_width(16)
    area.set_spacing(12)
    controls = {}
    for key, title, name in (("notification_seconds", "Desktop notifications (seconds)", "Notification duration in seconds"),
                             ("toast_seconds", "In-app messages (seconds)", "In-app message duration in seconds")):
        label = Gtk.Label(label=title, xalign=0)
        duration = Gtk.SpinButton.new_with_range(1, MAX_NOTIFICATION_SECONDS, 1)
        duration.set_numeric(True)
        duration.set_value(settings[key])
        duration.get_accessible().set_name(name)
        description = f"Choose 1–{MAX_NOTIFICATION_SECONDS} seconds."
        if key == "notification_seconds":
            description += " Your desktop may hide notifications sooner or omit their buttons. All drive actions remain available in the window."
        duration.set_tooltip_text(description)
        duration.get_accessible().set_description(description)
        label.set_mnemonic_widget(duration)
        area.pack_start(label, False, False, 0)
        area.pack_start(duration, False, False, 0)
        controls[key] = duration
    sound = Gtk.CheckButton.new_with_mnemonic("_Play notification sounds")
    sound.set_active(settings["notification_sound"])
    sound.set_tooltip_text("Uses your desktop's notification sound. Desktop sound settings and Do Not Disturb still apply.")
    sound.get_accessible().set_description(sound.get_tooltip_text())
    area.pack_start(sound, False, False, 0)
    hint = appearance.label("Defaults: desktop 10 seconds · app messages 3 seconds.", "muted")
    hint.set_max_width_chars(50)
    area.pack_start(hint, False, False, 0)
    dialog.show_all()
    result = None
    if dialog.run() == Gtk.ResponseType.OK:
        for duration in controls.values():
            duration.update()
        result = {key: duration.get_value_as_int() for key, duration in controls.items()}
        result["notification_sound"] = sound.get_active()
    dialog.destroy()
    return result


def ignored_drives(parent, volumes):
    dialog = Gtk.Dialog(title="Ignore List", transient_for=parent, modal=True, use_header_bar=True)
    dialog.get_style_context().add_class("mount-medic")
    dialog.set_default_size(580, 400)
    dialog.add_buttons("Close", Gtk.ResponseType.CANCEL, "Remove from ignore list", Gtk.ResponseType.OK)
    area = dialog.get_content_area()
    area.set_border_width(16)
    area.set_spacing(12)
    hint = appearance.label("Discovery alerts are muted for these drives.", "muted")
    hint.set_tooltip_text("Ignored drives stay silent until you remove them here or choose Monitor. "
                          "Ignoring never grants drive permissions.")
    hint.get_accessible().set_description(hint.get_tooltip_text())
    hint.set_max_width_chars(60)
    area.pack_start(hint, False, False, 0)
    model = Gtk.ListStore(str, str, str)
    for key, volume in volumes.items():
        description = drive_description(volume)
        model.append([key, description.split("\n", 2)[0] + "\n" +
                      (volume.get("device") or "Disconnected"), description + f"\nVolume ID: {key}"])
    tree = Gtk.TreeView(model=model, headers_visible=False)
    tree.set_tooltip_column(2)
    tree.get_accessible().set_name("Ignored drives, including disconnected drives")
    renderer = Gtk.CellRendererText(wrap_width=500, wrap_mode=Pango.WrapMode.WORD_CHAR, ypad=8)
    tree.append_column(Gtk.TreeViewColumn("Drive", renderer, text=1))
    selection = tree.get_selection()
    remove = dialog.get_widget_for_response(Gtk.ResponseType.OK)
    remove.set_sensitive(False)
    def selection_changed(value):
        selected, iterator = value.get_selected()
        remove.set_sensitive(iterator is not None)
        tree.get_accessible().set_description(selected[iterator][2] if iterator is not None else "")
    selection.connect("changed", selection_changed)
    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroll.add(tree)
    area.pack_start(scroll, True, True, 0)
    if not volumes:
        area.pack_start(appearance.label("No ignored drives.", "section-title"), False, False, 0)
    dialog.show_all()
    result = None
    if dialog.run() == Gtk.ResponseType.OK:
        selected, iterator = selection.get_selected()
        if iterator:
            result = selected[iterator][0]
    dialog.destroy()
    return result
