from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import time

from .engine import Engine, ROOT_STATE
from .model import MedicError
from .protocol import BUS_NAME, BUS_PATH, INTERFACE, XML, authorization, validate


def secure_state() -> None:
    ROOT_STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    for path in (ROOT_STATE, *ROOT_STATE.parents):
        info = path.lstat()
        if path.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
            raise MedicError("Privileged state directory is not securely owned")


def serve() -> None:
    from gi.repository import Gio, GLib
    if os.geteuid() != 0:
        raise MedicError("The worker must be started by the system bus as root")
    secure_state()
    engine = Engine()
    pool = ThreadPoolExecutor(max_workers=1)
    loop = GLib.MainLoop()
    state = {"busy": False, "last": time.monotonic()}
    connection = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)

    def authorize(sender: str, request: dict) -> int:
        identity = connection.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                                        "org.freedesktop.DBus", "GetConnectionUnixUser",
                                        GLib.Variant("(s)", (sender,)), GLib.VariantType("(u)"),
                                        Gio.DBusCallFlags.NONE, 10000, None)
        uid = identity.unpack()[0]
        action, flags = authorization(request)
        subject = ("system-bus-name", {"name": GLib.Variant("s", sender)})
        allowed = connection.call_sync("org.freedesktop.PolicyKit1", "/org/freedesktop/PolicyKit1/Authority",
                                       "org.freedesktop.PolicyKit1.Authority", "CheckAuthorization",
                                       GLib.Variant("((sa{sv})sa{ss}us)", (subject, action, {}, flags, "")),
                                       GLib.VariantType("((bba{ss}))"), Gio.DBusCallFlags.NONE,
                                       120000 if flags else 10000, None).unpack()[0][0]
        if not allowed:
            raise MedicError("Authorization denied or cancelled")
        return uid

    def finish(invocation, future):
        try:
            invocation.return_value(GLib.Variant("(s)", (json.dumps(future.result()),)))
        except Exception as error:
            invocation.return_dbus_error(INTERFACE + ".Error", str(error)[:2000])
        state.update(busy=False, last=time.monotonic())
        return False

    def execute(sender: str, request: dict):
        uid = authorize(sender, request)
        return engine.dispatch(uid, request)

    def call(conn, sender, path, interface, method, parameters, invocation):
        try:
            raw = parameters.unpack()[0]
            if len(raw) > 4096:
                raise MedicError("Request is too large")
            request = validate(json.loads(raw))
            if state["busy"]:
                raise MedicError("Another operation is running; retry when it finishes")
            state["busy"] = True
            future = pool.submit(execute, sender, request)
            future.add_done_callback(lambda done: GLib.idle_add(finish, invocation, done))
        except Exception as error:
            invocation.return_dbus_error(INTERFACE + ".Error", str(error)[:2000])

    info = Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0]
    registration = connection.register_object(BUS_PATH, info, call, None, None)
    owner = Gio.bus_own_name_on_connection(connection, BUS_NAME, Gio.BusNameOwnerFlags.NONE, None, None)

    def idle():
        if not state["busy"] and time.monotonic() - state["last"] >= 30:
            loop.quit()
            return False
        return True

    GLib.timeout_add_seconds(5, idle)
    try:
        loop.run()
    finally:
        pool.shutdown(wait=True)
        connection.unregister_object(registration)
        Gio.bus_unown_name(owner)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--request")
    args = parser.parse_args()
    if args.request:
        if os.geteuid() != 0:
            parser.error("Manual fallback requires explicit administrator execution")
        secure_state()
        uid = int(os.environ.get("SUDO_UID", "0"))
        print(json.dumps(Engine().dispatch(uid, validate(json.loads(args.request)))))
    else:
        serve()
