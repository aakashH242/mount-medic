"""Exercise the real notification client against a private session D-Bus server."""
import os
from pathlib import Path
import sys
import tempfile
import time
from threading import Thread
from unittest.mock import patch

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gio, GLib

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mount_medic.notifications import DISCOVERY_ACTIONS, Notifications, PATH, SERVICE
from mount_medic.storage import Preferences
from mount_medic.protocol import BUS_NAME

XML = """<node><interface name="org.freedesktop.Notifications">
<method name="Notify">
<arg type="s" direction="in"/><arg type="u" direction="in"/>
<arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="in"/>
<arg type="as" direction="in"/><arg type="a{sv}" direction="in"/><arg type="i" direction="in"/>
<arg type="u" direction="out"/>
</method>
<method name="CloseNotification"><arg type="u" direction="in"/></method>
<signal name="ActionInvoked"><arg type="u"/><arg type="s"/></signal>
<signal name="NotificationClosed"><arg type="u"/><arg type="u"/></signal>
</interface></node>"""


def wait_for(predicate, timeout=2):
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not predicate():
        while context.pending():
            context.iteration(False)
        assert time.monotonic() < deadline, "D-Bus result or timer did not arrive"
        time.sleep(0.001)


class NotificationServer:
    def __init__(self):
        self.bus = Gio.DBusConnection.new_for_address_sync(
            os.environ["DBUS_SESSION_BUS_ADDRESS"],
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None, None)
        self.calls = []
        self.closed = []
        self.pending = []
        self.mode = "reply"
        self.bus.register_object(PATH, Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0], self.call, None, None)
        self.bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                           "RequestName", GLib.Variant("(su)", (SERVICE, 0)), None, Gio.DBusCallFlags.NONE, 2000, None)

    def call(self, connection, sender, path, interface, method, parameters, invocation):
        if method == "Notify":
            self.calls.append(parameters.unpack())
            if self.mode == "delay":
                self.pending.append(invocation)
            elif self.mode == "fail":
                invocation.return_dbus_error("org.freedesktop.DBus.Error.Failed", "Synthetic notification failure")
            else:
                invocation.return_value(GLib.Variant("(u)", (len(self.calls),)))
        else:
            number = parameters.unpack()[0]
            self.closed.append(number)
            self.emit("NotificationClosed", GLib.Variant("(uu)", (number, 3)))
            invocation.return_value(None)

    def emit(self, name, parameters):
        self.bus.emit_signal(None, PATH, SERVICE, name, parameters)

    def release(self):
        self.bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                           "ReleaseName", GLib.Variant("(s)", (SERVICE,)), None, Gio.DBusCallFlags.NONE, 2000, None)


