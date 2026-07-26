"""Filesystem path checks shared by input discovery and output writes."""

from __future__ import annotations

import os
import stat
from pathlib import Path


def _is_trusted_system_symlink(path: Path) -> bool:
    """Allow stable root-owned aliases such as macOS ``/var`` and ``/tmp``."""
    try:
        link_status = path.lstat()
        parent_status = path.parent.stat()
    except OSError:
        return False
    writable_by_non_root = parent_status.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    return (
        stat.S_ISLNK(link_status.st_mode)
        and link_status.st_uid == 0
        and parent_status.st_uid == 0
        and not writable_by_non_root
    )


def has_untrusted_symlink_component(path: Path) -> bool:
    """Return whether an existing path component is a user-controlled symlink."""
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        if current.is_symlink() and not _is_trusted_system_symlink(current):
            return True
    return False
