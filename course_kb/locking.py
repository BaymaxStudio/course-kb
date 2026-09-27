"""跨平台进程锁与只读进程存活查询；锁由操作系统随句柄关闭释放。"""
from __future__ import annotations

import ctypes
import errno
from functools import lru_cache
import os
from typing import Callable

IS_WINDOWS = os.name == "nt"

# 固定 Win32 类型宽度；Unix 的 c_ulong 在部分平台为 64 位，不能代替 DWORD。
_DWORD = ctypes.c_uint32
_BOOL = ctypes.c_int32
_HANDLE = ctypes.c_void_p


class _Overlapped(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", _DWORD),
        ("OffsetHigh", _DWORD),
        ("hEvent", _HANDLE),
    ]


def _windows_error(code: int, operation: str, busy: bool = False) -> OSError:
    number = errno.EAGAIN if busy else (errno.EACCES if code == 5 else errno.EIO)
    error = (BlockingIOError if busy else OSError)(number, f"{operation} failed (Windows error {code})")
    error.winerror = code
    return error


def _configure_kernel32(kernel: object) -> None:
    """显式声明 HANDLE 和返回值，避免 64 位句柄被 ctypes 默认 int 截断。"""
    signatures = {
        "LockFileEx": ([_HANDLE, _DWORD, _DWORD, _DWORD, _DWORD, ctypes.POINTER(_Overlapped)], _BOOL),
        "UnlockFileEx": ([_HANDLE, _DWORD, _DWORD, _DWORD, ctypes.POINTER(_Overlapped)], _BOOL),
        "OpenProcess": ([_DWORD, _BOOL, _DWORD], _HANDLE),
        "WaitForSingleObject": ([_HANDLE, _DWORD], _DWORD),
        "CloseHandle": ([_HANDLE], _BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes = arguments
        function.restype = result


class _WindowsBackend:
    def __init__(self, kernel: object, get_handle: Callable[[int], int],
                 get_error: Callable[[], int]):
        self.kernel = kernel
        self.get_handle = get_handle
        self.get_error = get_error

    def _file_handle(self, descriptor: int) -> int:
        handle = self.get_handle(descriptor)
        if handle in {-1, -2, ctypes.c_void_p(-1).value}:
            raise _windows_error(6, "get_osfhandle")
        return handle

    def lock(self, descriptor: int, shared: bool) -> None:
        # 零长度文件也可锁定 EOF 之后的一字节；读句柄可以申请共享锁。
        # os.open/open 提供同步句柄，FAIL_IMMEDIATELY 保证竞争时不等待。
        overlapped = _Overlapped()
        flags = 0x1 | (0 if shared else 0x2)
        if not self.kernel.LockFileEx(self._file_handle(descriptor), flags, 0, 1, 0,
                                      ctypes.byref(overlapped)):
            code = self.get_error()
            raise _windows_error(code, "LockFileEx", busy=code == 33)

    def unlock(self, descriptor: int) -> None:
        overlapped = _Overlapped()
        if not self.kernel.UnlockFileEx(self._file_handle(descriptor), 0, 1, 0,
                                        ctypes.byref(overlapped)):
            raise _windows_error(self.get_error(), "UnlockFileEx")

    def process_alive(self, pid: int) -> bool:
        # Windows 的 os.kill(pid, 0) 会终止进程；这里仅申请 SYNCHRONIZE 权限。
        handle = self.kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            code = self.get_error()
            if code == 87:  # PID 不存在；PID 0 已由公共入口拒绝。
                return False
            if code == 5:  # 无查询权限时保守保留旧写锁。
                return True
            raise _windows_error(code, "OpenProcess")
        if handle in {-1, ctypes.c_void_p(-1).value}:
            raise _windows_error(6, "OpenProcess")
        try:
            state = self.kernel.WaitForSingleObject(handle, 0)
            if state == 0x00000102:
                return True
            if state == 0:
                return False
            code = self.get_error() if state == 0xFFFFFFFF else 87
            raise _windows_error(code, "WaitForSingleObject")
        finally:
            if not self.kernel.CloseHandle(handle):
                raise _windows_error(self.get_error(), "CloseHandle")


@lru_cache(maxsize=1)
def _windows_backend() -> _WindowsBackend:
    # 惰性导入让整个包可以在不提供 fcntl/msvcrt 的另一平台加载。
    import msvcrt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _configure_kernel32(kernel)
    return _WindowsBackend(kernel, msvcrt.get_osfhandle, ctypes.get_last_error)


def lock_file(descriptor: int, *, shared: bool = False) -> None:
    """非阻塞申请进程锁；竞争统一抛 BlockingIOError，其他系统错误保留。"""
    if IS_WINDOWS:
        _windows_backend().lock(descriptor, shared)
        return
    import fcntl
    flags = (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB
    try:
        fcntl.flock(descriptor, flags)
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EAGAIN}:
            raise BlockingIOError(errno.EAGAIN, "File lock is held by another operation") from error
        raise


def unlock_file(descriptor: int) -> None:
    """释放当前句柄的锁；调用者随后仍须关闭句柄。"""
    if IS_WINDOWS:
        _windows_backend().unlock(descriptor)
        return
    import fcntl
    fcntl.flock(descriptor, fcntl.LOCK_UN)


def process_alive(pid: int) -> bool:
    """只查询，不发送会改变目标进程状态的信号。"""
    maximum = 0xFFFFFFFF if IS_WINDOWS else 0x7FFFFFFF
    if not isinstance(pid, int) or isinstance(pid, bool) or not 0 < pid <= maximum:
        raise ValueError("Invalid writer PID; inspect write.lock before recovery")
    if IS_WINDOWS:
        return _windows_backend().process_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
