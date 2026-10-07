"""Private D-Bus only: retain the client identity and avoid activation on Quit."""
import gc
import os
from pathlib import Path
import sys
from unittest.mock import patch

os.environ["DBUS_SYSTEM_BUS_ADDRESS"] = os.environ["DBUS_SESSION_BUS_ADDRESS"]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mount_medic import client

connection = client.system_bus()
name = connection.get_unique_name()
del connection
gc.collect()
assert client.system_bus().get_unique_name() == name, "dropping a request lost the app identity"
with patch("os.geteuid", return_value=1000):
    assert client.call({"op": "forget_authorization"}) == {"forgotten": True}
print("PASS: stable app D-Bus identity; Quit does not activate an absent worker")
