"""Brief native messages for explicit user actions."""
from gi.repository import Atk, GLib, Gtk, Pango

from . import appearance
from .model import MedicError
from .storage import DEFAULT_TOAST_SECONDS

LEVELS = {
    "info": (Gtk.MessageType.INFO, "dialog-information-symbolic"),
    "warn": (Gtk.MessageType.WARNING, "dialog-warning-symbolic"),
    "error": (Gtk.MessageType.ERROR, "dialog-error-symbolic"),
}


class Toast(Gtk.InfoBar):
    def __init__(self, preferences):
        super().__init__(show_close_button=True)
        self.preferences = preferences
        self.timeout = None
        self.set_no_show_all(True)
        self.get_style_context().add_class("toast")
        self.icon = Gtk.Image()
        self.message = appearance.label("")
        self.message.set_max_width_chars(60)
        self.message.set_lines(3)
        self.message.set_ellipsize(Pango.EllipsizeMode.END)
        self.message.get_accessible().set_role(Atk.Role.STATUSBAR)
        area = self.get_content_area()
        area.pack_start(self.icon, False, False, 0)
        area.pack_start(self.message, True, True, 0)
        area.show_all()
        self.connect("response", lambda widget, response: self.dismiss())
        self.connect("destroy", lambda widget: self.cancel_timeout())

    def show_message(self, message, level="info"):
        kind, icon = LEVELS[level]
        self.cancel_timeout()
        self.set_message_type(kind)
        self.icon.set_from_icon_name(icon, Gtk.IconSize.LARGE_TOOLBAR)
        self.message.set_text(message)
        self.message.set_tooltip_text(message)
        self.message.get_accessible().set_description(message)
        self.show()
        # InfoBar's input-only window can leave old border pixels until its parent redraws.
        parent = self.get_parent()
        if parent:
            parent.queue_draw()
        try:
            seconds = self.preferences.toast_seconds()
        except (MedicError, OSError):
            # Reporting a preferences error must not depend on reading those preferences.
            seconds = DEFAULT_TOAST_SECONDS

        def expire():
            self.timeout = None
            self.hide()
            return False
        self.timeout = GLib.timeout_add(seconds * 1000, expire)

    def cancel_timeout(self):
        if self.timeout is not None:
            GLib.source_remove(self.timeout)
            self.timeout = None

    def dismiss(self):
        self.cancel_timeout()
        self.hide()
