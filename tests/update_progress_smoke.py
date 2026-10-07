"""Native progress, real app shutdown/reopen, and post-restart result delivery."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Event, Thread
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gio, GLib, Gtk
from mount_medic import __version__, updates
from mount_medic.model import MedicError
from mount_medic.protocol import BUS_NAME
from mount_medic.storage import Preferences
from mount_medic.update_progress import UpdateWindow, run
from update_fixture import TARGET_VERSION, metadata
from ui_smoke import capture

CHILD = """
import json,sys
from pathlib import Path
from mount_medic.desktop import MedicApplication
from gi.repository import GLib,Gtk
class App(MedicApplication):
    def start_watching(self): pass
    def refresh(self): pass
    def do_shutdown(self): Gtk.Application.do_shutdown(self)
    def do_activate(self):
        super().do_activate()
        Path(sys.argv[1]).write_text(json.dumps({'visible':self.window.get_visible(),
            'message':self.toast.message.get_text(), 'result':self.preferences.updates().get('install_result'),
            'delivered':self.pending_update_result is None}))
app=App()
if len(sys.argv)>2 and sys.argv[2]=='unwritable':
    def reject_save(*args): raise OSError('Synthetic unwritable update result')
    app.preferences.save_updates=reject_save
    app.preferences.mark_update_result_seen=reject_save
GLib.timeout_add_seconds(25,lambda:(app.quit(),False)[1])
raise SystemExit(app.run(['mount-medic','gui']))
"""


def owned():
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    return bus.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus', 'NameHasOwner',
                         GLib.Variant('(s)', (BUS_NAME,)), None, Gio.DBusCallFlags.NONE, 1000, None).unpack()[0]


def wait_for(predicate):
    deadline = time.monotonic() + 8
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError('Native fixture timed out')
        while GLib.MainContext.default().pending():
            GLib.MainContext.default().iteration(False)
        time.sleep(0.01)


def reject_unready_owner():
    script = """
