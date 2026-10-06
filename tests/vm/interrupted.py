"""Kill a disposable repair parent after its durable record, while its child lives."""
from pathlib import Path
import shutil
import subprocess
import sys
import time

from mount_medic.discovery import resolve
from mount_medic.model import MedicError
from mount_medic.safety import assert_unused


def verify_interruption(volume, engine, uid):
    assert Path("/sys/class/dmi/id/product_name").read_text().strip() == "MountMedicTest"
    assert volume.identity.hardware == "medic-test-disk"
    marker = Path("/run/medic-repair-started")
    marker.unlink(missing_ok=True)
    wrapper = Path("/run/medic-slow-ntfsfix")
    binary = shutil.which("ntfsfix")
    wrapper.write_text(f'#!/bin/sh\necho $$ > {marker}\nsleep 4\nexec {binary} "$@"\n')
    wrapper.chmod(0o700)
    subprocess.run([binary, volume.device], check=True, stdout=subprocess.DEVNULL)
    code = """
import sys
sys.path.insert(0, '/usr/local/lib/mount-medic')
from mount_medic import engine
engine.executable = lambda name: '/run/medic-slow-ntfsfix'
engine.Engine().dispatch(int(sys.argv[1]), {'op': 'automatic', 'id': sys.argv[2]})
"""
    process = subprocess.Popen([sys.executable, "-c", code, str(uid), volume.key], stdout=subprocess.DEVNULL)
    try:
        for _ in range(100):
            if marker.exists():
                break
            assert process.poll() is None, "repair parent stopped before mutation"
            time.sleep(0.05)
        assert marker.exists(), "repair never started"
        process.kill()
        process.wait(timeout=5)
        assert engine.registry.entry(uid, volume.key)["attempt"]["status"] == "running"
        try:
            engine.dispatch(uid, {"op": "automatic", "id": volume.key})
            raise AssertionError("automatic retry after interruption")
        except MedicError as error:
            assert "explicit retry" in str(error)
        competing = subprocess.run(["mount", "-t", "ntfs3", volume.device, "/mnt/medic-test"], capture_output=True)
        assert competing.returncode, "orphaned mutating child lost its block claim"
        for _ in range(100):
            try:
                assert_unused(resolve(volume.key))
                break
            except MedicError:
                time.sleep(0.1)
        report = engine.inspect(volume.key)
        assert report["state"] == "clean", report
        subprocess.run([binary, volume.device], check=True, stdout=subprocess.DEVNULL)
        result = engine.dispatch(uid, {"op": "repair", "id": volume.key, "clear_dirty": True, "retry": True})
        assert result["repair"]["status"] == "success"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    print("PASS: killed parent, retained child block claim, persistent failure latch, explicit retry", flush=True)
