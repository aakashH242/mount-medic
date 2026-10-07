from concurrent.futures import ThreadPoolExecutor
from collections import Counter
import json
import os
import subprocess
import threading
import time

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Atk", "1.0")
from gi.repository import Atk, Gio, GLib, Gtk, Pango

from . import appearance, client, dialogs, updates
from .appearance import checked_time
from .dialogs import drive_description
from .engine import REPAIR_NOTICE
from .feedback import Toast
from .model import MedicError, NEXT_STEPS
from .protocol import BUS_NAME
from .storage import Preferences, read_json
from .notifications import DISCOVERY_ACTIONS, Notifications

STATE_LABELS = {
    "unmanaged": "Not monitored", "clean": "No issue detected", "unknown": "Not verified",
    "mounted_rw": "Available / mounted", "mounted_ro": "Mounted read-only",
    "dirty": "Dirty flag set", "unclean_journal": "Unclean journal", "failed": "Repair needs attention",
    "hibernated": "Windows hibernated", "cached_metadata": "Windows metadata cached",
    "absent": "Disconnected", "mirror_mismatch": "MFT mirror mismatch",
    "io_or_corruption": "I/O error or corruption", "unsupported_flags": "Windows check required",
    "io_failure": "Device I/O failure",
    "busy": "In use", "insufficient_access": "Access unavailable", "missing_dependency": "Missing tools",
    "unsupported": "Unsupported layout / identity",
}

UI_HINTS = {
    "clean": "Limited checks found no issue.",
    "mounted_rw": "Offline checks require unmounting.",
    "mounted_ro": "Unmount before checking.",
    "unmanaged": "Choose Monitor to enable background checks.",
    "dirty": "Back up before attempting repair.",
    "unclean_journal": "Repair resets the journal; interrupted writes may be lost.",
    "unknown": "Check incomplete. Review diagnostics.",
    "missing_dependency": "Run mount-medic doctor to check missing tools.",
}


def check_result_text(reports):
    if not reports:
        return "No monitored drives to check."
    states = [STATE_LABELS.get(report["state"], report["state"]) for report in reports]
    if len(reports) == 1:
        volume = reports[0]["volume"]
        return f"{volume.get('label') or volume.get('device') or 'NTFS drive'}: {states[0]}"
    return f"{len(reports)} drives · " + " · ".join(f"{count} {state.lower()}" for state, count in Counter(states).items())


def check_result_level(reports):
    states = {report["state"] for report in reports}
    if states & {"failed", "io_failure", "io_or_corruption"}:
        return "error"
    return "warn" if states - {"clean", "mounted_rw"} else "info"


