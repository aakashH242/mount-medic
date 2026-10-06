"""Integration tests touch only newly generated regular files, never block devices."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent


def run(arguments):
    return subprocess.run(list(map(str, arguments)), capture_output=True, check=True)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def probe(path):
    before = digest(path)
    result = run([ROOT / "build/mount-medic-probe", path])
    assert digest(path) == before, "Read-only probe changed the image"
    return json.loads(result.stdout)


def main():
    with tempfile.TemporaryDirectory(prefix="mount-medic-images-") as folder:
        directory = Path(folder)
        clean = directory / "clean.img"
        with clean.open("wb") as stream:
            stream.truncate(32 * 1024 * 1024)
        run(["mkntfs", "-F", "-Q", "-L", "MountMedicTest", clean])
        content = directory / "sentinel"
        content.write_text("Never lose this payload.\n")
        run(["ntfscp", clean, content, "/sentinel"])
        assert probe(clean)["state"] == "clean"
        dirty = directory / "dirty.img"
        shutil.copyfile(clean, dirty)
        run(["ntfsfix", dirty])
        assert probe(dirty)["state"] == "dirty"
        before = digest(dirty)
        assert run(["ntfs-3g.probe", "--readonly", dirty]).returncode == 0
        assert run(["ntfsfix", "-n", dirty]).returncode == 0
        assert digest(dirty) == before
        run(["ntfsfix", "-d", dirty])
        assert probe(dirty)["state"] == "clean"
        assert run(["ntfscat", dirty, "/sentinel"]).stdout == content.read_bytes()
        hibernated = directory / "hibernated.img"
        shutil.copyfile(clean, hibernated)
        header = directory / "hiberfil.sys"
        header.write_bytes(b"hibr" + bytes(4092))
        run(["ntfscp", hibernated, header, "/hiberfil.sys"])
        assert probe(hibernated)["state"] == "hibernated"
        for mode, expected in (("cached", "cached_metadata"), ("unclean", "unclean_journal")):
            journal = directory / (mode + ".img")
            shutil.copyfile(clean, journal)
            run([ROOT / "build/journal-fixture", journal, mode])
            actual = probe(journal)
            assert actual["state"] == expected, (mode, actual)
            if mode == "unclean":
                run(["ntfsfix", "-d", journal])
                assert probe(journal)["state"] == "clean"
                assert run(["ntfscat", journal, "/sentinel"]).stdout == content.read_bytes()
        mismatch = directory / "mirror.img"
        shutil.copyfile(clean, mismatch)
        info = run(["ntfsinfo", "-m", mismatch]).stdout.decode()
        cluster = int(re.search(r"Cluster Size:\s+(\d+)", info)[1])
        mirror = int(re.search(r"LCN of Data Attribute for File_MFTMirr:\s+(\d+)", info)[1])
        with mismatch.open("r+b") as stream:
            stream.seek(cluster * mirror + 16)
            original = stream.read(1)
            stream.seek(-1, 1)
            stream.write(bytes([original[0] ^ 1]))
        assert probe(mismatch)["complete"] is False
        malformed = directory / "malformed.img"
        malformed.write_bytes(bytes(65536))
        assert probe(malformed)["complete"] is False
        print("PASS: clean, dirty, hibernated, cached metadata, unclean journal, mirror mismatch, malformed, limited repair; diagnostic hashes and sentinel unchanged")


if __name__ == "__main__":
    main()
