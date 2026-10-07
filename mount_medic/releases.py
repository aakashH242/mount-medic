"""Release format and bounded downloads, also shipped verbatim as bootstrap.py."""
import hashlib
import gzip
import io
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

REPOSITORY = "https://github.com/aakashH242/mount-medic"
MAX_ARCHIVE = 8 * 1024 * 1024
MAX_SOURCE = 32 * 1024 * 1024
NETWORK_TIMEOUT = 10


class ReleaseError(ValueError):
    pass


def version(value: str) -> tuple:
    if not isinstance(value, str) or not re.fullmatch(r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})", value):
        raise ReleaseError("Expected a stable version such as 1.1.0")
    return tuple(int(part) for part in value.split("."))


def validate_release(value: dict) -> dict:
    if not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != 1:
        raise ReleaseError("Unsupported release metadata; upgrade manually")
    version(value.get("version"))
    expected = f"mount-medic-{value['version']}.tar.gz"
    checksum = value.get("sha256")
    if value.get("archive") != expected or not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise ReleaseError("Invalid release archive or checksum")
    if type(value.get("size")) is not int or not 0 < value["size"] <= MAX_ARCHIVE:
        raise ReleaseError("Invalid release archive size")
    minimum = value.get("python")
    if not isinstance(minimum, list) or len(minimum) != 2 or any(type(number) is not int for number in minimum):
        raise ReleaseError("Invalid Python requirement")
    if tuple(minimum) < (3, 11) or tuple(minimum) > sys.version_info[:2]:
        raise ReleaseError("This release needs a newer Python; upgrade Python or install manually")
    packages = value.get("packages")
    if not isinstance(packages, dict) or set(packages) != {"debian", "fedora", "arch", "opensuse", "alpine"}:
        raise ReleaseError("Invalid release dependencies")
    for names in packages.values():
        if not isinstance(names, list) or not 1 <= len(names) <= 64 or any(
                not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9+_.-]{0,100}", name) for name in names):
            raise ReleaseError("Invalid release package name")
    return value


def asset_url(release: dict, name: str) -> str:
    return f"{REPOSITORY}/releases/download/v{release['version']}/{name}"


def allowed_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"} or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ReleaseError("Refusing an unexpected release download host")


class ReleaseRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, stream, code, message, headers, url):
        allowed_url(url)
        return super().redirect_request(request, stream, code, message, headers, url)


def download(url: str, limit: int) -> bytes:
    allowed_url(url)
    deadline = time.monotonic() + NETWORK_TIMEOUT
    request = Request(url, headers={"User-Agent": "Mount-Medic-Updater", "Accept": "application/octet-stream"})
    try:
        with build_opener(ReleaseRedirects()).open(request, timeout=NETWORK_TIMEOUT) as response:
            data = bytearray()
            while len(data) <= limit:
                if time.monotonic() > deadline:
                    raise ReleaseError("Release download timed out")
                chunk = response.read1(min(65536, limit + 1 - len(data)))
                if not chunk:
                    return bytes(data)
                data.extend(chunk)
            raise ReleaseError("Release download exceeds its size limit")
    except OSError as error:
        raise ReleaseError(f"Cannot reach GitHub releases: {error}") from error


def fetch_release(target: str = "latest") -> dict:
    if target != "latest":
        version(target)
    url = f"{REPOSITORY}/releases/latest/download/update.json" if target == "latest" else asset_url({"version": target}, "update.json")
    try:
        value = validate_release(json.loads(download(url, 16384)))
    except (ValueError, UnicodeError) as error:
        raise ReleaseError(f"Cannot read release metadata: {error}") from error
    if target != "latest" and value["version"] != target:
        raise ReleaseError("Release metadata does not match the requested version")
    return value


def verify_archive(data: bytes, release: dict) -> None:
    if len(data) != release["size"] or hashlib.sha256(data).hexdigest() != release["sha256"]:
        raise ReleaseError("Archive checksum or size does not match the published release")


def download_archive(release: dict, directory: Path) -> Path:
    data = download(asset_url(release, release["archive"]), release["size"])
    verify_archive(data, release)
    path = directory / release["archive"]
    path.write_bytes(data)
    return path


def extract_archive(archive: Path, destination: Path, release: dict) -> Path:
    prefix = f"mount-medic-{release['version']}"
    source = destination / prefix
    total = 0
    seen = set()
    try:
        # Bound decompression before tarfile parses extended headers as well as files.
        with gzip.open(archive, "rb") as compressed:
            unpacked = compressed.read(MAX_SOURCE + 1)
        if len(unpacked) > MAX_SOURCE:
            raise ReleaseError("Source archive expands beyond its size limit")
        with tarfile.open(fileobj=io.BytesIO(unpacked), mode="r:") as bundle:
            for member in bundle:
                path = PurePosixPath(member.name)
                if "\0" in member.name or path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != prefix or member.name in seen:
                    raise ReleaseError("Unsafe or duplicate archive path")
                seen.add(member.name)
                total += member.size
                if len(seen) > 1000 or total > MAX_SOURCE or member.size < 0 or not (member.isdir() or member.isfile()):
                    raise ReleaseError("Unsupported archive entry or archive too large")
                target = destination.joinpath(*path.parts)
                if target.exists():
                    raise ReleaseError("Archive paths overlap")
                if member.isdir():
                    target.mkdir(parents=True, mode=0o755)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                    with bundle.extractfile(member) as input_file, target.open("xb") as output:
                        shutil.copyfileobj(input_file, output)
                    target.chmod(0o644)
    except (tarfile.TarError, EOFError) as error:
        raise ReleaseError(f"Invalid source archive: {error}") from error
    required = ["Makefile", "install.py", "LICENSE", "native/probe.c", "mount_medic/__init__.py"]
    if any(not (source / name).is_file() for name in required) or source_version(source) != release["version"]:
        raise ReleaseError("Archive is incomplete or its version does not match")
    return source


def source_version(source: Path) -> str:
    match = re.search(r'^__version__ = "([^"]+)"$', (source / "mount_medic/__init__.py").read_text(), re.MULTILINE)
    if not match:
        raise ReleaseError("Source has no application version")
    version(match[1])
    return match[1]


def bootstrap() -> int:
    try:
        release = fetch_release()
        with tempfile.TemporaryDirectory(prefix="mount-medic-release-") as directory:
            path = Path(directory)
            archive = download_archive(release, path)
            source = extract_archive(archive, path / "source", release)
            return subprocess.call([sys.executable, str(source / "install.py"), *sys.argv[1:]], cwd=source)
    except (ReleaseError, OSError) as error:
        print(f"Mount Medic bootstrap: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(bootstrap())
