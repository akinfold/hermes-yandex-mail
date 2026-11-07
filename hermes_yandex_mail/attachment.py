"""Save one attachment to a file, safely, as its bytes stream in.

The bytes of an attachment never pass through the model's context: they go
straight to a file, and the tool hands back where the file is. Everything here
is plain file handling, free of Hermes and IMAP imports.

The file is written the careful way, because its name comes from whoever sent
the mail:

* the name is reduced to a single safe path component, and prefixed with
  :data:`PREFIX` and a random token, so it can neither leave the directory nor
  collide with or replace anything already there;
* the bytes go to a temporary file created exclusively with mode ``0600``,
  then are synced and linked into place, so a half-written file never appears
  under the final name and an existing file is never overwritten;
* a size cap is checked as the bytes arrive, and anything that fails — the cap,
  the network, the disk — removes what was written.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import stat
import time
import unicodedata
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

__all__ = ["PREFIX", "AttachmentTooLarge", "SavedFile", "prune", "safe_filename", "save"]

#: Every file this plugin writes starts with this, so it can find its own.
PREFIX = "yandex-mail_"
_TEMPORARY = "." + PREFIX
#: The longest the kept name may be, in UTF-8 bytes. With the prefix and the
#: token it stays well inside the 255 bytes filesystems allow for a name.
_NAME_BYTES = 180
_EXTENSION_BYTES = 16
#: Characters Windows does not allow in a name.
_RESERVED = re.compile(r"[<>:\"|?*$`';&(){}!#~]")
#: Control and formatting characters, the last including the bidirectional
#: overrides that can make "exe.pdf" display as "fdp.exe".
_INVISIBLE = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


class AttachmentTooLarge(ValueError):
    """The attachment turned out bigger than the size cap allows."""


@dataclass(frozen=True)
class SavedFile:
    """Where the attachment went, how big it is, and its SHA-256."""

    path: Path
    size: int
    sha256: str


def safe_filename(name: str, part_id: str) -> str:
    """A name that is one harmless path component, keeping the extension.

    Directory parts (``/`` and ``\\``), invisible characters, and characters
    Windows forbids are removed or replaced, leading and trailing dots and
    spaces are dropped, and a long name is cut to fit. A name with nothing left
    becomes ``attachment-<part_id>.bin``.
    """
    base = re.split(r"[/\\]", name)[-1]
    base = "".join(ch for ch in base if unicodedata.category(ch) not in _INVISIBLE)
    base = _RESERVED.sub("_", base).strip(". \t")
    stem, dot, extension = base.rpartition(".")
    if not dot or not stem or len(extension.encode()) > _EXTENSION_BYTES or " " in extension:
        stem, extension = base, ""
    suffix = f".{extension}" if extension else ""
    room = _NAME_BYTES - len(suffix.encode())
    stem = stem.encode()[:room].decode("utf-8", "ignore").rstrip(". ")
    return f"{stem}{suffix}" if stem else f"attachment-{part_id}.bin"


@contextlib.contextmanager
def _cache_directory(directory: Path) -> Iterator[int]:
    """Pin a directory writable only by its owner before touching cache files."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(directory, flags)
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISDIR(status.st_mode) or status.st_mode & 0o022:
            raise PermissionError("The document cache must not be group- or world-writable.")
        if hasattr(os, "getuid") and status.st_uid != os.getuid():
            raise PermissionError("The document cache must belong to the current user.")
        _check_identity(directory.lstat(), status)
        yield descriptor
    finally:
        os.close(descriptor)


def _check_identity(actual: os.stat_result, expected: os.stat_result) -> None:
    if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
        raise OSError("The document cache or attachment changed while saving.")


def prune(directory: Path, max_age: float, now: float | None = None) -> int:
    """Remove only this plugin's old regular files from a trusted cache directory."""
    cutoff = (time.time() if now is None else now) - max_age
    removed = 0
    try:
        with _cache_directory(directory) as cache:
            for name in os.listdir(cache):
                if not name.startswith((PREFIX, _TEMPORARY)):
                    continue
                with contextlib.suppress(OSError):
                    status = os.stat(name, dir_fd=cache, follow_symlinks=False)
                    if stat.S_ISREG(status.st_mode) and status.st_mtime < cutoff:
                        os.unlink(name, dir_fd=cache)
                        removed += 1
    except OSError:
        return 0
    return removed


def save(chunks: Iterable[bytes], directory: Path, filename: str, max_bytes: int) -> SavedFile:
    """Stream into a private file and publish it without replacing an existing name.

    ``filename`` must already be safe (see :func:`safe_filename`). Refuse unsafe
    cache permissions and filesystems without hard-link support. Any failure
    removes this call's temporary file and any name it published.
    """
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = uuid.uuid4().hex[:12]
    temporary = f"{_TEMPORARY}{token}.part"
    final = f"{PREFIX}{token}_{filename}"
    with _cache_directory(directory) as cache:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=cache)
        try:
            size, digest = _stream(chunks, descriptor, max_bytes)
            _publish(cache, descriptor, temporary, final, directory)
        finally:
            os.close(descriptor)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=cache)
    return SavedFile(directory / final, size, digest)


def _stream(chunks: Iterable[bytes], descriptor: int, max_bytes: int) -> tuple[int, str]:
    digest, size = hashlib.sha256(), 0
    for chunk in chunks:
        size += len(chunk)
        if size > max_bytes:
            raise AttachmentTooLarge(
                f"The attachment is larger than the {max_bytes}-byte limit; nothing was saved."
            )
        digest.update(chunk)
        _write_all(descriptor, chunk)
    os.fsync(descriptor)
    return size, digest.hexdigest()


def _publish(cache: int, descriptor: int, temporary: str, final: str, directory: Path) -> None:
    # Keep the original descriptor open: a replaced temporary name must never
    # publish someone else's inode, even when its bytes happen to match.
    os.link(temporary, final, src_dir_fd=cache, dst_dir_fd=cache, follow_symlinks=False)
    try:
        status = os.stat(final, dir_fd=cache, follow_symlinks=False)
        _check_identity(status, os.fstat(descriptor))
        if not stat.S_ISREG(status.st_mode):
            raise OSError("The saved attachment is not a regular file.")
        _check_identity(directory.lstat(), os.fstat(cache))
    except BaseException:
        os.unlink(final, dir_fd=cache)
        raise


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("The attachment write made no progress.")
        view = view[written:]
