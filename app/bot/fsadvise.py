"""Portable page-cache release after append-only journal writes.

Live gear22 would_send canaries append ~1 Hz ``metrics.jsonl`` under
``theta/``, ``tw_p50/``, and ``floor/``. Those files stay open in
``"a"`` mode and grow to hundreds of MiB per day. The kernel keeps the
written pages in file/inactive_file cache, which counts against the
systemd unit ``MemoryMax`` even when process RSS is small.

``posix_fadvise(..., POSIX_FADV_DONTNEED)`` after a successful flush
(and fsync, when used) tells the kernel those pages are not needed in
cache. This is **not** a durability change: data is already on disk
before the advise. Compact-to-parquet still only covers old days.

If ``posix_fadvise`` / ``POSIX_FADV_DONTNEED`` is missing (non-POSIX,
older Python, or a stub ``os``), this module is a no-op and never
raises into the bot.
"""

from __future__ import annotations

import os
from os import PathLike
from typing import IO, Union

AdviseTarget = Union[int, IO[str], IO[bytes], PathLike[str], str]


def advise_dontneed(fd_or_path: AdviseTarget) -> None:
    """Drop page cache for an already-written file. Never raises.

    Prefer the open append file descriptor (``fh.fileno()``). A path is
    accepted as a fallback: the helper opens it read-only, advises the
    whole file (offset=0, length=0), then closes that fd.
    """
    posix_fadvise = getattr(os, "posix_fadvise", None)
    dontneed = getattr(os, "POSIX_FADV_DONTNEED", None)
    if posix_fadvise is None or dontneed is None:
        return

    owned_fd: int | None = None
    try:
        if isinstance(fd_or_path, int):
            fd = fd_or_path
        elif hasattr(fd_or_path, "fileno"):
            fd = fd_or_path.fileno()
        else:
            owned_fd = os.open(os.fspath(fd_or_path), os.O_RDONLY)
            fd = owned_fd
        posix_fadvise(fd, 0, 0, dontneed)
    except (OSError, TypeError, ValueError, AttributeError):
        return
    finally:
        if owned_fd is not None:
            try:
                os.close(owned_fd)
            except OSError:
                return
