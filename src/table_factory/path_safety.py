"""Filesystem path checks shared by input discovery and output writes."""

from __future__ import annotations

import os
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


class PathSafetyError(Exception):
    """Base class for failures while securely resolving a filesystem path."""


class UntrustedSymlinkError(PathSafetyError):
    """A user-controlled symbolic link was found during descriptor traversal."""

    def __init__(self, *, final_component: bool) -> None:
        super().__init__("path contains an untrusted symbolic link")
        self.final_component = final_component


class PathIdentityChangedError(PathSafetyError):
    """A path component changed between its metadata check and open."""


class SecurePathUnsupportedError(PathSafetyError):
    """The host cannot provide the descriptor-relative operations we require."""


class PathInspectionError(PathSafetyError):
    """Filesystem metadata could not be inspected without exposing a host path."""


@dataclass(frozen=True, slots=True)
class FileIdentity:
    """Stable filesystem identity captured from an open descriptor."""

    device: int
    inode: int

    @classmethod
    def from_stat(cls, status: os.stat_result) -> FileIdentity:
        return cls(device=status.st_dev, inode=status.st_ino)


def _inspection_error(error: OSError | ValueError) -> PathInspectionError:
    detail = (error.strerror or "I/O error") if isinstance(error, OSError) else "invalid path"
    return PathInspectionError(f"cannot inspect path component: {detail}")


def _is_trusted_system_symlink(
    path: Path,
    link_status: os.stat_result,
) -> bool:
    """Allow stable root-owned aliases such as macOS ``/var`` and ``/tmp``."""
    try:
        parent_status = path.parent.stat()
    except (OSError, ValueError) as error:
        raise _inspection_error(error) from None
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
        try:
            link_status = current.lstat()
        except FileNotFoundError:
            return False
        except (OSError, ValueError) as error:
            raise _inspection_error(error) from None
        if stat.S_ISLNK(link_status.st_mode) and not _is_trusted_system_symlink(
            current,
            link_status,
        ):
            return True
    return False


def _require_secure_descriptor_operations() -> None:
    if (
        not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or os.open not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or os.mkdir not in os.supports_dir_fd
    ):
        raise SecurePathUnsupportedError(
            "secure descriptor-relative path operations are unavailable"
        )


def _open_flags(*, expected: Literal["directory", "regular", "either"], nofollow: bool) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if expected == "directory":
        flags |= os.O_DIRECTORY
    else:
        flags |= os.O_NONBLOCK
    if nofollow:
        flags |= os.O_NOFOLLOW
    return flags


def _trusted_system_symlink_at(
    directory_fd: int,
    link_status: os.stat_result,
) -> bool:
    """Descriptor-relative form of the stable system-alias exception."""
    parent_status = os.fstat(directory_fd)
    writable_by_non_root = parent_status.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    return (
        stat.S_ISLNK(link_status.st_mode)
        and link_status.st_uid == 0
        and parent_status.st_uid == 0
        and not writable_by_non_root
    )


def open_verified_entry(
    directory_fd: int,
    name: str,
    checked_status: os.stat_result,
    *,
    expected: Literal["directory", "regular", "either"],
    final_component: bool = False,
) -> tuple[int, os.stat_result]:
    """Open one checked entry relative to a pinned parent directory."""
    _require_secure_descriptor_operations()
    if (
        not name
        or name in {".", ".."}
        or os.sep in name
        or (os.altsep is not None and os.altsep in name)
    ):
        raise ValueError("entry name must be a plain filename")

    is_symlink = stat.S_ISLNK(checked_status.st_mode)
    if is_symlink and not _trusted_system_symlink_at(directory_fd, checked_status):
        raise UntrustedSymlinkError(final_component=final_component)
    if not is_symlink:
        if expected == "directory" and not stat.S_ISDIR(checked_status.st_mode):
            raise NotADirectoryError(name)
        if expected == "regular" and not stat.S_ISREG(checked_status.st_mode):
            raise OSError(f"path entry is not a regular file: {name}")
        if expected == "either" and not (
            stat.S_ISDIR(checked_status.st_mode) or stat.S_ISREG(checked_status.st_mode)
        ):
            raise OSError(f"path entry is neither a file nor a directory: {name}")

    descriptor = os.open(
        name,
        _open_flags(expected=expected, nofollow=not is_symlink),
        dir_fd=directory_fd,
    )
    try:
        opened_status = os.fstat(descriptor)
        if expected == "directory" and not stat.S_ISDIR(opened_status.st_mode):
            raise NotADirectoryError(name)
        if expected == "regular" and not stat.S_ISREG(opened_status.st_mode):
            raise OSError(f"path entry is not a regular file: {name}")
        if expected == "either" and not (
            stat.S_ISDIR(opened_status.st_mode) or stat.S_ISREG(opened_status.st_mode)
        ):
            raise OSError(f"path entry is neither a file nor a directory: {name}")
        if not is_symlink and not os.path.samestat(checked_status, opened_status):
            raise PathIdentityChangedError("path entry changed while it was being opened")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor, opened_status


def open_pinned_path(
    path: Path,
    *,
    create_directory: bool,
) -> tuple[int, os.stat_result]:
    """Resolve a path component-by-component and return its pinned descriptor."""
    _require_secure_descriptor_operations()
    absolute = Path(os.path.abspath(path))
    if absolute.anchor != os.sep:
        raise SecurePathUnsupportedError("secure path traversal requires a POSIX filesystem")

    current_fd = os.open(
        absolute.anchor,
        _open_flags(expected="directory", nofollow=False),
    )
    current_status = os.fstat(current_fd)
    try:
        components = absolute.parts[1:]
        for index, component in enumerate(components):
            final_component = index == len(components) - 1
            try:
                checked_status = os.stat(
                    component,
                    dir_fd=current_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                if not create_directory:
                    raise
                with suppress(FileExistsError):
                    os.mkdir(component, dir_fd=current_fd)
                checked_status = os.stat(
                    component,
                    dir_fd=current_fd,
                    follow_symlinks=False,
                )

            expected: Literal["directory", "regular", "either"] = (
                "directory" if create_directory or not final_component else "either"
            )
            child_fd, child_status = open_verified_entry(
                current_fd,
                component,
                checked_status,
                expected=expected,
                final_component=final_component,
            )
            os.close(current_fd)
            current_fd = child_fd
            current_status = child_status
        return current_fd, current_status
    except Exception:
        os.close(current_fd)
        raise