def exercise():
    server = NotificationServer()
    preferences = Preferences()
    actions, errors = [], []
    notifications = Notifications(preferences, lambda action, key: actions.append((action, key)), errors.append)
    key = "a" * 24
    notifications.show(key, "NTFS drive discovered", "<b>Backup & Work</b>", actions=DISCOVERY_ACTIONS)
    wait_for(lambda: notifications.active[key]["id"] is not None)
    payload = server.calls[-1]
    assert payload[-1] == 10_000
    assert payload[4] == "&lt;b&gt;Backup &amp; Work&lt;/b&gt;"
    assert payload[5] == ["default", "Open Mount Medic", "monitor", "Monitor", "ignore", "Ignore This Drive"]
    assert payload[6]["transient"] is True
    server.emit("ActionInvoked", GLib.Variant("(us)", (1, "repair")))
    server.emit("ActionInvoked", GLib.Variant("(us)", (1, "monitor")))
    wait_for(lambda: len(actions) == 1 and 1 in server.closed)
    assert actions == [("monitor", key)]
    assert not notifications.active

    notifications.show(key, "Discovered", "Drive", actions=DISCOVERY_ACTIONS)
    wait_for(lambda: notifications.active[key]["id"] is not None)
    server.emit("NotificationClosed", GLib.Variant("(uu)", (2, 2)))
    wait_for(lambda: not notifications.active)
    assert len(actions) == 1 and not preferences.ignored(), "dismissal must not ignore or enroll"

    preferences.set_notification_seconds(1)
    notifications.show(key, "Discovered", "Drive", actions=DISCOVERY_ACTIONS)
    wait_for(lambda: 3 in server.closed)
    assert server.calls[-1][-1] == 1000 and not notifications.active

    volume = {"id": key, "identity": {}, "label": "Backup", "device": "/dev/example"}
    preferences.ignore(volume)
    notifications.show(key, "Needs attention", "Suppressed")
    assert not notifications.active and len(server.calls) == 3
    preferences.unignore(key)

    server.mode = "delay"
    delivered = []
    notifications.show(key, "Discovered", "Drive", actions=DISCOVERY_ACTIONS, on_sent=lambda: delivered.append(key))
    wait_for(lambda: bool(server.pending))
    notifications.close(key)
    server.pending.pop().return_value(GLib.Variant("(u)", (4,)))
    wait_for(lambda: 4 in server.closed)
    assert not notifications.active, "late replies must not resurrect a cancelled notification"
    assert not delivered, "cancelled notifications must not acknowledge delivery"

    server.mode = "fail"
    notifications.show(key, "Discovered", "Drive")
    wait_for(lambda: bool(errors))
    assert not notifications.active and "unavailable" in errors[0]

    server.mode = "reply"
    notifications.show(key, "Discovered", "Drive", actions=DISCOVERY_ACTIONS)
    wait_for(lambda: notifications.active[key]["id"] is not None)
    server.release()
    wait_for(lambda: notifications.owner is None)
    replacement = NotificationServer()
    wait_for(lambda: notifications.owner == replacement.bus.get_unique_name())
    preferences.set_notification_seconds(10)
    notifications.show(key, "Discovered again", "Drive", actions=DISCOVERY_ACTIONS)
    wait_for(lambda: notifications.active[key]["id"] is not None)
    server.emit("ActionInvoked", GLib.Variant("(us)", (1, "monitor")))
    replacement.emit("ActionInvoked", GLib.Variant("(us)", (1, "ignore")))
    wait_for(lambda: len(actions) == 2)
    assert actions == [("monitor", key), ("ignore", key)], "ignore stale actions from the old daemon"
    notifications.close_all()
    wait_for(lambda: 1 in replacement.closed)
    messages = []
    def send_update_error():
        try:
            notifications.message("Mount Medic update failed", "<b>Cancelled & retained</b>")
            messages.append("sent")
        except Exception as error:
            messages.append(error)
    sender = Thread(target=send_update_error)
    sender.start()
    wait_for(lambda: bool(messages))
    sender.join(timeout=2)
    assert messages == ["sent"] and replacement.calls[-1][5] == []
    assert replacement.calls[-1][4] == "&lt;b&gt;Cancelled &amp; retained&lt;/b&gt;"
    owner = replacement.bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner",
                                      GLib.Variant("(s)", (BUS_NAME,)), None, Gio.DBusCallFlags.NONE, 2000, None)
    assert not owner.unpack()[0], "Error reporting took the application name and could block GUI restart"
    previous_errors = len(errors)
    replacement.mode = "fail"
    update_key = "update:1.2.0"
    acknowledge = lambda: preferences.save_updates({"notified": "1.2.0"})
    notifications.show(update_key, "Update available", "Install or ignore", on_sent=acknowledge)
    wait_for(lambda: len(errors) > previous_errors)
    assert preferences.updates().get("notified") is None
    replacement.mode = "reply"
    notifications.show(update_key, "Update available", "Install or ignore", on_sent=acknowledge)
    wait_for(lambda: preferences.updates().get("notified") == "1.2.0")
    notifications.close_all()
    replacement.release()
    wait_for(lambda: notifications.owner is None)
    previous_errors = len(errors)
    notifications.show(key, "Discovered", "Drive")
    wait_for(lambda: len(errors) > previous_errors)
    assert not notifications.active
    print("PASS: native D-Bus payload/actions, escaping, dismissal, real expiry, persistent ignore, late replies, failure, daemon restart and updater errors without application ownership")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}):
        exercise()
