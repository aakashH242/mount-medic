from dataclasses import asdict
import json
import os
from pathlib import Path
import selectors
import subprocess
import time

from .commands import ENVIRONMENT, OUTPUT_LIMIT, executable, run
from .discovery import VolumeUnavailable, discover, resolve
from .model import MedicError, diagnosis, repairable
from .safety import claim, operation_lock, require_supported, assert_safe_mount_options
from .storage import Registry

INSTALL_ROOT = Path("/usr/local/lib/mount-medic")
ROOT_STATE = Path("/var/lib/mount-medic")
REPAIR_NOTICE = ("ntfsfix performs limited metadata repair and resets the NTFS journal. "
                 "With -d it also clears the dirty flag. This cannot verify every file or "
                 "replace Windows chkdsk. Back up valuable data before authorizing writes.")


class Engine:
    def __init__(self, directory: Path = ROOT_STATE, probe: Path = INSTALL_ROOT / "mount-medic-probe"):
        self.registry = Registry(directory)
        self.probe = probe

    def list_volumes(self, uid: int) -> list[dict]:
        saved = self.registry.load(uid)
        volumes = discover()
        rows = []
        for volume in volumes:
            settings = saved.get(volume.key, {})
            state = "unmanaged" if not settings.get("monitor") else "unknown"
            if volume.mounts:
                state = "mounted_ro" if volume.mount_readonly else "mounted_rw"
            report = diagnosis(volume, state)
            report["settings"] = settings
            rows.append(report)
        present = {volume.key for volume in volumes}
        for key, settings in saved.items():
            if key not in present:
                rows.append({"volume": {"id": key, "identity": settings.get("identity", {}),
                                        "label": settings.get("label", "Disconnected drive"), "device": ""},
                             "state": "absent", "settings": settings, "actions": [],
                             "evidence": "", "next_steps": "Reconnect this enrolled volume."})
        return rows

    def configure(self, uid: int, request: dict) -> dict:
        saved = self.registry.load(uid)
        settings = saved.get(request["id"], {})
        try:
            volume = resolve(request["id"])
        except VolumeUnavailable:
            revoking = (request["op"] == "monitor" and not request["enabled"]) or (
                request["op"] == "configure" and not any(request[key] for key in ("monitor", "auto_repair", "auto_mount")))
            if not settings or not revoking:
                raise
            volume = None
        if request["op"] == "monitor":
            settings["monitor"] = request["enabled"]
        else:
            if request["auto_repair"] or request["auto_mount"]:
                require_supported(volume)
            settings.update(monitor=request["monitor"], auto_repair=request["auto_repair"],
                            auto_mount=request["auto_mount"])
        if volume is not None:
            settings.update(identity=asdict(volume.identity), label=volume.label)
        saved[request["id"]] = settings
        self.registry.save(uid, saved)
        return settings

    def inspect(self, volume_id: str) -> dict:
        volume = resolve(volume_id)
        if volume.mounts:
            return diagnosis(volume, "mounted_ro" if volume.mount_readonly else "mounted_rw")
        if not volume.supported or volume.duplicate:
            return diagnosis(volume, "unsupported")
        if not self.probe.is_file():
            return diagnosis(volume, "missing_dependency", "The native read-only probe is not installed")
        try:
            with claim(volume) as descriptor:
                return self.probe_volume(volume, descriptor)
        except PermissionError as error:
            return diagnosis(volume, "insufficient_access", str(error))
        except (OSError, MedicError) as error:
            return diagnosis(volume, "busy" if "mounted" in str(error) or "open by" in str(error) else "unknown", str(error))

    def probe_volume(self, volume, descriptor: int) -> dict:
        output = run([str(self.probe), f"/proc/self/fd/{descriptor}"], pass_fds=(descriptor,))
        report = diagnosis(volume, "unknown", output.stderr)
        try:
            value = json.loads(output.stdout)
            if output.returncode or value.get("schema") != 1:
                return report
            state = value["state"]
            if state == "io_or_corruption" and "$MFTMirr does not match $MFT" in output.stderr:
                state = "mirror_mismatch"
            report = diagnosis(volume, state, output.stderr)
            report["complete"] = value.get("complete") is True
            if repairable(report):
                try:
                    require_supported(volume)
                    report["actions"] = ["repair"]
                except MedicError as error:
                    report["next_steps"] = str(error)
            elif state == "clean":
                report["actions"] = ["mount"]
        except (ValueError, KeyError, TypeError, AttributeError):
            pass
        return report

    def check(self, uid: int, volume_id: str = "") -> list[dict]:
        keys = [volume_id] if volume_id else [key for key, entry in self.registry.load(uid).items() if entry.get("monitor")]
        reports = []
        deadline = time.monotonic() + 120
        for key in keys:
            if time.monotonic() > deadline:
                break
            try:
                report = self.inspect(key)
            except VolumeUnavailable as error:
                report = {"volume": {"id": key}, "state": "absent", "actions": [],
                          "evidence": str(error), "next_steps": "Reconnect this enrolled volume."}
            report["checked_at"] = int(time.time())
            reports.append(report)
        return reports

    def record_attempt(self, uid: int, record: dict) -> None:
        saved = self.registry.load(uid)
        entry = saved.setdefault(record["id"], {})
        entry["attempt"] = record
        self.registry.save(uid, saved)

    def repair(self, uid: int, request: dict) -> dict:
        volume = resolve(request["id"])
        require_supported(volume)
        settings = self.registry.entry(uid, volume.key)
        automatic = request["op"] == "automatic"
        if automatic and not (settings.get("monitor") and settings.get("auto_repair")):
            raise MedicError("This user has not authorized automatic repair for this volume")
        if settings.get("attempt", {}).get("status") != "success" and settings.get("attempt"):
            if automatic or not request.get("retry"):
                raise MedicError("A previous attempt failed or was interrupted; explicit retry is required")
        attempt = None
        try:
            with claim(volume) as descriptor:
                report = self.probe_volume(volume, descriptor)
                if not repairable(report):
                    return report
                command = [executable("ntfsfix")]
                if automatic or request.get("clear_dirty", True):
                    command.append("-d")
                command.append(f"/proc/self/fd/{descriptor}")
                attempt = {"id": volume.key, "status": "running", "started": int(time.time())}
                self.record_attempt(uid, attempt)
                status, output = self.execute_repair(command, descriptor, (uid, attempt))
                report = self.probe_volume(resolve(volume.key), descriptor)
                expected = "clean" if "-d" in command else "dirty"
                verified = (status == 0 and attempt["status"] == "running" and
                            report.get("complete") is True and report["state"] == expected)
            # The claim also checks device identity on exit, before success is durable.
            attempt.update(status="success" if verified else "failed", output=output,
                           finished=int(time.time()))
            self.record_attempt(uid, attempt)
            report.update(repair=attempt, checked_at=int(time.time()), notice=REPAIR_NOTICE)
            report["mount_requested"] = bool(verified and expected == "clean" and settings.get("auto_mount"))
            if not verified:
                report.update(state="failed", actions=[], next_steps="Review the unresolved attempt before explicitly retrying.")
            return report
        except BaseException:
            if attempt is not None:
                attempt.update(status="interrupted", finished=int(time.time()))
                self.record_attempt(uid, attempt)
            raise

    def execute_repair(self, command: list[str], descriptor: int, context: tuple) -> tuple:
        uid, attempt = context
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, env=ENVIRONMENT,
                                   pass_fds=(descriptor,), start_new_session=True, restore_signals=False)
        # Preserve Python's ignored SIGPIPE so a worker crash cannot kill the
        # mutating child merely by closing its output pipe.
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        output = bytearray()
        started = time.monotonic()
        try:
            while selector.get_map():
                if time.monotonic() - started > 120 and attempt["status"] == "running":
                    attempt["status"] = "overdue"
                    self.record_attempt(uid, attempt)
                for key, _ in selector.select(1):
                    chunk = os.read(key.fileobj.fileno(), 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    output.extend(chunk[:max(0, OUTPUT_LIMIT - len(output))])
            return process.wait(), output.decode(errors="replace")
        finally:
            # Never release the block claim while a mutating child is still alive.
            process.wait()
            selector.close()
            process.stdout.close()

    def prepare_mount(self, uid: int, request: dict) -> dict:
        settings = self.registry.entry(uid, request["id"])
        if request.get("background") and not settings.get("auto_mount"):
            raise MedicError("Automatic mounting is not enabled")
        report = self.inspect(request["id"])
        if report["state"] != "clean":
            raise MedicError("Only a currently unmounted volume passing read-only checks may be mounted")
        assert_safe_mount_options(resolve(request["id"]))
        return report

    def dispatch(self, uid: int, request: dict):
        if request["op"] == "list":
            return self.list_volumes(uid)
        with operation_lock(self.registry.directory):
            operation = request["op"]
            if operation == "check":
                return self.check(uid, request.get("id", ""))
            if operation in {"configure", "monitor"}:
                return self.configure(uid, request)
            if operation in {"repair", "automatic"}:
                return self.repair(uid, request)
            if operation == "prepare_mount":
                return self.prepare_mount(uid, request)
            raise MedicError("Unsupported operation")
