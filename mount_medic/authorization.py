from threading import Lock
import time

from .model import MedicError
from .protocol import authorization, authorization_hours


class Authorizer:
    def __init__(self, connection):
        self.connection = connection
        self.approvals = {}
        self.pending = {}
        self.lock = Lock()

    def user_id(self, sender):
        from gi.repository import Gio, GLib
        return self.connection.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                                         "org.freedesktop.DBus", "GetConnectionUnixUser",
                                         GLib.Variant("(s)", (sender,)), GLib.VariantType("(u)"),
                                         Gio.DBusCallFlags.NONE, 10000, None).unpack()[0]

    def authorize(self, sender, request):
        from gi.repository import Gio, GLib
        uid = self.user_id(sender)
        action, flags = authorization(request)
        hours = authorization_hours(request.get("auth_hours", 1))
        token = object()
        if flags:
            with self.lock:
                saved_uid, saved_action, expires = self.approvals.get(sender, (None, None, 0))
                if saved_uid == uid and saved_action == action and time.monotonic() < expires:
                    return uid
                self.pending[sender] = token
        subject = ("system-bus-name", {"name": GLib.Variant("s", sender)})
        try:
            allowed = self.connection.call_sync("org.freedesktop.PolicyKit1", "/org/freedesktop/PolicyKit1/Authority",
                                            "org.freedesktop.PolicyKit1.Authority", "CheckAuthorization",
                                            GLib.Variant("((sa{sv})sa{ss}us)", (subject, action, {}, flags, "")),
                                            GLib.VariantType("((bba{ss}))"), Gio.DBusCallFlags.NONE,
                                            120000 if flags else 10000, None).unpack()[0][0]
            if not allowed:
                raise MedicError("Authorization denied or cancelled")
            if flags:
                with self.lock:
                    # Quit or a settings change can revoke a still-open password prompt.
                    if self.pending.get(sender) is not token:
                        raise MedicError("Authorization was cleared before the operation started")
                    if self.user_id(sender) != uid:
                        raise MedicError("Authorization caller changed")
                    self.approvals[sender] = (uid, action, time.monotonic() + hours * 3600)
            return uid
        finally:
            if flags:
                with self.lock:
                    if self.pending.get(sender) is token:
                        self.pending.pop(sender)

    def forget(self, sender):
        with self.lock:
            self.approvals.pop(sender, None)
            self.pending.pop(sender, None)

    def active(self):
        with self.lock:
            now = time.monotonic()
            self.approvals = {sender: entry for sender, entry in self.approvals.items() if now < entry[2]}
            return bool(self.approvals)
