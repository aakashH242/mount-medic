from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mount_medic.installer import autostart, guided, install_tree, uninstall_tree
from mount_medic.storage import read_json
from mount_medic.model import MedicError


class InstallerTests(unittest.TestCase):
    def test_guided_setup_configures_notifications_only_after_installing(self):
        with patch("os.geteuid", return_value=1000), patch("mount_medic.installer.ask", side_effect=[False, True, False]), patch("mount_medic.installer.subprocess.run") as run, patch("mount_medic.installer.payload", return_value={}), patch("mount_medic.installer.elevated", side_effect=lambda command: command), patch("mount_medic.installer.setup_notifications") as setup, patch("builtins.print"):
            setup.side_effect = lambda: self.assertEqual(run.call_count, 2)
            guided(Path("/disposable/source"))
            setup.assert_called_once_with()

    def test_declined_install_never_elevates(self):
        with patch("os.geteuid", return_value=1000), patch("mount_medic.installer.ask", return_value=False), patch("mount_medic.installer.subprocess.run"), patch("mount_medic.installer.payload", return_value={}), patch("mount_medic.installer.elevated", side_effect=AssertionError("elevated after declining")), patch("builtins.print"):
            with self.assertRaisesRegex(MedicError, "cancelled"):
                guided(Path("/disposable/source"))

    def test_startup_enable_disable(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"XDG_CONFIG_HOME": directory}), patch.object(Path, "read_text", return_value="Name=Mount Medic\n"):
            autostart(True)
            target = Path(directory) / "autostart/io.github.aakashH242.MountMedic.desktop"
            self.assertTrue(target.is_file())
            autostart(False)
            self.assertFalse(target.exists())

    def test_reinstall_and_uninstall_preserve_unrelated_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unrelated = root / "unrelated"
            unrelated.write_text("keep")
            files = {"usr/local/lib/mount-medic/worker": (b"worker", 0o755)}
            with patch("mount_medic.installer.payload", return_value=files):
                install_tree(root, root)
                install_tree(root, root)
                uninstall_tree(root)
            self.assertEqual(unrelated.read_text(), "keep")

    def test_modified_installed_file_is_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relative = "usr/local/lib/mount-medic/worker"
            with patch("mount_medic.installer.payload", return_value={relative: (b"initial", 0o755)}):
                install_tree(root, root)
            (root / relative).write_bytes(b"local changes")
            self.assertEqual(uninstall_tree(root)["preserved_modified_files"], [str(root / relative)])

    def test_symlink_target_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "usr").symlink_to("/tmp")
            with patch("mount_medic.installer.payload", return_value={"usr/local/bin/mount-medic": (b"x", 0o755)}):
                with self.assertRaises(MedicError):
                    install_tree(root, root)

    def test_interrupted_upgrade_retains_previous_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = {"usr/local/lib/mount-medic/worker": (b"old", 0o755),
                        "usr/local/lib/mount-medic/retained": (b"keep", 0o644)}
            with patch("mount_medic.installer.payload", return_value=original):
                install_tree(root, root)
            partial = {"usr/local/lib/mount-medic/worker": (b"new", 0o755)}
            with patch("mount_medic.installer.payload", return_value=partial):
                install_tree(root, root)
            manifest = read_json(root / "usr/local/lib/mount-medic/manifest.json")
            self.assertIn("usr/local/lib/mount-medic/retained", manifest)
