import os
import base64
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from mount_medic.dependencies import missing_packages, package_install_command
from mount_medic.installer import autostart, decode_payload, guided, install_payload, install_tree, prepare_install, read_build_output, uninstall_tree
from mount_medic.storage import Preferences, read_json
from mount_medic.model import MedicError


class InstallerTests(unittest.TestCase):
    def test_target_payload_accepts_scoped_new_integration_and_rejects_unsafe_output(self):
        relative = "usr/share/dbus-1/system.d/io.github.aakashH242.MountMedic-new.conf"
        encoded = base64.b64encode(b"target integration").decode("ascii")
        self.assertEqual(decode_payload(json.dumps({relative: [encoded, 0o644]}).encode()), {relative: (b"target integration", 0o644)})
        unsafe = ["etc/sudoers", "usr/local/bin/other-app", "usr/local/lib/mount-medic/../other", "/usr/local/lib/mount-medic/worker",
                  "usr/local/lib/mount-medic//worker", "usr/local/lib/mount-medic/manifest.json", "usr/share/dbus-1/system.d/other.conf"]
        for path in unsafe:
            with self.subTest(path=path), self.assertRaises(MedicError):
                decode_payload(json.dumps({path: [encoded, 0o644]}).encode())
        for entry in ([encoded, 0o4755], [encoded, True], ["not base64", 0o644], {}, [encoded]):
            with self.subTest(entry=entry), self.assertRaises(MedicError):
                decode_payload(json.dumps({relative: entry}).encode())

    def test_build_output_rejects_symlinks_hardlinks_nonfiles_and_oversize(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            output.write_bytes(b"valid")
            self.assertEqual(read_build_output(output, 5), b"valid")
            link = root / "link"
            link.symlink_to(output)
            with self.assertRaises(OSError):
                read_build_output(link, 5)
            with self.assertRaises(MedicError):
                read_build_output(output, 4)
            os.link(output, root / "hardlink")
            with self.assertRaises(MedicError):
                read_build_output(output, 5)
            os.mkfifo(root / "fifo")
            with self.assertRaises(MedicError):
                read_build_output(root / "fifo", 5)

    def test_obsolete_external_files_are_removed_with_rollback_and_modified_files_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = "usr/share/dbus-1/system.d/io.github.aakashH242.MountMedic-old.conf"
            edited = "usr/share/dbus-1/system.d/io.github.aakashH242.MountMedic-local.conf"
            new = "usr/share/dbus-1/system.d/io.github.aakashH242.MountMedic-new.conf"
            original = {old: (b"old", 0o644), edited: (b"original", 0o644)}
            install_payload(original, root)
            (root / edited).write_bytes(b"local edit")
            with patch("mount_medic.installer.exchange_directories", side_effect=OSError("interrupted")):
                with self.assertRaises(OSError):
                    install_payload({new: (b"new", 0o644)}, root)
            self.assertEqual((root / old).read_bytes(), b"old")
            self.assertFalse((root / new).exists())
            install_payload({new: (b"new", 0o644)}, root)
            self.assertFalse((root / old).exists())
            self.assertEqual((root / edited).read_bytes(), b"local edit")
            self.assertEqual(set(read_json(root / "usr/local/lib/mount-medic/manifest.json")), {new})
            uninstall_tree(root)
            self.assertFalse((root / new).exists())

    def test_guided_setup_configures_notifications_only_after_installing(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}),
            patch("os.geteuid", return_value=1000),
            patch("os.getuid", return_value=1000),
            patch("mount_medic.installer.ask", side_effect=[True, False]),
            patch("mount_medic.installer.subprocess.run") as run,
            patch("mount_medic.installer.missing_packages", return_value=["ntfs-3g-devel"]),
            patch("mount_medic.installer.elevated", side_effect=lambda command: command) as elevate,
            patch("mount_medic.installer.shutil.copytree"),
            patch("mount_medic.installer.shutil.copy2"),
            patch("mount_medic.installer.setup_notifications") as setup,
            patch("builtins.print"),
        ):
            preferences = Preferences()
            preferences.save_updates({"install_error": "previous failed update"})
            setup.side_effect = lambda: self.assertEqual(run.call_count, 1)
            guided(Path("/disposable/source"))
            elevate.assert_called_once()
            command = run.call_args.args[0]
            self.assertEqual(command[-2:], ["--build-user", "1000"])
            self.assertIn("--setup", command)
            self.assertNotEqual(command[1], "/disposable/source/install.py")
            setup.assert_called_once_with()
            self.assertIsNone(preferences.updates()["install_error"])

    def test_declined_install_never_elevates(self):
        with patch("os.geteuid", return_value=1000), patch("mount_medic.installer.ask", return_value=False), patch("mount_medic.installer.missing_packages", return_value=[]), patch("mount_medic.installer.elevated", side_effect=AssertionError("elevated after declining")), patch("builtins.print"):
            with self.assertRaisesRegex(MedicError, "cancelled"):
                guided(Path("/disposable/source"))

    def test_setup_automatically_installs_missing_packages_and_builds_as_user(self):
        user = Mock(pw_gid=1000, pw_name="desktop", pw_dir="/home/desktop")
        with patch("mount_medic.installer.pwd.getpwuid", return_value=user), patch("mount_medic.installer.os.getgrouplist", return_value=[1000, 10]), patch("mount_medic.installer.family", return_value="fedora"), patch("mount_medic.installer.missing_packages", side_effect=[["ntfs-3g-devel"], []]), patch("mount_medic.installer.subprocess.run") as run, patch("mount_medic.installer.payload", return_value={}), patch("builtins.print"):
            prepare_install(Path("/disposable/source"), 1000)
        self.assertEqual(run.call_args_list[0].args[0], ["dnf", "install", "-y", "ntfs-3g-devel"])
        self.assertEqual(run.call_args_list[0].kwargs["env"]["HOME"], "/root")
        build = run.call_args_list[1]
        self.assertEqual((build.kwargs["user"], build.kwargs["group"], build.kwargs["extra_groups"]), (1000, 1000, [1000, 10]))

    def test_package_failure_stops_before_build(self):
        user = Mock(pw_gid=1000, pw_name="desktop", pw_dir="/home/desktop")
        with patch("mount_medic.installer.pwd.getpwuid", return_value=user), patch("mount_medic.installer.os.getgrouplist", return_value=[1000]), patch("mount_medic.installer.family", return_value="fedora"), patch("mount_medic.installer.missing_packages", return_value=["ntfs-3g-devel"]), patch("mount_medic.installer.subprocess.run", side_effect=subprocess.CalledProcessError(1, "dnf")) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                prepare_install(Path("/disposable/source"), 1000)
        self.assertEqual(run.call_count, 1)

    def test_setup_never_builds_as_root(self):
        with patch("mount_medic.installer.subprocess.run", side_effect=AssertionError("build ran")):
            with self.assertRaisesRegex(MedicError, "normal user"):
                prepare_install(Path("/disposable/source"), 0)

    def test_package_queries_distinguish_installed_missing_and_broken_database(self):
        for platform in ("debian", "fedora", "arch", "opensuse", "alpine"):
            with self.subTest(platform=platform), patch("mount_medic.dependencies.subprocess.run") as run:
                run.return_value = Mock(returncode=0, stdout="install ok installed", stderr="")
                self.assertEqual(missing_packages(platform), [])
                run.return_value.returncode = 1
                missing = missing_packages(platform)
                self.assertTrue(missing)
                self.assertTrue(set(missing).issubset(package_install_command(platform, missing)))
                run.return_value.returncode = 2
                with self.assertRaisesRegex(MedicError, "Cannot query"):
                    missing_packages(platform)

    def test_debian_residual_configuration_is_not_an_installed_package(self):
        with patch("mount_medic.dependencies.subprocess.run", return_value=Mock(returncode=0, stdout="deinstall ok config-files", stderr="")):
            self.assertTrue(missing_packages("debian"))

    def test_native_probe_is_really_built_without_root(self):
        capabilities = next(line.split()[1] for line in Path("/proc/self/status").read_text().splitlines()
                            if line.startswith("CapEff:"))
        required = (1 << 0) | (1 << 6) | (1 << 7)  # chown, setgid, setuid
        if os.geteuid() != 0 or int(capabilities, 16) & required != required:
            self.skipTest("Requires a disposable root container with UID/GID switching capabilities")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            checkout = Path(__file__).resolve().parent.parent
            shutil.copytree(checkout / "native", source / "native")
            shutil.copy2(checkout / "Makefile", source / "Makefile")
            os.chown(source, 65534, 65534)
            with patch("mount_medic.installer.missing_packages", return_value=[]), patch("mount_medic.installer.payload", return_value={}), patch("builtins.print"):
                prepare_install(source, 65534)
            self.assertEqual((source / "build/mount-medic-probe").stat().st_uid, 65534)

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

    def test_restrictive_umask_keeps_new_application_directories_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {"usr/local/lib/mount-medic/mount_medic/__init__.py": (b"app", 0o644),
                     "usr/local/share/applications/io.github.aakashH242.MountMedic.desktop": (b"desktop", 0o644)}
            previous = os.umask(0o077)
            try:
                with patch("mount_medic.installer.payload", return_value=files):
                    install_tree(root, root)
            finally:
                os.umask(previous)
            directories = [root / "usr", root / "usr/local", root / "usr/local/lib", root / "usr/local/share",
                           root / "usr/local/lib/mount-medic", root / "usr/local/lib/mount-medic/mount_medic",
                           root / "usr/local/share/applications"]
            self.assertEqual([path.stat().st_mode & 0o777 for path in directories], [0o755] * len(directories))
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)

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

    def test_upgrade_removes_obsolete_modules_and_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = {"usr/local/lib/mount-medic/worker": (b"old", 0o755),
                        "usr/local/lib/mount-medic/mount_medic/obsolete.py": (b"old module", 0o644),
                        "usr/local/lib/mount-medic/mount_medic/assets/old.png": (b"old icon", 0o644)}
            with patch("mount_medic.installer.payload", return_value=original):
                install_tree(root, root)
            replacement = {"usr/local/lib/mount-medic/worker": (b"new", 0o755)}
            with patch("mount_medic.installer.payload", return_value=replacement):
                install_tree(root, root)
            manifest = read_json(root / "usr/local/lib/mount-medic/manifest.json")
            self.assertEqual(set(manifest), set(replacement))
            self.assertEqual((root / "usr/local/lib/mount-medic/worker").read_bytes(), b"new")
            for removed in original.keys() - replacement.keys():
                self.assertFalse((root / removed).exists())
