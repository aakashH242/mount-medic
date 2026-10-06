from pathlib import Path
import os

from gi.repository import Gdk, Gtk, Pango

from .protocol import BUS_NAME

ASSETS = Path(__file__).with_name("assets")


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


def icon_button(icon: str, title: str) -> Gtk.Button:
    button = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
    button.set_tooltip_text(title)
    button.get_accessible().set_name(title)
    return button
