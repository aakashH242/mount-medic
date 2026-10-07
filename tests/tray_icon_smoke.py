"""Check KDE's real tray icon cache in a private D-Bus and offscreen Qt6 display.

Run: dbus-run-session -- python3 tests/tray_icon_smoke.py tests/artifacts/tray-icon
Requires the installed Plasma system tray QML plugin, ksplashqml, and Python GI.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, Gio, GLib

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mount_medic import appearance
from mount_medic.protocol import BUS_NAME


def rendered_color(path: Path, background: tuple) -> tuple:
    image = GdkPixbuf.Pixbuf.new_from_file(str(path))
    pixels = image.get_pixels()
    colors = {}
    for y in range(24, 56):
        for x in range(24, 56):
            offset = y * image.get_rowstride() + x * image.get_n_channels()
            color = tuple(pixels[offset:offset + 3])
            if color != background:
                colors[color] = colors.get(color, 0) + 1
    assert colors, "native tray did not render its icon"
    return max(colors, key=colors.get)


def check_native_tray(output: Path, foreground: str):
    output.mkdir(parents=True, exist_ok=True)
    for number in (1, 2, 3):
        (output / f"{number}.png").unlink(missing_ok=True)
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    owned = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner",
                          GLib.Variant("(s)", ("org.kde.StatusNotifierWatcher",)), None,
                          Gio.DBusCallFlags.NONE, 1000, None).unpack()[0]
    assert not owned, "use dbus-run-session; this test must not connect to your desktop tray"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        config = root / "config"
        config.mkdir()
        rgb = "255,255,255" if foreground == "white" else "0,0,0"
        background = (32, 35, 38) if foreground == "white" else (239, 240, 241)
        (config / "kdeglobals").write_text("[Icons]\nTheme=breeze\n" + "".join(
            f"[Colors:{section}]\nForegroundNormal={rgb}\nBackgroundNormal={','.join(map(str, background))}\n"
            for section in ("Window", "Button", "View", "Complementary")))
        bundle = root / "bundle"
        bundle.mkdir()
        name = BUS_NAME + "-symbolic.svg"
        data = (appearance.ASSETS / name).read_bytes()
        with patch.dict(os.environ, {"XDG_CACHE_HOME": str(root / "cache")}), patch.object(appearance, "ASSETS", bundle):
            (bundle / name).write_bytes(data.replace(b'fill="currentColor"', b'fill="#2e3436"'))
            old_path = appearance.tray_icon_path()
            (bundle / name).write_bytes(data)
            new_path = appearance.tray_icon_path()
            assert old_path != new_path, "changed SVG content must change the tray theme path"
            assert (new_path / name).read_bytes() == data
            assert appearance.tray_icon_path() == new_path, "unchanged icons should reuse their cache path"
            (new_path / name).write_bytes(b"partial icon")
            appearance.tray_icon_path()
            assert (new_path / name).read_bytes() == data, "damaged cached icons must be replaced"
        # Use a unique name so an installed application icon cannot shadow the fixture.
        icon_name = "mount-medic-cache-fixture-symbolic"
        shutil.copyfile(old_path / name, old_path / (icon_name + ".svg"))
        shutil.copyfile(new_path / name, new_path / (icon_name + ".svg"))
        properties = {"Id": "mount-medic-cache-fixture", "Category": "Hardware", "Status": "Active",
                      "IconName": icon_name, "IconThemePath": str(old_path), "Title": "Mount Medic fixture",
                      "ItemIsMenu": False, "AttentionIconName": "", "OverlayIconName": ""}
        item_xml = '<node><interface name="org.kde.StatusNotifierItem">' + "".join(
            f'<property name="{key}" type="{"b" if isinstance(value, bool) else "s"}" access="read"/>'
            for key, value in properties.items()) + '<signal name="NewIcon"/></interface></node>'
        item = Gio.DBusNodeInfo.new_for_xml(item_xml).interfaces[0]
        registration = bus.register_object("/StatusNotifierItem", item, None,
            lambda connection, sender, path, interface, key: GLib.Variant(
                "b" if isinstance(properties[key], bool) else "s", properties[key]), None)
        service = "org.mountmedic.TrayFixture"
        for name in (service, "org.kde.StatusNotifierWatcher"):
            bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "RequestName",
                          GLib.Variant("(su)", (name, 0)), None, Gio.DBusCallFlags.NONE, 1000, None)
        watcher = Gio.DBusNodeInfo.new_for_xml('''<node><interface name="org.kde.StatusNotifierWatcher">
          <method name="RegisterStatusNotifierHost"><arg type="s" direction="in"/></method>
          <property name="RegisteredStatusNotifierItems" type="as" access="read"/>
          <property name="IsStatusNotifierHostRegistered" type="b" access="read"/>
          <property name="ProtocolVersion" type="i" access="read"/>
        </interface></node>''').interfaces[0]
        host = bus.register_object("/StatusNotifierWatcher", watcher,
            lambda connection, sender, path, interface, method, arguments, invocation: invocation.return_value(None),
            lambda connection, sender, path, interface, key: {
                "RegisteredStatusNotifierItems": GLib.Variant("as", [service + "/StatusNotifierItem"]),
                "IsStatusNotifierHostRegistered": GLib.Variant("b", True),
                "ProtocolVersion": GLib.Variant("i", 0)}[key], None)
        package = root / "package"
        (package / "contents/splash").mkdir(parents=True)
        (package / "metadata.json").write_text(json.dumps({"KPackageStructure": "Plasma/LookAndFeel",
            "KPlugin": {"Id": "mount-medic-tray-check", "Name": "Mount Medic isolated tray check"}}))
        qml = '''import QtQuick
import org.kde.kirigami as Kirigami
import org.kde.plasma.private.systemtray as Tray
Rectangle {
    id: root; property int stage: 0; color: BACKGROUND
    Tray.StatusNotifierModel { id: trayModel }
    Rectangle {
        id: frame; width: 128; height: 80; color: root.color
        Repeater {
            model: trayModel
            Kirigami.Icon { required property var model; x: 24; y: 24; width: 32; height: 32; source: model.Icon || model.IconName }
        }
    }
    Timer {
        interval: 1200; repeat: true; running: true; property int counter: 0
        onTriggered: {
            counter++;
            frame.grabToImage(function(image) {
                image.saveToFile(OUTPUT + "/" + counter + ".png");
                if (counter === 3) Qt.quit();
            });
        }
    }
}'''
        color = "#%02x%02x%02x" % background
        qml = qml.replace("BACKGROUND", json.dumps(color)).replace("OUTPUT", json.dumps(str(output)))
        (package / "contents/splash/Splash.qml").write_text(qml)
        environment = {**os.environ, "XDG_CACHE_HOME": str(root / "cache"), "XDG_CONFIG_HOME": str(config),
                       "XDG_DATA_HOME": str(root / "data"), "QT_QPA_PLATFORM": "offscreen",
                       "QT_QPA_PLATFORMTHEME": "kde", "QT_QUICK_BACKEND": "software"}
        process = subprocess.Popen(["ksplashqml", "--test", "--window", "--nofork", str(package)],
                                   env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        loop = GLib.MainLoop()
        phase = 0
        deadline = time.monotonic() + 20
        def advance():
            nonlocal phase
            if phase == 0 and (output / "1.png").exists():
                (old_path / (icon_name + ".svg")).write_bytes(data)
                phase = 1
                bus.emit_signal(None, "/StatusNotifierItem", "org.kde.StatusNotifierItem", "NewIcon", None)
            elif phase == 1 and (output / "2.png").exists():
                properties["IconThemePath"] = str(new_path)
                phase = 2
                bus.emit_signal(None, "/StatusNotifierItem", "org.kde.StatusNotifierItem", "NewIcon", None)
            if process.poll() is not None:
                loop.quit()
                return False
            if time.monotonic() > deadline:
                process.terminate()
                loop.quit()
                return False
            return True
        GLib.timeout_add(40, advance)
        loop.run()
        log = process.communicate(timeout=5)[0]
        (output / "native.log").write_text(log)
        bus.unregister_object(registration)
        bus.unregister_object(host)
        assert process.returncode == 0, log
        colors = [rendered_color(output / f"{number}.png", background) for number in (1, 2, 3)]
        assert colors[:2] == [(46, 52, 54), (46, 52, 54)], colors
        expected = (255, 255, 255) if foreground == "white" else (0, 0, 0)
        assert colors[2] == expected, colors
        (output / "results.json").write_text(json.dumps({"foreground": foreground,
            "old_icon": colors[0], "same_path_after_replacement": colors[1], "content_path_after_replacement": colors[2]}, indent=2))
        print("PASS: KDE stale pixels reproduced; content-specific theme path rendered", foreground)


if __name__ == "__main__":
    check_native_tray(Path(sys.argv[1]).resolve(), sys.argv[2] if len(sys.argv) > 2 else "white")
