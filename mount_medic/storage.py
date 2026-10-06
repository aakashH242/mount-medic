import json
import os
from pathlib import Path
import tempfile

from .model import MedicError

DEFAULT_NOTIFICATION_SECONDS = 10
MAX_NOTIFICATION_SECONDS = 600


def notification_seconds(value) -> int:
    if type(value) is not int or not 1 <= value <= MAX_NOTIFICATION_SECONDS:
        raise MedicError(f"Notification duration must be a whole number from 1 to {MAX_NOTIFICATION_SECONDS} seconds")
    return value


def read_json(path: Path) -> dict:
    try:
        if path.stat().st_size > 2_000_000:
            raise MedicError(f"State file is too large: {path}")
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError("expected an object")
        return value
    except FileNotFoundError:
        return {}
    except (ValueError, OSError) as error:
        raise MedicError(f"Cannot read state {path}: {error}") from error


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".state-")
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, ensure_ascii=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


class Registry:
    def __init__(self, directory: Path):
        self.directory = directory

    def load(self, uid: int) -> dict:
        return read_json(self.directory / f"{uid}.json")

    def save(self, uid: int, value: dict) -> None:
        atomic_json(self.directory / f"{uid}.json", value)

    def entry(self, uid: int, volume_id: str) -> dict:
        return self.load(uid).get(volume_id, {})


class Preferences:
    def __init__(self):
        self.config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "mount-medic"
        self.state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "mount-medic"

    def notification_seconds(self) -> int:
        value = read_json(self.config / "preferences.json").get("notification_seconds", DEFAULT_NOTIFICATION_SECONDS)
        return notification_seconds(value)

    def set_notification_seconds(self, value: int) -> None:
        path = self.config / "preferences.json"
        saved = read_json(path)
        saved["notification_seconds"] = notification_seconds(value)
        atomic_json(path, saved)

    def ignored(self) -> dict:
        return read_json(self.config / "ignored.json")

    def ignore(self, volume: dict) -> None:
        saved = self.ignored()
        # Keep the full identity for review even when the drive is disconnected.
        saved[volume["id"]] = {key: volume[key] for key in ("id", "identity", "label", "device")}
        atomic_json(self.config / "ignored.json", saved)

    def unignore(self, volume_id: str) -> None:
        saved = self.ignored()
        saved.pop(volume_id, None)
        atomic_json(self.config / "ignored.json", saved)

    def changed(self, volume_id: str, fingerprint: str) -> bool:
        path = self.state / "notifications.json"
        seen = read_json(path)
        if seen.get(volume_id) == fingerprint:
            return False
        seen[volume_id] = fingerprint
        atomic_json(path, dict(list(seen.items())[-1000:]))
        return True

    def remember(self, reports: list[dict]) -> None:
        path = self.state / "last-check.json"
        saved = read_json(path)
        history = read_json(self.state / "history.json").get("reports", [])
        for report in reports:
            bounded = json.loads(json.dumps(report))
            bounded["evidence"] = bounded.get("evidence", "")[:4000]
            if "repair" in bounded:
                bounded["repair"]["output"] = bounded["repair"].get("output", "")[:4000]
            saved[report["volume"]["id"]] = bounded
            history.append(bounded)
        atomic_json(path, dict(list(saved.items())[-50:]))
        atomic_json(self.state / "history.json", {"reports": history[-50:]})
