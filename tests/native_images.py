"""Integration tests touch only newly generated regular files, never block devices."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
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


def volume_flags(image, side, change):
    assert image.is_file(), "Fixture writer only accepts regular files"
    with image.open("r+b") as stream:
        boot = stream.read(512)
        cluster = struct.unpack_from("<H", boot, 11)[0] * boot[13]
        record_bits = struct.unpack_from("<b", boot, 64)[0]
        record_size = 1 << -record_bits if record_bits < 0 else cluster * record_bits
        lcn = struct.unpack_from("<Q", boot, 48 if side == "primary" else 56)[0]
        start = lcn * cluster + 3 * record_size
        stream.seek(start)
        record = bytearray(stream.read(record_size))
        usa, count = struct.unpack_from("<HH", record, 4)
        for index in range(1, count):
            assert record[index * 512 - 2:index * 512] == record[usa:usa + 2]
            record[index * 512 - 2:index * 512] = record[usa + 2 * index:usa + 2 * index + 2]
        offset = struct.unpack_from("<H", record, 20)[0]
        while struct.unpack_from("<I", record, offset)[0] != 0x70:
            offset += struct.unpack_from("<I", record, offset + 4)[0]
        flag = offset + struct.unpack_from("<H", record, offset + 20)[0] + 10
        change(record, flag)
        for index in range(1, count):
            record[usa + 2 * index:usa + 2 * index + 2] = record[index * 512 - 2:index * 512]
            record[index * 512 - 2:index * 512] = record[usa:usa + 2]
        stream.seek(start)
        stream.write(record)


def stale_mirror(image):
    volume_flags(image, "mirror", lambda record, flag: struct.pack_into("<H", record, flag, 0))


def mirror_recovery(clean, directory, sentinel):
    for mode in ("normal", "new-token", "hibernated", "cached", "unclean", "reversed", "unsupported", "other-change", "torn", "bad-usa", "saved-tail", "duplicate-info", "nonresident-info", "usa-overlap", "bad-upcase"):
        image = directory / f"stale-{mode}.img"
        shutil.copyfile(clean, image)
        if mode == "hibernated":
            header = directory / "hibernation-header"
            header.write_bytes(b"hibr" + bytes(4092))
            run(["ntfscp", image, header, "/hiberfil.sys"])
        elif mode in {"cached", "unclean"}:
            run([ROOT / "build/journal-fixture", image, mode])
        else:
            run(["ntfsfix", image])
        if mode in {"hibernated", "cached", "unclean"}:
            volume_flags(image, "primary", lambda record, flag: struct.pack_into("<H", record, flag, 1))
        stale_mirror(image)
        if mode == "reversed":
            volume_flags(image, "primary", lambda record, flag: struct.pack_into("<H", record, flag, 0))
            volume_flags(image, "mirror", lambda record, flag: struct.pack_into("<H", record, flag, 1))
        if mode == "unsupported":
            volume_flags(image, "primary", lambda record, flag: struct.pack_into("<H", record, flag, 3))
        if mode in {"duplicate-info", "nonresident-info", "usa-overlap"}:
            def malformed(record, flag):
                if mode == "usa-overlap":
                    struct.pack_into("<H", record, 4, struct.unpack_from("<H", record, 20)[0])
                    return
                offset = struct.unpack_from("<H", record, 20)[0]
                while struct.unpack_from("<I", record, offset)[0] != 0x70:
                    offset += struct.unpack_from("<I", record, offset + 4)[0]
                if mode == "nonresident-info":
                    record[offset + 8] = 1
                    return
                length = struct.unpack_from("<I", record, offset + 4)[0]
                end = struct.unpack_from("<I", record, 24)[0] - 8
                record[end:end + length] = record[offset:offset + length]
                struct.pack_into("<I", record, end + length, 0xffffffff)
                struct.pack_into("<I", record, 24, end + length + 8)
            volume_flags(image, "primary", malformed)
            volume_flags(image, "mirror", malformed)
        if mode == "bad-upcase":
            info = run(["ntfsinfo", "-f", "-v", "-i", "10", clean]).stdout.decode()
            lcn = int(re.search(r"Runlist:[^\n]*\n\s*0x0\s+(0x[0-9a-f]+)", info)[1], 16)
            with image.open("r+b") as stream:
                boot = stream.read(512)
                cluster = struct.unpack_from("<H", boot, 11)[0] * boot[13]
                stream.seek(lcn * cluster + 2 * ord("a"))
                stream.write(bytes(2))
        if mode == "new-token":
            def token(record, flag):
                usa = struct.unpack_from("<H", record, 4)[0]
                value = struct.unpack_from("<H", record, usa)[0]
                struct.pack_into("<H", record, usa, (value + 1) % 65535 or 1)
            volume_flags(image, "mirror", token)
        if mode == "other-change":
            volume_flags(image, "mirror", lambda record, flag: record.__setitem__(8, record[8] ^ 1))
        if mode == "bad-usa":
            volume_flags(image, "mirror", lambda record, flag: struct.pack_into("<H", record, 6, 0))
        if mode == "saved-tail":
            volume_flags(image, "mirror", lambda record, flag: record.__setitem__(510, record[510] ^ 1))
        if mode == "torn":
            with image.open("r+b") as stream:
                boot = stream.read(512)
                cluster = struct.unpack_from("<H", boot, 11)[0] * boot[13]
                start = struct.unpack_from("<Q", boot, 56)[0] * cluster + 3 * 1024 + 510
                stream.seek(start)
                value = stream.read(1)[0]
                stream.seek(start)
                stream.write(bytes([value ^ 1]))
        actual = probe(image)
        if mode in {"normal", "new-token", "unclean"}:
            assert actual["complete"] is True, (mode, actual)
            assert actual["state"] == ("unclean_journal" if mode == "unclean" else "dirty"), (mode, actual)
            assert actual.get("stale_volume_mirror") is True
            if mode == "normal":
                before = digest(image)
                for fault in (1, 2, 3, 4):
                    value = json.loads(run([ROOT / "build/mirror-view-check", fault, image]).stdout)
                    assert value["complete"] is (fault == 4), (fault, value)
                    assert digest(image) == before, "Fault-injected diagnosis changed the image"
            run(["ntfsfix", "-d", image])
            repaired = probe(image)
            assert repaired["state"] == "clean" and not repaired.get("stale_volume_mirror"), repaired
            assert run(["ntfscat", image, "/sentinel"]).stdout == sentinel.read_bytes()
        else:
            assert actual["complete"] is False, (mode, actual)
            if mode in {"hibernated", "cached"}:
                assert actual["state"] == ("hibernated" if mode == "hibernated" else "cached_metadata"), actual
        image.unlink()
    print("PASS: stale volume mirror recovery; hibernation/cache/corruption/MST refusals; unchanged probe hashes and repaired sentinel")


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
        mirror_recovery(clean, directory, content)
        malformed = directory / "malformed.img"
        malformed.write_bytes(bytes(65536))
        assert probe(malformed)["complete"] is False
        print("PASS: clean, dirty, hibernated, cached metadata, unclean journal, mirror mismatch, malformed, limited repair; diagnostic hashes and sentinel unchanged")


if __name__ == "__main__":
    main()
