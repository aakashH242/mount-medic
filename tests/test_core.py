from contextlib import contextmanager
from pathlib import Path
import json
import os
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree

from mount_medic import cli, client
from mount_medic.discovery import VolumeUnavailable, discover, hardware_identity, parse_devices, udev_filesystem_metadata
from mount_medic.engine import Engine
from mount_medic.model import Identity, MedicError, Volume, diagnosis
from mount_medic.protocol import authorization, validate
from mount_medic.safety import assert_safe_mount_options, descriptor_device, mounted_in_text, require_supported, udisks_defaults
from mount_medic.storage import Preferences, Registry


def volume():
    return Volume(Identity("ABCD", "hardware-123", "partition-1", 1000000, 2048),
                  "/dev/test-not-real", label="Work", devnum="8:1")


@contextmanager
def fake_claim(selected):
    yield 123


class DiscoveryTests(unittest.TestCase):
    def test_kernel_serial_used_without_udev_serial(self):
        with patch.object(Path, "read_text", return_value="kernel-serial\n"):
            self.assertEqual(hardware_identity({"maj:min": "253:16"}), "kernel-serial")

    def test_mixed_mounts_are_not_labelled_readonly(self):
        payload = {"blockdevices": self.tree()}
        mounts = {"filesystems": [{"source": "/dev/a1", "target": "/rw", "options": "rw"},
                                  {"source": "/dev/a1", "target": "/ro", "options": "ro"}]}
        with patch("mount_medic.discovery.json_command", side_effect=[payload, mounts]), patch("mount_medic.discovery.executable", side_effect=lambda name: name), patch("os.geteuid", return_value=0):
            self.assertFalse(discover()[0].mount_readonly)

    def test_unprivileged_udev_metadata_matches_root_partition_identity(self):
        tree = self.tree()[0]
        root = parse_devices([tree])[0]
        tree["children"][0].update(fstype=None, uuid=None, partuuid=None)
        disk = subprocess.CompletedProcess([], 0, "", "")
        partition = subprocess.CompletedProcess([], 0, "ID_FS_TYPE=ntfs\nID_FS_UUID=123\nID_PART_ENTRY_UUID=456\n", "")
        with patch("mount_medic.discovery.run", side_effect=[disk, partition]), patch("mount_medic.discovery.executable", side_effect=lambda name: name):
            self.assertEqual(parse_devices([udev_filesystem_metadata(tree)])[0].key, root.key)

    def tree(self):
        return [{"path": "/dev/a", "type": "disk", "serial": "serial", "fstype": None,
                 "children": [{"path": "/dev/a1", "type": "part", "fstype": "ntfs", "uuid": "123",
                               "partuuid": "456", "start": 2048, "size": 100, "mountpoints": [None], "ro": False}]}]

    def test_device_rename_preserves_identity(self):
        tree = self.tree()
        original = parse_devices(tree)[0]
        tree[0]["children"][0]["path"] = "/dev/b1"
        self.assertEqual(original.key, parse_devices(tree)[0].key)

    def test_new_hardware_changes_identity(self):
        tree = self.tree()
        original = parse_devices(tree)[0]
        tree[0]["serial"] = "another"
        self.assertNotEqual(original.key, parse_devices(tree)[0].key)

    def test_duplicate_uuid_is_ambiguous(self):
        tree = self.tree()
        clone = json.loads(json.dumps(tree))[0]
        clone["children"][0]["path"] = "/dev/b1"
        self.assertTrue(all(item.duplicate for item in parse_devices(tree + [clone])))

    def test_mapper_is_not_plain_partition(self):
        tree = self.tree()
        tree[0]["children"][0]["type"] = "crypt"
        self.assertFalse(parse_devices(tree)[0].supported)

    def test_unusual_label_not_identity(self):
        tree = self.tree()
        original = parse_devices(tree)[0].key
        tree[0]["children"][0]["label"] = "$(touch /never)\n<big>"
        self.assertEqual(original, parse_devices(tree)[0].key)


