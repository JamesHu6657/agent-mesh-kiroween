"""Unit tests for mesh.platform.paths and mesh.platform.fs (Phase 1a / T02)."""

from __future__ import annotations

import ctypes
import errno
import ntpath
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from mesh.platform import fs, paths


class TestPlatformImportHygiene(unittest.TestCase):
    """Ensure importing mesh.platform on Linux never touches Windows-only modules."""

    @unittest.skipUnless(sys.platform != "win32", "POSIX import-hygiene check")
    def test_linux_import_never_touches_windows_modules(self) -> None:
        code = (
            "import ctypes, sys\n"
            "assert not hasattr(ctypes, 'windll')\n"
            "import mesh.platform\n"
            "from mesh.platform import paths, fs\n"
            "for mod in ('msvcrt', '_winapi', 'nt', 'mesh.platform._win32_ctypes'):\n"
            "    assert mod not in sys.modules, f'unexpectedly imported {mod}'\n"
        )
        repo_root = str(pathlib.Path(__file__).resolve().parents[2])
        env = dict(os.environ)
        env["PYTHONPATH"] = repo_root
        res = subprocess.run(
            [sys.executable, "-P", "-s", "-c", code],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, f"stderr={res.stderr}\nstdout={res.stdout}")


class TestPosixPlatformPathsFs(unittest.TestCase):
    """POSIX behavior checks ensuring Linux remains byte-identical."""

    @unittest.skipUnless(sys.platform != "win32", "POSIX-only behavior")
    def test_paths_posix_semantics(self) -> None:
        self.assertTrue(paths.is_fs_root("/"))
        self.assertFalse(paths.is_fs_root(""))
        self.assertFalse(paths.is_fs_root("/tmp"))
        self.assertFalse(paths.is_fs_root("///"))

        self.assertEqual(paths.norm_cmp("Foo/Bar/Baz"), "Foo/Bar/Baz")
        self.assertEqual(paths.temp_root(), "/tmp")
        with mock.patch.dict(os.environ, {"TMPDIR": "/var/tmp"}):
            self.assertEqual(paths.temp_root(), "/tmp")

        self.assertTrue(paths.has_traversal_segment("a/../b"))
        self.assertFalse(paths.has_traversal_segment("a\\..\\b"))

        with tempfile.TemporaryDirectory() as td:
            real_td = os.path.realpath(td)
            target = os.path.join(real_td, "target")
            os.makedirs(target)
            link = os.path.join(real_td, "link")
            os.symlink(target, link)
            sub = os.path.join(target, "sub")
            os.makedirs(sub)
            sibling = os.path.join(real_td, "target2")
            os.makedirs(sibling)

            self.assertTrue(paths.same_path(target, link))
            self.assertFalse(paths.same_path(target, sibling))

            self.assertTrue(paths.is_path_equal_or_parent(target, target))
            self.assertTrue(paths.is_path_equal_or_parent(target, sub))
            self.assertFalse(paths.is_path_equal_or_parent(target, sibling))
            self.assertFalse(paths.is_path_equal_or_parent(sub, target))

            self.assertTrue(paths.is_reparse_link(link))
            self.assertTrue(paths.is_reparse_link(link + "/"))
            self.assertTrue(paths.is_reparse_link(link + "//"))
            self.assertFalse(paths.is_reparse_link(target))
            self.assertFalse(paths.is_reparse_link(target + "/"))
            self.assertFalse(paths.is_reparse_link("/"))
            self.assertFalse(paths.is_reparse_link(os.path.join(real_td, "missing")))

            self.assertTrue(paths.is_reparse_stat(os.lstat(link)))
            self.assertFalse(paths.is_reparse_stat(os.stat(link)))
            self.assertFalse(paths.is_reparse_stat(os.lstat(target)))

            # Trailing-slash symlinked directory refusal & backslash-named POSIX dir preservation
            from mesh.adapters import smoke_schema
            from mesh.core import paths as core_paths
            from tests import tierlib

            self.assertIsNotNone(smoke_schema._has_symlink(os.path.join(target, "f.json"), root=link + "/"))
            repo_dir = os.path.join(real_td, "repo")
            os.makedirs(os.path.join(repo_dir, "tests", "fixtures", "real"))
            with self.assertRaises(tierlib.TierError):
                tierlib.plan_promotion(link + "/", repo_dir)

            bs_dir = os.path.join(real_td, "dir\\")
            os.makedirs(bs_dir)
            self.assertEqual(paths.strip_trailing_seps(bs_dir), bs_dir)
            self.assertEqual(paths.strip_trailing_seps(target + "/"), target)
            self.assertEqual(paths.strip_trailing_seps("/"), "/")
            self.assertEqual(paths.strip_trailing_seps("//"), "/")
            self.assertEqual(paths.strip_trailing_seps("///"), "/")
            self.assertEqual(paths.strip_trailing_seps(""), "")
            bs_file = os.path.join(bs_dir, "file.json")
            pathlib.Path(bs_file).write_text("{}\n", encoding="utf-8")
            self.assertIsNone(smoke_schema._has_symlink(bs_file, root=bs_dir))
            tierlib._check_no_symlinks(bs_file)

            # make_owned_ancestors_writable refuses "/" boundary identically to pre-T02 POSIX
            self.assertFalse(core_paths.make_owned_ancestors_writable(os.path.join(sub, "leaf"), "/"))
            with (
                mock.patch.object(core_paths, "resolve_trusted_path", return_value="/"),
                mock.patch.object(core_paths.os, "chmod", side_effect=AssertionError("chmod must not run")),
            ):
                self.assertFalse(core_paths.make_owned_ancestors_writable(os.path.join(sub, "leaf"), "/"))

    @unittest.skipUnless(sys.platform != "win32", "POSIX-only behavior")
    def test_fs_posix_atomic_read_fsync_nofollow(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "src.tmp")
            dst = os.path.join(td, "dst.txt")
            with open(dst, "wb") as f:
                f.write(b"old")
            with open(src, "wb") as f:
                f.write(b"new-payload")
            fs.atomic_replace(src, dst)
            fs.fsync_dir(td)
            self.assertFalse(os.path.exists(src))
            self.assertEqual(fs.read_bytes_shared(dst), b"new-payload")

            # Large read (> 64 KiB)
            big = os.path.join(td, "big.bin")
            big_data = b"x" * (130 * 1024)
            with open(big, "wb") as f:
                f.write(big_data)
            self.assertEqual(fs.read_bytes_shared(big), big_data)

            # open_regular_nofollow on regular file
            fd = fs.open_regular_nofollow(dst, os.O_RDONLY)
            try:
                self.assertEqual(os.read(fd, 64), b"new-payload")
            finally:
                os.close(fd)

            # Rejects symlinks with ELOOP
            sym = os.path.join(td, "sym.txt")
            os.symlink(dst, sym)
            with self.assertRaises(OSError) as ctx:
                fs.open_regular_nofollow(sym, os.O_RDONLY)
            self.assertEqual(ctx.exception.errno, errno.ELOOP)

            # Rejects directories with EINVAL (and preserves optional label message)
            subdir = os.path.join(td, "subdir")
            os.makedirs(subdir)
            with self.assertRaises(OSError) as ctx:
                fs.open_regular_nofollow(subdir, os.O_RDONLY, label="log file")
            self.assertEqual(ctx.exception.errno, errno.EINVAL)
            self.assertIn("Refusing non-regular log file", str(ctx.exception))

            # Rejects FIFOs without blocking
            fifo = os.path.join(td, "pipe")
            os.mkfifo(fifo)
            with self.assertRaises(OSError) as ctx:
                fs.open_regular_nofollow(fifo, os.O_RDONLY, label="activity file")
            self.assertEqual(ctx.exception.errno, errno.EINVAL)
            self.assertIn("Refusing non-regular activity file", str(ctx.exception))


