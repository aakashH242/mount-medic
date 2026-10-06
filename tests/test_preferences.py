import tempfile
import unittest
from unittest.mock import patch

from mount_medic.installer import setup_notifications
from mount_medic.model import MedicError
from mount_medic.storage import Preferences, atomic_json, notification_seconds


class PreferenceTests(unittest.TestCase):
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
            atomic_json(preferences.config / "preferences.json", {"unrelated": True})
            preferences.set_notification_seconds(600)
            self.assertEqual(Preferences().notification_seconds(), 600)
            from mount_medic.storage import read_json
            self.assertTrue(read_json(preferences.config / "preferences.json")["unrelated"])
            for invalid in (True, 0, -1, 601, 1.5, "10", None):
                with self.subTest(value=invalid), self.assertRaises(MedicError):
                    notification_seconds(invalid)

    def test_setup_accepts_default_and_validates_input(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"XDG_CONFIG_HOME": directory}), patch("sys.stdin.isatty", return_value=True):
            with patch("builtins.input", return_value=""):
                setup_notifications()
            self.assertEqual(Preferences().notification_seconds(), 10)
            with patch("builtins.input", side_effect=["oops", "0", "601", "25"]), patch("builtins.print"):
                setup_notifications()
            self.assertEqual(Preferences().notification_seconds(), 25)
            with patch("builtins.input", return_value=""):
                setup_notifications()
            self.assertEqual(Preferences().notification_seconds(), 25)

    def test_noninteractive_setup_does_not_prompt(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"XDG_CONFIG_HOME": directory}), patch("sys.stdin.isatty", return_value=False), patch("builtins.input", side_effect=AssertionError("unexpected prompt")):
            setup_notifications()
            self.assertEqual(Preferences().notification_seconds(), 10)