class ProtocolTests(unittest.TestCase):
    def test_repair_result_shows_verified_mount(self):
        checked = {**diagnosis(volume(), "clean"), "repair": {"status": "success"}, "mount_requested": True}
        mounted = {"state": "mounted", "observed_state": "mounted_rw", "volume": volume().as_dict()}
        with patch("mount_medic.client.call", return_value=checked), patch("mount_medic.client.udisks_operation", return_value=mounted):
            self.assertEqual(client.repair({"op": "automatic", "id": volume().key})["state"], "mounted_rw")

    def test_mount_failure_has_failed_exit_status(self):
        self.assertEqual(cli.exit_status({"state": "clean", "mount": {"state": "failed"}}), 2)

    def test_configure_flags_require_selected_volume(self):
        args = cli.parser().parse_args(["configure", "--auto-repair", "on"])
        with self.assertRaises(MedicError):
            cli.configuration(args)

    def test_background_cannot_request_authentication(self):
        requests = [{"op": "automatic", "id": volume().key}, {"op": "check"},
                    {"op": "list"}, {"op": "monitor", "id": volume().key, "enabled": True},
                    {"op": "prepare_mount", "id": volume().key, "background": True}]
        self.assertEqual([authorization(validate(item))[1] for item in requests], [0] * len(requests))

    def test_admin_operation_requests_authentication(self):
        self.assertEqual(authorization({"op": "repair"})[1], 1)

    def test_polkit_still_requires_authentication_for_new_admin_approvals(self):
        policy = ElementTree.parse(Path(__file__).resolve().parents[1] / "integration/io.github.aakashH242.mount-medic.policy")
        defaults = {action.attrib["id"].rsplit(".", 1)[1]:
                    {entry.tag: entry.text for entry in action.find("defaults")}
                    for action in policy.getroot().findall("action")}
        self.assertEqual(defaults, {
            "inspect": {"allow_any": "no", "allow_inactive": "no", "allow_active": "yes"},
            "admin": {"allow_any": "auth_admin", "allow_inactive": "auth_admin", "allow_active": "auth_admin"},
        })

    def test_injected_argument_rejected(self):
        with self.assertRaises(MedicError):
            validate({"op": "automatic", "id": volume().key, "command": "sh"})

    def test_malformed_operation_has_controlled_error(self):
        with self.assertRaises(MedicError):
            validate({"op": []})

    def test_device_path_rejected(self):
        with self.assertRaises(MedicError):
            validate({"op": "automatic", "id": "/dev/sda"})

    def test_integer_is_not_boolean(self):
        with self.assertRaises(MedicError):
            validate({"op": "monitor", "id": volume().key, "enabled": 1})

    def test_dry_run_never_contacts_worker(self):
        args = cli.parser().parse_args(["repair", volume().key, "--dry-run"])
        with patch("mount_medic.client.call", side_effect=AssertionError("worker contacted")), patch("mount_medic.client.discover", return_value=[volume()]):
            self.assertTrue(cli.execute(args)["dry_run"])

    def test_noninteractive_manual_repair_is_cancelled(self):
        with patch("sys.stdin.isatty", return_value=False), patch("builtins.print"):
            with self.assertRaises(MedicError):
                cli.confirm("repair")


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.engine = Engine(Path(self.temporary.name), Path("/nonexistent/probe"))
        self.volume = volume()
        self.uid = 1000
        self.request = {"op": "automatic", "id": self.volume.key}
        self.resolve = patch("mount_medic.engine.resolve", return_value=self.volume)
        self.resolve.start()
        self.addCleanup(self.resolve.stop)

    def authorize(self):
        self.engine.configure(self.uid, {"op": "configure", "id": self.volume.key,
                                        "monitor": True, "auto_repair": True, "auto_mount": False})

    def report(self, state):
        return {**diagnosis(self.volume, state), "complete": state in {"dirty", "clean", "unclean_journal"}}

    def test_new_drive_not_checked(self):
        with patch.object(self.engine, "inspect", side_effect=AssertionError("unmanaged probe")):
            self.assertEqual(self.engine.check(self.uid), [])

    def test_dependency_failure_is_not_reported_absent(self):
        with patch.object(self.engine, "inspect", side_effect=MedicError("Missing dependency: lsblk")):
            with self.assertRaisesRegex(MedicError, "Missing dependency"):
                self.engine.check(self.uid, self.volume.key)

    def test_monitoring_does_not_authorize_repair(self):
        self.engine.configure(self.uid, {"op": "monitor", "id": self.volume.key, "enabled": True})
        with self.assertRaises(MedicError):
            self.engine.repair(self.uid, self.request)

    def test_other_user_cannot_use_approval(self):
        self.authorize()
        with self.assertRaises(MedicError):
            self.engine.repair(1001, self.request)

    def test_disconnected_drive_permissions_can_be_revoked(self):
        self.authorize()
        with patch("mount_medic.engine.resolve", side_effect=VolumeUnavailable("absent")):
            saved = self.engine.configure(self.uid, {"op": "configure", "id": self.volume.key,
                                                    "monitor": False, "auto_repair": False, "auto_mount": False})
        self.assertFalse(saved["auto_repair"])

    def test_disconnected_drive_cannot_gain_permissions(self):
        self.authorize()
        with patch("mount_medic.engine.resolve", side_effect=VolumeUnavailable("absent")):
            with self.assertRaises(MedicError):
                self.engine.configure(self.uid, {"op": "configure", "id": self.volume.key,
                                                "monitor": True, "auto_repair": True, "auto_mount": True})

    def test_mounted_volume_does_not_probe(self):
        self.volume.mounts = ["/somewhere"]
        with patch.object(self.engine, "probe_volume", side_effect=AssertionError("offline check")):
            self.assertEqual(self.engine.inspect(self.volume.key)["state"], "mounted_rw")

    def test_check_never_runs_repair(self):
        self.authorize()
        with patch.object(self.engine, "inspect", return_value=self.report("dirty")), patch.object(self.engine, "execute_repair", side_effect=AssertionError("write")):
            self.assertEqual(self.engine.check(self.uid)[0]["state"], "dirty")

    def test_hibernation_stops_repair(self):
        self.authorize()
        with patch("mount_medic.engine.claim", fake_claim), patch.object(self.engine, "probe_volume", return_value=self.report("hibernated")), patch.object(self.engine, "execute_repair", side_effect=AssertionError("write")):
            self.assertEqual(self.engine.repair(self.uid, self.request)["state"], "hibernated")

    def test_incomplete_dirty_result_stops_repair(self):
        self.authorize()
        report = {**self.report("dirty"), "complete": False}
        with patch("mount_medic.engine.claim", fake_claim), patch.object(self.engine, "probe_volume", return_value=report), patch.object(self.engine, "execute_repair", side_effect=AssertionError("write")):
            self.assertNotIn("repair", self.engine.repair(self.uid, self.request))

    def test_failed_attempt_latches_across_processes(self):
        self.authorize()
        self.engine.record_attempt(self.uid, {"id": self.volume.key, "status": "running"})
        restarted = Engine(Path(self.temporary.name))
        with self.assertRaises(MedicError):
            restarted.repair(self.uid, self.request)

    def test_success_requires_independent_verification(self):
        self.authorize()
        with patch("mount_medic.engine.claim", fake_claim), patch.object(self.engine, "probe_volume", return_value=self.report("dirty")), patch.object(self.engine, "execute_repair", return_value=(0, "success")), patch("mount_medic.engine.executable", return_value="/usr/bin/ntfsfix"):
            self.assertEqual(self.engine.repair(self.uid, self.request)["repair"]["status"], "failed")

    def test_successful_repair_is_recorded(self):
        self.authorize()
        with patch("mount_medic.engine.claim", fake_claim), patch.object(self.engine, "probe_volume", side_effect=[self.report("dirty"), self.report("clean")]), patch.object(self.engine, "execute_repair", return_value=(0, "done")), patch("mount_medic.engine.executable", return_value="/usr/bin/ntfsfix"):
            self.assertEqual(self.engine.repair(self.uid, self.request)["repair"]["status"], "success")

    def test_identity_change_at_claim_exit_latches_interruption(self):
        @contextmanager
        def replaced(selected):
            yield 123
            raise MedicError("Device replaced")
        self.authorize()
        with patch("mount_medic.engine.claim", replaced), patch.object(self.engine, "probe_volume", side_effect=[self.report("dirty"), self.report("clean")]), patch.object(self.engine, "execute_repair", return_value=(0, "done")), patch("mount_medic.engine.executable", return_value="/usr/bin/ntfsfix"):
            with self.assertRaises(MedicError):
                self.engine.repair(self.uid, self.request)
        self.assertEqual(self.engine.registry.entry(self.uid, self.volume.key)["attempt"]["status"], "interrupted")

    def test_no_attempt_record_before_writing(self):
        self.authorize()
        with patch("mount_medic.engine.claim", fake_claim), patch.object(self.engine, "probe_volume", side_effect=MedicError("read failed")):
            with self.assertRaises(MedicError):
                self.engine.repair(self.uid, self.request)
        self.assertNotIn("attempt", self.engine.registry.entry(self.uid, self.volume.key))

    def test_attempt_durable_before_mutation(self):
        self.authorize()
        def verify_record(command, descriptor, context):
            self.assertEqual(self.engine.registry.entry(self.uid, self.volume.key)["attempt"]["status"], "running")
            return 0, "done"
        with patch("mount_medic.engine.claim", fake_claim), patch.object(self.engine, "probe_volume", side_effect=[self.report("dirty"), self.report("clean")]), patch.object(self.engine, "execute_repair", side_effect=verify_record), patch("mount_medic.engine.executable", return_value="/usr/bin/ntfsfix"):
            self.engine.repair(self.uid, self.request)

    def test_stopping_states_never_write(self):
        self.authorize()
        for state in ("hibernated", "cached_metadata", "io_failure", "io_or_corruption", "unknown", "unsupported_flags", "mirror_mismatch"):
            with self.subTest(state=state), patch("mount_medic.engine.claim", fake_claim), patch.object(self.engine, "probe_volume", return_value=self.report(state)), patch.object(self.engine, "execute_repair", side_effect=AssertionError("write")):
                self.assertEqual(self.engine.repair(self.uid, self.request)["state"], state)

    def test_missing_probe_is_actionable(self):
        self.assertEqual(self.engine.inspect(self.volume.key)["state"], "missing_dependency")

    def test_readonly_mount_has_distinct_status(self):
        self.volume.mounts = ["/somewhere"]
        self.volume.mount_readonly = True
        self.assertEqual(self.engine.inspect(self.volume.key)["state"], "mounted_ro")

    def test_probe_bad_json_stays_unknown(self):
        output = subprocess.CompletedProcess([], 0, "not json", "")
        with patch("mount_medic.engine.run", return_value=output):
            self.assertEqual(self.engine.probe_volume(self.volume, 123)["state"], "unknown")

    def test_readonly_device_diagnosis_does_not_offer_repair(self):
        self.volume.readonly = True
        output = subprocess.CompletedProcess([], 0, '{"schema":1,"state":"dirty","complete":true}', "")
        with patch("mount_medic.engine.run", return_value=output):
            self.assertEqual(self.engine.probe_volume(self.volume, 123)["actions"], [])

    def test_mirror_mismatch_is_not_generic_io_repair(self):
        output = subprocess.CompletedProcess([], 0, '{"schema":1,"state":"io_or_corruption","complete":false}', "$MFTMirr does not match $MFT")
        with patch("mount_medic.engine.run", return_value=output):
            self.assertEqual(self.engine.probe_volume(self.volume, 123)["actions"], [])