class TestWindowsPurePlatformLogic(unittest.TestCase):
    """Cross-platform unit tests exercising pure Windows branch logic on Linux & Windows."""

    def test_drive_and_unc_root_detection_with_ntpath(self) -> None:
        for root_path in (
            "C:\\",
            "C:/",
            "D:\\",
            "\\\\server\\share\\",
            "\\\\server\\share",
            "//server/share",
            "//server/share/",
            "\\\\?\\C:\\",
            "\\\\?\\UNC\\server\\share\\",
            "/",
            "\\",
        ):
            self.assertTrue(
                paths.is_fs_root_win(root_path, pathmod=ntpath),
                f"Expected root True for {root_path!r}",
            )

        for non_root in (
            "",
            "C:",
            "C:\\Users",
            "C:/Users/runneradmin",
            "\\\\server\\share\\sub",
            "relative\\path",
        ):
            self.assertFalse(
                paths.is_fs_root_win(non_root, pathmod=ntpath),
                f"Expected root False for {non_root!r}",
            )

    def test_norm_cmp_same_path_and_containment_with_ntpath(self) -> None:
        self.assertEqual(
            paths.norm_cmp_win("C:/Users/RunnerAdmin/Mesh", pathmod=ntpath),
            "c:\\users\\runneradmin\\mesh",
        )
        self.assertTrue(
            paths.same_path_win(
                "C:\\Repo\\Work",
                "c:/repo/work",
                realpath_fn=ntpath.normpath,
                pathmod=ntpath,
            )
        )
        self.assertTrue(
            paths.is_path_equal_or_parent_win(
                "C:\\Repo",
                "c:/repo/sub/file.py",
                realpath_fn=ntpath.normpath,
                pathmod=ntpath,
            )
        )
        self.assertFalse(
            paths.is_path_equal_or_parent_win(
                "C:\\Repo",
                "C:\\Repo2\\file.py",
                realpath_fn=ntpath.normpath,
                pathmod=ntpath,
            )
        )
        self.assertFalse(
            paths.is_path_equal_or_parent_win(
                "C:\\Repo",
                "D:\\Repo\\file.py",
                realpath_fn=ntpath.normpath,
                pathmod=ntpath,
            )
        )
        self.assertTrue(
            paths.is_path_equal_or_parent_win(
                "\\\\Server\\Share",
                "//server/share/dir",
                realpath_fn=ntpath.normpath,
                pathmod=ntpath,
            )
        )

    def test_mixed_separator_traversal_rejection(self) -> None:
        for bad in (
            "a/../b",
            "a\\..\\b",
            "a\\../b",
            "a/..\\b",
            "C:/foo\\..\\bar",
            "..",
            "C:..",
            "C:..\\x",
            "C:../x",
            "c:..\\bar",
            "C:foo/..",
        ):
            self.assertTrue(paths.has_traversal_segment_win(bad), f"Expected traversal for {bad!r}")
        for ok in (
            "C:\\foo/bar\\baz",
            "C:/foo/..hidden/file",
            "sub\\dir/name..txt",
            "C:foo",
            "C:..hidden/file",
            "C:",
        ):
            self.assertFalse(paths.has_traversal_segment_win(ok), f"Unexpected traversal for {ok!r}")
        self.assertEqual(paths.strip_trailing_seps_win("C:\\", pathmod=ntpath), "C:\\")
        self.assertEqual(paths.strip_trailing_seps_win("C:\\foo\\/", pathmod=ntpath), "C:\\foo")
        self.assertEqual(paths.strip_trailing_seps_win("\\\\server\\share\\", pathmod=ntpath), "\\\\server\\share\\")

    def test_reparse_stat_attributes(self) -> None:
        attr_reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        attr_dir = getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0x10)
        st_symlink = SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_file_attributes=0)
        st_junction = SimpleNamespace(
            st_mode=stat.S_IFDIR | 0o755,
            st_file_attributes=attr_dir | attr_reparse,
        )
        st_plain_dir = SimpleNamespace(
            st_mode=stat.S_IFDIR | 0o755,
            st_file_attributes=attr_dir,
        )
        self.assertTrue(paths.is_reparse_stat_win(st_symlink))
        self.assertTrue(paths.is_reparse_stat_win(st_junction))
        self.assertFalse(paths.is_reparse_stat_win(st_plain_dir))

    def test_atomic_replace_win_backoff_retry(self) -> None:
        sleeps = []
        attempts = 0

        def flaky_replace(src: str, dst: str) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                exc = PermissionError("sharing violation")
                exc.winerror = 32  # type: ignore[attr-defined]
                raise exc
            if attempts == 2:
                exc = PermissionError("access denied")
                exc.winerror = 5  # type: ignore[attr-defined]
                raise exc

        fs.atomic_replace_win("a", "b", replace_fn=flaky_replace, sleep_fn=sleeps.append)
        self.assertEqual(attempts, 3)
        self.assertEqual(sleeps, [0.002, 0.005])

        # Exhausting all 5 retries raises on the 6th attempt
        sleeps_exhaust = []
        calls_exhaust = 0

        def always_locked(src: str, dst: str) -> None:
            nonlocal calls_exhaust
            calls_exhaust += 1
            exc = PermissionError("locked")
            exc.winerror = 32  # type: ignore[attr-defined]
            raise exc

        with self.assertRaises(PermissionError):
            fs.atomic_replace_win("a", "b", replace_fn=always_locked, sleep_fn=sleeps_exhaust.append)
        self.assertEqual(calls_exhaust, 6)
        self.assertEqual(sleeps_exhaust, [0.002, 0.005, 0.010, 0.020, 0.040])

        # Non-5/32 errors fail immediately without sleeping
        sleeps_fast = []

        def missing(src: str, dst: str) -> None:
            exc = FileNotFoundError("gone")
            exc.winerror = 2  # type: ignore[attr-defined]
            raise exc

        with self.assertRaises(FileNotFoundError):
            fs.atomic_replace_win("a", "b", replace_fn=missing, sleep_fn=sleeps_fast.append)
        self.assertEqual(sleeps_fast, [])
        self.assertIsNone(fs.fsync_dir_win("/any/dir"))

    def test_win32_ctypes_binding_and_handle_cleanup(self) -> None:
        from ctypes import wintypes
        from mesh.platform import _win32_ctypes as w32

        class _FakeFn:
            pass

        class _FakeDll:
            def __init__(self) -> None:
                self.CreateFileW = _FakeFn()
                self.CloseHandle = _FakeFn()
                self.ReadFile = _FakeFn()
                self.GetFileType = _FakeFn()
                self.GetFileInformationByHandle = _FakeFn()
                self.SetFilePointerEx = _FakeFn()
                self.SetEndOfFile = _FakeFn()

        dll = w32.bind_kernel32(_FakeDll())
        self.assertEqual(dll.CreateFileW.restype, wintypes.HANDLE)
        self.assertEqual(len(dll.CreateFileW.argtypes), 7)
        self.assertEqual(dll.CloseHandle.restype, wintypes.BOOL)
        self.assertEqual(dll.GetFileType.restype, wintypes.DWORD)
        self.assertEqual(dll.GetFileInformationByHandle.restype, wintypes.BOOL)
        self.assertEqual(dll.SetFilePointerEx.restype, wintypes.BOOL)
        self.assertEqual(dll.SetEndOfFile.restype, wintypes.BOOL)

        # Verify handle is closed on every error path of open_regular_nofollow_win and read_bytes_shared_win
        closed = []
        created_args = []

        class _MockKernel:
            def __init__(self, *, ftype=w32.FILE_TYPE_DISK, attrs=w32.FILE_ATTRIBUTE_NORMAL, fail_read=False):
                self.ftype = ftype
                self.attrs = attrs
                self.fail_read = fail_read
                self.truncated = False

            def CreateFileW(self, *args):
                created_args.append(args)
                return 4242

            def CloseHandle(self, h):
                closed.append(h)
                return 1

            def GetFileType(self, h):
                return self.ftype

            def GetFileInformationByHandle(self, h, info_ptr):
                info = ctypes.cast(info_ptr, ctypes.POINTER(w32.BY_HANDLE_FILE_INFORMATION)).contents
                info.dwFileAttributes = self.attrs
                return 1

            def SetFilePointerEx(self, h, dist, new_ptr, method):
                return 1

            def SetEndOfFile(self, h):
                self.truncated = True
                return 1

            def ReadFile(self, h, buf, size, nread_ptr, ov):
                if self.fail_read:
                    return 0
                nread = ctypes.cast(nread_ptr, wintypes.LPDWORD).contents
                if hasattr(self, "chunks"):
                    if not self.chunks:
                        nread.value = 0
                        return 1
                    chunk = self.chunks.pop(0)
                    ctypes.memmove(buf, chunk, len(chunk))
                    nread.value = len(chunk)
                    return 1
                nread.value = 0
                return 1

        fake_msvcrt_fail = SimpleNamespace(
            open_osfhandle=mock.Mock(side_effect=OSError(errno.EMFILE, "too many"))
        )

        # 1. Reparse point rejected with ELOOP, handle closed, NEVER truncated even with O_CREAT|O_TRUNC
        closed.clear()
        k_reparse = _MockKernel(attrs=w32.FILE_ATTRIBUTE_REPARSE_POINT)
        with self.assertRaises(OSError) as ctx:
            fs.open_regular_nofollow_win(
                "C:\\junc", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, k32=k_reparse, msvcrt_mod=fake_msvcrt_fail
            )
        self.assertEqual(ctx.exception.errno, errno.ELOOP)
        self.assertEqual(closed, [4242])
        self.assertFalse(k_reparse.truncated)
        # Ensure OPEN_ALWAYS (4) was used rather than CREATE_ALWAYS (2)
        self.assertEqual(created_args[-1][4], w32.OPEN_ALWAYS)
        self.assertTrue(created_args[-1][5] & w32.FILE_FLAG_OPEN_REPARSE_POINT)

        # 2. Non-disk file (e.g. pipe) rejected with EINVAL, handle closed
        closed.clear()
        with self.assertRaises(OSError) as ctx:
            fs.open_regular_nofollow_win(
                "\\\\.\\pipe\\x",
                os.O_RDONLY,
                label="log file",
                k32=_MockKernel(ftype=w32.FILE_TYPE_PIPE),
                msvcrt_mod=fake_msvcrt_fail,
            )
        self.assertEqual(ctx.exception.errno, errno.EINVAL)
        self.assertIn("Refusing non-regular log file", str(ctx.exception))
        self.assertEqual(closed, [4242])

        # 3. Directory rejected with EINVAL, handle closed
        closed.clear()
        with self.assertRaises(OSError) as ctx:
            fs.open_regular_nofollow_win(
                "C:\\dir",
                os.O_RDONLY,
                label="activity file",
                k32=_MockKernel(attrs=w32.FILE_ATTRIBUTE_DIRECTORY),
                msvcrt_mod=fake_msvcrt_fail,
            )
        self.assertEqual(ctx.exception.errno, errno.EINVAL)
        self.assertIn("Refusing non-regular activity file", str(ctx.exception))
        self.assertEqual(closed, [4242])

        # 4. open_osfhandle error closes raw Win32 handle
        closed.clear()
        with self.assertRaises(OSError):
            fs.open_regular_nofollow_win(
                "C:\\ok.txt", os.O_RDONLY, k32=_MockKernel(), msvcrt_mod=fake_msvcrt_fail
            )
        self.assertEqual(closed, [4242])

        # 5. Success with O_TRUNC truncates after checks and transfers handle to open_osfhandle with O_NOINHERIT
        closed.clear()
        k_ok = _MockKernel()
        fake_msvcrt_ok = SimpleNamespace(open_osfhandle=mock.Mock(return_value=99))
        with (
            mock.patch.object(fs.os, "O_NOINHERIT", 0x0080, create=True),
            mock.patch.object(fs.os, "O_BINARY", 0x8000, create=True),
        ):
            fd = fs.open_regular_nofollow_win(
                "C:\\ok.txt", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, k32=k_ok, msvcrt_mod=fake_msvcrt_ok
            )
        self.assertEqual(fd, 99)
        self.assertTrue(k_ok.truncated)
        self.assertEqual(closed, [])
        osf_call_flags = fake_msvcrt_ok.open_osfhandle.call_args[0][1]
        self.assertTrue(osf_call_flags & 0x0080, f"Expected O_NOINHERIT in osf_flags, got {osf_call_flags:#x}")
        self.assertTrue(osf_call_flags & 0x8000, f"Expected O_BINARY in osf_flags, got {osf_call_flags:#x}")

        # 5b. CreateFileW returning wintypes.HANDLE (c_void_p) is unwrapped via .value
        from ctypes import wintypes as _wintypes

        class _HandleMockKernel(_MockKernel):
            def CreateFileW(self, *args):
                created_args.append(args)
                return _wintypes.HANDLE(4242)

        def _strict_int_open_osfhandle(h_val: int, osf_flags: int) -> int:
            if not isinstance(h_val, int) or isinstance(h_val, bool):
                raise TypeError(f"expected int handle, got {type(h_val).__name__}")
            return 100 + h_val

        fake_msvcrt_strict = SimpleNamespace(open_osfhandle=mock.Mock(side_effect=_strict_int_open_osfhandle))
        with (
            mock.patch.object(fs.os, "O_NOINHERIT", 0x0080, create=True),
            mock.patch.object(fs.os, "O_BINARY", 0x8000, create=True),
        ):
            fd_h = fs.open_regular_nofollow_win(
                "C:\\handle.txt", os.O_RDONLY, k32=_HandleMockKernel(), msvcrt_mod=fake_msvcrt_strict
            )
        self.assertEqual(fd_h, 4342)
        self.assertIs(type(fake_msvcrt_strict.open_osfhandle.call_args[0][0]), int)
        self.assertEqual(fake_msvcrt_strict.open_osfhandle.call_args[0][0], 4242)

        self.assertTrue(w32.is_invalid_handle(_wintypes.HANDLE(-1)))
        self.assertTrue(w32.is_invalid_handle(_wintypes.HANDLE(0)))
        self.assertTrue(w32.is_invalid_handle(_wintypes.HANDLE(None)))
        self.assertTrue(w32.is_invalid_handle(0))
        self.assertFalse(w32.is_invalid_handle(_wintypes.HANDLE(4242)))

        k_handle_inv = SimpleNamespace(
            CreateFileW=mock.Mock(return_value=_wintypes.HANDLE(-1)),
            CloseHandle=mock.Mock(side_effect=AssertionError("CloseHandle must not be called")),
        )
        with self.assertRaises(OSError):
            fs.open_regular_nofollow_win(
                "C:\\missing_h.txt", os.O_RDONLY, k32=k_handle_inv, msvcrt_mod=fake_msvcrt_strict
            )

        # 6. read_bytes_shared_win passes FILE_SHARE_READ|WRITE|DELETE and closes handle on error
        closed.clear()
        created_args.clear()
        with self.assertRaises(OSError):
            fs.read_bytes_shared_win("C:\\err.txt", k32=_MockKernel(fail_read=True))
        self.assertEqual(
            created_args[-1][2],
            w32.FILE_SHARE_READ | w32.FILE_SHARE_WRITE | w32.FILE_SHARE_DELETE,
        )
        self.assertEqual(closed, [4242])

        # 6b. read_bytes_shared_win concatenates multi-chunk reads and closes handle
        closed.clear()
        k_chunks = _MockKernel()
        k_chunks.chunks = [b"hello ", b"world"]
        self.assertEqual(fs.read_bytes_shared_win("C:\\ok.txt", k32=k_chunks), b"hello world")
        self.assertEqual(closed, [4242])

        # 7. INVALID_HANDLE_VALUE never invokes CloseHandle and raises WinError OSError
        closed.clear()
        k_bad_handle = SimpleNamespace(
            CreateFileW=mock.Mock(return_value=w32.INVALID_HANDLE_VALUE),
            CloseHandle=mock.Mock(side_effect=AssertionError("CloseHandle must not be called")),
        )
        with self.assertRaises(OSError):
            fs.read_bytes_shared_win("C:\\missing.txt", k32=k_bad_handle)
        with self.assertRaises(OSError):
            fs.open_regular_nofollow_win("C:\\missing.txt", os.O_RDONLY, k32=k_bad_handle, msvcrt_mod=fake_msvcrt_ok)

        # 8. O_RDONLY | O_TRUNC rejected cleanly before opening
        with self.assertRaises(OSError) as ctx:
            fs.open_regular_nofollow_win("C:\\ro.txt", os.O_RDONLY | os.O_TRUNC, k32=_MockKernel(), msvcrt_mod=fake_msvcrt_ok)
        self.assertEqual(ctx.exception.errno, errno.EINVAL)


