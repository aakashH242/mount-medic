"""Native desktop notifications with bounded lifetime and scoped action callbacks."""
from gi.repository import Gio, GLib

from .appearance import ASSETS
from .protocol import BUS_NAME

SERVICE = "org.freedesktop.Notifications"
PATH = "/org/freedesktop/Notifications"
DISCOVERY_ACTIONS = ("monitor", "Monitor", "ignore", "Ignore This Drive")


class Notifications:
    def __init__(self, preferences, on_action, on_error):
        self.preferences = preferences
        self.on_action = on_action
        self.on_error = on_error
        self.active = {}
        self.generation = 0
        self.proxy = Gio.DBusProxy.new_for_bus_sync(
            Gio.BusType.SESSION, Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES | Gio.DBusProxyFlags.DO_NOT_AUTO_START_AT_CONSTRUCTION,
            None, SERVICE, PATH, SERVICE, None)
        self.owner = self.proxy.get_name_owner()
        self.proxy.connect("g-signal", self.signal)
        self.proxy.connect("notify::g-name-owner", self.owner_changed)

    def show(self, key, title, body, *, actions=(), on_sent=None):
        if key in self.preferences.ignored():
            return
        self.close(key)
        seconds = self.preferences.notification_seconds()
        entry = {"id": None, "timer": None, "owner": self.owner, "generation": self.generation,
                 "actions": {"default", *actions[::2]}}
        self.active[key] = entry
        parameters = self.parameters(title, body, ["default", "Open Mount Medic", *actions])

        def sent(proxy, result, unused):
            try:
                entry["id"] = proxy.call_finish(result).unpack()[0]
                if entry["generation"] != self.generation:
                    return
                entry["owner"] = entry["owner"] or self.owner
                if self.active.get(key) is not entry:
                    self.close_remote(entry)
                    return
                # Some servers ignore expire_timeout. Recall it ourselves as well.
                def expire():
                    entry["timer"] = None
                    self.close(key)
                    return False
                entry["timer"] = GLib.timeout_add(seconds * 1000, expire)
                if on_sent:
                    on_sent()
            except GLib.Error as error:
                if self.active.get(key) is entry:
                    self.active.pop(key)
                    self.on_error(f"Desktop notifications unavailable: {error.message}. Open Mount Medic to manage drives.")

        self.proxy.call("Notify", parameters, Gio.DBusCallFlags.NONE, 3000, None, sent, None)

    def parameters(self, title, body, actions):
        return GLib.Variant("(susssasa{sv}i)", (
            "Mount Medic", 0, str(ASSETS / f"{BUS_NAME}.png"), title, GLib.markup_escape_text(body), actions,
            {"desktop-entry": GLib.Variant("s", BUS_NAME), "transient": GLib.Variant("b", True)},
            self.preferences.notification_seconds() * 1000))

    def message(self, title, body):
        # A short-lived updater must send before exiting without owning the application's D-Bus name.
        self.proxy.call_sync("Notify", self.parameters(title, body, []), Gio.DBusCallFlags.NONE, 3000, None)

    def close_remote(self, entry):
        if entry["id"] and entry["owner"]:
            # Address the unique owner: a restarted daemon can reuse notification IDs.
            self.proxy.get_connection().call(entry["owner"], PATH, SERVICE, "CloseNotification",
                                             GLib.Variant("(u)", (entry["id"],)), None,
                                             Gio.DBusCallFlags.NONE, 3000, None, None, None)

    def close(self, key):
        entry = self.active.pop(key, None)
        if entry:
            if entry["timer"]:
                GLib.source_remove(entry["timer"])
            self.close_remote(entry)

    def close_all(self):
        for key in list(self.active):
            self.close(key)

    def owner_changed(self, proxy, unused):
        owner = proxy.get_name_owner()
        if owner == self.owner:
            return
        if self.owner:
            self.generation += 1
            self.close_all()
        else:
            for entry in self.active.values():
                entry["owner"] = owner
        self.owner = owner

    def signal(self, proxy, sender, name, parameters):
        if name not in {"ActionInvoked", "NotificationClosed"}:
            return
        number, value = parameters.unpack()
        for key, entry in list(self.active.items()):
            if entry["id"] != number or entry["owner"] != sender:
                continue
            if name == "ActionInvoked" and value in entry["actions"]:
                self.close(key)
                self.on_action(value, key)
            elif name == "NotificationClosed":
                if entry["timer"]:
                    GLib.source_remove(entry["timer"])
                self.active.pop(key)
            return
