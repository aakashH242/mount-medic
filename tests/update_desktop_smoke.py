"""Real session D-Bus shutdown coordination, with no drive or network activity."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gi.repository import Gio, GLib
from mount_medic.protocol import BUS_NAME
from mount_medic.updates import stop_desktop

CHILD = """
import sys,time
from mount_medic.desktop import MedicApplication
from gi.repository import GLib,Gtk
class App(MedicApplication):
    def start_watching(self): pass
    def refresh(self): pass
    def do_shutdown(self):
        if sys.argv[2] == 'slow': time.sleep(16)
        Gtk.Application.do_shutdown(self)
app = App()
app.busy = sys.argv[2] == 'busy'
GLib.timeout_add_seconds(20, lambda: (app.quit(), False)[1])
raise SystemExit(app.run(['mount-medic', sys.argv[1]]))
"""


def exercise():
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    def owned():
        return bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner",
                             GLib.Variant("(s)", (BUS_NAME,)), None, Gio.DBusCallFlags.NONE, 1000, None).unpack()[0]
    assert not owned(), "Run this test in a private session bus"
    assert stop_desktop() == 0, "No-running-app case must not open the GUI"
    for mode, busy, expected in (("gui", "idle", 10), ("watch", "idle", 11), ("watch", "busy", 2), ("gui", "slow", 10), ("watch", "slow", 11), ("gui", "replacement", 3)):
        print(f"Checking {mode}/{busy}", flush=True)
        with tempfile.TemporaryDirectory() as directory:
            environment = {**os.environ, "XDG_CONFIG_HOME": directory, "XDG_STATE_HOME": directory}
            process = subprocess.Popen([sys.executable, "-B", "-c", CHILD, mode, busy], env=environment, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while not owned():
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise AssertionError("Fixture app did not register: " + process.stderr.read().decode())
                    time.sleep(0.05)
                # Each real CLI/GUI updater has its own GApplication process lifecycle.
                if busy == "replacement":
                    queued = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "RequestName",
                                           GLib.Variant("(su)", (BUS_NAME, 0)), None, Gio.DBusCallFlags.NONE, 1000, None).unpack()[0]
                    assert queued == 2, "Replacement owner was not queued"
                started = time.monotonic()
                coordinator = subprocess.run([sys.executable, "-B", "-c",
                                              "from mount_medic.updates import stop_desktop; raise SystemExit(stop_desktop())"], timeout=25)
                assert coordinator.returncode == expected, coordinator.returncode
                if busy == "slow":
                    assert time.monotonic() - started >= 15.5, "Slow-shutdown fixture did not exceed the old deadline"
                if busy == "busy":
                    assert process.poll() is None, "Update interrupted an active operation"
                else:
                    assert process.wait(timeout=5) == 0
                if busy == "replacement":
                    owner = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "GetNameOwner",
                                          GLib.Variant("(s)", (BUS_NAME,)), None, Gio.DBusCallFlags.NONE, 1000, None).unpack()[0]
                    assert owner == bus.get_unique_name(), "Replacement did not retain the application name"
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=5)
                process.stderr.close()
                if busy == "replacement":
                    bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "ReleaseName",
                                  GLib.Variant("(s)", (BUS_NAME,)), None, Gio.DBusCallFlags.NONE, 1000, None)
    print("PASS: real session D-Bus idle GUI/background shutdown, restart modes, active-operation refusal and absent-app handling")


if __name__ == "__main__":
    exercise()
