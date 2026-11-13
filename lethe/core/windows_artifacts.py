"""Windows handle validation for bounded artifact reads."""
import os
from pathlib import Path
import stat


def validate_components(root: Path, relative: Path) -> None:
    from lethe.core.artifacts import ArtifactRejected
    current = root
    for component in [None, *relative.parts]:
        if component is not None:
            current = current / component
        try:
            info = current.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ArtifactRejected("unsafe_file")
        if current == root / relative and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise ArtifactRejected("unsafe_file")


def open_verified(root: Path, relative: Path) -> int:
    import ctypes
    from ctypes import wintypes
    import msvcrt
    from lethe.core.artifacts import ArtifactRejected

    validate_components(root, relative)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    final_path = kernel.GetFinalPathNameByHandleW
    final_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    final_path.restype = wintypes.DWORD
    # OPEN_REPARSE_POINT prevents following a substituted final link.
    handle = create(str(root / relative), 0x80000000, 7, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), "Cannot open artifact")
    fd = None
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = final_path(handle, buffer, len(buffer), 0)
        if not size or size >= len(buffer):
            raise ArtifactRejected("unsafe_file")
        resolved = buffer.value
        if resolved.startswith("\\\\?\\UNC\\"):
            resolved = "\\\\" + resolved[8:]
        elif resolved.startswith("\\\\?\\"):
            resolved = resolved[4:]
        actual = Path(resolved)
        expected = root / relative
        if os.path.normcase(str(actual)) != os.path.normcase(str(expected)):
            raise ArtifactRejected("path_escape")
        validate_components(root, relative)
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        handle = None
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ArtifactRejected("unsafe_file")
        return fd
    except BaseException:
        if fd is not None:
            os.close(fd)
        raise
    finally:
        if handle is not None:
            close(handle)
