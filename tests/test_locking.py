"""本机锁实测与可在非 Windows 主机运行的 Win32 调用协议测试。"""
import ctypes
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from course_kb import locking
from course_kb.model import DataError, managed_files, read_snapshot


class NativeLockTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="course-kb-lock-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.guard = self.root / "guard"
        self.guard.touch()

    def descriptor(self, flags=os.O_RDONLY):
        descriptor = os.open(self.guard, flags)
        self.addCleanup(os.close, descriptor)
        return descriptor

    def test_shared_readers_coexist_and_writer_is_nonblocking(self):
        first, second, writer = self.descriptor(), self.descriptor(), self.descriptor(os.O_RDWR)
        locking.lock_file(first, shared=True)
        locking.lock_file(second, shared=True)
        with self.assertRaises(BlockingIOError):
            locking.lock_file(writer)
        locking.unlock_file(first)
        with self.assertRaises(BlockingIOError):
            locking.lock_file(writer)
        locking.unlock_file(second)
        locking.lock_file(writer)
        with self.assertRaises(BlockingIOError):
            locking.lock_file(first, shared=True)
        locking.unlock_file(writer)
        self.assertEqual(self.guard.stat().st_size, 0)

    def test_process_exit_releases_kernel_lock(self):
        code = '''import os, sys
from course_kb.locking import lock_file
fd = os.open(sys.argv[1], os.O_RDWR)
lock_file(fd)
print("locked", flush=True)
sys.stdin.readline()
os._exit(0)
'''
        process = subprocess.Popen([sys.executable, "-c", code, str(self.guard)],
                                   cwd=Path(__file__).resolve().parents[1],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(process.stderr.close)
        self.addCleanup(process.stdout.close)
        self.addCleanup(process.stdin.close)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        self.assertEqual(process.stdout.readline().strip(), "locked")
        descriptor = self.descriptor(os.O_RDWR)
        with self.assertRaises(BlockingIOError):
            locking.lock_file(descriptor)
        process.stdin.write("exit\n")
        process.stdin.flush()
        self.assertEqual(process.wait(timeout=5), 0)
        locking.lock_file(descriptor)
        locking.unlock_file(descriptor)

    def test_imports_do_not_require_either_platform_lock_module(self):
        code = '''import builtins
original = builtins.__import__
def checked(name, *args, **kwargs):
    if name in {"fcntl", "msvcrt"}:
        raise ModuleNotFoundError("simulated unavailable platform module: " + name, name=name)
    return original(name, *args, **kwargs)
builtins.__import__ = checked
import course_kb.model
import course_kb.transactions
import course_kb.locking
'''
        process = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)

    def test_uninitialized_read_snapshot_is_readonly_and_detects_changes(self):
        (self.root / "manifest.json").write_text(json.dumps({"courses": []}))
        before = {path.name for path in self.root.iterdir()}
        with read_snapshot(self.root):
            pass
        self.assertEqual({path.name for path in self.root.iterdir()}, before)
        with self.assertRaisesRegex(DataError, "changed while being read"):
            with read_snapshot(self.root):
                (self.root / "manifest.json").write_text(json.dumps({"courses": [], "edited": True}))
        self.assertFalse((self.root / ".course-kb").exists())

    def test_managed_paths_use_portable_json_separators(self):
        course = self.root / "COURSE1000"
        (course / "raw").mkdir(parents=True)
        (course / "course.json").write_text("{}")
        (course / "raw/source.pdf").write_bytes(b"data")
        (self.root / "manifest.json").write_text(json.dumps({"courses": [{"code": "COURSE1000", "directory": "COURSE1000"}]}))
        paths = managed_files(self.root)
        self.assertIn("COURSE1000/raw/source.pdf", paths)
        self.assertTrue(all("\\" not in path for path in paths))


