import hashlib

from mount_medic import __version__
from mount_medic.dependencies import PACKAGES, required_packages

major, minor, patch = map(int, __version__.split("."))
TARGET_VERSION = f"{major}.{minor + 1}.0"
LATER_VERSION = f"{major}.{minor + 2}.0"


def metadata(target=TARGET_VERSION, data=b"archive"):
    return {"schema": 1, "version": target, "archive": f"mount-medic-{target}.tar.gz", "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "python": [3, 11],
            "packages": {name: required_packages(name) for name in PACKAGES}}
