import re

from .model import MedicError

BUS_NAME = "io.github.aakashH242.MountMedic"
BUS_PATH = "/io/github/aakashH242/MountMedic"
INTERFACE = BUS_NAME + "1"
BASE_ACTION = "io.github.aakashH242.mount-medic."
XML = f"""<node><interface name='{INTERFACE}'>
<method name='Call'><arg type='s' direction='in'/><arg type='s' direction='out'/></method>
</interface></node>"""
SCHEMAS = {
    "list": {}, "check": {"id": str},
    "monitor": {"id": str, "enabled": bool},
    "configure": {"id": str, "monitor": bool, "auto_repair": bool, "auto_mount": bool},
    "automatic": {"id": str},
    "repair": {"id": str, "clear_dirty": bool, "retry": bool},
    "prepare_mount": {"id": str, "background": bool},
}


def validate(request: dict) -> dict:
    if not isinstance(request, dict) or not isinstance(request.get("op"), str) or request["op"] not in SCHEMAS:
        raise MedicError("Unknown request")
    schema = SCHEMAS[request["op"]]
    if set(request) - {"op", *schema}:
        raise MedicError("Unexpected request fields")
    for name, expected in schema.items():
        if name not in request and request["op"] == "check":
            continue
        if type(request.get(name)) is not expected:
            raise MedicError(f"Invalid {name}")
    if "id" in request and not re.fullmatch(r"[0-9a-f]{24}", request["id"]):
        raise MedicError("Use a volume ID from mount-medic list")
    if request["op"] == "configure" and request["auto_repair"] and not request["monitor"]:
        raise MedicError("Enable monitoring before automatic repair")
    return request


def authorization(request: dict) -> tuple[str, int]:
    if request["op"] in {"configure", "repair"}:
        return BASE_ACTION + "admin", 1
    return BASE_ACTION + "inspect", 0