class SafetyTests(unittest.TestCase):
    def test_block_descriptor_still_reports_its_device_number(self):
        with patch("os.readlink", return_value="/dev/example1"), patch.object(Path, "stat") as info:
            info.return_value.st_mode = stat.S_IFBLK
            info.return_value.st_rdev = os.makedev(8, 1)
            self.assertEqual(descriptor_device(Path("/proc/1/fd/71")), "8:1")

    def test_kernel_descriptors_do_not_require_getattr(self):
        for target in ("socket:[925]", "pipe:[926]", "anon_inode:[io_uring]", "anon_inode:[eventfd]", "anon_inode:inotify"):
            with self.subTest(target=target), patch("os.readlink", return_value=target), patch.object(Path, "stat", side_effect=PermissionError("SELinux kernel descriptor getattr")):
                self.assertEqual(descriptor_device(Path("/proc/1/fd/71")), "")

    def test_inaccessible_regular_target_still_blocks(self):
        for target in ("/tmp/socket:[925]", "/tmp/anon_inode:[io_uring]", "/dev/example1"):
            with self.subTest(target=target), patch("os.readlink", return_value=target), patch.object(Path, "stat", side_effect=PermissionError("unreadable target")):
                with self.assertRaises(PermissionError):
                    descriptor_device(Path("/proc/1/fd/71"))

    def test_udev_mount_override_is_rejected(self):
        fstab = subprocess.CompletedProcess([], 1, "", "")
        properties = subprocess.CompletedProcess([], 0, "UDISKS_MOUNT_OPTIONS_NTFS_DEFAULTS=remove_hiberfile\n", "")
        with patch("mount_medic.safety.run", side_effect=[fstab, properties]), patch("mount_medic.safety.executable", side_effect=lambda name: name), patch.object(Path, "exists", return_value=False):
            with self.assertRaisesRegex(MedicError, "prohibited"):
                assert_safe_mount_options(volume())

    def test_allowlist_and_unrelated_device_are_not_mount_defaults(self):
        config = "[defaults]\nntfs_allow=force\n[/dev/unrelated]\nntfs_defaults=force\n"
        fstab = subprocess.CompletedProcess([], 1, "", "")
        properties = subprocess.CompletedProcess([], 0, "", "")
        with patch("mount_medic.safety.run", side_effect=[fstab, properties]), patch("mount_medic.safety.executable", side_effect=lambda name: name), patch.object(Path, "exists", return_value=True), patch.object(Path, "read_text", return_value=config):
            self.assertIsNone(assert_safe_mount_options(volume()))

    def test_device_defaults_override_global_and_udev_override_device(self):
        config = "[defaults]\nntfs_defaults=force\n[/dev/test-not-real]\nntfs_defaults=ro\n"
        properties = subprocess.CompletedProcess([], 0, "UDISKS_MOUNT_OPTIONS_NTFS_DEFAULTS=rw\n", "")
        with patch("mount_medic.safety.run", return_value=properties), patch("mount_medic.safety.executable", side_effect=lambda name: name), patch.object(Path, "exists", return_value=True), patch.object(Path, "read_text", return_value=config):
            self.assertEqual(udisks_defaults(volume())["ntfs_defaults"], "rw")

    def test_another_namespace_mount_detected(self):
        self.assertTrue(mounted_in_text("24 1 8:1 / /mnt rw - ntfs3 /dev/alias rw", volume()))

    def test_fuse_mount_detected_by_source(self):
        self.assertTrue(mounted_in_text("24 1 0:41 / /mnt rw - fuseblk /dev/test-not-real rw", volume()))

    def test_malformed_mountinfo_blocks(self):
        with self.assertRaises(MedicError):
            mounted_in_text("unparseable", volume())

    def test_weak_identity_cannot_repair(self):
        item = Volume(Identity("123", "", "456", 100, 2048), "/dev/not-real")
        with self.assertRaises(MedicError):
            require_supported(item)

    def test_notification_dedup_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            preferences = Preferences()
            preferences.state = Path(directory)
            results = [preferences.changed("drive", state) for state in ("dirty", "dirty", "clean", "dirty")]
            self.assertEqual(results, [True, False, True, True])

    def test_broken_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "1000.json").write_text("invalid")
            with self.assertRaises(MedicError):
                Registry(Path(directory)).load(1000)


if __name__ == "__main__":
    unittest.main()
