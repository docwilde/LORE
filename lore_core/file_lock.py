# SPDX-License-Identifier: AGPL-3.0-only
"""Cross-process locks and atomic replacements for curated file stores."""

import contextlib
import hashlib
import os
import tempfile
from pathlib import Path


@contextlib.contextmanager
def locked_paths(*paths: Path, lock_root: Path):
    """Lock stable sidecars in path order, including both sides of a move."""
    handles = []
    try:
        for path in sorted(set(paths), key=str):
            digest = hashlib.sha256(str(path.absolute()).encode("utf-8")).hexdigest()
            lock = lock_root / ".locks" / (digest + ".lock")
            lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(lock.parent, 0o700)
            handle = open(lock, "a+b")
            os.chmod(lock, 0o600)
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    if not handle.read(1):
                        handle.write(b"\0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except BaseException:
                handle.close()
                raise
            handles.append(handle)
        yield
    finally:
        for handle in reversed(handles):
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def atomic_write_text(path: Path, body: str) -> None:
    """Replace a file without a visible partial write or shared temp name."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(body)
        os.replace(name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)