class WindowsProtocolTests(unittest.TestCase):
    def setUp(self):
        self.kernel = SimpleNamespace(**{name: Mock(return_value=1) for name in (
            "LockFileEx", "UnlockFileEx", "OpenProcess", "WaitForSingleObject", "CloseHandle")})
        self.error = 0
        self.handle = 0x100000042 if ctypes.sizeof(ctypes.c_void_p) == 8 else 0x4242
        self.backend = locking._WindowsBackend(self.kernel, lambda descriptor: self.handle, lambda: self.error)

    def test_win32_signatures_preserve_handle_width_and_dword_layout(self):
        locking._configure_kernel32(self.kernel)
        self.assertIs(self.kernel.OpenProcess.restype, ctypes.c_void_p)
        self.assertIs(self.kernel.LockFileEx.argtypes[0], ctypes.c_void_p)
        self.assertEqual(ctypes.sizeof(self.kernel.WaitForSingleObject.restype), 4)
        self.assertEqual(ctypes.sizeof(self.kernel.LockFileEx.restype), 4)
        size = ctypes.sizeof(ctypes.c_void_p)
        self.assertEqual(locking._Overlapped.Offset.offset, 2 * size)
        self.assertEqual(locking._Overlapped.OffsetHigh.offset, 2 * size + 4)
        self.assertEqual(ctypes.sizeof(locking._Overlapped), 32 if size == 8 else 20)

    def test_shared_and_exclusive_lock_match_unlock_byte_range(self):
        self.backend.lock(7, shared=True)
        args = self.kernel.LockFileEx.call_args.args
        self.assertEqual(args[:5], (self.handle, 1, 0, 1, 0))
        overlapped = ctypes.cast(args[5], ctypes.POINTER(locking._Overlapped)).contents
        self.assertEqual((overlapped.Internal, overlapped.InternalHigh, overlapped.Offset, overlapped.OffsetHigh, overlapped.hEvent), (0, 0, 0, 0, None))
        self.backend.lock(8, shared=False)
        self.assertEqual(self.kernel.LockFileEx.call_args.args[1], 3)
        self.backend.unlock(7)
        self.assertEqual(self.kernel.UnlockFileEx.call_args.args[:4], (self.handle, 0, 1, 0))
        pointer = self.kernel.UnlockFileEx.call_args.args[4]
        self.assertEqual(ctypes.cast(pointer, ctypes.POINTER(locking._Overlapped)).contents.Offset, 0)

    def test_lock_contention_is_nonblocking_and_other_errors_are_not_swallowed(self):
        self.kernel.LockFileEx.return_value = 0
        self.error = 33
        with self.assertRaises(BlockingIOError) as failure:
            self.backend.lock(7, shared=False)
        self.assertEqual(failure.exception.winerror, 33)
        self.error = 5
        with self.assertRaises(PermissionError):
            self.backend.lock(7, shared=False)
        self.kernel.UnlockFileEx.return_value = 0
        self.error = 158
        with self.assertRaises(OSError) as failure:
            self.backend.unlock(7)
        self.assertEqual(failure.exception.winerror, 158)

    def test_invalid_file_handles_never_reach_lock_api(self):
        for invalid in (-1, -2, ctypes.c_void_p(-1).value):
            backend = locking._WindowsBackend(self.kernel, lambda descriptor: invalid, lambda: 0)
            with self.assertRaises(OSError):
                backend.lock(7, shared=True)
        self.kernel.LockFileEx.assert_not_called()

    def test_windows_process_query_is_readonly_and_always_closes_handle(self):
        self.kernel.OpenProcess.return_value = self.handle
        for state, alive in ((0x102, True), (0, False)):
            self.kernel.WaitForSingleObject.return_value = state
            with patch.object(locking, "IS_WINDOWS", True), patch.object(locking, "_windows_backend", return_value=self.backend), patch.object(locking.os, "kill", side_effect=AssertionError("Windows must never use os.kill")):
                self.assertEqual(locking.process_alive(4321), alive)
            self.kernel.OpenProcess.assert_called_with(0x00100000, False, 4321)
            self.kernel.WaitForSingleObject.assert_called_with(self.handle, 0)
            self.kernel.CloseHandle.assert_called_with(self.handle)
        self.assertEqual(self.kernel.CloseHandle.call_count, 2)

    def test_process_not_found_access_denied_and_probe_errors(self):
        self.kernel.OpenProcess.return_value = None
        self.error = 87
        self.assertFalse(self.backend.process_alive(4321))
        self.error = 5
        self.assertTrue(self.backend.process_alive(4321))
        self.error = 6
        with self.assertRaises(OSError):
            self.backend.process_alive(4321)
        self.kernel.CloseHandle.assert_not_called()
        self.kernel.OpenProcess.return_value = self.handle
        self.kernel.WaitForSingleObject.return_value = 0xFFFFFFFF
        with self.assertRaises(OSError):
            self.backend.process_alive(4321)
        self.kernel.CloseHandle.assert_called_once_with(self.handle)

    def test_public_windows_dispatch_never_loads_posix_backend(self):
        with patch.object(locking, "IS_WINDOWS", True), patch.object(locking, "_windows_backend", return_value=self.backend):
            locking.lock_file(7, shared=True)
            locking.unlock_file(7)
        self.kernel.LockFileEx.assert_called_once()
        self.kernel.UnlockFileEx.assert_called_once()

    def test_pid_validation_precedes_any_native_call(self):
        with patch.object(locking, "IS_WINDOWS", True), patch.object(locking, "_windows_backend", return_value=self.backend):
            for value in (None, True, 0, -1, 1.5, "123", 0x100000000):
                with self.assertRaises(ValueError):
                    locking.process_alive(value)
        self.kernel.OpenProcess.assert_not_called()


if __name__ == "__main__":
    unittest.main()
