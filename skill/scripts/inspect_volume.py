#!/usr/bin/env python3
"""Collect native Linux device/mount evidence without changing the target."""

import argparse
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess


SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
QUERY_TIMEOUT_SECONDS = 20
DEVICE_COLUMNS = "NAME,TYPE,MAJ:MIN,SIZE,FSTYPE,LABEL,UUID,PARTUUID,MODEL,SERIAL,RO,MOUNTPOINTS"


def query(command: list) -> dict:
    executable = shutil.which(command[0], path=SYSTEM_PATH)
    if executable is None:
        return {"command": command, "error": "Required system tool is unavailable"}
    try:
        result = subprocess.run(
            [executable, *command[1:]], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, errors="replace",
            timeout=QUERY_TIMEOUT_SECONDS, env={"PATH": SYSTEM_PATH, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"command": command, "error": str(error)}
    report = {"command": command, "exit_code": result.returncode, "stderr": result.stderr}
    try:
        report["data"] = json.loads(result.stdout) if result.stdout.strip() else None
    except ValueError:
        report.update(error="System tool did not return valid JSON", stdout=result.stdout)
    if result.returncode != 0:
        # findmnt uses 1 for no matches; this observes only our mount namespace.
        if command[0] == "findmnt" and result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip():
            report["note"] = "No matching mount in this process's mount namespace"
        else:
            report["error"] = "System query failed; inspect its exit code and stderr"
    elif report.get("data") is None:
        report["error"] = "System query returned no usable data"
    return report


def inspect(device: Path = None) -> dict:
    report = {
        "scope": "Device metadata and current mount namespace only; no filesystem integrity or repair eligibility verdict",
        "available_tools": {
            name: shutil.which(name, path=SYSTEM_PATH) is not None
            for name in ("lsblk", "findmnt", "ntfs-3g.probe", "ntfsfix", "fuser", "udisksctl")
        },
    }
    command = ["lsblk", "--json", "--bytes", "--paths", "--tree", "--output", DEVICE_COLUMNS]
    if device is not None:
        resolved = device.resolve(strict=True)
        info = resolved.stat()
        if not stat.S_ISBLK(info.st_mode):
            raise ValueError("Select a block device; regular files and other device types are not accepted")
        report.update(requested_device=str(device), resolved_device=str(resolved),
                      device_number=f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}")
        command.extend(["--inverse", "--", str(resolved)])
    report["devices"] = query(command)
    if device is not None:
        report["mounts"] = query([
            "findmnt", "--kernel", "--json", "--output", "SOURCE,TARGET,FSTYPE,OPTIONS,MAJ:MIN",
            "--source", str(resolved),
        ])
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("device", nargs="?", type=Path, help="Selected block-device path; omit to list devices")
    args = parser.parse_args()
    try:
        report = inspect(args.device)
    except (OSError, RuntimeError, ValueError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=True))
        return 2
    print(json.dumps(report, indent=2, ensure_ascii=True))
    return 2 if any("error" in report.get(section, {}) for section in ("devices", "mounts")) else 0


if __name__ == "__main__":
    raise SystemExit(main())
