import io
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
import shutil
from unittest.mock import Mock, patch

from mount_medic import __version__, updates
from mount_medic.cli import execute, exit_status, main as cli_main, parser
from mount_medic.dependencies import missing_packages
from mount_medic.installer import LIBRARY, TRANSACTION, install_tree, prepare_install, recover_install
from mount_medic.model import MedicError
from mount_medic.releases import (ReleaseError, allowed_url, extract_archive, fetch_release,
                                 download, validate_release, verify_archive, version)
from mount_medic.storage import Preferences, read_json


from update_fixture import LATER_VERSION, TARGET_VERSION, metadata


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.environment = patch.dict(os.environ, {"XDG_CONFIG_HOME": self.directory.name, "XDG_STATE_HOME": self.directory.name})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.preferences = Preferences()
        packages = patch("mount_medic.dependencies.missing_packages", return_value=[])
        packages.start()
        self.addCleanup(packages.stop)

    def test_numeric_versions_and_strict_stable_format(self):
        self.assertGreater(version("1.10.0"), version("1.9.9"))
        for value in ("1.01.0", "v1.2.0", "1.2", "1.2.0-beta", None, "1.2.0/../", "9999999.0.0"):
            with self.subTest(value=value), self.assertRaises(ReleaseError):
                version(value)

    def test_metadata_rejects_unsupported_or_hostile_inputs(self):
        for change in ({"schema": 2}, {"schema": True}, {"archive": "../../file"}, {"size": True}, {"size": 20_000_000},
                       {"sha256": "no"}, {"sha256": int("1" * 64)}, {"python": [99, 0]}, {"python": [3, True]}, {"packages": {"arch": ["--overwrite"]}}):
            with self.subTest(change=change), self.assertRaises(ReleaseError):
                validate_release({**metadata(), **change})
        hostile = metadata()
        hostile["packages"]["debian"] = ["--allow-unauthenticated"]
        with self.assertRaises(ReleaseError):
            validate_release(hostile)

    def test_download_hosts_are_https_allowlisted(self):
        allowed_url("https://release-assets.githubusercontent.com/assets/example?signature=token")
        for url in ("http://github.com/file", "https://github.com.evil.test/file", "file:///etc/shadow",
                    "https://user:pass@github.com/file", "https://github.com:8443/file"):
            with self.subTest(url=url), self.assertRaises(ReleaseError):
                allowed_url(url)

    def test_pinned_metadata_and_corrupted_archives(self):
        with patch("mount_medic.releases.download", return_value=json.dumps(metadata()).encode()):
            self.assertEqual(fetch_release(TARGET_VERSION)["version"], TARGET_VERSION)
            with self.assertRaisesRegex(ReleaseError, "match"):
                fetch_release(LATER_VERSION)
        for data in (b"corrupt", b"archive-extra"):
            with self.assertRaisesRegex(ReleaseError, "checksum"):
                verify_archive(data, metadata())

    def test_network_download_is_bounded_and_obeys_the_deadline(self):
        response = Mock()
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=response)
        opener.open.return_value.__exit__ = Mock(return_value=False)
        response.read1.return_value = b"abc"
        with patch("mount_medic.releases.build_opener", return_value=opener):
            with self.assertRaisesRegex(ReleaseError, "size limit"):
                download("https://github.com/aakashH242/mount-medic/releases/latest/download/update.json", 2)
            with patch("mount_medic.releases.time.monotonic", side_effect=[0, 0, 11]):
                with self.assertRaisesRegex(ReleaseError, "timed out"):
                    download("https://github.com/aakashH242/mount-medic/releases/latest/download/update.json", 10)

    def test_ignore_and_alert_suppression_are_exact_versions(self):
        release = metadata()
        with patch("mount_medic.updates.fetch_release", return_value=release):
            self.assertEqual(updates.check(self.preferences)["available"], TARGET_VERSION)
        self.assertTrue(updates.notification_due(self.preferences, release))
        self.preferences.save_updates({"notified": TARGET_VERSION})
        self.assertFalse(updates.notification_due(self.preferences, release))
        updates.ignore(self.preferences, TARGET_VERSION)
        with patch("mount_medic.updates.fetch_release", return_value=release):
            self.assertTrue(updates.check(self.preferences)["ignored"])
        self.assertTrue(updates.notification_due(self.preferences, metadata(LATER_VERSION)))
        self.assertEqual(Preferences().updates()["ignored"], TARGET_VERSION)

    def test_offline_manual_check_reports_error_and_keeps_last_success(self):
        with patch("mount_medic.updates.fetch_release", return_value=metadata()):
            updates.check(self.preferences)
        previous = self.preferences.updates()["last_check"]
        with patch("mount_medic.updates.fetch_release", side_effect=ReleaseError("offline")):
            with self.assertRaisesRegex(MedicError, "offline"):
                updates.check(self.preferences)
        self.assertEqual(self.preferences.updates()["last_check"], previous)
        self.assertIn("offline", updates.status(self.preferences)["error"])
        self.assertFalse(updates.due(self.preferences))

    def test_installation_failure_remains_until_a_successful_upgrade(self):
        self.preferences.save_updates({"install_error": "Administrator approval was cancelled", "error": "offline"})
        with patch("mount_medic.updates.fetch_release", return_value=metadata()):
            result = updates.check(self.preferences)
        self.assertEqual(result["error"], "Administrator approval was cancelled")
        self.assertIsNone(self.preferences.updates()["error"])
        with patch("os.geteuid", return_value=1000), patch.object(Path, "is_file", return_value=True), patch("mount_medic.updates.download_archive", return_value=Path("/tmp/archive")), patch("mount_medic.updates.stop_desktop", return_value=0), patch("mount_medic.updates.worker_running", return_value=False), patch("mount_medic.installer.elevated", side_effect=lambda command: command), patch("mount_medic.updates.subprocess.run", return_value=Mock(returncode=0)):
            updates.install(metadata())
        self.assertIsNone(updates.status(self.preferences)["error"])

    def test_clock_changes_cannot_disable_checks(self):
        for last in (0, 999999999999, "invalid", float("nan")):
            self.preferences.save_updates({"last_attempt": last})
            self.assertTrue(updates.due(self.preferences))

    def test_malformed_cache_is_not_reported_current(self):
        for cached in ({}, [], 0, "invalid"):
            self.preferences.save_updates({"release": cached})
            self.assertEqual(updates.status(self.preferences)["state"], "unchecked")
        (self.preferences.config / "updates.json").write_text("{broken")
        self.assertEqual(updates.status(self.preferences)["state"], "unchecked")
        self.assertTrue(updates.status(self.preferences)["error"])

    def test_a_new_check_recovers_a_damaged_update_cache(self):
        self.preferences.config.mkdir(parents=True, exist_ok=True)
        for contents in ("{truncated", "[]", "null"):
            with self.subTest(contents=contents):
                (self.preferences.config / "updates.json").write_text(contents)
                self.assertTrue(updates.due(self.preferences))
                with patch("mount_medic.updates.fetch_release", return_value=metadata()):
                    result = updates.check(self.preferences)
                self.assertEqual(result["available"], TARGET_VERSION)
                self.assertIsNone(result["error"])
                self.assertFalse(updates.due(self.preferences))

    def test_cli_updates_never_discover_or_authenticate_for_checks_and_dry_run(self):
        with patch("mount_medic.updates.fetch_release", return_value=metadata()), patch("mount_medic.client.discover", side_effect=AssertionError("drive discovery")), patch("mount_medic.updates.install", side_effect=AssertionError("installation")):
            result = execute(parser().parse_args(["update", "install", "--dry-run", "--json"]))
            self.assertEqual(exit_status(result), 1)
            self.assertTrue(result["dry_run"])
            self.assertEqual(self.preferences.updates(), {}, "dry-run changed update preferences")
            result = execute(parser().parse_args(["update", "ignore", TARGET_VERSION, "--json"]))
            self.assertEqual(exit_status(result), 0)
            self.assertEqual(result["ignored_version"], TARGET_VERSION)

    def test_dependency_changes_require_review_and_root_rechecks_approval(self):
        with patch("mount_medic.dependencies.family", return_value="arch"), patch("mount_medic.dependencies.missing_packages", return_value=["polkit"]), patch("mount_medic.installer.ask", return_value=False) as consent:
            with self.assertRaisesRegex(MedicError, "packages were not approved"):
                updates.confirm_dependencies(metadata(), None)
            self.assertIn("full Arch system upgrade", consent.call_args.args[0])
        with patch("mount_medic.installer.pwd.getpwuid", return_value=Mock(pw_dir="/nonexistent")), patch("mount_medic.installer.family", return_value="arch"), patch("mount_medic.installer.missing_packages", return_value=["polkit"]), patch("mount_medic.installer.subprocess.run", side_effect=AssertionError("unapproved package installation")):
            with self.assertRaisesRegex(MedicError, "dependencies changed"):
                prepare_install(Path("/disposable/source"), 65534, {**metadata(), "approved_packages": []})

    def test_headless_update_does_not_create_a_session_bus(self):
        with patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": ""}):
            self.assertEqual(updates.stop_desktop(), 0)

    def test_package_query_timeout_reports_cli_json_and_gui_failure(self):
        errors = io.StringIO()
        notifications = Mock()
        with patch("mount_medic.updates.fetch_release", return_value=metadata()), patch("os.geteuid", return_value=1000), patch.object(Path, "is_file", return_value=True), patch("mount_medic.updates.shutil.which", return_value="/usr/bin/pkexec"), patch("mount_medic.dependencies.family", return_value="debian"), patch("mount_medic.dependencies.missing_packages", side_effect=missing_packages), patch("mount_medic.dependencies.subprocess.run", side_effect=subprocess.TimeoutExpired("dpkg-query", 30)), patch("sys.stderr", errors):
            with patch.object(sys, "argv", ["mount-medic", "update", "install", "--json"]):
                self.assertEqual(cli_main(), 2)
            self.assertIn("Package query timed out", json.loads(errors.getvalue())["error"])
            with patch.object(sys, "argv", ["update", TARGET_VERSION, metadata()["sha256"], "--restart", "gui"]), patch.dict(sys.modules, {"mount_medic.notifications": notifications}):
                self.assertEqual(updates.main(), 2)
            self.assertIn("Package query timed out", self.preferences.updates()["install_error"])
            notifications.desktop_message.assert_called_once_with("Mount Medic update failed", self.preferences.updates()["install_error"], "error")

    def test_declined_authentication_restarts_previous_app(self):
        with patch("os.geteuid", return_value=1000), patch.object(Path, "is_file", return_value=True), patch("mount_medic.updates.download_archive", return_value=Path("/tmp/archive")), patch("mount_medic.updates.stop_desktop", return_value=10), patch("mount_medic.updates.worker_running", return_value=False), patch("mount_medic.installer.elevated", side_effect=lambda command: command), patch("mount_medic.updates.subprocess.run", return_value=Mock(returncode=126)) as run, patch("mount_medic.updates.subprocess.Popen") as restart:
            with self.assertRaisesRegex(MedicError, "cancelled"):
                updates.install(metadata())
            restart.assert_called_once_with(["/usr/local/bin/mount-medic", "gui"], start_new_session=True)
            self.assertIs(run.call_args.kwargs["stdout"], sys.stderr, "installer progress would corrupt CLI --json output")

    def test_active_desktop_operation_prevents_installation(self):
        with patch("os.geteuid", return_value=1000), patch.object(Path, "is_file", return_value=True), patch("mount_medic.updates.download_archive"), patch("mount_medic.updates.stop_desktop", return_value=2), patch("mount_medic.updates.subprocess.run", side_effect=AssertionError("installer ran")):
            with self.assertRaisesRegex(MedicError, "active operation"):
                updates.install(metadata())

    def test_legacy_installation_requires_one_manual_upgrade(self):
        with patch("os.geteuid", return_value=1000), patch.object(Path, "is_file", side_effect=[True, False]), patch("mount_medic.updates.download_archive", side_effect=AssertionError("downloaded without a trusted updater helper")):
            with self.assertRaisesRegex(MedicError, "manually"):
                updates.install(metadata())

    def test_archive_rejects_traversal_links_and_duplicate_paths(self):
        for name, kind in (("../escape", tarfile.REGTYPE), ("/escape", tarfile.REGTYPE),
                           (f"mount-medic-{TARGET_VERSION}/link", tarfile.SYMTYPE), (f"mount-medic-{TARGET_VERSION}/link", tarfile.LNKTYPE)):
            with self.subTest(name=name, kind=kind), tempfile.TemporaryDirectory() as directory:
                archive = Path(directory) / "source.tar.gz"
                with tarfile.open(archive, "w:gz") as bundle:
                    entry = tarfile.TarInfo(name)
                    entry.type = kind
                    entry.linkname = "/etc/shadow"
                    bundle.addfile(entry, io.BytesIO())
                with self.assertRaises(ReleaseError):
                    extract_archive(archive, Path(directory) / "output", metadata())
                self.assertFalse((Path(directory) / "escape").exists())

    def test_archive_bounds_decompression_before_parsing_headers(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "source.tar.gz"
            with gzip.open(archive, "wb") as compressed:
                compressed.write(b"\0" * 4096)
            with patch("mount_medic.releases.MAX_SOURCE", 1024), self.assertRaisesRegex(ReleaseError, "expands"):
                extract_archive(archive, Path(directory) / "output", metadata())
            self.assertFalse((Path(directory) / "output").exists())

    def test_release_assets_round_trip_the_shared_bootstrap_and_source(self):
        from scripts.release import package
        checkout = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "checkout"
            source.mkdir()
            for name in ("mount_medic", "native", "integration"):
                shutil.copytree(checkout / name, source / name, ignore=shutil.ignore_patterns("__pycache__"))
            for name in ("Makefile", "install.py", "LICENSE", "pyproject.toml"):
                shutil.copy2(checkout / name, source / name)
            tracked = "\0".join(str(path.relative_to(source)) for path in source.rglob("*") if path.is_file()).encode()
            output = Path(directory) / "assets"
            with patch("scripts.release.subprocess.check_output", return_value=tracked):
                release = package(source, output)
            self.assertEqual(release["version"], __version__)
            verify_archive((output / release["archive"]).read_bytes(), release)
            extracted = extract_archive(output / release["archive"], Path(directory) / "extracted", release)
            self.assertEqual((extracted / "mount_medic/updates.py").read_bytes(), (source / "mount_medic/updates.py").read_bytes())
            self.assertEqual((output / "bootstrap.py").read_bytes(), (source / "mount_medic/releases.py").read_bytes())


class RecoveryTests(unittest.TestCase):
    def test_process_death_at_each_switch_recovers_a_complete_generation(self):
        old = {str(LIBRARY / "worker"): (b"old", 0o755),
               str(LIBRARY / "mount_medic/obsolete.py"): (b"removed", 0o644),
               "usr/local/bin/mount-medic": (b"old launcher", 0o755)}
        new = {str(LIBRARY / "worker"): (b"new", 0o755), "usr/local/bin/mount-medic": (b"new launcher", 0o755),
               "usr/local/share/applications/io.github.aakashH242.MountMedic.desktop": (b"desktop", 0o644)}
        for point in ("journal", "external", "exchange", "commit"):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with patch("mount_medic.installer.payload", return_value=old):
                    install_tree(root, root)
                old_manifest = read_json(root / LIBRARY / "manifest.json")
                history = root / "var/lib/mount-medic/1000.json"
                history.parent.mkdir(parents=True)
                history.write_text('{"permissions":"preserved"}')
                script = """
import os, sys
from pathlib import Path
from unittest.mock import patch
from mount_medic import installer
root = Path(sys.argv[1]); point = sys.argv[2]
new = {str(installer.LIBRARY / 'worker'): (b'new', 0o755), 'usr/local/bin/mount-medic': (b'new launcher', 0o755), 'usr/local/share/applications/io.github.aakashH242.MountMedic.desktop': (b'desktop', 0o644)}
write = installer.write_file; exchange = installer.exchange_directories; atomic = installer.atomic_json
def written(path, content):
    write(path, content)
    if point == 'external' and path == root / 'usr/local/bin/mount-medic': os._exit(99)
def exchanged(first, second):
    exchange(first, second)
    if point == 'exchange': os._exit(99)
def committed(path, data):
    atomic(path, data)
    if point == 'journal' and path.name == 'status.json' and not data.get('committed'): os._exit(99)
    if point == 'commit' and data.get('committed'): os._exit(99)
with patch.object(installer, 'payload', return_value=new), patch.object(installer, 'write_file', written), patch.object(installer, 'exchange_directories', exchanged), patch.object(installer, 'atomic_json', committed):
    installer.install_tree(root, root)
"""
                result = subprocess.run([sys.executable, "-B", "-c", script, str(root), point], capture_output=True, text=True)
                self.assertEqual(result.returncode, 99, result.stderr)
                self.assertTrue((root / TRANSACTION / "status.json").exists())
                recover_install(root)
                chosen = new if point == "commit" else old
                for name, content in chosen.items():
                    self.assertEqual((root / name).read_bytes(), content[0])
                if point != "commit":
                    self.assertEqual(read_json(root / LIBRARY / "manifest.json"), old_manifest)
                self.assertEqual((root / LIBRARY / "mount_medic/obsolete.py").exists(), point != "commit")
                self.assertEqual((root / "usr/local/share/applications/io.github.aakashH242.MountMedic.desktop").exists(), point == "commit")
                self.assertEqual(history.read_text(), '{"permissions":"preserved"}')
                self.assertFalse((root / TRANSACTION).exists())

    def test_switch_failure_rolls_back_external_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = {str(LIBRARY / "worker"): (b"old", 0o755), "usr/local/bin/mount-medic": (b"old", 0o755)}
            with patch("mount_medic.installer.payload", return_value=original):
                install_tree(root, root)
            changed = {name: (b"new", mode) for name, (unused, mode) in original.items()}
            with patch("mount_medic.installer.payload", return_value=changed), patch("mount_medic.installer.exchange_directories", side_effect=OSError("unsupported")):
                with self.assertRaises(OSError):
                    install_tree(root, root)
            self.assertEqual((root / LIBRARY / "worker").read_bytes(), b"old")
            self.assertEqual((root / "usr/local/bin/mount-medic").read_bytes(), b"old")


if __name__ == "__main__":
    unittest.main()