import sys
from gi.repository import Gio
from mount_medic.protocol import BUS_NAME
app=Gio.Application(application_id=BUS_NAME, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
app.connect('command-line',lambda *args:2)
app.register(None)
app.hold()
app.run(['fixture'])
"""
    process = subprocess.Popen([sys.executable, '-B', '-c', script])
    try:
        wait_for(owned)
        window = UpdateWindow(TARGET_VERSION)
        with patch('mount_medic.update_progress.time.monotonic', side_effect=[0,0,1,16]):
            try:
                window.wait_for_restart(process, 'gui')
                raise AssertionError('Bus owner without a visible app was accepted')
            except MedicError:
                pass
        window.destroy()
        print('PASS: bus registration alone is not accepted as a reopened app')
    finally:
        process.terminate()
        process.wait(timeout=5)
        wait_for(lambda: not owned())


def dependencies_dialog():
    window = UpdateWindow(TARGET_VERSION)
    window.show_all()
    for response in (Gtk.ResponseType.YES, Gtk.ResponseType.CANCEL):
        result = []
        def answer():
            for dialog in Gtk.Window.list_toplevels():
                if isinstance(dialog, Gtk.MessageDialog):
                    assert dialog.get_transient_for() == window and dialog.get_modal()
                    dialog.response(response)
                    return False
            return True
        GLib.timeout_add(30, answer)
        thread = Thread(target=lambda: result.append(window.confirm('Synthetic required packages; no installation.')))
        thread.start()
        wait_for(lambda: bool(result))
        thread.join(timeout=2)
        assert result == [response == Gtk.ResponseType.YES]
    window.destroy()
    print('PASS: required-package confirmation stays on the GTK thread; accept/cancel both return')


def exercise(output, failure):
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, XDG_CONFIG_HOME=directory, XDG_STATE_HOME=directory):
        preferences = Preferences()
        old_marker, new_marker = Path(directory)/'old.json', Path(directory)/'new.json'
        processes = []
        real_popen = subprocess.Popen
        old = real_popen([sys.executable, '-B', '-c', CHILD, str(old_marker)])
        processes.append(old)
        wait_for(lambda: old_marker.exists() and owned())
        stages, rendered = [], Event()
        original_stage = UpdateWindow.stage

        def stage(window, text):
            stages.append(text)
            original_stage(window, text)
            if text == 'Downloading the update…':
                def screenshot():
                    assert window.get_visible() and not window.close_button.get_sensitive()
                    assert window.emit('delete-event', None), 'Closing progress could interrupt installation'
                    assert window.get_visible()
                    assert window.message.get_text() == text
                    capture(window, output/f'{failure}-downloading.png')
                    rendered.set()
                    return False
                GLib.idle_add(screenshot)

        def download(*args):
            assert rendered.wait(5), 'Progress UI did not stay responsive during download'
            if failure == 'offline':
                raise MedicError('Synthetic network failure')
            return Path(directory)/'archive'

        def launch(command, **kwargs):
            if command[0] == '/usr/local/bin/mount-medic':
                assert kwargs['env'].get('MOUNT_MEDIC_UPDATE_RESULT'), 'Restart lost its outcome'
                if failure == 'restart':
                    raise OSError('Synthetic launcher failure')
                extra = ['unwritable'] if failure == 'reporting' else []
                process = real_popen([sys.executable, '-B', '-c', CHILD, str(new_marker), *extra], **kwargs)
                processes.append(process)
                return process
            return real_popen(command, **kwargs)

        def installer(command):
            script = "print('MM_UPDATE_STAGE:verify',flush=True); print('MM_UPDATE_STAGE:build',flush=True); print('MM_UPDATE_STAGE:install',flush=True)"
            script += '\nraise SystemExit(126)' if failure == 'cancelled' else '\nraise SystemExit(0)'
            return [sys.executable, '-c', script]

        def dismiss_error():
            for window in Gtk.Window.list_toplevels():
                if isinstance(window, UpdateWindow) and window.finished:
                    assert window.close_button.get_sensitive()
                    assert window.message.get_accessible().get_description() == window.message.get_text()
                    capture(window, output/f'{failure}-result.png')
                    screen = window.get_screen()
                    assert window.get_allocated_height() <= screen.get_height(), 'Update result pushed Close off-screen'
                    close = window.close_button.translate_coordinates(window, 0, 0)
                    assert close and close[1] + window.close_button.get_allocated_height() <= window.get_allocated_height()
                    if failure == 'offline':
                        window.emit('delete-event', None)
                    else:
                        window.response(Gtk.ResponseType.CLOSE)
                    return False
            return True

        timer = GLib.timeout_add(50, dismiss_error)
        original_store = updates.store_result
        def store(result):
            if failure == 'reporting':
                raise OSError('Synthetic unwritable update result')
            original_store(result)
        try:
            with patch('os.geteuid', return_value=1000), patch('mount_medic.updates.installed', return_value=True), patch('mount_medic.updates.shutil.which', return_value='/usr/bin/pkexec'), patch('mount_medic.updates.fetch_release', return_value=metadata()), patch('mount_medic.updates.confirm_dependencies', return_value=[]), patch('mount_medic.updates.download_archive', side_effect=download), patch('mount_medic.updates.worker_running', return_value=False), patch('mount_medic.installer.elevated', side_effect=installer), patch('mount_medic.updates.source_version', return_value=__version__ if failure in ('cancelled','offline') else TARGET_VERSION), patch('mount_medic.updates.subprocess.Popen', side_effect=launch), patch('mount_medic.updates.store_result', side_effect=store), patch('mount_medic.notifications.desktop_message'), patch.object(UpdateWindow, 'stage', stage):
                result = run(SimpleNamespace(version=TARGET_VERSION, sha256=metadata()['sha256'], restart='gui'))
            assert result == (0 if failure in ('success','reporting') else 2)
            if failure in ('success','cancelled','reporting'):
                wait_for(new_marker.exists)
                restarted = json.loads(new_marker.read_text())
                assert restarted['visible'] and restarted['delivered']
                if failure != 'reporting':
                    assert restarted['result']['seen']
                expected = f'Kept version {__version__}' if failure == 'cancelled' else 'Update installed successfully'
                assert expected in restarted['message'], restarted
                assert old.wait(timeout=5) == 0
                assert 'Installing update…' in stages and 'Restarting Mount Medic…' in stages
            if failure == 'offline':
                assert old.poll() is None and not new_marker.exists(), 'Download failure closed the current app'
            if failure == 'restart':
                saved = preferences.updates()['install_result']
                assert 'Update installed successfully' in saved['message'] and 'Synthetic launcher failure' in saved['message']
                assert 'Kept version' not in saved['message']
            print(f'PASS: native update progress {failure}: {stages}', flush=True)
        finally:
            if failure in ('success','reporting'):
                GLib.source_remove(timer)
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=5)
            wait_for(lambda: not owned())


if __name__ == '__main__':
    assert Gtk.init_check()[0]
    if os.environ.get('MM_UI_FONT'):
        Gtk.Settings.get_default().set_property('gtk-font-name', os.environ['MM_UI_FONT'])
    output = Path(sys.argv[1])
    output.mkdir(parents=True, exist_ok=True)
    assert not owned(), 'Use a private D-Bus session'
    for failure in ('success','cancelled','offline','restart','reporting'):
        exercise(output, failure)
    reject_unready_owner()
    dependencies_dialog()
