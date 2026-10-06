import os
from pathlib import Path
import selectors
import subprocess
import time

from .model import MedicError

OUTPUT_LIMIT = 65536
ENVIRONMENT = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "LANG": "C"}


def executable(name: str) -> str:
    for directory in ENVIRONMENT["PATH"].split(":"):
        path = Path(directory) / name
        if path.is_file() and os.access(path, os.X_OK):
            if os.geteuid() == 0:
                for parent in (path.resolve(), *path.resolve().parents):
                    info = parent.stat()
                    if info.st_uid != 0 or info.st_mode & 0o022:
                        raise MedicError(f"Untrusted executable: {path}")
            return str(path)
    raise MedicError(f"Missing dependency: {name}")


def run(arguments: list[str], timeout: int = 30, pass_fds: tuple = ()) -> subprocess.CompletedProcess:
    process = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               stdin=subprocess.DEVNULL, env=ENVIRONMENT, start_new_session=True,
                               pass_fds=pass_fds)
    selector = selectors.DefaultSelector()
    buffers = {process.stdout: bytearray(), process.stderr: bytearray()}
    for stream in buffers:
        selector.register(stream, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    try:
        while selector.get_map():
            if time.monotonic() >= deadline:
                raise MedicError("Read-only command timed out")
            for key, _ in selector.select(0.2):
                chunk = os.read(key.fileobj.fileno(), 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                else:
                    buffers[key.fileobj].extend(chunk)
                    if sum(map(len, buffers.values())) > OUTPUT_LIMIT:
                        raise MedicError("Command output exceeded its limit")
        process.wait(timeout=max(0.1, deadline - time.monotonic()))
        return subprocess.CompletedProcess(arguments, process.returncode,
                                           buffers[process.stdout].decode(errors="replace"),
                                           buffers[process.stderr].decode(errors="replace"))
    except subprocess.TimeoutExpired as error:
        raise MedicError("Read-only command timed out") from error
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        selector.close()
        process.stdout.close()
        process.stderr.close()


def json_command(arguments: list[str]) -> dict:
    import json
    result = run(arguments)
    if result.returncode:
        raise MedicError(result.stderr.strip() or "System query failed")
    try:
        return json.loads(result.stdout)
    except (ValueError, TypeError) as error:
        raise MedicError("Invalid structured output from system tool") from error
