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
import time
import unicodedata
import uuid
from collections.abc import Iterable
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
_RESERVED = re.compile(r'[<>:"|?*]')
#: Control and formatting characters, the last including the bidirectional
#: overrides that can make "exe.pdf" display as "fdp.exe".
_INVISIBLE = frozenset({"Cc", "Cf", "Cs"})


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


def prune(directory: Path, max_age: float, now: float | None = None) -> int:
    """Delete this plugin's files in ``directory`` older than ``max_age`` seconds.

    Only files carrying :data:`PREFIX` (or a temporary one left by a crash) are
    touched, and only at the top level, which is where :func:`save` writes.
    Returns how many were removed.
    """
    cutoff = (time.time() if now is None else now) - max_age
    removed = 0
    try:
        entries = list(directory.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if not entry.name.startswith((PREFIX, _TEMPORARY)):
            continue
        with contextlib.suppress(OSError):
            status = entry.lstat()
            if entry.is_file() and not entry.is_symlink() and status.st_mtime < cutoff:
                entry.unlink()
                removed += 1
    return removed


def save(chunks: Iterable[bytes], directory: Path, filename: str, max_bytes: int) -> SavedFile:
    """Write ``chunks`` to a new file in ``directory`` named after ``filename``.

    ``filename`` must already be safe (see :func:`safe_filename`). Raises
    :class:`AttachmentTooLarge` as soon as more than ``max_bytes`` have arrived,
    and leaves no file behind on that or any other failure.
    """
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = uuid.uuid4().hex[:12]
    temporary = directory / f"{_TEMPORARY}{token}.part"
    final = directory / f"{PREFIX}{token}_{filename}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    digest, size = hashlib.sha256(), 0
    descriptor = os.open(temporary, flags, 0o600)
    try:
        try:
            for chunk in chunks:
                size += len(chunk)
                if size > max_bytes:
                    raise AttachmentTooLarge(
                        f"The attachment is larger than the {max_bytes}-byte limit; "
                        "nothing was saved."
                    )
                digest.update(chunk)
                _write_all(descriptor, chunk)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _link(temporary, final)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
    return SavedFile(final, size, digest.hexdigest())


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(descriptor, view) :]


def _link(source: Path, target: Path) -> None:
    """Give ``source`` the name ``target``, never replacing an existing file."""
    try:
        os.link(source, target)
    except FileExistsError:
        raise
    except OSError:
        # A filesystem without hard links. rename() would replace an existing
        # target on POSIX, so check first; the random token makes a race moot.
        if os.path.lexists(target):
            raise FileExistsError(target) from None
        os.rename(source, target)
