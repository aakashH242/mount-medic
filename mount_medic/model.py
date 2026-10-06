from dataclasses import asdict, dataclass, field
import hashlib
import json


class MedicError(Exception):
    """An operation is unavailable or cannot safely continue."""


@dataclass(frozen=True)
class Identity:
    uuid: str
    hardware: str
    partition: str
    size: int
    start: int

    @property
    def key(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True).encode()
        return hashlib.sha256(payload).hexdigest()[:24]

    @property
    def strong(self) -> bool:
        return bool(self.uuid and self.hardware and self.size > 0)


@dataclass
class Volume:
    identity: Identity
    device: str
    label: str = ""
    model: str = ""
    transport: str = ""
    devnum: str = ""
    mounts: list[str] = field(default_factory=list)
    readonly: bool = False
    supported: bool = True
    duplicate: bool = False
    mount_readonly: bool = False

    @property
    def key(self) -> str:
        return self.identity.key

    def as_dict(self) -> dict:
        return {**asdict(self), "id": self.key}


NEXT_STEPS = {
    "clean": "No issue found by these limited checks; this is not a full NTFS integrity check.",
    "dirty": "Limited NTFS repair can clear the dirty flag. Back up valuable data first.",
    "unclean_journal": "Limited repair resets the journal; interrupted transactions may be lost.",
    "hibernated": "Resume Windows, save work, disable Fast Startup, and fully shut down Windows.",
    "cached_metadata": "Windows may have cached metadata. Fully shut down Windows; use chkdsk if it persists.",
    "io_or_corruption": "Do not keep repairing. Back up or image the drive and investigate hardware; use Windows chkdsk for filesystem damage.",
    "io_failure": "The device returned an I/O failure. Stop repairs; prioritize backup/imaging and investigate the hardware.",
    "mirror_mismatch": "The MFT mirror differs. Other safety checks could not complete; use Windows chkdsk.",
    "unknown": "The result is incomplete. Review diagnostics; do not infer that the drive is healthy.",
    "insufficient_access": "Administrator-assisted read-only inspection is unavailable. Check installation and permissions.",
    "missing_dependency": "Run mount-medic doctor for missing tools and distribution-specific installation instructions.",
    "unsupported": "This storage layout or identity is not safe for automatic repair.",
    "unsupported_flags": "NTFS has additional maintenance flags. Complete a Windows filesystem check.",
    "busy": "The volume is mounted or in use. Close applications and explicitly unmount it first.",
    "mounted_rw": "Available / mounted read-write. Offline checks were not run.",
    "mounted_ro": "Mounted read-only. Explicitly unmount before offline inspection.",
    "unmanaged": "Enable checks to enroll this drive. Discovery alone does not inspect its filesystem.",
    "absent": "The enrolled volume is not connected.",
    "failed": "Automatic retries are paused. Review diagnostics before explicitly retrying.",
}


def diagnosis(volume: Volume, state: str, evidence: str = "") -> dict:
    return {"volume": volume.as_dict(), "state": state, "evidence": evidence,
            "actions": [], "next_steps": NEXT_STEPS.get(state, NEXT_STEPS["unknown"])}


def repairable(report: dict) -> bool:
    return report.get("complete") is True and report.get("state") in {"dirty", "unclean_journal"}