class MedicApplication(Gtk.Application):
    def __init__(self):
        GLib.set_prgname(BUS_NAME)
        super().__init__(application_id=BUS_NAME, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.window = None
        self.rows = {}
        self.reports = {}
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.preferences = Preferences()
        for key, report in read_json(self.preferences.state / "last-check.json").items():
            if isinstance(report, dict) and isinstance(report.get("checked_at"), int):
                self.reports[key] = {**report, "actions": [], "next_steps": "Saved result. Run a fresh check before acting."}
        self.watching = False
        self.background = False
        self.busy = False
        self.pending = False
        self.debounce = None
        self.indicator = None
        self.notifications = None
        self.discovered = set()
        self.dialog_open = False
        self.update_checking = False
        self.update_dialog = None
        self.update_candidate = None
        self.updating = False

    def do_startup(self):
        Gtk.Application.do_startup(self)
        GLib.set_application_name("Mount Medic")
        appearance.initialize()

    def do_shutdown(self):
        if self.notifications:
            self.notifications.close_all()
        try:
            client.call({"op": "forget_authorization"})
        except MedicError:
            # Process exit also revokes approval by disconnecting from the system bus.
            pass
        Gtk.Application.do_shutdown(self)

    def do_command_line(self, command_line):
        arguments = command_line.get_arguments()
        if "--quit-for-update" in arguments:
            if self.busy or self.dialog_open or any(window.get_visible() and window.get_modal() for window in Gtk.Window.list_toplevels()):
                return 2
            mode = 10 if self.window and self.window.get_visible() else 11
            self.quit()
            return mode
        if "watch" in arguments:
            self.watch_in_background()
        else:
            self.activate()
        return 0

    def do_activate(self):
        if self.window is None:
            self.build_window()
        self.window.show_all()
        self.window.present()
        self.start_watching()
        self.refresh()

    def build_window(self):
        self.window = Gtk.ApplicationWindow(application=self, title="Mount Medic")
        self.window.set_icon_name(BUS_NAME)
        self.window.get_style_context().add_class("mount-medic")
        self.window.set_default_size(760, 640)
        self.window.set_resizable(True)
        self.window.connect("delete-event", self.close_window)
        self.build_header()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.get_style_context().add_class("page")
        self.window.add(box)
        heading = Gtk.Box(spacing=12)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        titles.pack_start(appearance.label("Your drives", "page-title"), False, False, 0)
        self.summary = appearance.label("Discovering NTFS drives…", "muted")
        titles.pack_start(self.summary, False, False, 0)
        heading.pack_start(titles, True, True, 0)
        self.refresh_button = appearance.icon_button("view-refresh-symbolic", "Refresh drive list")
        self.refresh_button.set_valign(Gtk.Align.CENTER)
        self.refresh_button.connect("clicked", lambda button: self.refresh())
        heading.pack_end(self.refresh_button, False, False, 0)
        box.pack_start(heading, False, False, 0)
        self.toast = Toast(self.preferences)
        box.pack_start(self.toast, False, False, 0)
        self.build_drive_list(box)
        self.build_inspector(box)
        self.progress = appearance.label("Read-only checks · Close keeps monitoring", "status-line")
        self.progress.set_tooltip_text("Checks never write to your drive. Closing the window keeps Mount Medic running. "
                                       "Closing does not interrupt an active repair. Use Settings → Quit Mount Medic to stop it.")
        self.progress.get_accessible().set_role(Atk.Role.STATUSBAR)
        self.spinner = Gtk.Spinner()
        status = Gtk.Box(spacing=8)
        status.pack_start(self.spinner, False, False, 0)
        status.pack_start(self.progress, True, True, 0)
        box.pack_start(status, False, False, 0)
        self.selection_changed(self.selection)

    def build_header(self):
        header = Gtk.HeaderBar(show_close_button=True)
        # Desktop decoration preferences can otherwise hide controls or add a second app icon.
        header.set_decoration_layout(":minimize,maximize,close")
        brand = Gtk.Box(spacing=10)
        icon = Gtk.Image.new_from_icon_name(BUS_NAME, Gtk.IconSize.DIALOG)
        icon.set_pixel_size(32)
        brand.pack_start(icon, False, False, 0)
        label = appearance.label("Mount Medic", "brand-name")
        label.set_valign(Gtk.Align.CENTER)
        label.set_line_wrap(False)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_width_chars(10)
        brand.pack_start(label, False, False, 0)
        header.pack_start(brand)
        settings = Gtk.MenuButton(label="Settings")
        menu = Gtk.Menu()
        for title, callback in (("Updates", self.update_settings), ("Notifications", self.notification_settings), ("Security", self.security_settings), ("Startup", self.startup_settings),
                                ("Quit Mount Medic", lambda item: self.quit())):
            item = Gtk.MenuItem(label=title)
            item.connect("activate", callback)
            menu.append(item)
        menu.show_all()
        settings.set_popup(menu)
        header.pack_end(settings)
        ignored = Gtk.Button(label="Ignore List")
        ignored.connect("clicked", self.ignored_drives)
        header.pack_end(ignored)
        self.window.set_titlebar(header)

    def build_drive_list(self, box):
        self.store = Gtk.ListStore(str, str, str, str, str, str)
        tree = Gtk.TreeView(model=self.store)
        tree.set_enable_search(False)
        tree.set_tooltip_column(4)
        tree.get_accessible().set_name("NTFS drives and their current status")
        for number, title in ((1, "Drive"), (5, "Monitoring"), (2, "Status"), (3, "Last check")):
            renderer = Gtk.CellRendererText(ypad=14, xpad=12, ellipsize=Pango.EllipsizeMode.END)
            if number == 3:
                renderer.set_property("ellipsize", Pango.EllipsizeMode.NONE)
            column = Gtk.TreeViewColumn(title)
            renderer.set_property("width-chars", {1: 13, 2: 16, 3: 11, 5: 16}[number])
            column.pack_start(renderer, True)
            column.add_attribute(renderer, "markup" if number == 1 else "text", number)
            column.set_expand(number == 1)
            tree.append_column(column)
        self.selection = tree.get_selection()
        self.selection_handler = self.selection.connect("changed", self.selection_changed)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(160)
        scroll.add(tree)
        self.drive_stack = Gtk.Stack()
        self.drive_stack.get_style_context().add_class("drive-list")
        self.drive_stack.add_named(scroll, "drives")
        empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, valign=Gtk.Align.CENTER)
        empty.get_style_context().add_class("empty-state")
        empty.pack_start(appearance.label("No NTFS drives found", "section-title"), False, False, 0)
        hint = appearance.label("Connect an NTFS drive, then refresh.", "muted")
        hint.set_tooltip_text("New drives stay unmonitored until you choose Monitor. Discovery does not inspect the filesystem.")
        hint.get_accessible().set_description(hint.get_tooltip_text())
        empty.pack_start(hint, False, False, 0)
        self.drive_stack.add_named(empty, "empty")
        box.pack_start(self.drive_stack, True, True, 0)

    def build_inspector(self, box):
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        panel.get_style_context().add_class("inspector")
        self.detail_title = appearance.label("Select a drive", "section-title")
        panel.pack_start(self.detail_title, False, False, 0)
        self.detail = appearance.label("")
        self.detail.set_selectable(True)
        self.detail.set_max_width_chars(64)
        panel.pack_start(self.detail, False, False, 0)
        self.permission_summary = appearance.label("", "muted")
        self.permission_summary.get_style_context().add_class("permissions")
        panel.pack_start(self.permission_summary, False, False, 0)
        self.identity_details = appearance.label("")
        self.identity_details.set_selectable(True)
        self.identity_details.set_max_width_chars(64)
        self.identity_expander = Gtk.Expander(label="Drive details")
        self.identity_expander.add(self.identity_details)
        panel.pack_start(self.identity_expander, False, False, 0)
        buttons = Gtk.Box(spacing=8, margin_top=8)
        self.buttons = {}
        for title, callback in (("Monitor", self.manage_selected), ("Check now", self.check_selected),
                                ("Repair", self.repair), ("Permissions", self.settings)):
            button = Gtk.Button(label=title)
            button.connect("clicked", callback)
            buttons.pack_start(button, False, False, 0)
            self.buttons[title] = button
        self.buttons["Check now"].get_style_context().add_class("primary-action")
        self.buttons["Check now"].set_tooltip_text("Read-only inspection. Checking once does not enable background checks.")
        self.buttons["Permissions"].set_tooltip_text("Manage background checks, automatic repair, and mounting separately.")
        self.more_button = Gtk.MenuButton(label="More")
        self.more_button.get_accessible().set_name("More drive actions")
        menu = Gtk.Menu()
        for title, callback in (("Mount", self.mount), ("Unmount and inspect", self.unmount),
                                ("Ignore This Drive", self.ignore_selected), ("Diagnostics", self.details)):
            item = Gtk.MenuItem(label=title)
            item.connect("activate", callback)
            menu.append(item)
            self.buttons[title] = item
        menu.show_all()
        self.more_button.set_popup(menu)
        buttons.pack_end(self.more_button, False, False, 0)
        panel.pack_start(buttons, False, False, 0)
        box.pack_start(panel, False, False, 0)
        panel.show_all()
        panel.set_no_show_all(True)
        panel.hide()
        self.inspector = panel

    def close_window(self, window, event):
        self.watch_in_background()
        window.hide()
        return True

    def selected(self):
        model, iterator = self.selection.get_selected()
        return self.rows.get(model[iterator][0]) if iterator else None

    def selection_changed(self, selection):
        row = self.selected()
        self.refresh_button.set_sensitive(not self.busy)
        self.more_button.set_sensitive(not self.busy and row is not None)
        self.identity_expander.set_sensitive(row is not None)
        for name, button in self.buttons.items():
            button.set_sensitive(not self.busy and (row is not None or name == "Check now"))
        if not row:
            self.detail_title.set_text("Select a drive")
            self.detail.set_text("Choose a drive to see its status.")
            self.detail.set_tooltip_text(None)
            self.detail.get_accessible().set_description("")
            self.permission_summary.set_text("Select a drive to manage its permissions.")
            self.identity_details.set_text("")
            self.buttons["Check now"].set_label("Check monitored drives")
            return
        volume = row["volume"]
        self.buttons["Check now"].set_label("Check now")
        self.detail_title.set_text(STATE_LABELS.get(row["state"], "Not verified"))
        guidance = row.get("next_steps", "Run a fresh check before acting.")
        # Keep saved-result warnings and worker refusals visible, even for familiar states.
        self.detail.set_text(UI_HINTS.get(row["state"], guidance) if guidance == NEXT_STEPS.get(row["state"]) else guidance)
        self.detail.set_tooltip_text(guidance)
        self.detail.get_accessible().set_description(guidance)
        self.identity_details.set_text(drive_description(volume) + f"\nVolume ID: {volume['id']}\n"
                                       f"Last check: {checked_time(row.get('checked_at'))}")
        settings = row.get("settings", {})
        self.buttons["Monitor"].set_label("Remove" if settings.get("monitor") else "Monitor")
        self.buttons["Monitor"].set_sensitive(not self.busy and (settings.get("monitor", False) or row["state"] != "absent"))
        self.buttons["Ignore This Drive"].set_sensitive(not self.busy and not settings.get("monitor"))
        self.permission_summary.set_text("  ·  ".join(
            f"{title} {'on' if settings.get(key) else 'off'}" for key, title in
            (("monitor", "Checks"), ("auto_repair", "Auto-repair"), ("auto_mount", "Auto-mount"))))
        for name, permitted in (("Repair", "repair" in row.get("actions", [])),
                                ("Mount", "mount" in row.get("actions", [])),
                                ("Unmount and inspect", bool(volume.get("mounts")))):
            self.buttons[name].set_sensitive(not self.busy and permitted)
            self.buttons[name].set_tooltip_text("Review the drive and confirm before continuing." if permitted else
                                              "Run a check and review diagnostics; this action is not currently eligible.")

    def submit(self, operation, callback=None, *, on_error=None):
        if self.busy:
            self.pending = True
            (on_error or self.show_error)("An operation is already running. Wait for its result before requesting another action.")
            return
        self.busy = True
        if self.window:
            self.spinner.start()
            self.progress.set_text("Working…")
            self.selection_changed(self.selection)
        future = self.pool.submit(operation)

        def finish():
            self.busy = False
            if self.window:
                self.spinner.stop()
            try:
                value = future.result()
                if self.window:
                    self.progress.set_text("Finished · Review drive status")
                if callback:
                    callback(value)
            except Exception as error:
                (on_error or self.show_error)(str(error))
            if self.window:
                self.selection_changed(self.selection)
            if self.pending:
                self.pending = False
                GLib.idle_add(self.cycle if self.watching else self.refresh)
            return False

        future.add_done_callback(lambda done: GLib.idle_add(finish))

    def refresh(self):
        self.submit(client.list_volumes, self.render)

    def render(self, rows):
        previous = self.selected()["volume"]["id"] if self.window and self.selected() else None
        self.rows = {}
        for row in rows:
            key = row["volume"]["id"]
            if key in self.reports and row["state"] not in {"absent", "mounted_rw", "mounted_ro"}:
                row.update({name: value for name, value in self.reports[key].items() if name not in {"volume", "settings"}})
            self.rows[key] = row
        self.discover_notifications(rows)
        if not self.window:
            return
        # Rebuilding the list must not briefly disable controls and discard keyboard focus.
        with self.selection.handler_block(self.selection_handler):
            self.render_drive_list(previous)
        self.selection_changed(self.selection)
        monitored = sum(bool(row.get("settings", {}).get("monitor")) for row in self.rows.values())
        count = len(self.rows)
        self.summary.set_text(f"{count} {'drive' if count == 1 else 'drives'} · {monitored} monitored")
        self.drive_stack.set_visible_child_name("drives" if count else "empty")
        self.inspector.set_visible(bool(count))

    def render_drive_list(self, previous):
        self.store.clear()
        ignored = self.preferences.ignored()
        for key, row in self.rows.items():
            volume = row["volume"]
            checked = row.get("checked_at")
            name = GLib.markup_escape_text(volume.get("label") or "NTFS drive")
            device = GLib.markup_escape_text(volume.get("device") or "Disconnected")
            size = volume.get("identity", {}).get("size", 0) / (1024 ** 3)
            settings = row.get("settings", {})
            membership = "Added" if settings.get("monitor") else "Not added"
            if key in ignored:
                membership = "Added · ignored" if membership == "Added" else "Ignored"
            monitoring = "\n".join([membership, *(
                f"{title} {'on' if settings.get(option) else 'off'}"
                for option, title in (("auto_repair", "Auto-repair"), ("auto_mount", "Auto-mount")))])
            iterator = self.store.append([key, f"<b>{name}</b>\n<small>{device} · {size:.1f} GiB</small>",
                                          STATE_LABELS.get(row["state"], row["state"]),
                                          checked_time(checked), drive_description(volume) + "\n" +
                                          STATE_LABELS.get(row["state"], row["state"]) +
                                          f"\nLast check: {checked_time(checked)}\nMonitoring: {monitoring}", monitoring])
            if key == previous:
                self.selection.select_iter(iterator)
        if self.rows and self.selected() is None:
            self.selection.select_path(Gtk.TreePath.new_first())

    def checked(self, reports):
        self.preferences.remember(reports)
        for report in reports:
            key = report["volume"]["id"]
            self.reports[key] = report
            if key in self.rows:
                self.rows[key].update(report)
        self.render(list(self.rows.values()))

    def check_selected(self, button):
        row = self.selected()
        request = {"op": "check"}
        if row:
            request["id"] = row["volume"]["id"]
        self.show_feedback("Checking…\nRead-only inspection is running.")
        self.run_manual_check(request, self.show_feedback)

    def check_from_tray(self, item):
        def notify_result(message, level):
            title, unused, body = message.partition("\n")
            self.notify("manual-check", title, body, level=level)
        self.run_manual_check({"op": "check"}, notify_result)

    def run_manual_check(self, request, feedback):
        def completed(reports):
            self.checked(reports)
            feedback("Check complete\n" + check_result_text(reports), check_result_level(reports))
        self.submit(lambda: client.call(request), completed, on_error=lambda message: feedback("Check failed\n" + message, "error"))

    def show_feedback(self, message, level="info"):
        if self.update_dialog and self.update_dialog.get_visible():
            self.update_dialog.toast.show_message(message, level)
        elif self.window and self.window.get_visible():
            self.toast.show_message(message, level)
            self.progress.set_text(message.split("\n", 1)[0])
        else:
            self.notify("action-result", "Mount Medic", message, level=level)

    def confirmation(self, title: str, text: str) -> bool:
        dialog = Gtk.MessageDialog(transient_for=self.window, modal=True, message_type=Gtk.MessageType.WARNING,
                                   buttons=Gtk.ButtonsType.OK_CANCEL, text=title)
        dialog.format_secondary_text(text)
        answer = dialog.run()
        dialog.destroy()
        return answer == Gtk.ResponseType.OK

    def settings(self, button=None, volume_id=None):
        row = self.rows.get(volume_id) if volume_id else self.selected()
        if not row or self.busy or self.dialog_open:
            return
        volume = row["volume"]
        settings = dict(row.get("settings", {}))
        if button == "monitor":
            settings.update(monitor=True, auto_repair=False, auto_mount=False)
        self.dialog_open = True
        try:
            values = dialogs.drive_permissions(self.window, volume, settings)
        finally:
            self.dialog_open = False
        if values is not None:
            self.save_permissions(row, values)

    def save_permissions(self, row, values):
        key = row["volume"]["id"]
        request = {"op": "configure", "id": key, **values}
        # Save every choice atomically, even if a CLI changed grants while the dialog was open.

        def saved(result):
            if result.get("monitor"):
                self.preferences.unignore(key)
            self.close_notification(key)
            self.cycle()
            message = "Drive permissions saved." if result.get("monitor") else "Drive removed from monitoring."
            self.show_feedback(message + "\n" + (row["volume"].get("label") or "NTFS drive"))

        self.submit(lambda: client.call(request), saved)

    def manage_selected(self, button):
        row = self.selected()
        if not row:
            return
        if not row.get("settings", {}).get("monitor"):
            self.settings("monitor", row["volume"]["id"])
        elif self.confirmation("Remove from monitored drives?", drive_description(row["volume"]) +
                               "\n\nStop background checks and revoke automatic repair and mounting. Diagnostic history is kept."):
            self.save_permissions(row, {"monitor": False, "auto_repair": False, "auto_mount": False})

    def ignore_selected(self, button):
        row = self.selected()
        if row:
            self.ignore_drive(row["volume"])

    def ignore_drive(self, volume):
        try:
            self.preferences.ignore(volume)
            self.close_notification(volume["id"])
            self.render(list(self.rows.values()))
            self.show_feedback("Drive added to Ignore List.\n" + (volume.get("label") or "NTFS drive"))
        except (MedicError, OSError) as error:
            self.show_error(str(error))

    def ignored_drives(self, button):
        try:
            key = dialogs.ignored_drives(self.window, self.preferences.ignored())
            if key:
                self.preferences.unignore(key)
                self.discovered.discard(key)
                self.render(list(self.rows.values()))
                self.show_feedback("Drive removed from Ignore List.")
        except (MedicError, OSError) as error:
            self.show_error(str(error))

    def notification_settings(self, button):
        try:
            settings = {"notification_seconds": self.preferences.notification_seconds(), "toast_seconds": self.preferences.toast_seconds(),
                        "notification_sound": self.preferences.notification_sound_enabled()}
            result = dialogs.notification_settings(self.window, settings)
            if result is not None:
                self.preferences.set_notification_settings(result)
                self.show_feedback("Notification settings saved.")
        except (MedicError, OSError) as error:
            self.show_error(str(error))

    def security_settings(self, button):
        try:
            hours = self.preferences.authorization_hours()
            result = dialogs.security_settings(self.window, hours)
            if result is not None and result != hours:
                def save():
                    client.call({"op": "forget_authorization"})
                    self.preferences.set_authorization_hours(result)
                self.submit(save, lambda unused: self.show_feedback(f"Security settings saved.\nApproval will last up to {result} " +
                                                                    ("hour" if result == 1 else "hours") + " after the next password prompt."))
        except (MedicError, OSError) as error:
            self.show_error(str(error))

    def repair(self, button):
        row = self.selected()
        if not row:
            return
        request = {"op": "repair", "id": row["volume"]["id"], "clear_dirty": True,
                   "retry": bool(row.get("repair") or row.get("settings", {}).get("attempt"))}
        text = drive_description(row["volume"]) + "\n\n" + REPAIR_NOTICE
        if request["retry"]:
            text += "\nThis is an explicit retry after reviewing the previous attempt. Fresh safety checks run first."
        dialog = Gtk.MessageDialog(transient_for=self.window, modal=True, message_type=Gtk.MessageType.WARNING,
                                   buttons=Gtk.ButtonsType.OK_CANCEL, text="Attempt limited repair?")
        dialog.format_secondary_text(text)
        clear = Gtk.CheckButton(label="Clear dirty flag (-d). Uncheck to request a subsequent Windows check.")
        clear.set_active(True)
        dialog.get_content_area().add(clear)
        dialog.show_all()
        answer = dialog.run()
        request["clear_dirty"] = clear.get_active()
        dialog.destroy()
        if answer == Gtk.ResponseType.OK:
            def repaired(result):
                self.checked([result])
                level = "info" if result.get("repair", {}).get("status") == "success" else "error"
                if result.get("mount", {}).get("state") == "failed":
                    level = "warn"
                self.show_feedback("Repair finished.\n" + result.get("next_steps", check_result_text([result])), level)
            self.submit(lambda: client.repair(request), repaired)

    def mount(self, button):
        row = self.selected()
        if row and self.confirmation("Mount this drive?", drive_description(row["volume"])):
            def mounted(result):
                self.refresh()
                self.show_feedback("Drive mounted.\n" + (row["volume"].get("label") or "NTFS drive"))
            self.submit(lambda: client.udisks_operation(row["volume"]["id"], "Mount"), mounted)

    def unmount(self, button):
        row = self.selected()
        if not row or not self.confirmation("Unmount and inspect?", "Save work and close files first. Busy drives will not be forced.\n\n" + drive_description(row["volume"])):
            return
        key = row["volume"]["id"]

        def unmount_and_check():
            client.udisks_operation(key, "Unmount")
            try:
                return client.call({"op": "check", "id": key})
            except MedicError as error:
                raise MedicError(f"Drive unmounted, but checking failed: {error}") from error

        def completed(reports):
            self.checked(reports)
            self.show_feedback("Drive unmounted. Check complete.\n" + check_result_text(reports), check_result_level(reports))
        self.submit(unmount_and_check, completed)

    def details(self, button):
        row = self.selected()
        if row:
            dialogs.drive_diagnostics(self.window, row)

    def startup_settings(self, button):
        from .installer import autostart, autostart_enabled
        try:
            enabled = autostart_enabled()
            result = dialogs.startup_settings(self.window, enabled)
            if result is not None and result != enabled:
                autostart(result)
                if result:
                    self.watch_in_background()
                self.show_feedback("Start after login " + ("enabled." if result else "disabled."))
        except (OSError, MedicError) as error:
            self.show_error(str(error))

    def show_error(self, message):
        if self.window and self.window.get_visible() or self.update_dialog and self.update_dialog.get_visible():
            self.progress.set_text(message)
            self.show_feedback(message, "error")
        elif self.preferences.changed("application", message):
            self.notify("application", "Mount Medic needs attention", message, level="error")

    def notification_error(self, message):
        if self.window and self.window.get_visible() or self.update_dialog and self.update_dialog.get_visible():
            self.progress.set_text(message)
            self.show_feedback(message, "error")
        else:
            print(message)

    def notify(self, key, title, body, *, actions=(), on_sent=None, level="info"):
        try:
            if self.notifications is None:
                self.notifications = Notifications(self.preferences, self.notification_action, self.notification_error)
            self.notifications.show(key, title, body, actions=actions, on_sent=on_sent, level=level)
        except (GLib.Error, MedicError, OSError) as error:
            self.notification_error(str(error))

    def close_notification(self, key):
        if self.notifications:
            self.notifications.close(key)

    def discover_notifications(self, rows):
        present = {row["volume"]["id"] for row in rows if row["state"] != "absent"}
        ignored = self.preferences.ignored()
        for key in self.discovered - present:
            self.close_notification(key)
        for row in rows:
            key = row["volume"]["id"]
            if key in present and key not in self.discovered and key not in ignored and not row.get("settings", {}).get("monitor"):
                volume = row["volume"]
                body = f"{volume.get('label') or 'NTFS drive'} · {volume.get('device')}\nChoose Monitor to set permissions, or ignore this drive."
                self.notify(key, "NTFS drive discovered", body, actions=DISCOVERY_ACTIONS)
        self.discovered = present

    def notification_action(self, action, key):
        if key.startswith("update:"):
            if action == "default":
                self.update_settings()
            elif self.update_candidate and key == "update:" + self.update_candidate["version"]:
                if action == "update.ignore":
                    self.ignore_update()
                elif action == "update.install":
                    self.install_update()
            return
        if action == "default":
            self.activate()
            return
        if action == "ignore":
            row = self.rows.get(key)
            if row and not row.get("settings", {}).get("monitor"):
                self.ignore_drive(row["volume"])
            return
        if action != "monitor":
            return
        if self.window is None:
            self.build_window()
        self.window.show_all()
        self.window.present()
        # Resolve the same identity afresh; a reused /dev path is not consent.
        def show_settings(rows):
            self.render(rows)
            row = self.rows.get(key)
            if row is None or row["state"] == "absent":
                self.show_error("This drive disconnected. Reconnect it before choosing Monitor.")
            else:
                self.settings(None if row.get("settings", {}).get("monitor") else "monitor", volume_id=key)
        self.submit(client.list_volumes, show_settings)

    def watch_in_background(self):
        if not self.background:
            self.background = True
            self.hold()
        self.start_watching()

    def start_watching(self):
        if self.watching:
            return
        self.watching = True
        GLib.timeout_add_seconds(5, self.check_updates)
        GLib.timeout_add_seconds(60, self.update_tick)
        self.setup_indicator()
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                          "StartServiceByName", GLib.Variant("(su)", ("org.freedesktop.UDisks2", 0)),
                          GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, 5000, None)
            bus.signal_subscribe("org.freedesktop.UDisks2", None, None, None, None,
                                 Gio.DBusSignalFlags.NONE, self.device_event, None)
            self.device_bus = bus
        except GLib.Error:
            self.show_error("Hotplug notifications unavailable; login and manual checks still work.")
        GLib.timeout_add_seconds(15, self.cycle)

    def update_settings(self, button=None):
        if self.window is None:
            self.build_window()
        if self.update_dialog:
            self.update_dialog.present()
            return
        self.update_dialog = dialogs.UpdatesDialog(self.window, lambda button: self.check_updates(manual=True),
                                                   self.ignore_update, self.install_update)
        self.update_dialog.connect("destroy", lambda dialog: setattr(self, "update_dialog", None))
        self.update_dialog.set_status(updates.status(self.preferences), self.update_checking)
        if self.updating:
            self.update_dialog.install.set_sensitive(False)
        self.update_dialog.show_all()

    def update_tick(self):
        self.check_updates()
        return True

    def check_updates(self, manual=False):
        if self.update_checking or (not manual and (self.busy or not updates.due(self.preferences))):
            return False
        self.update_checking = True
        if self.update_dialog:
            self.update_dialog.set_status(updates.status(self.preferences), True)

        def fetch():
            succeeded = False
            try:
                updates.check(self.preferences)
                succeeded = True
            except (MedicError, OSError):
                pass  # Automatic failures remain quiet; the Updates dialog shows the saved error.
            GLib.idle_add(completed, succeeded)

        def completed(succeeded):
            self.update_checking = False
            result = updates.status(self.preferences)
            if self.update_dialog:
                self.update_dialog.set_status(result)
            if manual:
                if not succeeded:
                    self.show_feedback("Update check failed.\n" + (result.get("error") or "Try again later."), "error")
                else:
                    self.show_feedback("Update check complete.\n" + (f"Version {result['available']} is available." if result["available"] else "You’re up to date."))
            if succeeded and result["available"]:
                self.update_candidate = result["release"]
                if not manual and updates.notification_due(self.preferences, self.update_candidate):
                    def delivered():
                        if self.update_candidate["version"] == result["available"]:
                            try:
                                self.preferences.save_updates({"notified": result["available"]})
                            except (MedicError, OSError) as error:
                                self.notification_error(str(error))
                    self.notify("update:" + result["available"], f"Mount Medic {result['available']} is available",
                                f"Installed: {result['installed']}", actions=updates.UPDATE_ACTIONS, on_sent=delivered)
            return False

        threading.Thread(target=fetch, name="release-check", daemon=True).start()
        return False

    def ignore_update(self, button=None):
        candidate = self.update_candidate if button is None else self.update_dialog.release
        if candidate:
            try:
                updates.ignore(self.preferences, candidate["version"])
                self.close_notification("update:" + candidate["version"])
                if self.update_dialog:
                    self.update_dialog.set_status(updates.status(self.preferences))
                self.show_feedback(f"Version {candidate['version']} ignored.")
            except (MedicError, OSError) as error:
                self.show_error(str(error))

    def install_update(self, button=None):
        if self.updating:
            return
        if self.busy:
            self.show_error("Finish the current operation before installing an update.")
            return
        candidate = self.update_candidate if button is None else self.update_dialog.release
        if not candidate:
            self.update_settings()
            return
        if not updates.installed():
            self.show_error("Manually install the first updater-enabled release to enable in-app upgrades.")
            return
        mode = "gui" if self.window and self.window.get_visible() else "watch"
        if self.update_dialog:
            self.update_dialog.destroy()
        self.close_notification("update:" + candidate["version"])
        # A separate user process survives this application's coordinated shutdown.
        script = "import sys; sys.path.insert(0, '/usr/local/lib/mount-medic'); from mount_medic.updates import main; raise SystemExit(main())"
        try:
            self.preferences.state.mkdir(parents=True, exist_ok=True, mode=0o700)
            with (self.preferences.state / "update.log").open("w") as log:
                process = subprocess.Popen(["/usr/bin/python3", "-IB", "-c", script, candidate["version"], candidate["sha256"],
                                           "--restart", mode], stdout=log, stderr=log, start_new_session=True)
        except OSError as error:
            self.show_error(str(error))
            return
        self.updating = True
        def completed():
            if process.poll() is None:
                return True
            self.updating = False
            if process.returncode:
                self.show_error("Update failed or was cancelled. See Settings → Updates and " + str(self.preferences.state / "update.log"))
            if self.update_dialog:
                self.update_dialog.set_status(updates.status(self.preferences), self.update_checking)
            return False
        GLib.timeout_add(500, completed)
        if self.window:
            self.progress.set_text(f"Installing {candidate['version']}… The app will restart when finished.")
        self.show_feedback(f"Installing version {candidate['version']}…\nThe app will restart when finished.")

    def setup_indicator(self):
        tray_icon = BUS_NAME + "-symbolic"
        try:
            icon_path = appearance.tray_icon_path()
        except OSError:
            icon_path = appearance.ASSETS
        for name in ("AyatanaAppIndicator3", "AppIndicator3"):
            try:
                gi.require_version(name, "0.1")
                import importlib
                library = importlib.import_module("gi.repository." + name)
                self.indicator = library.Indicator.new("mount-medic", tray_icon, library.IndicatorCategory.HARDWARE)
                self.indicator.set_icon_theme_path(str(icon_path))
                self.indicator.set_icon_full(tray_icon, "Mount Medic")
                self.indicator.set_status(library.IndicatorStatus.ACTIVE)
                menu = Gtk.Menu()
                for title, callback in (("Open Mount Medic", lambda item: self.activate()),
                                        ("Check now", self.check_from_tray),
                                        ("Quit", lambda item: self.quit())):
                    item = Gtk.MenuItem(label=title)
                    item.connect("activate", callback)
                    menu.append(item)
                menu.show_all()
                self.indicator.set_menu(menu)
                return
            except (ImportError, ValueError):
                continue

    def device_event(self, *arguments):
        if self.debounce:
            GLib.source_remove(self.debounce)
        self.debounce = GLib.timeout_add_seconds(2, self.cycle)

    def cycle(self):
        self.debounce = None

        def scan():
            rows = client.list_volumes()
            reports = []
            deadline = time.monotonic() + 120
            for row in rows:
                if time.monotonic() > deadline:
                    break
                settings = row.get("settings", {})
                if not settings.get("monitor") or row["state"] == "absent":
                    continue
                key = row["volume"]["id"]
                report = client.call({"op": "check", "id": key})[0]
                if settings.get("auto_repair") and "repair" in report.get("actions", []):
                    try:
                        report = client.repair({"op": "automatic", "id": key})
                    except MedicError as error:
                        report.update(state="failed", evidence=str(error))
                reports.append(report)
            return rows, reports

        def completed(value):
            rows, reports = value
            self.checked(reports)
            self.render(rows)
            for report in reports:
                key = report["volume"]["id"]
                fingerprint = report["state"] + str(report.get("repair", {}).get("started", ""))
                changed = self.preferences.changed(key, fingerprint)
                if changed and (report["state"] not in {"clean", "mounted_rw"} or report.get("repair")):
                    self.notify(key, "Mount Medic recovery completed" if report.get("repair", {}).get("status") == "success" else "NTFS drive needs attention",
                                report.get("next_steps", report["state"]))

        self.submit(scan, completed)
        return False


def launch(mode: str):
    if os.geteuid() == 0:
        raise MedicError("Run the desktop app as your normal user; it authenticates privileged actions separately")
    if not Gtk.init_check()[0]:
        raise MedicError("No graphical display is available. Use the CLI commands.")
    return MedicApplication().run(["mount-medic", mode])
