import io
import os
from pathlib import Path
import pty
import tarfile
import tempfile
import unittest


class DownloadInstallTests(unittest.TestCase):
    def test_piped_install_keeps_terminal_and_cleans_up_on_success_or_failure(self):
        script = Path(__file__).resolve().parent.parent / "install.sh"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binaries = root / "bin"
            binaries.mkdir()
            temporary = root / "downloads"
            temporary.mkdir()
            archive = root / "source.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                for name in ("install.py", "Makefile"):
                    entry = tarfile.TarInfo("mount-medic-main/" + name)
                    entry.size = 0
                    bundle.addfile(entry, io.BytesIO())
            mocks = {
                "curl": '#!/bin/sh\nset -eu\n[ "$MM_DOWNLOAD_FAIL" = 0 ] || exit 22\ncp "$MM_ARCHIVE" "$4"\n',
                "python3": '#!/bin/sh\nset -eu\n[ -t 0 ]\n[ -f "$1" ]\n[ -f "$(dirname "$1")/Makefile" ]\nprintf "guided-installer\\n"\nexit "$MM_INSTALL_EXIT"\n',
            }
            for name, content in mocks.items():
                path = binaries / name
                path.write_text(content)
                path.chmod(0o755)
            for download_failure, install_exit in ((0, 0), (0, 2), (1, 0)):
                with self.subTest(download_failure=download_failure, install_exit=install_exit):
                    environment = dict(os.environ, PATH=str(binaries) + os.pathsep + os.environ["PATH"],
                                       TMPDIR=str(temporary), MM_ARCHIVE=str(archive),
                                       MM_DOWNLOAD_FAIL=str(download_failure), MM_INSTALL_EXIT=str(install_exit))
                    process, terminal = pty.fork()
                    if process == 0:
                        os.execvpe("bash", ["bash", "-c", 'cat "$1" | bash -s -- --download', "test", str(script)], environment)
                    output = b""
                    try:
                        while chunk := os.read(terminal, 4096):
                            output += chunk
                    except OSError:
                        pass
                    finally:
                        os.close(terminal)
                    _, status = os.waitpid(process, 0)
                    self.assertEqual(os.waitstatus_to_exitcode(status), 22 if download_failure else install_exit, output)
                    self.assertEqual(b"guided-installer" in output, not download_failure, output)
                    self.assertEqual(list(temporary.iterdir()), [])
