from datetime import datetime
from pathlib import Path
import hashlib
import os
import tempfile

from gi.repository import Gdk, Gtk, Pango

from .protocol import BUS_NAME
from .usage import storage_summary, volume_usage

ASSETS = Path(__file__).with_name("assets")


def tray_icon_path() -> Path:
    name = BUS_NAME + "-symbolic.svg"
    data = (ASSETS / name).read_bytes()
    # KDE retains pixels for unchanged icon names and theme paths across updates.
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    directory = cache / "mount-medic" / "icons" / hashlib.sha256(data).hexdigest()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    icon = directory / name
    if not icon.is_file() or icon.read_bytes() != data:
        with tempfile.TemporaryDirectory(dir=directory) as temporary:
            staged = Path(temporary) / name
            staged.write_bytes(data)
            staged.replace(icon)
    return directory


def checked_time(timestamp) -> str:
    try:
        return datetime.fromtimestamp(timestamp).strftime("%d %b %I:%M %p") if timestamp else "—"
    except (TypeError, ValueError, OverflowError, OSError):
        return "—"


def initialize():
    Gdk.set_program_class(BUS_NAME)
    Gtk.IconTheme.get_default().append_search_path(str(ASSETS))
    Gtk.Window.set_default_icon_name(BUS_NAME)
    provider = Gtk.CssProvider()
    load_styles(provider)
    Gtk.Settings.get_default().connect("notify::gtk-theme-name", lambda settings, name: load_styles(provider))
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def load_styles(provider):
    theme = os.environ.get("GTK_THEME") or Gtk.Settings.get_default().get_property("gtk-theme-name")
    css = (ASSETS / "style.css").read_bytes()
    if "highcontrast" in theme.lower().replace("-", ""):
        css += (ASSETS / "high-contrast.css").read_bytes()
    provider.load_from_data(css)


def label(text: str, style: str = "") -> Gtk.Label:
    widget = Gtk.Label(label=text, xalign=0, wrap=True)
    widget.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
    if style:
        widget.get_style_context().add_class(style)
    return widget


def detail_grid(values: dict):
    grid = Gtk.Grid(column_spacing=24, row_spacing=8, margin_top=8)
    grid.get_style_context().add_class("drive-details")
    fields = {}
    for index, title in enumerate(values):
        heading = label(title, "muted")
        heading.get_style_context().add_class("detail-label")
        heading.set_valign(Gtk.Align.START)
        value = label("", "detail-name" if title == "Drive" else "")
        value.set_selectable(True)
        value.set_hexpand(True)
        value.set_valign(Gtk.Align.START)
        value.set_max_width_chars(48)
        if title in {"Device", "Filesystem UUID", "Volume ID"}:
            value.get_style_context().add_class("detail-id")
        grid.attach(heading, 0, index, 1, 1)
        grid.attach(value, 1, index, 1, 1)
        fields[title] = value
    set_detail_values(fields, values)
    return grid, fields


def set_detail_values(fields, values):
    for title, widget in fields.items():
        text = str(values.get(title, ""))
        widget.set_text(text)
        widget.get_accessible().set_name(f"{title}: {text}" if text else title)


def storage_meter() -> Gtk.ProgressBar:
    meter = Gtk.ProgressBar()
    meter.set_no_show_all(True)
    meter.get_style_context().add_class("storage-meter")
    return meter


def update_storage_meter(meter, volume):
    usage = volume_usage(volume)
    meter.set_visible(usage is not None)
    if usage:
        meter.set_fraction(usage["used"] / usage["total"])
    description = "Storage used: " + storage_summary(volume)
    meter.set_tooltip_text(description)
    meter.get_accessible().set_name(description)


def icon_button(icon: str, title: str) -> Gtk.Button:
    button = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
    button.set_tooltip_text(title)
    button.get_accessible().set_name(title)
    return button
