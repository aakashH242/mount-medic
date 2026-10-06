import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SKILL = Path(__file__).resolve().parent.parent / "skill"
spec = importlib.util.spec_from_file_location("skill_inspection", SKILL / "scripts/inspect_volume.py")
inspection = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inspection)


class StandaloneSkillTests(unittest.TestCase):
    def test_copied_bundle_resolves_its_resources_and_runs_without_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "mount-medic"
            shutil.copytree(SKILL, bundle, ignore=shutil.ignore_patterns("__pycache__"))
            for document in bundle.rglob("*.md"):
                for target in re.findall(r"\]\(([^)]+)\)", document.read_text()):
                    relative = target.split("#", 1)[0]
                    if not relative or "://" in relative:
                        continue
                    resource = (document.parent / relative).resolve()
                    self.assertTrue(resource.is_relative_to(bundle) and resource.exists(), target)
            script = bundle / "scripts/inspect_volume.py"
            result = subprocess.run([sys.executable, "-I", str(script), "--help"], cwd=root,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([sys.executable, "-I", str(script), str(bundle / "SKILL.md")], cwd=root,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual((result.returncode, bool(json.loads(result.stdout).get("error"))), (2, True))

    def test_target_queries_keep_device_as_data_and_preserve_evidence(self):
        device = Path("/dev/fake; never-execute-this")
        devices = {"blockdevices": [{"name": str(device), "label": "$(untrusted)", "mountpoints": ["/mnt/data"]}]}
        mounts = {"filesystems": [{"source": str(device), "target": "/mnt/data", "options": "rw"}]}
        results = [subprocess.CompletedProcess([], 0, json.dumps(data), "") for data in (devices, mounts)]
        info = SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=os.makedev(8, 1))
        with patch.object(Path, "resolve", return_value=device), patch.object(Path, "stat", return_value=info), \
                patch.object(inspection.shutil, "which", side_effect=lambda name, **kwargs: "/usr/bin/" + name), \
                patch.object(inspection.subprocess, "run", side_effect=results) as run:
            report = inspection.inspect(device)
        self.assertEqual((report["devices"]["data"], report["mounts"]["data"]), (devices, mounts))
        self.assertEqual([call.args[0][0] for call in run.call_args_list], ["/usr/bin/lsblk", "/usr/bin/findmnt"])
        for call in run.call_args_list:
            self.assertEqual(call.args[0][-1], str(device))
            self.assertFalse(call.kwargs.get("shell", False))

    def test_missing_tool_does_not_spawn_process(self):
        with patch.object(inspection.shutil, "which", return_value=None), \
                patch.object(inspection.subprocess, "run") as run:
            report = inspection.query(["lsblk", "--json"])
        self.assertTrue("error" in report and not run.called)

    def test_failed_or_incomplete_queries_are_not_success(self):
        cases = [
            subprocess.CompletedProcess([], 0, "not json", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 1, "", "permission denied"),
            OSError("execution failed"),
            subprocess.TimeoutExpired("findmnt", 20),
        ]
        for result in cases:
            with self.subTest(result=result), patch.object(inspection.shutil, "which", return_value="/usr/bin/findmnt"), \
                    patch.object(inspection.subprocess, "run", side_effect=[result]):
                self.assertIn("error", inspection.query(["findmnt", "--json"]))

    def test_no_mount_match_is_preserved_without_claiming_repair_eligibility(self):
        result = subprocess.CompletedProcess([], 1, "", "")
        with patch.object(inspection.shutil, "which", return_value="/usr/bin/findmnt"), \
                patch.object(inspection.subprocess, "run", return_value=result):
            report = inspection.query(["findmnt", "--json"])
        self.assertEqual((report["exit_code"], report["data"], "error" in report), (1, None, False))

    def test_rejects_non_block_or_missing_target_before_query(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            regular = root / "file"
            regular.touch()
            for device in (root, regular, root / "absent"):
                with self.subTest(device=device), patch.object(inspection, "query") as query:
                    with self.assertRaises((OSError, ValueError)):
                        inspection.inspect(device)
                    query.assert_not_called()

    def test_partial_result_survives_failure_exit(self):
        report = {"devices": {"data": {"blockdevices": []}}, "mounts": {"error": "permission denied"}}
        output = io.StringIO()
        with patch.object(sys, "argv", ["inspect_volume.py"]), patch.object(sys, "stdout", output), \
                patch.object(inspection, "inspect", return_value=report):
            status = inspection.main()
        self.assertEqual((status, json.loads(output.getvalue())), (2, report))
