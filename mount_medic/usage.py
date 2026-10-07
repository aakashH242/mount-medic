"""Filesystem space figures shared by discovery and the native views."""

GB = 1_000_000_000
MAX_BYTES = (1 << 64) - 1


def filesystem_usage(total, used) -> dict | None:
    if type(total) not in (int, str) or type(used) not in (int, str):
        return None
    try:
        total, used = int(total), int(used)
    except ValueError:
        return None
    if not 0 <= used <= total <= MAX_BYTES or total == 0:
        return None
    free = total - used
    return {"total": total, "used": used, "free": free, "free_percent": 100 * free / total}


def volume_usage(volume: dict) -> dict | None:
    usage = volume.get("usage")
    return filesystem_usage(usage.get("total"), usage.get("used")) if isinstance(usage, dict) else None


def storage_summary(volume: dict) -> str:
    usage = volume_usage(volume)
    if usage:
        return f"{usage['used'] / GB:.1f} / {usage['total'] / GB:.1f} GB ({usage['free_percent']:.1f}% free)"
    size = volume.get("identity", {}).get("size")
    capacity = f"{size / GB:.1f} GB total · " if type(size) is int and 0 < size <= MAX_BYTES else ""
    return capacity + "Usage unavailable"