class TestWindowsNativePlatformPathsFs(unittest.TestCase):
    """Real Win32 syscall tests executed on Windows CI."""

    @unittest.skipUnless(sys.platform == "win32", "Windows-only native syscall tests")
    def test_win32_drive_roots_and_temp_root(self) -> None:
        drive, _ = os.path.splitdrive(os.path.abspath("."))
        self.assertTrue(paths.is_fs_root(drive + "\\"))
        self.assertTrue(paths.is_fs_root(drive + "/"))
        self.assertFalse(paths.is_fs_root(os.path.abspath(".")))
        self.assertTrue(paths.has_traversal_segment("a/..\\b"))

        tr = paths.temp_root()
        self.assertEqual(tr, os.path.realpath(tempfile.gettempdir()))
        # Verify 8.3 short name like RUNNER~1 is expanded when realpath expands it
        self.assertTrue(paths.same_path(tr, tempfile.gettempdir()))

    @unittest.skipUnless(sys.platform == "win32", "Windows-only native 8.3 short path test")
    def test_win32_short_path_canonicalization(self) -> None:
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetShortPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        k32.GetShortPathNameW.restype = wintypes.DWORD

        with tempfile.TemporaryDirectory(dir=paths.temp_root()) as td:
            long_dir = os.path.join(td, "very long directory name for 8dot3 check")
            os.makedirs(long_dir)
            buf = ctypes.create_unicode_buffer(1024)
            n = k32.GetShortPathNameW(long_dir, buf, len(buf))
            self.assertGreater(n, 0)
            short_dir = buf.value
            # Whether 8.3 generation is enabled on this volume or not, same_path & norm_cmp match
            self.assertTrue(paths.same_path(short_dir, long_dir))
            child = os.path.join(long_dir, "SubFile.txt")
            pathlib.Path(child).write_text("ok", encoding="utf-8")
            self.assertTrue(paths.is_path_equal_or_parent(short_dir, child.lower().replace("\\", "/")))

    @unittest.skipUnless(sys.platform == "win32", "Windows-only NTFS junction test")
    def test_win32_ntfs_junction_rejected(self) -> None:
        import _winapi

        with tempfile.TemporaryDirectory(dir=paths.temp_root()) as td:
            target = os.path.join(td, "target_dir")
            os.makedirs(target)
            secret = os.path.join(target, "secret.txt")
            pathlib.Path(secret).write_text("secret", encoding="utf-8")
            junc = os.path.join(td, "junc_dir")
            _winapi.CreateJunction(target, junc)

            self.assertTrue(paths.is_reparse_link(junc))
            self.assertTrue(paths.is_reparse_link(junc + "\\"))
            self.assertTrue(paths.is_reparse_link(junc + "/"))
            self.assertTrue(paths.is_reparse_stat(os.lstat(junc)))
            self.assertFalse(paths.is_reparse_link(target))
            self.assertFalse(paths.is_reparse_stat(os.lstat(target)))

            with self.assertRaises(OSError) as ctx:
                fs.open_regular_nofollow(junc, os.O_RDONLY)
            self.assertEqual(ctx.exception.errno, errno.ELOOP)

            with self.assertRaises(OSError) as ctx2:
                fs.open_regular_nofollow(junc, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
            self.assertEqual(ctx2.exception.errno, errno.ELOOP)
            self.assertEqual(pathlib.Path(secret).read_text(encoding="utf-8"), "secret")

    @unittest.skipUnless(sys.platform == "win32", "Windows-only sharing violation & fs test")
    def test_win32_sharing_violation_retry_and_fs_ops(self) -> None:
        with tempfile.TemporaryDirectory(dir=paths.temp_root()) as td:
            src = os.path.join(td, "src.json")
            dst = os.path.join(td, "dst.json")
            pathlib.Path(dst).write_bytes(b"initial")
            pathlib.Path(src).write_bytes(b"updated")

            # Hold dst open via Python CRT open() (which omits FILE_SHARE_DELETE) briefly,
            # forcing WinError 32/5 before releasing so atomic_replace succeeds via backoff.
            holder_ready = threading.Event()
            holder_err: list[BaseException] = []

            def hold_briefly() -> None:
                try:
                    with open(dst, "rb") as f:
                        _ = f.read()
                        holder_ready.set()
                        time.sleep(0.005)
                except BaseException as exc:
                    holder_err.append(exc)
                    holder_ready.set()

            t = threading.Thread(target=hold_briefly, daemon=True)
            t.start()
            self.assertTrue(holder_ready.wait(timeout=2.0))
            fs.atomic_replace(src, dst)
            t.join(timeout=2.0)
            self.assertEqual(holder_err, [])
            self.assertEqual(fs.read_bytes_shared(dst), b"updated")

            # fsync_dir is a safe no-op on Windows
            fs.fsync_dir(td)

            # open_regular_nofollow creates, writes, truncates, and reads regular files (non-inheritable)
            reg = os.path.join(td, "reg.txt")
            fd = fs.open_regular_nofollow(reg, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            try:
                self.assertFalse(os.get_inheritable(fd))
                os.write(fd, b"hello")
            finally:
                os.close(fd)
            fd2 = fs.open_regular_nofollow(reg, os.O_RDONLY)
            try:
                self.assertFalse(os.get_inheritable(fd2))
                self.assertEqual(os.read(fd2, 64), b"hello")
            finally:
                os.close(fd2)

            # Rejects plain directory and NUL device
            with self.assertRaises(OSError):
                fs.open_regular_nofollow(td, os.O_RDONLY)
            with self.assertRaises(OSError):
                fs.open_regular_nofollow("NUL", os.O_RDONLY)

            # Real Windows: wintypes.HANDLE returned from CreateFileW unwraps cleanly into real msvcrt.open_osfhandle
            from ctypes import wintypes as _wintypes
            from mesh.platform import _win32_ctypes as _w32

            real_k32 = _w32.get_kernel32()

            class _WrappedHandleK32:
                def __getattr__(self, name: str):
                    return getattr(real_k32, name)

                def CreateFileW(self, *args):
                    return _wintypes.HANDLE(real_k32.CreateFileW(*args))

            fd3 = fs.open_regular_nofollow_win(reg, os.O_RDONLY, k32=_WrappedHandleK32())
            try:
                self.assertEqual(os.read(fd3, 64), b"hello")
            finally:
                os.close(fd3)


class TestPhase1bEntryPathsFsync(unittest.TestCase):
    """Cross-platform and native Windows tests for Phase 1b / T03."""

    def test_bin_mesh_cmd_exists_with_crlf_and_isolation_vars(self) -> None:
        repo_root = pathlib.Path(__file__).resolve().parents[2]
        cmd_path = repo_root / "bin" / "mesh.cmd"
        self.assertTrue(cmd_path.is_file(), f"Missing {cmd_path}")
        raw = cmd_path.read_bytes()
        self.assertIn(b"\r\n", raw, "bin/mesh.cmd must use CRLF line endings")
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""), "bin/mesh.cmd must not contain bare LF")
        text = raw.decode("utf-8")
        self.assertIn("_MESH_ORIG_PYTHONPATH", text)
        self.assertIn("PYTHONPATH", text)
        self.assertIn("-P -s -m mesh %*", text)
        self.assertIn("\\Microsoft\\WindowsApps\\", text)
        self.assertIn("py.exe", text)
        # Ensure exit /b %ERRORLEVEL% is never inside a parenthesized block.
        depth = 0
        for line in text.splitlines():
            if "exit /b %ERRORLEVEL%" in line:
                self.assertEqual(depth, 0, f"Parse-time %ERRORLEVEL% inside block: {line!r}")
            depth += line.count("(") - line.count(")")

    def test_entry_win_abspath_fallback_drive_relative(self) -> None:
        from mesh.cli import entry

        fake_nt_no_getfull = SimpleNamespace(getcwd=lambda: "C:\\Work")
        with (
            mock.patch.object(entry.sys, "platform", "win32"),
            mock.patch.object(entry, "nt", fake_nt_no_getfull, create=True),
        ):
            self.assertEqual(entry._win_abspath("C:foo"), "C:\\Work\\foo")
            self.assertEqual(entry._win_abspath("c:foo/bar"), "C:\\Work\\foo\\bar")
            self.assertEqual(entry._win_abspath("C:"), "C:\\Work")
            self.assertEqual(entry._win_abspath("D:foo"), "D:foo")
            self.assertEqual(entry._win_abspath("C:\\abs\\path"), "C:\\abs\\path")
            self.assertEqual(entry._win_abspath("\\rooted"), "C:\\rooted")
            self.assertEqual(entry._win_abspath("rel/path"), "C:\\Work\\rel\\path")

        fake_nt_root_cwd = SimpleNamespace(
            getcwd=lambda: "C:\\",
            _getfullpathname=mock.Mock(side_effect=OSError("unavailable")),
        )
        with (
            mock.patch.object(entry.sys, "platform", "win32"),
            mock.patch.object(entry, "nt", fake_nt_root_cwd, create=True),
        ):
            self.assertEqual(entry._win_abspath("C:foo"), "C:\\foo")
            self.assertEqual(entry._win_abspath("C:"), "C:\\")
            self.assertEqual(entry._win_abspath("D:foo"), "D:foo")

    def test_entry_win_quote_cmdline_matches_list2cmdline(self) -> None:
        from mesh.cli import entry

        samples = [
            ["C:\\Python312\\python.exe", "-P", "-s", "-m", "mesh", "--version"],
            ["C:\\Program Files\\Python312\\python.exe", "arg with spaces", 'say "hi"', "", "a\tb", "trail\\"],
            ['C:\\path\\"quoted"\\x\\', "plain", '\\\\"escaped'],
        ]
        for argv in samples:
            self.assertEqual(entry._win_quote_cmdline(argv), subprocess.list2cmdline(argv))

    def test_entry_win_realpath_site_and_createprocess_mocks(self) -> None:
        from mesh.cli import entry

        fake_finals = {
            "C:\\Repo\\mesh": "\\\\?\\C:\\Repo\\mesh",
            "c:\\python312": "\\\\?\\C:\\Python312",
            "C:\\Python312\\Lib\\site.py": "\\\\?\\C:\\Python312\\Lib\\site.py",
            "C:\\Python312\\Lib\\site-packages": "\\\\?\\C:\\Python312\\Lib\\site-packages",
            "\\\\server\\share\\mesh": "\\\\?\\UNC\\server\\share\\mesh",
        }

        def fake_getfinal(p: str) -> str:
            if p in fake_finals:
                return fake_finals[p]
            raise FileNotFoundError(p)

        def fake_getfull(p: str) -> str:
            p = p.replace("/", "\\")
            if p.startswith(("C:\\", "c:\\", "\\\\")):
                return p
            return "C:\\Work\\" + p

        fake_nt = SimpleNamespace(
            _getfinalpathname=fake_getfinal,
            _getfullpathname=fake_getfull,
            getcwd=lambda: "C:\\Work",
            environ={"Path": "C:\\Windows", "PythonPath": "C:\\Evil;C:\\Repo"},
        )

        closed_handles = []
        waited = []
        created = []

        class _FakeWinApi:
            INFINITE = -1

            @staticmethod
            def CreateProcess(app, cmdline, pa, ta, inherit, flags, env, cwd, si):
                created.append((app, cmdline, inherit, flags, dict(env), cwd, si))
                return (101, 202, 3333, 4444)

            @staticmethod
            def CloseHandle(h):
                closed_handles.append(h)

            @staticmethod
            def WaitForSingleObject(h, ms):
                waited.append((h, ms))
                return 0

            @staticmethod
            def GetExitCodeProcess(h):
                self.assertEqual(h, 101)
                return 7

        fake_site = SimpleNamespace(
            __file__="c:/Python312/Lib/site.py",
            getsitepackages=lambda: ["C:\\Python312", "C:\\Python312\\Lib\\site-packages"],
        )
        fake_mesh = SimpleNamespace(__path__=["C:\\Repo\\mesh"])

        with (
            mock.patch.object(entry.sys, "platform", "win32"),
            mock.patch.object(entry, "nt", fake_nt, create=True),
            mock.patch.object(entry, "_winapi", _FakeWinApi, create=True),
            mock.patch.object(entry.sys, "executable", "C:\\Program Files\\Python312\\python.exe"),
            mock.patch.object(entry.sys, "argv", ["mesh", "status", "--format", "json"]),
            mock.patch.object(entry.sys, "base_prefix", "c:/python312"),
            mock.patch.object(entry.sys, "base_exec_prefix", "c:/python312"),
            mock.patch.object(entry.sys, "flags", SimpleNamespace(safe_path=0, no_user_site=0, no_site=0)),
            mock.patch.dict(entry.sys.modules, {"site": fake_site, "mesh": fake_mesh}),
        ):
            self.assertEqual(entry._resolve_realpath("\\\\server\\share\\mesh"), "\\\\server\\share\\mesh")
            self.assertEqual(
                entry._resolve_realpath("C:/Python312/lib/python3.12/site.py"),
                "C:\\Python312\\lib\\python3.12\\site.py",
            )
            self.assertEqual(entry._mesh_install_root(), "C:\\Repo")
            self.assertIs(entry._stdlib_site_module(), fake_site)
            self.assertTrue(entry._is_site_packages_dir("c:/python312/lib/site-packages"))

            with self.assertRaises(SystemExit) as cm:
                entry.ensure_isolated_process()
            self.assertEqual(cm.exception.code, 7)
            self.assertEqual(closed_handles, [202, 101])
            self.assertEqual(waited, [(101, -1)])
            self.assertEqual(len(created), 1)
            app, cmdline, inherit, flags, child_env, cwd, si = created[0]
            self.assertEqual(app, "C:\\Program Files\\Python312\\python.exe")
            self.assertIn("-P -s -m mesh status --format json", cmdline)
            self.assertEqual(inherit, 1)
            self.assertEqual(flags, 0)
            self.assertIsNone(cwd)
            self.assertIsNone(si)
            self.assertEqual(child_env["_MESH_ORIG_PYTHONPATH"], "C:\\Evil;C:\\Repo")
            self.assertEqual(child_env["PYTHONPATH"], "C:\\Repo")
            self.assertNotIn("PythonPath", child_env)

            # Handle hp is still closed even if WaitForSingleObject raises.
            closed_handles.clear()
            with mock.patch.object(_FakeWinApi, "WaitForSingleObject", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    entry.ensure_isolated_process()
            self.assertEqual(closed_handles, [202, 101])

            # Already isolated when inside site-packages strips PYTHONPATH and returns without CreateProcess.
            created.clear()
            fake_mesh_sp = SimpleNamespace(__path__=["C:\\Python312\\Lib\\site-packages\\mesh"])
            fake_nt_iso = SimpleNamespace(
                _getfinalpathname=fake_getfinal,
                _getfullpathname=fake_getfull,
                getcwd=lambda: "C:\\Work",
                environ={"Path": "C:\\Windows", "_MESH_ORIG_PYTHONPATH": "__UNSET__"},
            )
            with (
                mock.patch.object(entry, "nt", fake_nt_iso, create=True),
                mock.patch.object(entry.sys, "flags", SimpleNamespace(safe_path=1, no_user_site=1, no_site=0)),
                mock.patch.dict(entry.sys.modules, {"mesh": fake_mesh_sp}),
            ):
                entry.ensure_isolated_process()
            self.assertEqual(created, [])

    def test_mesh_cli_argv_and_win_shim_builder(self) -> None:
        from mesh.core import paths as core_paths

        self.assertEqual(core_paths.mesh_cli_argv(), [sys.executable, "-P", "-s", "-m", "mesh"])
        self.assertEqual(
            core_paths.mesh_cli_argv("--home", "H", "version"),
            [sys.executable, "-P", "-s", "-m", "mesh", "--home", "H", "version"],
        )

        cmd_custom = core_paths._build_win_mesh_cmd_body("C:\\Users\\a%b\\mesh", "C:\\Py\\python.exe", False)
        self.assertIn('set "PYTHONPATH=C:\\Users\\a%%b\\mesh"\r\n', cmd_custom)
        self.assertIn('"C:\\Py\\python.exe" -P -s -m mesh %*\r\n', cmd_custom)
        self.assertIn("exit /b %ERRORLEVEL%\r\n", cmd_custom)

        cmd_site = core_paths._build_win_mesh_cmd_body("C:\\Py\\Lib\\site-packages", "C:\\Py\\python.exe", True)
        self.assertIn('set "PYTHONPATH="\r\n', cmd_site)

    def test_win_trusted_paths_match_8dot3_and_rejects_mismatches(self) -> None:
        from mesh.core import paths as core_paths

        attr_dir = getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0x10)
        attr_rep = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        plain_a = SimpleNamespace(st_dev=1, st_ino=99, st_file_attributes=attr_dir, st_mode=stat.S_IFDIR)
        plain_b = SimpleNamespace(st_dev=1, st_ino=99, st_file_attributes=attr_dir, st_mode=stat.S_IFDIR)
        rep_a = SimpleNamespace(st_dev=1, st_ino=99, st_file_attributes=attr_dir | attr_rep, st_mode=stat.S_IFDIR)

        self.assertTrue(
            core_paths._win_trusted_paths_match(
                "c:\\users\\RUNNER~1\\AppData\\Local\\Temp\\work",
                "C:\\Users\\runneradmin\\AppData\\Local\\Temp\\work",
                pathmod=ntpath,
                lstat_fn=lambda p: plain_a if "RUNNER~1" in p else plain_b,
                is_reparse_stat_fn=paths.is_reparse_stat_win,
                same_path_fn=lambda a, b: True,
            )
        )
        # Reparse point disguised as 8.3 short name is rejected
        self.assertFalse(
            core_paths._win_trusted_paths_match(
                "C:\\Users\\RUNNER~1\\Temp",
                "C:\\Users\\runneradmin\\Temp",
                pathmod=ntpath,
                lstat_fn=lambda p: rep_a if "RUNNER~1" in p else plain_b,
                is_reparse_stat_fn=paths.is_reparse_stat_win,
                same_path_fn=lambda a, b: True,
            )
        )
        # Non-8.3 mismatch (e.g. symlink target) is rejected
        self.assertFalse(
            core_paths._win_trusted_paths_match(
                "C:\\repo\\link\\sub",
                "C:\\outside\\target\\sub",
                pathmod=ntpath,
                lstat_fn=lambda p: plain_a,
                is_reparse_stat_fn=paths.is_reparse_stat_win,
                same_path_fn=lambda a, b: True,
            )
        )

    def test_fsync_dir_wiring_and_stop_roots(self) -> None:
        from mesh.adapters import smoke_schema
        from mesh.core import repos
        from mesh.daemon import merge, supervisor
        from tests import tierlib

        self.assertIs(repos.fsync_dir, fs.fsync_dir)
        self.assertIs(supervisor.fsync_dir, fs.fsync_dir)
        self.assertIs(merge.fsync_dir, fs.fsync_dir)

        with tempfile.TemporaryDirectory() as td:
            home = os.path.join(td, "home")
            os.makedirs(home, exist_ok=True)
            with mock.patch.object(repos, "fsync_dir") as mock_fsync:
                repos.write_deliver_intent(home, {"project_id": "prj_1"})
            mock_fsync.assert_called_once_with(os.path.join(home, repos.DELIVER_INTENT_DIR))

            run_dir = os.path.join(td, "run")
            os.makedirs(run_dir, exist_ok=True)
            with mock.patch.object(merge, "fsync_dir") as mock_fsync:
                merge._save_commit_ready(
                    run_dir,
                    task_id="tsk_1",
                    pre_head="a",
                    tip="b",
                    symref="refs/heads/main",
                    message="msg",
                )
            mock_fsync.assert_called_once_with(run_dir)

            with mock.patch.object(supervisor, "fsync_dir") as mock_fsync:
                supervisor.write_unusable_marker({"id": "run_1", "run_dir": run_dir, "pgid": 0}, "adopt_pending")
            mock_fsync.assert_called_once_with(run_dir)

            ab = supervisor._SpawnAbort(mock.Mock(), "run_2", (), run_dir=run_dir, home=home)
            ab.known.add((1234, 5678))
            with mock.patch.object(supervisor, "fsync_dir") as mock_fsync:
                supervisor._persist_abort_members(ab)
            mock_fsync.assert_called_once_with(run_dir)

            # Stop roots accept files inside temp_root() without error.
            sample = os.path.join(td, "sample.txt")
            pathlib.Path(sample).write_text("ok\n", encoding="utf-8")
            self.assertIsNone(smoke_schema._has_symlink(sample))
            tierlib._check_no_symlinks(sample)

    @unittest.skipUnless(sys.platform == "win32", "Windows-only native entry, cmd, paths, and rmtree tests")
    def test_win32_entry_isolation_and_bin_mesh_cmd(self) -> None:
        repo_root = str(pathlib.Path(__file__).resolve().parents[2])
        with tempfile.TemporaryDirectory(dir=paths.temp_root()) as td:
            evil_dir = os.path.join(td, "evil")
            os.makedirs(evil_dir)
            pathlib.Path(os.path.join(evil_dir, "json.py")).write_text("raise SystemExit(99)\n", encoding="utf-8")

            env = {
                "PATH": os.environ.get("PATH", ""),
                "SYSTEMROOT": os.environ.get("SYSTEMROOT", "C:\\Windows"),
                "PYTHONPATH": f"{evil_dir};{repo_root}",
            }
            # 1. python -m mesh --version with hostile PYTHONPATH and cwd=evil_dir
            res = subprocess.run(
                [sys.executable, "-m", "mesh", "--version"],
                cwd=evil_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(res.returncode, 0, f"stdout={res.stdout!r}\nstderr={res.stderr!r}")
            self.assertIn("1.0.0.dev1", res.stdout)

            # 2. Exact non-zero exit code propagation across _winapi.CreateProcess
            res_err = subprocess.run(
                [sys.executable, "-m", "mesh", "task", "create", "--bogus"],
                cwd=evil_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(res_err.returncode, 2, f"stdout={res_err.stdout!r}\nstderr={res_err.stderr!r}")

            # 3. bin/mesh.cmd --version, --help, failing rc propagation, and _MESH_ORIG_PYTHONPATH preservation
            cmd_path = os.path.join(repo_root, "bin", "mesh.cmd")
            env_cmd = dict(os.environ)
            env_cmd["MESH_PYTHON"] = sys.executable
            env_cmd["PYTHONPATH"] = evil_dir
            env_cmd.pop("_MESH_ORIG_PYTHONPATH", None)
            res_cmd = subprocess.run(
                [cmd_path, "--version"],
                cwd=td,
                env=env_cmd,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(res_cmd.returncode, 0, f"stdout={res_cmd.stdout!r}\nstderr={res_cmd.stderr!r}")
            self.assertIn("1.0.0.dev1", res_cmd.stdout)

            res_help = subprocess.run(
                [cmd_path, "--help"],
                cwd=td,
                env=env_cmd,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(res_help.returncode, 0, f"stdout={res_help.stdout!r}\nstderr={res_help.stderr!r}")

            # Non-zero exit code propagation via MESH_PYTHON path
            res_err_cmd = subprocess.run(
                [cmd_path, "task", "create", "--bogus"],
                cwd=td,
                env=env_cmd,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(res_err_cmd.returncode, 2, f"stdout={res_err_cmd.stdout!r}\nstderr={res_err_cmd.stderr!r}")

            # Non-zero exit code propagation via PATH python.exe discovery (without MESH_PYTHON)
            env_path_only = dict(env_cmd)
            env_path_only.pop("MESH_PYTHON", None)
            env_path_only["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env_path_only.get("PATH", "")
            res_path_err = subprocess.run(
                [cmd_path, "task", "create", "--bogus"],
                cwd=td,
                env=env_path_only,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(res_path_err.returncode, 2, f"stdout={res_path_err.stdout!r}\nstderr={res_path_err.stderr!r}")

            # Positive _MESH_ORIG_PYTHONPATH & PYTHONPATH & arbitrary rc assertions via copied bin/mesh.cmd
            import json as _json
            import shutil as _shutil

            probe_root = os.path.join(td, "cmd_probe")
            os.makedirs(os.path.join(probe_root, "bin"))
            os.makedirs(os.path.join(probe_root, "mesh"))
            probe_cmd = os.path.join(probe_root, "bin", "mesh.cmd")
            _shutil.copy2(cmd_path, probe_cmd)
            pathlib.Path(os.path.join(probe_root, "mesh", "__init__.py")).write_text("", encoding="utf-8")
            pathlib.Path(os.path.join(probe_root, "mesh", "__main__.py")).write_text(
                "import json, os, sys\n"
                "print(json.dumps({\n"
                "    'orig': os.environ.get('_MESH_ORIG_PYTHONPATH'),\n"
                "    'pp': os.environ.get('PYTHONPATH'),\n"
                "    'argv': sys.argv[1:],\n"
                "}))\n"
                "sys.exit(int(os.environ.get('MESH_PROBE_RC', '0')))\n",
                encoding="utf-8",
            )
            probe_env = dict(env_cmd)
            probe_env["MESH_PROBE_RC"] = "7"
            res_probe = subprocess.run(
                [probe_cmd, "arg1", "arg2"],
                cwd=td,
                env=probe_env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(res_probe.returncode, 7, f"stdout={res_probe.stdout!r}\nstderr={res_probe.stderr!r}")
            doc_set = _json.loads(res_probe.stdout.strip())
            self.assertEqual(doc_set["orig"], evil_dir)
            self.assertTrue(paths.same_path(doc_set["pp"], probe_root))
            self.assertEqual(doc_set["argv"], ["arg1", "arg2"])

            probe_env_unset = dict(env_cmd)
            probe_env_unset.pop("PYTHONPATH", None)
            probe_env_unset.pop("MESH_PROBE_RC", None)
            res_probe_unset = subprocess.run(
                [probe_cmd],
                cwd=td,
                env=probe_env_unset,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(res_probe_unset.returncode, 0, f"stdout={res_probe_unset.stdout!r}\nstderr={res_probe_unset.stderr!r}")
            doc_unset = _json.loads(res_probe_unset.stdout.strip())
            self.assertEqual(doc_unset["orig"], "__UNSET__")
            self.assertTrue(paths.same_path(doc_unset["pp"], probe_root))

    @unittest.skipUnless(sys.platform == "win32", "Windows-only core.paths junction, 8.3, rmtree, and bin_dir tests")
    def test_win32_core_paths_junction_8dot3_rmtree_and_bin_dir(self) -> None:
        import _winapi
        from ctypes import wintypes
        from mesh.core import paths as core_paths

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetShortPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        k32.GetShortPathNameW.restype = wintypes.DWORD

        with tempfile.TemporaryDirectory(dir=paths.temp_root()) as td:
            # Drive root and mixed traversal rejected
            drive, _ = os.path.splitdrive(td)
            self.assertIsNone(core_paths.resolve_trusted_path(drive + "\\"))
            self.assertIsNone(core_paths.resolve_trusted_path(td + "\\a/../b"))
            self.assertIsNone(core_paths.resolve_trusted_path(td + "/a\\../b"))

            # 8.3 short name canonicalized and accepted
            long_dir = os.path.join(td, "long directory for trusted path")
            os.makedirs(long_dir)
            buf = ctypes.create_unicode_buffer(1024)
            self.assertGreater(k32.GetShortPathNameW(long_dir, buf, len(buf)), 0)
            resolved = core_paths.resolve_trusted_path(buf.value, root=td)
            self.assertEqual(resolved, os.path.realpath(long_dir))

            # NTFS junction rejected by has_symlink_component and resolve_trusted_path
            target = os.path.join(td, "target_dir")
            os.makedirs(target)
            junc = os.path.join(td, "junc_dir")
            _winapi.CreateJunction(target, junc)
            self.assertTrue(core_paths.has_symlink_component(junc))
            self.assertTrue(core_paths.has_symlink_component(junc + "/"))
            self.assertIsNone(core_paths.resolve_trusted_path(junc, root=td))
            self.assertIsNone(core_paths.resolve_trusted_path(os.path.join(junc, "sub"), root=td))

            # rmtree_allow_readonly removes read-only pack files inside boundary
            ro_tree = os.path.join(td, "git_like")
            pack_dir = os.path.join(ro_tree, ".git", "objects", "pack")
            os.makedirs(pack_dir)
            pack_file = os.path.join(pack_dir, "pack-test.idx")
            pathlib.Path(pack_file).write_bytes(b"pack")
            os.chmod(pack_file, stat.S_IREAD)
            core_paths.rmtree_allow_readonly(ro_tree, boundary_real=td)
            self.assertFalse(os.path.exists(ro_tree))

            # get_mesh_bin_dir returns repo bin/ in clone and generates mesh.cmd in LOCALAPPDATA for package install
            clone_bin = core_paths.get_mesh_bin_dir()
            self.assertTrue(os.path.isfile(os.path.join(clone_bin, "mesh.cmd")))

            local_app = os.path.join(td, "localappdata")
            pkg_root = os.path.join(td, "pkg_root")
            os.makedirs(pkg_root)
            with (
                mock.patch.object(core_paths, "get_install_root", return_value=pkg_root),
                mock.patch.dict(os.environ, {"LOCALAPPDATA": local_app}),
            ):
                bdir = core_paths.get_mesh_bin_dir()
                self.assertTrue(paths.is_path_equal_or_parent(local_app, bdir))
                self.assertTrue(os.path.isfile(os.path.join(bdir, "mesh.cmd")))
                self.assertTrue(os.path.isfile(os.path.join(bdir, "mesh")))


class TestFinalReviewPathsFsFixes(unittest.TestCase):
    """Regression tests for R13 P2-1, P2-2, and P3-1."""

    def test_p2_1_norm_cmp_and_trusted_paths_match_strip_extended_prefix(self) -> None:
        from mesh.core import paths as core_paths

        self.assertEqual(
            paths.norm_cmp_win(r"\\?\C:\Repo\Sub\File.py", pathmod=ntpath),
            r"c:\repo\sub\file.py",
        )
        self.assertEqual(
            paths.norm_cmp_win(r"\\?\UNC\Srv\Share\Dir", pathmod=ntpath),
            r"\\srv\share\dir",
        )
        self.assertTrue(
            paths.is_path_equal_or_parent_win(
                r"C:\Repo",
                r"\\?\C:\Repo\sub\file.py",
                realpath_fn=ntpath.normpath,
                pathmod=ntpath,
            )
        )
        reg_st = SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_file_attributes=0)
        self.assertTrue(
            core_paths._win_trusted_paths_match(
                r"C:\Repo\sub\file.py",
                r"\\?\c:\repo\sub\file.py",
                pathmod=ntpath,
                lstat_fn=lambda _p: reg_st,
                same_path_fn=lambda a, b: paths.same_path_win(
                    a, b, realpath_fn=ntpath.normpath, pathmod=ntpath
                ),
            )
        )
        self.assertTrue(
            core_paths._win_trusted_paths_match(
                r"\\Srv\Share\file.py",
                r"\\?\UNC\srv\share\file.py",
                pathmod=ntpath,
                lstat_fn=lambda _p: reg_st,
                same_path_fn=lambda a, b: paths.same_path_win(
                    a, b, realpath_fn=ntpath.normpath, pathmod=ntpath
                ),
            )
        )
        self.assertFalse(
            core_paths._win_trusted_paths_match(
                r"C:\Repo\file.py",
                r"\\?\D:\Repo\file.py",
                pathmod=ntpath,
                lstat_fn=lambda _p: reg_st,
                same_path_fn=lambda a, b: paths.same_path_win(
                    a, b, realpath_fn=ntpath.normpath, pathmod=ntpath
                ),
            )
        )

    def test_p2_2_rmtree_allow_readonly_win_ownership_and_sharing_retry_mocked(self) -> None:
        from mesh.core import paths as core_paths

        with tempfile.TemporaryDirectory() as td:
            root = os.path.realpath(td)
            target = os.path.join(root, "tree")
            child = os.path.join(target, "ro.txt")
            os.makedirs(target)
            pathlib.Path(child).write_text("x", encoding="utf-8")

            # 1. Foreign-owned file: must refuse without calling os.chmod
            chmods: list[tuple[str, int]] = []
            sleeps: list[float] = []

            def _rmtree_denied(_p: str, *, onexc: Any) -> None:
                exc = PermissionError(errno.EACCES, "denied", child)
                onexc(lambda _x: None, child, exc)

            with (
                mock.patch.object(core_paths.sys, "platform", "win32"),
                mock.patch.object(core_paths, "resolve_trusted_path", side_effect=lambda p, **_k: p),
                mock.patch.object(core_paths, "is_reparse_link", return_value=False),
                mock.patch.object(core_paths, "has_symlink_component", return_value=False),
                mock.patch.object(core_paths, "is_path_equal_or_parent", return_value=True),
                mock.patch.object(core_paths.perms, "is_owned_by_current_user", return_value=False),
                mock.patch.object(core_paths.os, "chmod", side_effect=lambda p, m: chmods.append((p, m))),
                mock.patch.object(core_paths.shutil, "rmtree", side_effect=_rmtree_denied),
            ):
                with self.assertRaises(PermissionError):
                    core_paths.rmtree_allow_readonly(target, boundary_real=root)
            self.assertEqual(chmods, [])

            # 2. Owned file with WinError 32 sharing violation: retries with backoff then succeeds
            attempts = 0

            def _flaky_unlink(_p: str) -> None:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    err = PermissionError(errno.EACCES, "sharing", child)
                    err.winerror = 32  # type: ignore[attr-defined]
                    raise err

            def _rmtree_win32_flaky(_p: str, *, onexc: Any) -> None:
                init_err = PermissionError(errno.EACCES, "sharing", child)
                init_err.winerror = 32  # type: ignore[attr-defined]
                onexc(_flaky_unlink, child, init_err)

            with (
                mock.patch.object(core_paths.sys, "platform", "win32"),
                mock.patch.object(core_paths, "resolve_trusted_path", side_effect=lambda p, **_k: p),
                mock.patch.object(core_paths, "is_reparse_link", return_value=False),
                mock.patch.object(core_paths, "has_symlink_component", return_value=False),
                mock.patch.object(core_paths, "is_path_equal_or_parent", return_value=True),
                mock.patch.object(core_paths.perms, "is_owned_by_current_user", return_value=True),
                mock.patch.object(core_paths.os, "chmod", side_effect=lambda p, m: chmods.append((p, m))),
                mock.patch.object(core_paths.time, "sleep", side_effect=sleeps.append),
                mock.patch.object(core_paths.shutil, "rmtree", side_effect=_rmtree_win32_flaky),
            ):
                core_paths.rmtree_allow_readonly(target, boundary_real=root)
            self.assertEqual(attempts, 2)
            self.assertEqual(sleeps, [0.005])
            self.assertEqual(chmods[0], (child, stat.S_IWRITE))

            # 3. Exhausting WinError 5 sharing retries stops after (0.005, 0.015, 0.030)
            sleeps.clear()

            def _always_err5(_p: str) -> None:
                err = PermissionError(errno.EACCES, "locked", child)
                err.winerror = 5  # type: ignore[attr-defined]
                raise err

            def _rmtree_win5(_p: str, *, onexc: Any) -> None:
                init_err = PermissionError(errno.EACCES, "locked", child)
                init_err.winerror = 5  # type: ignore[attr-defined]
                onexc(_always_err5, child, init_err)

            with (
                mock.patch.object(core_paths.sys, "platform", "win32"),
                mock.patch.object(core_paths, "resolve_trusted_path", side_effect=lambda p, **_k: p),
                mock.patch.object(core_paths, "is_reparse_link", return_value=False),
                mock.patch.object(core_paths, "has_symlink_component", return_value=False),
                mock.patch.object(core_paths, "is_path_equal_or_parent", return_value=True),
                mock.patch.object(core_paths.perms, "is_owned_by_current_user", return_value=True),
                mock.patch.object(core_paths.os, "chmod"),
                mock.patch.object(core_paths.time, "sleep", side_effect=sleeps.append),
                mock.patch.object(core_paths.shutil, "rmtree", side_effect=_rmtree_win5),
            ):
                with self.assertRaises(PermissionError):
                    core_paths.rmtree_allow_readonly(target, boundary_real=root)
            self.assertEqual(sleeps, [0.005, 0.015, 0.030])

    def test_p3_1_unlink_nonregular_win_never_rmdir_plain_dir_mocked(self) -> None:
        rmdirs: list[str] = []

        # Plain directory (not a reparse link): os.rmdir must NEVER run
        with (
            mock.patch("mesh.platform.paths.is_reparse_link", return_value=False),
            mock.patch.object(fs.os, "chmod"),
            mock.patch.object(fs.os, "unlink", side_effect=PermissionError(errno.EACCES, "is dir")),
            mock.patch.object(fs.os, "rmdir", side_effect=rmdirs.append),
        ):
            with self.assertRaises(PermissionError):
                fs.unlink_nonregular_win(r"C:\repo\empty_dir")
        self.assertEqual(rmdirs, [])

        # Reparse directory (NTFS junction): os.rmdir fallback removes the junction
        with (
            mock.patch("mesh.platform.paths.is_reparse_link", return_value=True),
            mock.patch.object(fs.os, "chmod"),
            mock.patch.object(fs.os, "unlink", side_effect=PermissionError(errno.EACCES, "reparse")),
            mock.patch.object(fs.os, "rmdir", side_effect=rmdirs.append),
        ):
            fs.unlink_nonregular_win(r"C:\repo\junc_dir")
        self.assertEqual(rmdirs, [r"C:\repo\junc_dir"])

    @unittest.skipUnless(sys.platform == "win32", "Windows-only \\\\?\\ prefix, readonly rmtree, and unlink tests")
    def test_p2_1_p2_2_p3_1_real_windows(self) -> None:
        import _winapi
        from mesh.core import paths as core_paths

        with tempfile.TemporaryDirectory(dir=paths.temp_root()) as td:
            td_real = os.path.realpath(td)
            # P2-1: \\?\ prefix matches normal path
            sub = os.path.join(td_real, "sub_dir")
            os.makedirs(sub)
            ext_sub = "\\\\?\\" + os.path.abspath(sub)
            self.assertEqual(paths.norm_cmp(ext_sub), paths.norm_cmp(sub))
            self.assertTrue(paths.same_path(ext_sub, sub))

            # P3-1: real empty directory is NOT removed by unlink_nonregular, whereas junction IS
            plain_dir = os.path.join(td_real, "plain_empty")
            os.makedirs(plain_dir)
            with self.assertRaises(OSError):
                fs.unlink_nonregular(plain_dir)
            self.assertTrue(os.path.isdir(plain_dir))

            target = os.path.join(td_real, "target_dir")
            os.makedirs(target)
            keep_f = os.path.join(target, "keep.txt")
            pathlib.Path(keep_f).write_text("keep", encoding="utf-8")
            junc = os.path.join(td_real, "junc_dir")
            _winapi.CreateJunction(target, junc)
            fs.unlink_nonregular(junc)
            self.assertFalse(os.path.lexists(junc))
            self.assertEqual(pathlib.Path(keep_f).read_text(encoding="utf-8"), "keep")

            # P2-2: rmtree_allow_readonly cleans tree containing read-only file
            ro_tree = os.path.join(td_real, "ro_tree")
            os.makedirs(ro_tree)
            ro_file = os.path.join(ro_tree, "pack.idx")
            pathlib.Path(ro_file).write_text("pack", encoding="utf-8")
            os.chmod(ro_file, stat.S_IREAD)
            core_paths.rmtree_allow_readonly(ro_tree, boundary_real=td_real)
            self.assertFalse(os.path.exists(ro_tree))


if __name__ == "__main__":
    unittest.main()
