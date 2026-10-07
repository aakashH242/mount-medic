import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mount_medic.installer import autostart, autostart_enabled, autostart_path, setup_notifications, setup_security
from mount_medic.model import MedicError
from mount_medic.storage import Preferences, atomic_json, notification_seconds


class PreferenceTests(unittest.TestCase):
    def test_security_duration_defaults_persists_and_preserves_other_settings(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"XDG_CONFIG_HOME": directory}):
            preferences = Preferences()
            self.assertEqual(preferences.authorization_hours(), 1)
            preferences.set_notification_seconds(23)
            preferences.set_authorization_hours(24)
            self.assertEqual(Preferences().authorization_hours(), 24)
            self.assertEqual(Preferences().notification_seconds(), 23)
            path = preferences.config / "preferences.json"
            before = path.read_bytes()
            for value in (0, 25, True, 1.5, "24", None):
                with self.subTest(value=value), self.assertRaises(MedicError):
                    preferences.set_authorization_hours(value)
                self.assertEqual(path.read_bytes(), before)

    def test_security_setup_validates_whole_hours_and_preserves_saved_choice(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"XDG_CONFIG_HOME": directory}):
            with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value=""):
                setup_security()
            self.assertEqual(Preferences().authorization_hours(), 1)
            with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=["no", "0", "25", "1.5", "24"]), patch("builtins.print"):
                setup_security()
            with patch("sys.stdin.isatty", return_value=False), patch("builtins.input", side_effect=AssertionError("unexpected prompt")):
                setup_security()
            self.assertEqual(Preferences().authorization_hours(), 24)

    def test_startup_toggle_reads_and_changes_the_existing_desktop_entry(self):
        entry = "[Desktop Entry]\nName=Mount Medic\nType=Application\nExec=mount-medic --watch\n"
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"XDG_CONFIG_HOME": directory}):
            self.assertFalse(autostart_enabled())
            with patch.object(Path, "read_text", return_value=entry):
                autostart(True)
            self.assertEqual(autostart_path().read_text(), entry)
            self.assertTrue(autostart_enabled())
            for disabled in ("Hidden = true\n", "X-GNOME-Autostart-enabled=false\n"):
                autostart_path().write_text(entry + disabled)
                self.assertFalse(autostart_enabled())
            autostart(False)
            self.assertFalse(autostart_path().exists())
            autostart_path().write_text("broken entry")
            with self.assertRaisesRegex(MedicError, "Cannot read startup settings"):
                autostart_enabled()
            self.assertEqual(autostart_path().read_text(), "broken entry")

    def test_ignore_survives_restart_and_never_matches_just_label_or_path(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"XDG_CONFIG_HOME": directory}):
            first = {"id": "a" * 24, "identity": {"uuid": "A", "hardware": "disk-a"}, "label": "Backup", "device": "/dev/example1"}
            second = {**first, "id": "b" * 24, "identity": {"uuid": "B", "hardware": "disk-b"}}
            preferences = Preferences()
            preferences.ignore(first)
            preferences.set_notification_seconds(27)
            restarted = Preferences()
            self.assertEqual(restarted.ignored(), {first["id"]: first})
            self.assertNotIn(second["id"], restarted.ignored())
            restarted.ignore(second)
            restarted.unignore(first["id"])
            self.assertEqual(restarted.ignored(), {second["id"]: second})
            self.assertEqual(restarted.notification_seconds(), 27)

    def test_notification_duration_defaults_and_preserves_other_preferences(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"XDG_CONFIG_HOME": directory}):
            preferences = Preferences()
            self.assertEqual(preferences.notification_seconds(), 10)
            self.assertEqual(preferences.toast_seconds(), 3)
            atomic_json(preferences.config / "preferences.json", {"unrelated": True})
            preferences.set_notification_seconds(600)
            self.assertEqual(Preferences().notification_seconds(), 600)
            from mount_medic.storage import read_json
            self.assertTrue(read_json(preferences.config / "preferences.json")["unrelated"])
            preferences.set_notification_settings({"notification_seconds": 23, "toast_seconds": 7})
            self.assertEqual(Preferences().notification_seconds(), 23)
            self.assertEqual(Preferences().toast_seconds(), 7)
            with self.assertRaises(MedicError):
                preferences.set_notification_settings({"notification_seconds": 30, "toast_seconds": 0})
            self.assertEqual(Preferences().notification_seconds(), 23)
            for invalid in (True, 0, -1, 601, 1.5, "10", None):
                with self.subTest(value=invalid), self.assertRaises(MedicError):
                    notification_seconds(invalid)

    def test_setup_accepts_default_and_validates_input(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"XDG_CONFIG_HOME": directory}), patch("sys.stdin.isatty", return_value=True):
            with patch("builtins.input", return_value=""):
                setup_notifications()
            self.assertEqual(Preferences().notification_seconds(), 10)
            self.assertTrue(Preferences().notification_sound_enabled())
            with patch("builtins.input", side_effect=["oops", "0", "601", "25", "0", "7", "maybe", "no"]), patch("builtins.print"):
                setup_notifications()
            self.assertEqual(Preferences().notification_seconds(), 25)
            self.assertEqual(Preferences().toast_seconds(), 7)
            self.assertFalse(Preferences().notification_sound_enabled())
            with patch("builtins.input", return_value=""):
                setup_notifications()
            self.assertEqual(Preferences().notification_seconds(), 25)
            self.assertFalse(Preferences().notification_sound_enabled())
            with patch("builtins.input", side_effect=["", "", "yes"]):
                setup_notifications()
            self.assertTrue(Preferences().notification_sound_enabled())

    def test_sound_defaults_persists_and_validates_before_writing_any_setting(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"XDG_CONFIG_HOME": directory}):
            preferences = Preferences()
            self.assertTrue(preferences.notification_sound_enabled())
            path = preferences.config / "preferences.json"
            atomic_json(path, {"unrelated": True, "notification_seconds": 23, "toast_seconds": 7})
            self.assertTrue(preferences.notification_sound_enabled())
            preferences.set_notification_settings({"notification_sound": False})
            self.assertFalse(Preferences().notification_sound_enabled())
            preferences.set_notification_seconds(25)
            self.assertFalse(Preferences().notification_sound_enabled())
            saved = path.read_bytes()
            for invalid in (0, 1, "false", None, []):
                with self.subTest(value=invalid), self.assertRaises(MedicError):
                    preferences.set_notification_settings({"notification_seconds": 30, "notification_sound": invalid})
                self.assertEqual(path.read_bytes(), saved)
            self.assertEqual(Preferences().toast_seconds(), 7)
            self.assertIn('"unrelated": true', path.read_text())
            atomic_json(path, {"notification_sound": "false"})
            with self.assertRaisesRegex(MedicError, "Notification sound"):
                preferences.notification_sound_enabled()

    def test_noninteractive_setup_does_not_prompt(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"XDG_CONFIG_HOME": directory}), patch("sys.stdin.isatty", return_value=False), patch("builtins.input", side_effect=AssertionError("unexpected prompt")):
            setup_notifications()
            self.assertEqual(Preferences().notification_seconds(), 10)
            self.assertEqual(Preferences().toast_seconds(), 3)
            self.assertTrue(Preferences().notification_sound_enabled())
            Preferences().set_notification_settings({"notification_sound": False})
            setup_notifications()
            self.assertFalse(Preferences().notification_sound_enabled())
