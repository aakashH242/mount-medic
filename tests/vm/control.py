"""QMP/guest-agent control through Unix sockets inside the disposable runner."""
import argparse
import base64
import json
import socket
import time


def request(stream, command, arguments):
    stream.write((json.dumps({"execute": command, "arguments": arguments, "id": command}) + "\n").encode())
    while True:
        result = json.loads(stream.readline())
        if result.get("id") != command:
            continue
        if "error" in result:
            raise RuntimeError(result["error"])
        return result["return"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("socket")
    parser.add_argument("command")
    parser.add_argument("arguments", nargs="?", default="{}")
    args = parser.parse_args()
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(30)
        connection.connect(args.socket)
        stream = connection.makefile("rwb", buffering=0)
        if args.socket.endswith("qmp.sock"):
            json.loads(stream.readline())
            request(stream, "qmp_capabilities", {})
        result = request(stream, args.command, json.loads(args.arguments))
        if args.command == "guest-exec":
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                status = request(stream, "guest-exec-status", {"pid": result["pid"]})
                if status.get("exited"):
                    for name in ("out-data", "err-data"):
                        if name in status:
                            status[name] = base64.b64decode(status[name]).decode(errors="replace")
                    result = status
                    break
                time.sleep(0.2)
        print(json.dumps(result))


if __name__ == "__main__":
    main()
