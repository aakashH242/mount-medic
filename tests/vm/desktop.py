"""Runs from the guest's real desktop autostart, never on the host."""
import json
import os
from pathlib import Path
import sys
import traceback
import time

sys.path.insert(0, "/usr/local/lib/mount-medic")


def phase(name):
    Path.home().joinpath("desktop-phase").write_text(name)


def hotplug_window(key):
    from mount_medic.desktop import MedicApplication, GLib, Gtk
    app = MedicApplication()
    state = {"phase": "WINDOW", "started": time.monotonic(), "done": False}
    failure = []

    def accept_monitor():
        try:
            dialog = next(window for window in Gtk.Window.list_toplevels() if window.get_title() == "Drive permissions")
            controls = [child for child in dialog.get_content_area().get_children() if isinstance(child, Gtk.CheckButton)]
            assert [control.get_active() for control in controls] == [True, False, False]
            phase("AUTH_ENROLL")
            dialog.response(Gtk.ResponseType.OK)
        except Exception:
            failure.append(traceback.format_exc())
            app.quit()
        return False

    def observe():
        try:
            assert time.monotonic() - state["started"] < 100, (state, app.rows)
            current = state["phase"]
            if current == "WINDOW" and time.monotonic() - state["started"] > 18:
                state["phase"] = "REMOVE"
                phase("REMOVE")
            elif current in {"REMOVE", "REPLACE"} and app.rows.get(key, {}).get("state") == "absent":
                state["phase"] = "ADD" if current == "REMOVE" else "ADD_REPLACEMENT"
                phase(state["phase"])
            elif current == "ADD" and app.rows.get(key, {}).get("state") == "clean":
                state["phase"] = "REPLACE"
                phase("REPLACE")
            elif current == "ADD_REPLACEMENT":
                new = [row for identity, row in app.rows.items() if identity != key and row["state"] == "unmanaged"]
                if new:
                    assert not new[0].get("settings", {}).get("auto_repair")
                    assert new[0]["volume"]["id"] not in app.reports, "unmanaged replacement was probed"
                    state["replacement"] = new[0]["volume"]["id"]
                    assert app.notifications and app.notifications.active.get(state["replacement"]), "discovery notification missing"
                    state["phase"] = "NOTIFICATION"
                    state["notified"] = time.monotonic()
                    phase("NOTIFICATION")
            elif current == "NOTIFICATION" and time.monotonic() - state["notified"] > 8:
                state["phase"] = "ENROLL"
                GLib.timeout_add_seconds(2, accept_monitor)
                app.notification_action("monitor", state["replacement"])
            elif current == "ENROLL":
                replacement = app.rows.get(state["replacement"], {})
                if replacement.get("settings", {}).get("monitor") and replacement.get("state") == "clean":
                    assert not replacement["settings"].get("auto_repair")
                    state["done"] = True
                    app.quit()
                    return False
        except Exception:
            failure.append(traceback.format_exc())
            app.quit()
            return False
        return True

    def open_window():
        app.activate()
        phase("WINDOW")
        return False

    GLib.timeout_add_seconds(1, open_window)
    GLib.timeout_add_seconds(1, observe)
    app.run(["mount-medic", "watch"])
    assert not failure and state["done"], failure


def main():
    assert Path("/sys/class/dmi/id/product_name").read_text().strip() == "MountMedicTest"
    from mount_medic import client
    from mount_medic.model import MedicError
    key = Path.home().joinpath("volume-id").read_text()
    rows = client.call({"op": "list"})
    assert any(row["volume"]["id"] == key for row in rows)
    report = client.call({"op": "check", "id": key})[0]
    assert report["state"] == "clean", report
    try:
        client.call({"op": "automatic", "id": "0" * 24})
        raise AssertionError("stale request accepted")
    except MedicError:
        pass
    phase("DIRTY_FOR_AUTO")
    for _ in range(40):
        if Path.home().joinpath("dirty-ready").exists():
            break
        time.sleep(0.25)
    assert Path.home().joinpath("dirty-ready").exists()
    repaired = client.repair({"op": "automatic", "id": key})
    assert repaired["repair"]["status"] == "success" and repaired["state"].startswith("mounted_"), repaired
    mounted = repaired["mount"]
    assert mounted["state"] == "mounted"
    assert Path(mounted["mountpoint"], "sentinel.txt").read_text() == "keep these bytes\n"
    client.udisks_operation(key, "Unmount", True)
    time.sleep(10)  # Let the desktop finish registering its native polkit agent.
    request = {"op": "configure", "id": key, "monitor": True, "auto_repair": False, "auto_mount": True}
    phase("AUTH_CANCEL")
    try:
        client.call(request)
        raise AssertionError("cancelled authentication changed permissions")
    except MedicError as error:
        assert "denied or cancelled" in str(error), error
    assert next(row for row in client.call({"op": "list"}) if row["volume"]["id"] == key)["settings"]["auto_repair"]
    phase("AUTH_ACCEPT")
    assert client.call(request)["auto_repair"] is False
    hotplug_window(key)
    return {"passed": True, "session": os.environ.get("XDG_SESSION_TYPE"),
            "desktop": os.environ.get("XDG_CURRENT_DESKTOP"),
            "checks": "D-Bus, polkit cancel/accept, automatic repair plus noninteractive UDisks, user file access, native window, hotplug/reconnect/replacement, unmanaged replacement not probed, notification enrollment action"}


if __name__ == "__main__":
    try:
        result = main()
    except Exception:
        result = {"passed": False, "error": traceback.format_exc()}
    Path.home().joinpath("desktop-result.json").write_text(json.dumps(result))
