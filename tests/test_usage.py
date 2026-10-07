import unittest

from mount_medic.discovery import parse_devices
from mount_medic.usage import GB, filesystem_usage, storage_summary


class StorageUsageTests(unittest.TestCase):
    def test_used_total_and_percentage_use_filesystem_bytes(self):
        usage = filesystem_usage(500 * GB, 123 * GB)
        self.assertEqual(storage_summary({"usage": usage}), "123.0 / 500.0 GB (75.4% free)")
        self.assertEqual(usage["free"], 377 * GB)

    def test_native_numeric_strings_empty_and_full_filesystems(self):
        for used, percent in ((0, 100), (500 * GB, 0)):
            with self.subTest(used=used):
                self.assertEqual(filesystem_usage(str(500 * GB), str(used))["free_percent"], percent)

    def test_invalid_or_inconsistent_figures_are_unavailable(self):
        for total, used in ((None, 1), (100, None), (0, 0), (100, -1), (100, 101),
                            (True, 0), (100, False), (100, 1.5), ("bad", 1), (1 << 65, 0)):
            with self.subTest(total=total, used=used):
                self.assertIsNone(filesystem_usage(total, used))

    def test_legacy_or_missing_usage_does_not_claim_zero_used(self):
        volume = {"identity": {"size": 500 * GB}}
        self.assertEqual(storage_summary(volume), "500.0 GB total · Usage unavailable")
        self.assertEqual(storage_summary({}), "Usage unavailable")

    def test_discovery_keeps_partition_identity_separate_from_space(self):
        tree = [{"path": "/dev/fixture", "type": "disk", "serial": "disk-a", "children": [
            {"path": "/dev/fixture1", "type": "part", "fstype": "ntfs", "uuid": "abcd", "size": 500 * GB,
             "mountpoints": ["/fixture"], "fssize": 480 * GB, "fsused": 120 * GB}]}]
        volume = parse_devices(tree)[0]
        self.assertEqual(storage_summary(volume.as_dict()), "120.0 / 480.0 GB (75.0% free)")
        tree[0]["children"][0]["fsused"] = 200 * GB
        self.assertEqual(parse_devices(tree)[0].key, volume.key)
        tree[0]["children"][0]["mountpoints"] = [None]
        self.assertIsNone(parse_devices(tree)[0].usage)


if __name__ == "__main__":
    unittest.main()
