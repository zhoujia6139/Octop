"""No-follow, bounded local workspace content transport.

The host chooses export roots and exclusions. POSIX operations pin directories
with dir_fd/O_NOFOLLOW. Windows pins every directory without FILE_SHARE_DELETE
and opens the leaf with FILE_FLAG_OPEN_REPARSE_POINT. Hard links and nonregular
files are never read or modified. These APIs do not operate remote backends.
"""

from __future__ import annotations

import contextlib
import os
import stat
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


@dataclass(frozen=True)
class SyncFile:
    path: str
    size: int
    fingerprint: tuple[int, int, int, int, int]


def _parts(path: str) -> tuple[str, ...]:
    value = PurePosixPath(path)
    if (
        not path
        or not value.parts
        or value.is_absolute()
        or value.as_posix() != path
        or "\\" in path
        or any(p in {"..", "."} or ":" in p or "\x00" in p for p in value.parts)
    ):
        raise ValueError("canonical workspace-relative path required")
    return value.parts


def _fingerprint(row: os.stat_result) -> tuple[int, int, int, int, int]:
    return row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns, row.st_ctime_ns


def _regular(fd: int) -> os.stat_result:
    row = os.fstat(fd)
    if not stat.S_ISREG(row.st_mode) or row.st_nlink != 1:
        raise PermissionError("only unlinked regular workspace files are transferable")
    return row


def _posix_flag(name: str) -> int:
    # POSIX-only constants are absent in Windows type stubs. Missing runtime
    # support raises instead of weakening the no-follow requirement.
    return int(getattr(os, name))


def _win_open(path: Path, *, directory: bool = False, write: bool = False, rename: bool = False) -> int:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    # These Windows-only FFI attributes are absent in POSIX type stubs.
    winapi: Any = ctypes
    crt: Any = msvcrt
    options: Any = os
    kernel = winapi.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    handle = create(
        str(path),
        0x80000000 | (0x40000000 if write else 0) | (0x00010000 if rename else 0),
        3,
        None,
        4 if write else 3,
        0x00200000 | (0x02000000 if directory else 0),
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise winapi.WinError(winapi.get_last_error())
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    info = kernel.GetFileInformationByHandleEx
    info.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    attributes = (wintypes.DWORD * 2)()
    if not info(handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes)) or attributes[0] & 0x400:
        close(handle)
        raise PermissionError("workspace reparse points are excluded")
    try:
        return int(crt.open_osfhandle(handle, options.O_BINARY | (os.O_RDWR if write else os.O_RDONLY)))
    except BaseException:
        close(handle)
        raise


def _win_commit(source: Path, target: Path) -> None:
    """Rename the pinned source handle without reopening it with DELETE access.

    The source denies delete sharing, so another process cannot exchange it
    between validation and commit. Parent handles are already pinned by commit.
    """
    import ctypes
    import msvcrt
    from ctypes import wintypes

    winapi: Any = ctypes
    crt: Any = msvcrt
    name = str(target)
    units = len(name.encode("utf-16-le")) // 2

    class RenameInfo(ctypes.Structure):
        _fields_ = [
            ("ReplaceIfExists", wintypes.BOOLEAN),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", wintypes.WCHAR * (units + 1)),
        ]

    info = RenameInfo()
    info.ReplaceIfExists = True
    info.RootDirectory = None
    info.FileNameLength = units * 2
    info.FileName = name
    kernel = winapi.WinDLL("kernel32", use_last_error=True)
    rename_file = kernel.SetFileInformationByHandle
    rename_file.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    rename_file.restype = wintypes.BOOL
    fd = _win_open(source, rename=True)
    try:
        _regular(fd)
        if not rename_file(crt.get_osfhandle(fd), 3, ctypes.byref(info), ctypes.sizeof(info)):
            raise winapi.WinError(winapi.get_last_error())
    finally:
        os.close(fd)


@contextlib.contextmanager
def _parent(root: Path, parts: tuple[str, ...], *, create: bool = False) -> Iterator[int | Path]:
    with contextlib.ExitStack() as stack:
        if os.name == "nt":
            current = Path(root.anchor)
            stack.callback(os.close, _win_open(current, directory=True))
            for index, part in enumerate((*root.parts[1:], *parts)):
                current = current / part
                if create and index >= len(root.parts) - 1:
                    current.mkdir(exist_ok=True)
                stack.callback(os.close, _win_open(current, directory=True))
            yield current
        else:
            flags = os.O_RDONLY | _posix_flag("O_DIRECTORY") | _posix_flag("O_NOFOLLOW")
            directory = os.open(root.anchor, flags)
            stack.callback(os.close, directory)
            for index, part in enumerate((*root.parts[1:], *parts)):
                if create and index >= len(root.parts) - 1:
                    with contextlib.suppress(FileExistsError):
                        os.mkdir(part, dir_fd=directory)
                directory = os.open(part, flags, dir_fd=directory)
                stack.callback(os.close, directory)
            yield directory


