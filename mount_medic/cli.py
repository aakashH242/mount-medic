import argparse
import json
import sys

from . import __version__
from . import client
from .engine import REPAIR_NOTICE
from .model import MedicError


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Limited NTFS recovery with explicit per-drive permission")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("gui", "watch", "list", "check", "repair", "mount", "unmount", "configure", "doctor", "uninstall"):
        sub = commands.add_parser(name)
        sub.add_argument("--json", action="store_true")
        sub.add_argument("--dry-run", action="store_true")
        if name in {"check", "configure"}:
            sub.add_argument("volume_id", nargs="?")
        if name in {"repair", "mount", "unmount"}:
            sub.add_argument("volume_id")
        if name == "repair":
            sub.add_argument("--keep-dirty", action="store_true")
            sub.add_argument("--retry", action="store_true", help="Review and retry a failed or interrupted attempt")
        if name == "configure":
            for setting in ("monitor", "auto-repair", "auto-mount"):
                sub.add_argument("--" + setting, choices=("on", "off"))
    return root


def confirm(message: str) -> None:
    print(message)
    if not sys.stdin.isatty() or input("Continue? [y/N] ").strip().lower() != "y":
        raise MedicError("Cancelled; no operation was requested")


def select_volume(volume_id: str) -> dict:
    matches = [row for row in client.list_volumes() if row["volume"]["id"] == volume_id]
    if len(matches) != 1:
        raise MedicError("Select an unambiguous ID from mount-medic list")
    return matches[0]


def describe(row: dict) -> str:
    volume = row["volume"]
    return json.dumps({"drive": volume, "warning": REPAIR_NOTICE}, ensure_ascii=True, indent=2)


def configuration(args) -> dict:
    if not args.volume_id:
        if any(getattr(args, name) is not None for name in ("monitor", "auto_repair", "auto_mount")):
            raise MedicError("Select a volume ID before changing its permissions")
        return client.list_volumes()
    row = select_volume(args.volume_id)
    settings = row.get("settings", {})
    if args.auto_repair is None and args.auto_mount is None and args.monitor is not None:
        return client.call({"op": "monitor", "id": args.volume_id, "enabled": args.monitor == "on"})
    request = {"op": "configure", "id": args.volume_id}
    for name in ("monitor", "auto_repair", "auto_mount"):
        value = getattr(args, name)
        request[name] = settings.get(name, False) if value is None else value == "on"
    if not any(getattr(args, name) is not None for name in ("monitor", "auto_repair", "auto_mount")):
        return row
    confirm(describe(row) + "\nNew permissions: " + json.dumps(request))
    return client.call(request)


def execute(args):
    if args.dry_run:
        preview = {"dry_run": True, "operation": args.command,
                   "volume_id": getattr(args, "volume_id", None), "notice": REPAIR_NOTICE,
                   "effects": "Discovery only. No authentication, state changes, or disk operations.",
                   "eligibility": "Full read-only safety checks are required again at execution."}
        if preview["volume_id"]:
            volumes = [volume for volume in client.discover() if volume.key == preview["volume_id"]]
            if len(volumes) != 1:
                raise MedicError("Volume is absent or ambiguous")
            preview["volume"] = volumes[0].as_dict()
            if args.command == "repair":
                preview["command"] = ["ntfsfix", *([] if args.keep_dirty else ["-d"]), volumes[0].device]
            elif args.command == "configure":
                preview["changes"] = {name: getattr(args, name) for name in ("monitor", "auto_repair", "auto_mount")}
        return preview
    if args.command in {"gui", "watch"}:
        from .desktop import launch
        launch(args.command)
        return None
    if args.command == "list":
        return client.list_volumes()
    if args.command == "doctor":
        return client.doctor()
    if args.command == "check":
        request = {"op": "check"}
        if args.volume_id:
            request["id"] = args.volume_id
        return client.call(request)
    if args.command == "configure":
        return configuration(args)
    if args.command == "repair":
        row = select_volume(args.volume_id)
        confirm(describe(row) + f"\nProposed command: ntfsfix {'(keep dirty)' if args.keep_dirty else '-d'} {row['volume'].get('device')}")
        return client.repair({"op": "repair", "id": args.volume_id,
                              "clear_dirty": not args.keep_dirty, "retry": args.retry})
    if args.command in {"mount", "unmount"}:
        row = select_volume(args.volume_id)
        confirm(f"{args.command.title()} this exact volume?\n" + describe(row))
        return client.udisks_operation(args.volume_id, args.command.title())
    if args.command == "uninstall":
        from .installer import uninstall_interactive
        return uninstall_interactive()


def exit_status(value) -> int:
    rows = value if isinstance(value, list) else [value]
    if any(isinstance(row, dict) and (row.get("state") in {"failed", "unavailable", "missing_dependency", "insufficient_access"}
                                    or row.get("mount", {}).get("state") == "failed") for row in rows):
        return 2
    return 1 if any(isinstance(row, dict) and row.get("state") not in
                    {None, "ready", "clean", "mounted_rw", "mounted", "unmounted", "unmanaged"} for row in rows) else 0


def main() -> int:
    args = parser().parse_args()
    try:
        value = execute(args)
        if value is not None:
            if args.json or not isinstance(value, list):
                print(json.dumps(value, indent=2, ensure_ascii=True))
            else:
                for row in value:
                    volume = row["volume"]
                    print(f"{volume['id']}  {row['state']}  {json.dumps(volume.get('label') or volume.get('device', ''))}")
                    print("  " + row.get("next_steps", ""))
        return exit_status(value)
    except (MedicError, OSError, ValueError, ImportError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=True) if args.json else f"Mount Medic: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Cancelled. An already-started privileged repair continues independently.", file=sys.stderr)
        return 2
