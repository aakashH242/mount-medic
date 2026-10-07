"""Generate the three release assets from a clean, tested tagged checkout."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import re
import tomllib

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mount_medic.dependencies import PACKAGES, required_packages
from mount_medic.releases import source_version, validate_release


def package(source: Path, output: Path) -> dict:
    target = source_version(source)
    requirement = tomllib.loads((source / "pyproject.toml").read_text())["project"]["requires-python"]
    minimum = re.fullmatch(r">=([0-9]+)\.([0-9]+)", requirement)
    if not minimum:
        raise ValueError("Release metadata needs an explicit minimum Python version")
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"mount-medic-{target}.tar.gz"
    # git's tracked list excludes caches, local screenshots and unrelated checkout files.
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=source).decode().split("\0")
    paths = [name for name in tracked if name.startswith(("mount_medic/", "native/", "integration/"))
             or name in {"Makefile", "install.py", "LICENSE", "pyproject.toml"}]
    with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as bundle:
        for name in sorted(paths):
            path = source / name
            if path.is_symlink() or not path.is_file():
                raise ValueError("Release sources must be regular tracked files")
            info = bundle.gettarinfo(str(path), arcname=f"mount-medic-{target}/{name}")
            info.uid = info.gid = info.mtime = 0
            info.uname = info.gname = ""
            info.mode = 0o644
            with path.open("rb") as stream:
                bundle.addfile(info, stream)
    data = archive.read_bytes()
    release = validate_release({"schema": 1, "version": target, "archive": archive.name,
                                "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "python": [int(number) for number in minimum.groups()],
                                "packages": {name: required_packages(name) for name in PACKAGES}})
    (output / "update.json").write_text(json.dumps(release, indent=2) + "\n")
    (output / "bootstrap.py").write_bytes((source / "mount_medic/releases.py").read_bytes())
    return release


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output", type=Path, default=Path("dist/release"))
    args = parser.parse_args()
    source = Path(__file__).resolve().parent.parent
    if args.tag != "v" + source_version(source):
        parser.error("Tag must match mount_medic/__init__.py")
    tag = subprocess.check_output(["git", "rev-parse", args.tag + "^{commit}"], cwd=source).strip()
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source).strip()
    if tag != head or subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=source).strip():
        parser.error("Build releases only from a clean checkout of the requested tag")
    print(json.dumps(package(source, args.output)))


if __name__ == "__main__":
    main()