def ensure_directory(root: Path) -> None:
    with _parent(Path(root.anchor), tuple(root.parts[1:]), create=True):
        pass


@contextlib.contextmanager
def _file(root: Path, path: str, *, write: bool = False) -> Iterator[int]:
    parts = _parts(path)
    with _parent(root, parts[:-1], create=write) as parent:
        fd = (
            _win_open(parent / parts[-1], write=write)
            if isinstance(parent, Path)
            else os.open(
                parts[-1],
                (os.O_RDWR | os.O_CREAT if write else os.O_RDONLY)
                | _posix_flag("O_NOFOLLOW")
                | _posix_flag("O_NONBLOCK"),
                0o600,
                dir_fd=parent,
            )
        )
        try:
            _regular(fd)
            yield fd
        finally:
            os.close(fd)


def scan(root: Path, roots: Sequence[str], *, limit: int = 10000) -> list[SyncFile]:
    """Complete recursive enumeration or raise; no partial deletion watermark.

    Links, hard links and special files are omitted. Disappearing paths or a
    changing directory cause the scan to fail, allowing a later retry.
    """
    result: list[SyncFile] = []

    def walk(parts: tuple[str, ...]) -> None:
        if len(parts) > 32:
            raise ValueError("workspace traversal depth exceeds 32")
        with _parent(root, parts) as parent:
            before = os.stat(parent) if isinstance(parent, Path) else os.fstat(parent)
            names = sorted(os.listdir(parent))
            for name in names:
                child = (*parts, name)
                row = (
                    os.lstat(parent / name)
                    if isinstance(parent, Path)
                    else os.stat(name, dir_fd=parent, follow_symlinks=False)
                )
                if stat.S_ISLNK(row.st_mode) or getattr(row, "st_file_attributes", 0) & 0x400:
                    continue
                if stat.S_ISDIR(row.st_mode):
                    if not name.startswith(".") and name not in {"node_modules", "__pycache__"}:
                        walk(child)
                elif stat.S_ISREG(row.st_mode) and row.st_nlink == 1:
                    if len(result) >= limit:
                        raise ValueError("workspace file limit exceeded")
                    result.append(SyncFile("/".join(child), row.st_size, _fingerprint(row)))
            after = os.stat(parent) if isinstance(parent, Path) else os.fstat(parent)
            if _fingerprint(before) != _fingerprint(after):
                raise OSError("workspace directory changed during enumeration")

    for entry in roots:
        parts = _parts(entry)
        try:
            walk(parts)
        except FileNotFoundError:
            # Only absence of the selected root is an empty root. A missing
            # descendant during a scan is incomplete and must be retried.
            with _parent(root, parts[:-1]) as parent:
                try:
                    os.stat(parent / parts[-1], follow_symlinks=False) if isinstance(parent, Path) else os.stat(
                        parts[-1], dir_fd=parent, follow_symlinks=False
                    )
                except FileNotFoundError:
                    continue
            raise
    return result


def read_chunk(
    root: Path, path: str, offset: int, size: int, fingerprint: tuple[int, int, int, int, int] | None = None
) -> bytes:
    if offset < 0 or not 0 < size <= 1024 * 1024:
        raise ValueError("invalid bounded read")
    with _file(root, path) as fd:
        before = _fingerprint(_regular(fd))
        if fingerprint is not None and before != fingerprint:
            raise OSError("workspace file changed")
        os.lseek(fd, offset, os.SEEK_SET)
        content = os.read(fd, size)
        if _fingerprint(_regular(fd)) != before:
            raise OSError("workspace file changed during read")
        return content


def write_chunk(root: Path, path: str, offset: int, content: bytes) -> None:
    if offset < 0 or len(content) > 1024 * 1024:
        raise ValueError("invalid bounded write")
    with _file(root, path, write=True) as fd:
        os.lseek(fd, offset, os.SEEK_SET)
        view = memoryview(content)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)


def commit(root: Path, staged: str, target: str) -> None:
    source_parts, target_parts = _parts(staged), _parts(target)
    with _parent(root, source_parts[:-1]) as src, _parent(root, target_parts[:-1], create=True) as dest:
        if isinstance(src, Path) and isinstance(dest, Path):
            _win_commit(src / source_parts[-1], dest / target_parts[-1])
        elif isinstance(src, int) and isinstance(dest, int):
            with _file(root, staged):
                os.replace(source_parts[-1], target_parts[-1], src_dir_fd=src, dst_dir_fd=dest)
                os.fsync(dest)


def remove(root: Path, path: str) -> None:
    parts = _parts(path)
    with contextlib.suppress(FileNotFoundError), _parent(root, parts[:-1]) as parent:
        if isinstance(parent, Path):
            (parent / parts[-1]).unlink(missing_ok=True)
        else:
            try:
                os.unlink(parts[-1], dir_fd=parent)
                os.fsync(parent)
            except FileNotFoundError:
                pass


__all__ = ["SyncFile", "commit", "read_chunk", "remove", "scan", "write_chunk"]
