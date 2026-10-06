"""Native dialogs shared by discovery actions and the drive manager."""
from gi.repository import Gtk, Pango

from . import appearance
from .engine import REPAIR_NOTICE
from .storage import MAX_NOTIFICATION_SECONDS


def drive_description(volume: dict) -> str:
    identity = volume.get("identity", {})
    size = identity.get("size", 0) / (1024 ** 3)
    return (f"{volume.get('label') or 'NTFS drive'} · {size:.1f} GiB\n"
            f"Device: {volume.get('device') or 'Disconnected'}\n"
            f"Filesystem UUID: {identity.get('uuid', 'Unknown')}\n"
            f"Disk identity: {identity.get('hardware') or 'Unavailable'}")


def drive_permissions(parent, volume, settings):
    dialog = Gtk.Dialog(title="Drive permissions", transient_for=parent, modal=True, use_header_bar=True)
    dialog.set_default_size(580, -1)
    dialog.get_style_context().add_class("mount-medic")
    dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Apply", Gtk.ResponseType.OK)
    dialog.get_widget_for_response(Gtk.ResponseType.OK).get_style_context().add_class("primary-action")
    area = dialog.get_content_area()
    area.set_spacing(12)
    area.set_border_width(16)
    label = appearance.label(drive_description(volume))
    label.set_selectable(True)
    label.set_max_width_chars(60)
    area.pack_start(label, False, False, 0)
    controls = {}
    for key, title, hint in (
            ("monitor", "Background checks", "Read-only checks at login and when this drive connects."),
            ("auto_repair", "Automatic repair", "One eligible limited repair attempt, including dirty-flag clearing."),
            ("auto_mount", "Automatic mounting", "Mount normally after successful repair. No forced mounts.")):
        control = Gtk.CheckButton(label=title)
        control.set_active(bool(settings.get(key)))
        control.set_tooltip_text(hint)
        area.pack_start(control, False, False, 0)
        description = appearance.label(hint, "muted")
        description.set_margin_start(26)
        area.pack_start(description, False, False, 0)
        controls[key] = control
    def monitoring_changed(control):
        for key in ("auto_repair", "auto_mount"):
            if not control.get_active():
                controls[key].set_active(False)
            controls[key].set_sensitive(control.get_active())
    controls["monitor"].connect("toggled", monitoring_changed)
    monitoring_changed(controls["monitor"])
    notice = Gtk.Label(label=REPAIR_NOTICE + "\nSaving these permissions requires administrator authentication.", wrap=True, xalign=0)
    notice.set_width_chars(60)
    notice.set_max_width_chars(65)
    area.pack_start(notice, False, False, 0)
    dialog.show_all()
    result = None
    if dialog.run() == Gtk.ResponseType.OK:
        result = {key: control.get_active() for key, control in controls.items()}
    dialog.destroy()
    return result


def notification_settings(parent, seconds):
    dialog = Gtk.Dialog(title="Notification settings", transient_for=parent, modal=True, use_header_bar=True)
    dialog.get_style_context().add_class("mount-medic")
    dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK)
    area = dialog.get_content_area()
    area.set_border_width(16)
    area.set_spacing(12)
    label = Gtk.Label(label="Notification duration (seconds)", xalign=0)
    duration = Gtk.SpinButton.new_with_range(1, MAX_NOTIFICATION_SECONDS, 1)
    duration.set_numeric(True)
    duration.set_value(seconds)
    duration.get_accessible().set_name("Notification duration in seconds")
    label.set_mnemonic_widget(duration)
    area.pack_start(label, False, False, 0)
    area.pack_start(duration, False, False, 0)
    hint = appearance.label("Default: 10 seconds. Your desktop may hide notifications sooner or disable their buttons. "
                            "All drive actions remain available in this window.", "muted")
    hint.set_max_width_chars(50)
    area.pack_start(hint, False, False, 0)
    dialog.show_all()
    result = None
    if dialog.run() == Gtk.ResponseType.OK:
        duration.update()
        result = duration.get_value_as_int()
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
    hint = appearance.label("Ignored drives stay silent until you remove them here or choose Monitor. "
                            "Ignoring never grants drive permissions.", "muted")
    hint.set_max_width_chars(60)
    area.pack_start(hint, False, False, 0)
    model = Gtk.ListStore(str, str)
    for key, volume in volumes.items():
        model.append([key, drive_description(volume) + f"\nVolume ID: {key}"])
    tree = Gtk.TreeView(model=model, headers_visible=False)
    tree.get_accessible().set_name("Ignored drives, including disconnected drives")
    renderer = Gtk.CellRendererText(wrap_width=500, wrap_mode=Pango.WrapMode.WORD_CHAR, ypad=8)
    tree.append_column(Gtk.TreeViewColumn("Drive", renderer, text=1))
    selection = tree.get_selection()
    remove = dialog.get_widget_for_response(Gtk.ResponseType.OK)
    remove.set_sensitive(False)
    selection.connect("changed", lambda value: remove.set_sensitive(value.get_selected()[1] is not None))
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
