"""Native update window that survives the main application's restart."""
from threading import Event, Thread
from pathlib import Path
import subprocess
import sys
import time

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Atk, GLib, Gtk

from . import appearance, updates
from .model import MedicError
from .protocol import BUS_NAME


class UpdateWindow(Gtk.Dialog):
    def __init__(self, target):
        super().__init__(title="Updating Mount Medic", modal=True)
        self.set_icon_name(BUS_NAME)
        self.set_default_size(440, 220)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_deletable(False)
        self.finished = False
        self.exit_code = 2
        self.result_message = None
        self.connect("delete-event", self.delete)
        self.connect("response", self.close)
        box = self.get_content_area()
        box.set_border_width(20)
        box.set_spacing(14)
        box.pack_start(appearance.label(f"Updating to {target}", "page-title"), False, False, 0)
        self.message = appearance.label("Preparing update…")
        self.message.set_line_wrap(True)
        self.message.set_width_chars(36)
        self.message.set_max_width_chars(60)
        self.message.set_selectable(True)
        self.message.get_accessible().set_role(Atk.Role.STATUSBAR)
        self.message_scroll = Gtk.ScrolledWindow()
        self.message_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.message_scroll.set_min_content_height(64)
        self.message_scroll.set_max_content_height(240)
        self.message_scroll.set_propagate_natural_height(True)
        self.message_scroll.add(self.message)
        box.pack_start(self.message_scroll, False, False, 0)
        self.spinner = Gtk.Spinner()
        self.spinner.start()
        box.pack_start(self.spinner, False, False, 0)
        self.detail = appearance.label("Mount Medic will reopen when the update finishes.", "muted")
        self.detail.set_line_wrap(True)
        self.detail.set_width_chars(36)
        self.detail.set_max_width_chars(60)
        box.pack_start(self.detail, False, False, 0)
        self.close_button = self.add_button("Close", Gtk.ResponseType.CLOSE)
        self.close_button.set_sensitive(False)

    def close(self, dialog, response):
        if self.finished:
            self.destroy()
            Gtk.main_quit()

    def delete(self, dialog, event):
        self.close(dialog, Gtk.ResponseType.CLOSE)
        return True

    def stage(self, message):
        def show():
            self.display_message(message)
            return False
        GLib.idle_add(show)

    def display_message(self, message):
        self.message.set_text(message)
        self.message.set_tooltip_text(message)
        self.message.get_accessible().set_description(message)

    def confirm(self, message):
        completed = Event()
        answer = []

        def ask():
            try:
                dialog = Gtk.MessageDialog(transient_for=self, modal=True, text="Install required packages?",
                                           secondary_text=message)
                dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Install", Gtk.ResponseType.YES)
                answer.append(dialog.run() == Gtk.ResponseType.YES)
                dialog.destroy()
            finally:
                completed.set()
            return False
        GLib.idle_add(ask)
        completed.wait()
        return bool(answer and answer[0])

    def result(self, message):
        self.result_message = message

    def wait_for_restart(self, process, mode):
        deadline = time.monotonic() + 15
        script = ("import sys; sys.path.insert(0, sys.argv[1]); from mount_medic.updates import desktop_ready; "
                  "raise SystemExit(0 if desktop_ready(sys.argv[2]) else 2)")
        while time.monotonic() < deadline:
            try:
                ready = subprocess.run([sys.executable, "-IB", "-c", script, str(Path(__file__).resolve().parent.parent), mode],
                                       timeout=max(0.1, deadline - time.monotonic())).returncode == 0
            except subprocess.TimeoutExpired:
                break
            if ready:
                return
            if process.poll() is not None:
                break
            time.sleep(0.1)
        raise MedicError("Mount Medic could not reopen. Open it from your application menu to see the update result.")

    def complete(self, code):
        self.exit_code = code
        self.finished = True
        self.spinner.stop()
        if code == 0:
            self.destroy()
            Gtk.main_quit()
        else:
            self.display_message(self.result_message or "Update failed. See Settings → Updates for details.")
            self.close_button.set_sensitive(True)
            self.set_deletable(True)
            self.set_title("Mount Medic update result")
            self.detail.set_text("See Settings → Updates for details. You can close this window.")
            self.present()
            self.close_button.grab_focus()
            self.resize(440, 1)
        return False


def run(args):
    if not Gtk.init_check()[0]:
        raise MedicError("Update progress requires a graphical session. Use mount-medic update install in a terminal.")
    appearance.initialize()
    window = UpdateWindow(args.version)
    window.show_all()

    def install():
        try:
            code = updates.run_update(args, window)
        except Exception as error:
            print(f"Mount Medic update: {error}", file=sys.stderr)
            code = 2
            window.result("Could not finish the update. See Settings → Updates for details.\n" + str(error))
        GLib.idle_add(window.complete, code)
    Thread(target=install, name="update-install").start()
    Gtk.main()
    return window.exit_code
