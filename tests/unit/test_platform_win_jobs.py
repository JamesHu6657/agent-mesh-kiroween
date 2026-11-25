"""Unit tests for Win32 Job Objects, handle-pinned kill, and children.py (Phase 3b / T07)."""

from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
import os
import pathlib
import queue
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid

from mesh.daemon import children
from mesh.platform import _win32, _win32_ctypes as w32, proc as plat_proc
from tests import win_sim


@dataclass
class _FakeJobObj:
    name: str | None
    limit_flags: int = 0
    sddl: str | None = None
    pids: set[int] = field(default_factory=set)
    terminated_code: int | None = None
    ref_count: int = 0
    owner_sid: str = "S-1-5-21-1000-2000-3000-1001"


@dataclass
class _FakeProcObj:
    pid: int
    ppid: int
    creation_time: int
    alive: bool = True
    exit_code: int = w32.STILL_ACTIVE
    access_denied_open: bool = False
    access_denied_terminate: bool = False
    threads: list[int] = field(default_factory=list)
    job_ids: set[int] = field(default_factory=set)


class _FakeWinJobKernel:
    """In-memory simulator for Win32 Job Objects, threads, events, SIDs, and processes."""

    def __init__(self, procs: dict[int, _FakeProcObj], *, current_pid: int = 100) -> None:
        self.procs = procs
        self.current_pid = current_pid
        self.user_sid = "S-1-5-21-1000-2000-3000-1001"
        self.last_error = 0
        self.next_handle = 0x1000
        self.open_handles: list[int] = []
        self.closed_handles: list[int] = []
        self.handle_table: dict[int, tuple[str, Any]] = {}

        self.local_allocs: list[int] = []
        self.local_frees: list[int] = []
        self.next_ptr = 0x70000000

        self.jobs_by_id: dict[int, _FakeJobObj] = {}
        self.named_jobs: dict[str, int] = {}
        self.next_job_id = 1

        self.events_by_name: dict[str, dict[str, Any]] = {}
        self.op_log: list[tuple[Any, ...]] = []
        self.console_pids: list[int] = [current_pid]
        self.fail_console_process_list: bool = False

        # Injectibles for fail-closed tests
        self.fail_set_info_error: int | None = None
        self.fail_assign_error: int | None = None
        self.fail_resume_error: int | None = None
        self.fail_nt_get_next_status: int | None = None
        self.omit_nt_get_next: bool = False
        self.fail_open_job_error: int | None = None
        self.fail_query_job_error: int | None = None
        self.fail_is_in_job_error: int | None = None
        self.force_more_data_once: bool = False
        self.force_short_list_once: bool = False
        self.console_ctrl_succeeds: bool = True

    def get_last_error(self) -> int:
        return self.last_error

    def set_last_error(self, code: int = 0) -> None:
        self.last_error = int(code)

    def _alloc_handle(self, kind: str, payload: Any) -> int:
        self.next_handle += 4
        h = self.next_handle
        self.open_handles.append(h)
        self.handle_table[h] = (kind, payload)
        return h

    def _alloc_local_bytes(self, raw: bytes) -> tuple[int, ctypes.Array]:
        buf = ctypes.create_string_buffer(raw, len(raw))
        addr = ctypes.addressof(buf)
        self.local_allocs.append(addr)
        if not hasattr(self, "_pinned_bufs"):
            self._pinned_bufs: dict[int, ctypes.Array] = {}
        self._pinned_bufs[addr] = buf
        return addr, buf

    def LocalFree(self, ptr: Any) -> Any:
        val = ptr.value if hasattr(ptr, "value") else int(ptr or 0)
        if val:
            self.local_frees.append(val)
            if hasattr(self, "_pinned_bufs"):
                self._pinned_bufs.pop(val, None)
        return 0

    def _as_int_handle(self, handle: Any) -> int:
        if hasattr(handle, "value"):
            return int(handle.value or 0)
        return int(handle)

    def CloseHandle(self, handle: int) -> int:
        h = self._as_int_handle(handle)
        self.closed_handles.append(h)
        item = self.handle_table.pop(h, None)
        if item is not None and item[0] == "job":
            job_id = item[1]
            job = self.jobs_by_id.get(job_id)
            if job is not None:
                job.ref_count -= 1
                if job.ref_count <= 0:
                    if job.limit_flags & w32.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE:
                        for pid in list(job.pids):
                            p = self.procs.get(pid)
                            if p is not None:
                                p.alive = False
                                p.exit_code = 137
                    if job.name:
                        self.named_jobs.pop(job.name, None)
        return 1

    def GetCurrentProcess(self) -> int:
        return -1

    def GetCurrentProcessId(self) -> int:
        return self.current_pid

    # --- Token & SDDL simulation ---
    def OpenProcessToken(self, proc_handle: int, access: int, out_ptr: Any) -> int:
        _ = (proc_handle, access)
        h = self._alloc_handle("token", self.user_sid)
        ctypes.cast(out_ptr, ctypes.POINTER(wintypes.HANDLE))[0] = h
        return 1

    def _encode_sid(self, sid_str: str) -> bytes:
        parts = sid_str.split("-")
        rev = int(parts[1])
        auth = int(parts[2])
        subs = [int(x) for x in parts[3:]]
        return bytes([rev, len(subs)]) + auth.to_bytes(6, "big") + b"".join(
            struct.pack("<I", s) for s in subs
        )

    def GetTokenInformation(
        self, tok_handle: int, info_class: int, buf: Any, buf_len: int, needed_ptr: Any
    ) -> int:
        item = self.handle_table.get(self._as_int_handle(tok_handle))
        if item is None or item[0] != "token":
            self.last_error = w32.ERROR_INVALID_HANDLE
            return 0
        sid_raw = self._encode_sid(item[1])
        hdr_sz = ctypes.sizeof(w32.TOKEN_USER) if info_class == w32.TokenUser else ctypes.sizeof(w32.TOKEN_OWNER)
        total = hdr_sz + len(sid_raw)
        ctypes.cast(needed_ptr, wintypes.LPDWORD)[0] = total
        if buf is None or buf_len < total:
            self.last_error = w32.ERROR_INSUFFICIENT_BUFFER
            return 0
        sid_addr = ctypes.addressof(buf) + hdr_sz
        ctypes.memmove(sid_addr, sid_raw, len(sid_raw))
        if info_class == w32.TokenUser:
            tu = w32.TOKEN_USER.from_buffer(buf)
            tu.User.Sid = sid_addr
        else:
            to = w32.TOKEN_OWNER.from_buffer(buf)
            to.Owner = sid_addr
        return 1

    def IsValidSid(self, psid: int) -> int:
        return 1 if psid else 0

    def ConvertSidToStringSidW(self, psid: int, out_str_ptr: Any) -> int:
        raw_hdr = ctypes.string_at(psid, 8)
        rev = raw_hdr[0]
        sub_cnt = raw_hdr[1]
        auth = int.from_bytes(raw_hdr[2:8], "big")
        full = ctypes.string_at(psid, 8 + 4 * sub_cnt)
        subs = [str(struct.unpack("<I", full[8 + 4 * i : 12 + 4 * i])[0]) for i in range(sub_cnt)]
        sid_str = "-".join(["S", str(rev), str(auth)] + subs)
        buf = ctypes.create_unicode_buffer(sid_str)
        addr = ctypes.addressof(buf)
        self.local_allocs.append(addr)
        if not hasattr(self, "_pinned_bufs"):
            self._pinned_bufs = {}
        self._pinned_bufs[addr] = buf
        ctypes.cast(out_str_ptr, ctypes.POINTER(ctypes.c_void_p))[0] = addr
        return 1

    def ConvertStringSecurityDescriptorToSecurityDescriptorW(
        self, sddl: str, rev: int, out_sd_ptr: Any, out_len_ptr: Any
    ) -> int:
        _ = (rev, out_len_ptr)
        encoded = (sddl + "\x00").encode("utf-8")
        addr, _ = self._alloc_local_bytes(encoded)
        ctypes.cast(out_sd_ptr, ctypes.POINTER(ctypes.c_void_p))[0] = addr
        return 1

    # --- Process handles & inspection ---
    def OpenProcess(self, access: int, inherit: bool, pid: int) -> Any:
        _ = inherit
        self.op_log.append(("OpenProcess", pid, access))
        entry = self.procs.get(pid)
        if entry is None:
            self.last_error = w32.ERROR_INVALID_PARAMETER
            return None
        if entry.access_denied_open:
            self.last_error = w32.ERROR_ACCESS_DENIED
            return None
        self.last_error = 0
        return self._alloc_handle("proc", (entry, access))

    def WaitForSingleObject(self, handle: int, timeout_ms: int) -> int:
        _ = timeout_ms
        item = self.handle_table.get(int(handle))
        if item is None or item[0] != "proc":
            self.last_error = w32.ERROR_INVALID_HANDLE
            return w32.WAIT_FAILED
        entry, _access = item[1]
        self.op_log.append(("WaitForSingleObject", entry.pid))
        return w32.WAIT_TIMEOUT if entry.alive else w32.WAIT_OBJECT_0

    def GetExitCodeProcess(self, handle: int, out_ptr: Any) -> int:
        item = self.handle_table.get(int(handle))
        if item is None or item[0] != "proc":
            self.last_error = w32.ERROR_INVALID_HANDLE
            return 0
        entry, _access = item[1]
        code = w32.STILL_ACTIVE if entry.alive else entry.exit_code
        ctypes.cast(out_ptr, wintypes.LPDWORD)[0] = code
        return 1

    def GetProcessTimes(self, handle: int, ct_ptr: Any, et_ptr: Any, kt_ptr: Any, ut_ptr: Any) -> int:
        _ = (et_ptr, kt_ptr, ut_ptr)
        item = self.handle_table.get(int(handle))
        if item is None or item[0] != "proc":
            self.last_error = w32.ERROR_INVALID_HANDLE
            return 0
        entry, _access = item[1]
        self.op_log.append(("GetProcessTimes", entry.pid, entry.creation_time))
        ct = ctypes.cast(ct_ptr, ctypes.POINTER(w32.FILETIME)).contents
        ct.dwLowDateTime = entry.creation_time & 0xFFFFFFFF
        ct.dwHighDateTime = (entry.creation_time >> 32) & 0xFFFFFFFF
        return 1

    def TerminateProcess(self, handle: int, exit_code: int) -> int:
        item = self.handle_table.get(int(handle))
        if item is None or item[0] != "proc":
            self.last_error = w32.ERROR_INVALID_HANDLE
            return 0
        entry, access = item[1]
        self.op_log.append(("TerminateProcess", entry.pid, exit_code))
        if entry.access_denied_terminate or not (access & w32.PROCESS_TERMINATE):
            self.last_error = w32.ERROR_ACCESS_DENIED
            return 0
        entry.alive = False
        entry.exit_code = int(exit_code)
        return 1

    # --- Job Object APIs ---
    def CreateJobObjectW(self, sa_ptr: Any, name: str | None) -> Any:
        sddl = None
        if sa_ptr:
            sa = ctypes.cast(sa_ptr, ctypes.POINTER(w32.SECURITY_ATTRIBUTES)).contents
            if sa.lpSecurityDescriptor:
                sddl = ctypes.string_at(sa.lpSecurityDescriptor).split(b"\x00", 1)[0].decode("utf-8")
        self.op_log.append(("CreateJobObjectW", name, sddl))
        if name is not None and name in self.named_jobs:
            job_id = self.named_jobs[name]
            self.jobs_by_id[job_id].ref_count += 1
            self.last_error = w32.ERROR_ALREADY_EXISTS
            return self._alloc_handle("job", job_id)
        job_id = self.next_job_id
        self.next_job_id += 1
        self.jobs_by_id[job_id] = _FakeJobObj(
            name=name, sddl=sddl, ref_count=1, owner_sid=self.user_sid
        )
        if name is not None:
            self.named_jobs[name] = job_id
        self.last_error = 0
        return self._alloc_handle("job", job_id)

    def OpenJobObjectW(self, access: int, inherit: bool, name: str) -> Any:
        _ = inherit
        self.op_log.append(("OpenJobObjectW", access, name))
        if self.fail_open_job_error is not None:
            self.last_error = self.fail_open_job_error
            return None
        job_id = self.named_jobs.get(name)
        if job_id is None:
            self.last_error = w32.ERROR_FILE_NOT_FOUND
            return None
        self.jobs_by_id[job_id].ref_count += 1
        self.last_error = 0
        return self._alloc_handle("job", job_id)

    def GetSecurityInfo(
        self,
        handle: Any,
        obj_type: int,
        sec_info: int,
        ppsid_owner: Any,
        ppsid_group: Any,
        ppdacl: Any,
        ppsacl: Any,
        ppsd: Any,
    ) -> int:
        _ = (obj_type, sec_info, ppsid_group, ppdacl, ppsacl)
        if getattr(self, "fail_get_security_info_error", None) is not None:
            return int(self.fail_get_security_info_error)
        item = self.handle_table.get(self._as_int_handle(handle))
        if item is None or item[0] != "job":
            return w32.ERROR_INVALID_HANDLE
        job = self.jobs_by_id[item[1]]
        sid_raw = self._encode_sid(job.owner_sid)
        sd_addr, _ = self._alloc_local_bytes(sid_raw)
        if ppsid_owner is not None:
            ctypes.cast(ppsid_owner, ctypes.POINTER(ctypes.c_void_p))[0] = sd_addr
        if ppsd is not None:
            ctypes.cast(ppsd, ctypes.POINTER(ctypes.c_void_p))[0] = sd_addr
        return 0

    def SetInformationJobObject(
        self, job_handle: int, info_class: int, info_ptr: Any, info_len: int
    ) -> int:
        if self.fail_set_info_error is not None:
            self.last_error = self.fail_set_info_error
            return 0
        item = self.handle_table.get(int(job_handle))
        if item is None or item[0] != "job":
            self.last_error = w32.ERROR_INVALID_HANDLE
            return 0
        job = self.jobs_by_id[item[1]]
        if info_class == w32.JobObjectExtendedLimitInformation:
            if info_len != ctypes.sizeof(w32.JOBOBJECT_EXTENDED_LIMIT_INFORMATION):
                self.last_error = w32.ERROR_BAD_LENGTH
                return 0
            ext = ctypes.cast(info_ptr, ctypes.POINTER(w32.JOBOBJECT_EXTENDED_LIMIT_INFORMATION)).contents
            job.limit_flags = int(ext.BasicLimitInformation.LimitFlags)
            self.op_log.append(("SetInformationJobObject", item[1], job.limit_flags))
            return 1
        self.last_error = w32.ERROR_INVALID_PARAMETER
        return 0

    def QueryInformationJobObject(
        self, job_handle: int, info_class: int, buf: Any, buf_len: int, ret_len_ptr: Any
    ) -> int:
        if self.fail_query_job_error is not None:
            self.last_error = self.fail_query_job_error
            return 0
        item = self.handle_table.get(int(job_handle))
        if item is None or item[0] != "job":
            self.last_error = w32.ERROR_INVALID_HANDLE
            return 0
        if info_class != w32.JobObjectBasicProcessIdList:
            self.last_error = w32.ERROR_INVALID_PARAMETER
            return 0
        job = self.jobs_by_id[item[1]]
        live_pids = sorted(pid for pid in job.pids if self.procs.get(pid) and self.procs[pid].alive)
        header_off = w32.JOBOBJECT_BASIC_PROCESS_ID_LIST.ProcessIdList.offset
        elem_sz = ctypes.sizeof(ctypes.c_size_t)
        cap = max(0, (buf_len - header_off) // elem_sz)

        hdr = w32.JOBOBJECT_BASIC_PROCESS_ID_LIST.from_buffer(buf)
        hdr.NumberOfAssignedProcesses = len(live_pids)

        if self.force_more_data_once:
            self.force_more_data_once = False
            hdr.NumberOfProcessIdsInList = 0
            self.last_error = w32.ERROR_MORE_DATA
            return 0

        if self.force_short_list_once and live_pids:
            self.force_short_list_once = False
            hdr.NumberOfProcessIdsInList = max(0, len(live_pids) - 1)
            return 1

        if cap < len(live_pids):
            hdr.NumberOfProcessIdsInList = cap
            for i in range(cap):
                ctypes.cast(ctypes.addressof(buf) + header_off + i * elem_sz, ctypes.POINTER(ctypes.c_size_t))[0] = live_pids[i]
            self.last_error = w32.ERROR_MORE_DATA
            return 0

        hdr.NumberOfProcessIdsInList = len(live_pids)
        for i, pid in enumerate(live_pids):
            ctypes.cast(ctypes.addressof(buf) + header_off + i * elem_sz, ctypes.POINTER(ctypes.c_size_t))[0] = pid
        if ret_len_ptr:
            ctypes.cast(ret_len_ptr, wintypes.LPDWORD)[0] = header_off + len(live_pids) * elem_sz
        return 1

    def AssignProcessToJobObject(self, job_handle: int, proc_handle: int) -> int:
        if self.fail_assign_error is not None:
            self.last_error = self.fail_assign_error
            return 0
        j_item = self.handle_table.get(int(job_handle))
        p_item = self.handle_table.get(int(proc_handle))
        if j_item is None or j_item[0] != "job" or p_item is None or p_item[0] != "proc":
            self.last_error = w32.ERROR_INVALID_HANDLE
            return 0
        job_id = j_item[1]
        entry, _ = p_item[1]
        self.jobs_by_id[job_id].pids.add(entry.pid)
        entry.job_ids.add(job_id)
        self.op_log.append(("AssignProcessToJobObject", job_id, entry.pid))
        return 1

    def IsProcessInJob(self, proc_handle: int, job_handle: int, out_bool_ptr: Any) -> int:
        if self.fail_is_in_job_error is not None:
            self.last_error = self.fail_is_in_job_error
            return 0
        p_item = self.handle_table.get(int(proc_handle))
        j_item = self.handle_table.get(int(job_handle))
        if p_item is None or p_item[0] != "proc" or j_item is None or j_item[0] != "job":
            self.last_error = w32.ERROR_INVALID_HANDLE
            return 0
        entry, _ = p_item[1]
        job_id = j_item[1]
        in_job = job_id in entry.job_ids or entry.pid in self.jobs_by_id[job_id].pids
        ctypes.cast(out_bool_ptr, ctypes.POINTER(wintypes.BOOL))[0] = 1 if in_job else 0
        return 1

    def TerminateJobObject(self, job_handle: int, exit_code: int) -> int:
        item = self.handle_table.get(int(job_handle))
        if item is None or item[0] != "job":
            self.last_error = w32.ERROR_INVALID_HANDLE
            return 0
        job = self.jobs_by_id[item[1]]
        job.terminated_code = int(exit_code)
        self.op_log.append(("TerminateJobObject", item[1], int(exit_code)))
        for pid in list(job.pids):
            p = self.procs.get(pid)
            if p is not None:
                p.alive = False
                p.exit_code = int(exit_code)
        return 1

    # --- Thread enumeration & resume ---
    def NtGetNextThread(
        self,
        proc_handle: int,
        prev_thread: Any,
        access: int,
        attrs: int,
        flags: int,
        out_thread_ptr: Any,
    ) -> int:
        _ = (prev_thread, attrs, flags)
        if self.fail_nt_get_next_status is not None:
            return self.fail_nt_get_next_status
        item = self.handle_table.get(int(proc_handle))
        if item is None or item[0] != "proc":
            return -1073741816  # STATUS_INVALID_HANDLE
        entry, _ = item[1]
        if not entry.threads:
            return w32.STATUS_NO_MORE_ENTRIES
        h_thr = self._alloc_handle("thread", (entry.pid, entry.threads[0], access))
        ctypes.cast(out_thread_ptr, ctypes.POINTER(wintypes.HANDLE))[0] = h_thr
        return w32.STATUS_SUCCESS

    def RtlNtStatusToDosError(self, status: int) -> int:
        if status == w32.STATUS_ACCESS_DENIED:
            return w32.ERROR_ACCESS_DENIED
        return w32.ERROR_INVALID_PARAMETER

    def CreateToolhelp32Snapshot(self, flags: int, pid: int) -> int:
        _ = pid
        if flags == w32.TH32CS_SNAPTHREAD:
            threads: list[tuple[int, int]] = []
            for p in self.procs.values():
                for tid in p.threads:
                    threads.append((p.pid, tid))
            return self._alloc_handle("th_snap", {"threads": threads, "idx": 0})
        return self._alloc_handle("snap", {})

    def Thread32First(self, snap: int, te_ptr: Any) -> int:
        item = self.handle_table.get(int(snap))
        if item is None or item[0] != "th_snap":
            self.last_error = w32.ERROR_INVALID_HANDLE
            return 0
        te = ctypes.cast(te_ptr, ctypes.POINTER(w32.THREADENTRY32)).contents
        if te.dwSize != ctypes.sizeof(w32.THREADENTRY32):
            self.last_error = w32.ERROR_BAD_LENGTH
            return 0
        state = item[1]
        state["idx"] = 0
        if not state["threads"]:
            self.last_error = w32.ERROR_NO_MORE_FILES
            return 0
        pid, tid = state["threads"][0]
        te.th32OwnerProcessID = pid
        te.th32ThreadID = tid
        return 1

    def Thread32Next(self, snap: int, te_ptr: Any) -> int:
        item = self.handle_table.get(int(snap))
        if item is None or item[0] != "th_snap":
            self.last_error = w32.ERROR_INVALID_HANDLE
            return 0
        state = item[1]
        state["idx"] += 1
        if state["idx"] >= len(state["threads"]):
            self.last_error = w32.ERROR_NO_MORE_FILES
            return 0
        te = ctypes.cast(te_ptr, ctypes.POINTER(w32.THREADENTRY32)).contents
        pid, tid = state["threads"][state["idx"]]
        te.th32OwnerProcessID = pid
        te.th32ThreadID = tid
        return 1

    def OpenThread(self, access: int, inherit: bool, tid: int) -> Any:
        _ = inherit
        for p in self.procs.values():
            if tid in p.threads:
                return self._alloc_handle("thread", (p.pid, tid, access))
        self.last_error = w32.ERROR_INVALID_PARAMETER
        return None

    def ResumeThread(self, thr_handle: int) -> int:
        item = self.handle_table.get(int(thr_handle))
        if item is None or item[0] != "thread":
            self.last_error = w32.ERROR_INVALID_HANDLE
            return w32.THREAD_RESUME_FAILED
        if self.fail_resume_error is not None:
            self.last_error = self.fail_resume_error
            return w32.THREAD_RESUME_FAILED
        pid, tid, _ = item[1]
        self.op_log.append(("ResumeThread", pid, tid))
        return 1

    # --- Events & Console Ctrl ---
    def CreateEventW(self, sa_ptr: Any, manual_reset: bool, init_state: bool, name: str | None) -> Any:
        _ = (sa_ptr, manual_reset, init_state)
        if name is not None and name in self.events_by_name:
            self.last_error = w32.ERROR_ALREADY_EXISTS
            return self._alloc_handle("event", name)
        if name is not None:
            self.events_by_name[name] = {"signalled": bool(init_state)}
        self.last_error = 0
        return self._alloc_handle("event", name)

    def OpenEventW(self, access: int, inherit: bool, name: str) -> Any:
        _ = (access, inherit)
        if name not in self.events_by_name:
            self.last_error = w32.ERROR_FILE_NOT_FOUND
            return None
        self.last_error = 0
        return self._alloc_handle("event", name)

    def SetEvent(self, ev_handle: int) -> int:
        item = self.handle_table.get(int(ev_handle))
        if item is None or item[0] != "event":
            self.last_error = w32.ERROR_INVALID_HANDLE
            return 0
        ev_name = item[1]
        if ev_name in self.events_by_name:
            self.events_by_name[ev_name]["signalled"] = True
        self.op_log.append(("SetEvent", ev_name))
        return 1

    def GenerateConsoleCtrlEvent(self, event: int, group_id: int) -> int:
        self.op_log.append(("GenerateConsoleCtrlEvent", event, group_id))
        return 1 if self.console_ctrl_succeeds else 0

    def GetConsoleProcessList(self, buf: Any, cap: int) -> int:
        self.op_log.append(("GetConsoleProcessList", int(cap)))
        if self.fail_console_process_list:
            self.last_error = w32.ERROR_INVALID_HANDLE
            return 0
        pids = list(self.console_pids)
        if len(pids) <= int(cap):
            for idx, pid in enumerate(pids):
                buf[idx] = pid
        return len(pids)


class _FakeNtNoGetNext:
    """ntdll simulator omitting NtGetNextThread to exercise Toolhelp32 thread fallback."""

    def RtlNtStatusToDosError(self, status: int) -> int:
        return w32.ERROR_ACCESS_DENIED if status == w32.STATUS_ACCESS_DENIED else w32.ERROR_INVALID_PARAMETER


class TestPlatformWinJobsHygieneAndLayouts(unittest.TestCase):
    """Import hygiene and Win32 Job Object ctypes ABI checks."""

    @unittest.skipUnless(sys.platform != "win32", "POSIX import-hygiene check")
    def test_linux_import_never_touches_windows_modules(self) -> None:
        code = (
            "import ctypes, sys\n"
            "assert not hasattr(ctypes, 'windll')\n"
            "from mesh.platform import proc\n"
            "from mesh.daemon import children\n"
            "for fn in (lambda: proc.run_job_name('/tmp/h', 'r1'), lambda: proc.run_stop_event_name('/tmp/h', 'r1')):\n"
            "    try:\n"
            "        fn()\n"
            "        raise AssertionError('expected NotImplementedError')\n"
            "    except NotImplementedError:\n"
            "        pass\n"
            "for mod in ('msvcrt', '_winapi', 'nt', 'mesh.platform._win32_ctypes', 'mesh.platform._win32'):\n"
            "    assert mod not in sys.modules, f'unexpectedly imported {mod}'\n"
        )
        repo_root = str(pathlib.Path(__file__).resolve().parents[2])
        env = dict(os.environ, PYTHONPATH=repo_root)
        res = subprocess.run(
            [sys.executable, "-P", "-s", "-c", code],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, f"stderr={res.stderr}\nstdout={res.stdout}")

    def test_win32_import_never_loads_posix_or_fcntl(self) -> None:
        # asyncio must bind for POSIX before the platform flip.
        code = (
            "import asyncio\n"
            + win_sim.SIMULATED_WIN32_PRELUDE
            + "from mesh.platform import proc\n"
            "from mesh.daemon import children\n"
            "from mesh.daemon.server import DaemonServer\n"
            "DaemonServer('/tmp/unused-home', testing_mode=True).setup_subreaper()\n"
            "assert 'mesh.platform._posix' not in sys.modules\n"
        )
        repo_root = str(pathlib.Path(__file__).resolve().parents[2])
        env = dict(os.environ, PYTHONPATH=repo_root)
        res = subprocess.run(
            [sys.executable, "-P", "-s", "-c", code],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, f"stderr={res.stderr}\nstdout={res.stdout}")

    def test_job_ctypes_struct_layouts_and_bindings(self) -> None:
        is_64 = ctypes.sizeof(ctypes.c_void_p) == 8

        self.assertEqual(ctypes.sizeof(w32.SECURITY_ATTRIBUTES), 24 if is_64 else 12)
        self.assertEqual(w32.SECURITY_ATTRIBUTES.nLength.offset, 0)
        self.assertEqual(w32.SECURITY_ATTRIBUTES.lpSecurityDescriptor.offset, 8 if is_64 else 4)
        self.assertEqual(w32.SECURITY_ATTRIBUTES.bInheritHandle.offset, 16 if is_64 else 8)

        self.assertEqual(ctypes.sizeof(w32.JOBOBJECT_BASIC_LIMIT_INFORMATION), 64 if is_64 else 48)
        self.assertEqual(w32.JOBOBJECT_BASIC_LIMIT_INFORMATION.LimitFlags.offset, 16)
        self.assertEqual(
            w32.JOBOBJECT_BASIC_LIMIT_INFORMATION.MinimumWorkingSetSize.offset,
            24 if is_64 else 20,
        )
        self.assertEqual(
            w32.JOBOBJECT_BASIC_LIMIT_INFORMATION.ActiveProcessLimit.offset,
            40 if is_64 else 28,
        )
        self.assertEqual(
            w32.JOBOBJECT_BASIC_LIMIT_INFORMATION.Affinity.offset,
            48 if is_64 else 32,
        )
        self.assertEqual(
            w32.JOBOBJECT_BASIC_LIMIT_INFORMATION.SchedulingClass.offset,
            60 if is_64 else 40,
        )

        self.assertEqual(ctypes.sizeof(w32.IO_COUNTERS), 48)
        self.assertEqual(w32.IO_COUNTERS.OtherTransferCount.offset, 40)

        self.assertEqual(ctypes.sizeof(w32.JOBOBJECT_EXTENDED_LIMIT_INFORMATION), 144 if is_64 else 112)
        self.assertEqual(
            w32.JOBOBJECT_EXTENDED_LIMIT_INFORMATION.IoInfo.offset,
            64 if is_64 else 48,
        )
        self.assertEqual(
            w32.JOBOBJECT_EXTENDED_LIMIT_INFORMATION.ProcessMemoryLimit.offset,
            112 if is_64 else 96,
        )
        self.assertEqual(
            w32.JOBOBJECT_EXTENDED_LIMIT_INFORMATION.PeakJobMemoryUsed.offset,
            136 if is_64 else 108,
        )

        self.assertEqual(ctypes.sizeof(w32.JOBOBJECT_BASIC_PROCESS_ID_LIST), 16 if is_64 else 12)
        self.assertEqual(w32.JOBOBJECT_BASIC_PROCESS_ID_LIST.NumberOfAssignedProcesses.offset, 0)
        self.assertEqual(w32.JOBOBJECT_BASIC_PROCESS_ID_LIST.NumberOfProcessIdsInList.offset, 4)
        self.assertEqual(w32.JOBOBJECT_BASIC_PROCESS_ID_LIST.ProcessIdList.offset, 8)

        self.assertEqual(ctypes.sizeof(w32.THREADENTRY32), 28)
        self.assertEqual(w32.THREADENTRY32.dwSize.offset, 0)
        self.assertEqual(w32.THREADENTRY32.th32ThreadID.offset, 8)
        self.assertEqual(w32.THREADENTRY32.th32OwnerProcessID.offset, 12)

        class _FakeFn:
            pass

        class _FakeK32:
            def __init__(self) -> None:
                for name in (
                    "CreateJobObjectW",
                    "OpenJobObjectW",
                    "SetInformationJobObject",
                    "QueryInformationJobObject",
                    "AssignProcessToJobObject",
                    "IsProcessInJob",
                    "TerminateJobObject",
                    "OpenThread",
                    "ResumeThread",
                    "Thread32First",
                    "Thread32Next",
                    "CreateEventW",
                    "OpenEventW",
                    "SetEvent",
                    "GenerateConsoleCtrlEvent",
                    "GetConsoleProcessList",
                ):
                    setattr(self, name, _FakeFn())

        class _FakeNt:
            def __init__(self) -> None:
                self.NtGetNextThread = _FakeFn()

        k32 = w32.bind_kernel32_jobs(_FakeK32())
        self.assertEqual(k32.CreateJobObjectW.restype, wintypes.HANDLE)
        self.assertEqual(len(k32.CreateJobObjectW.argtypes), 2)
        self.assertEqual(k32.OpenJobObjectW.restype, wintypes.HANDLE)
        self.assertEqual(len(k32.OpenJobObjectW.argtypes), 3)
        self.assertEqual(k32.SetInformationJobObject.restype, wintypes.BOOL)
        self.assertEqual(len(k32.SetInformationJobObject.argtypes), 4)
        self.assertEqual(k32.QueryInformationJobObject.restype, wintypes.BOOL)
        self.assertEqual(len(k32.QueryInformationJobObject.argtypes), 5)
        self.assertEqual(k32.AssignProcessToJobObject.restype, wintypes.BOOL)
        self.assertEqual(k32.IsProcessInJob.restype, wintypes.BOOL)
        self.assertEqual(len(k32.IsProcessInJob.argtypes), 3)
        self.assertEqual(k32.TerminateJobObject.restype, wintypes.BOOL)
        self.assertEqual(k32.OpenThread.restype, wintypes.HANDLE)
        self.assertEqual(k32.ResumeThread.restype, wintypes.DWORD)
        self.assertEqual(k32.Thread32First.restype, wintypes.BOOL)
        self.assertEqual(k32.Thread32Next.restype, wintypes.BOOL)
        self.assertEqual(k32.CreateEventW.restype, wintypes.HANDLE)
        self.assertEqual(k32.OpenEventW.restype, wintypes.HANDLE)
        self.assertEqual(k32.SetEvent.restype, wintypes.BOOL)
        self.assertEqual(k32.GenerateConsoleCtrlEvent.restype, wintypes.BOOL)
        self.assertEqual(k32.GetConsoleProcessList.restype, wintypes.DWORD)
        self.assertEqual(len(k32.GetConsoleProcessList.argtypes), 2)

        nt = w32.bind_ntdll_jobs(_FakeNt())
        self.assertEqual(nt.NtGetNextThread.restype, ctypes.c_int32)
        self.assertEqual(len(nt.NtGetNextThread.argtypes), 6)

    @unittest.skipUnless(sys.platform != "win32", "POSIX-only Non-Windows guard")
    def test_posix_facade_guards_and_signal_if_same(self) -> None:
        with self.assertRaises(NotImplementedError):
            plat_proc.create_ephemeral_job()
        with self.assertRaises(NotImplementedError):
            plat_proc.create_run_job("/tmp/home", "run_1")
        with self.assertRaises(NotImplementedError):
            plat_proc.run_job_name("/tmp/home", "run_1")
        with self.assertRaises(NotImplementedError):
            plat_proc.run_stop_event_name("/tmp/home", "run_1")

        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            st = plat_proc.get_proc_stat(child.pid)
            self.assertIsNotNone(st)
            self.assertFalse(plat_proc.signal_if_same(child.pid, st["starttime"] + 1, signal.SIGTERM))
            self.assertIsNone(child.poll())
            self.assertTrue(plat_proc.kill_if_same(child.pid, st["starttime"]))
            child.wait(timeout=10)
            self.assertFalse(plat_proc.kill_if_same(child.pid, st["starttime"]))
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)


class TestWin32JobsAndKillMocked(unittest.TestCase):
    """Mocked Win32 syscall tests for Job Objects, handle-pinned kill, and children.py."""

    def test_ephemeral_and_named_run_jobs_and_error_paths(self) -> None:
        fake = _FakeWinJobKernel({})

        # Ephemeral job sets KILL_ON_JOB_CLOSE and leaves breakaway flags 0
        eph = _win32.create_ephemeral_job_win(
            k32=fake,
            get_last_error_fn=fake.get_last_error,
            set_last_error_fn=fake.set_last_error,
        )
        self.assertTrue(eph.ephemeral)
        self.assertIsNone(eph.name)
        j_id = fake.handle_table[eph.fileno()][1]
        self.assertEqual(fake.jobs_by_id[j_id].limit_flags, w32.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
        self.assertEqual(
            fake.jobs_by_id[j_id].limit_flags
            & (w32.JOB_OBJECT_LIMIT_BREAKAWAY_OK | w32.JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK),
            0,
        )
        eph.close()
        eph.close()  # idempotent
        self.assertTrue(eph.closed)
        with self.assertRaises(OSError):
            eph.fileno()

        # SetInformationJobObject failure closes job handle without leaking
        fake.fail_set_info_error = w32.ERROR_ACCESS_DENIED
        with self.assertRaises(PermissionError):
            _win32.create_ephemeral_job_win(
                k32=fake,
                get_last_error_fn=fake.get_last_error,
                set_last_error_fn=fake.set_last_error,
            )
        fake.fail_set_info_error = None

        # Named run job builds deterministic Local\ name, owner-only SDDL, LimitFlags=0
        with mock.patch("mesh.platform.paths.norm_cmp", side_effect=lambda p: p.lower()), \
                mock.patch("mesh.platform.paths.os.path.realpath", side_effect=lambda p: p.replace("/", "\\")):
            name_a = _win32.run_job_name_win("C:/Users/Test/MeshHome", "run_123")
            name_b = _win32.run_job_name_win("c:\\users\\test\\meshhome", "run_123")
        self.assertEqual(name_a, name_b)
        self.assertRegex(name_a, r"^Local\\mesh-job-[0-9a-f]{16}-run_123$")

        with self.assertRaises(ValueError):
            _win32.run_job_name_win("/tmp/home", "bad\\id")
        with self.assertRaises(ValueError):
            _win32.run_job_name_win("/tmp/home", "../escape")

        run_job = _win32.create_run_job_win(
            "/tmp/home_a",
            "run_abc",
            k32=fake,
            advapi=fake,
            get_last_error_fn=fake.get_last_error,
            set_last_error_fn=fake.set_last_error,
        )
        self.assertFalse(run_job.ephemeral)
        rj_id = fake.handle_table[run_job.fileno()][1]
        self.assertEqual(fake.jobs_by_id[rj_id].limit_flags, 0)
        self.assertEqual(
            fake.jobs_by_id[rj_id].sddl,
            f"D:P(A;;GA;;;{fake.user_sid})(A;;GA;;;SY)(A;;GA;;;BA)",
        )

        # Creating duplicate named job fails closed (closes handle, raises FileExistsError)
        with self.assertRaises(FileExistsError):
            _win32.create_run_job_win(
                "/tmp/home_a",
                "run_abc",
                k32=fake,
                advapi=fake,
                get_last_error_fn=fake.get_last_error,
                set_last_error_fn=fake.set_last_error,
            )

        # Open existing job succeeds (verifies READ_CONTROL + trusted owner SID); missing job returns None;
        # ACCESS_DENIED raises PermissionError; untrusted owner SID fails closed and closes handle.
        fake.op_log.clear()
        reopened = _win32.open_run_job_win(
            "/tmp/home_a",
            "run_abc",
            terminate=True,
            k32=fake,
            advapi=fake,
            get_last_error_fn=fake.get_last_error,
        )
        self.assertIsNotNone(reopened)
        open_ops = [op for op in fake.op_log if op[0] == "OpenJobObjectW"]
        self.assertTrue(open_ops and (open_ops[-1][1] & w32.READ_CONTROL))
        reopened.close()

        # Trusted built-in owner SID (Administrators S-1-5-32-544) is accepted
        job_obj = fake.jobs_by_id[fake.named_jobs[run_job.name]]
        job_obj.owner_sid = "S-1-5-32-544"
        reopened_admin = _win32.open_run_job_win(
            "/tmp/home_a",
            "run_abc",
            terminate=True,
            k32=fake,
            advapi=fake,
            get_last_error_fn=fake.get_last_error,
        )
        self.assertIsNotNone(reopened_admin)
        reopened_admin.close()

        # Untrusted/squatted owner SID must close handle and raise PermissionError
        job_obj.owner_sid = "S-1-5-21-999-888-777-2002"
        before_ref = job_obj.ref_count
        with self.assertRaises(PermissionError):
            _win32.open_run_job_win(
                "/tmp/home_a",
                "run_abc",
                terminate=True,
                k32=fake,
                advapi=fake,
                get_last_error_fn=fake.get_last_error,
            )
        self.assertEqual(job_obj.ref_count, before_ref)
        # GetSecurityInfo failure must close handle and raise PermissionError
        job_obj.owner_sid = fake.user_sid
        fake.fail_get_security_info_error = w32.ERROR_ACCESS_DENIED
        with self.assertRaises(PermissionError):
            _win32.open_run_job_win(
                "/tmp/home_a",
                "run_abc",
                terminate=True,
                k32=fake,
                advapi=fake,
                get_last_error_fn=fake.get_last_error,
            )
        fake.fail_get_security_info_error = None
        self.assertEqual(job_obj.ref_count, before_ref)
        # While run_job is still open, only run_job's handle remains unclosed; closing it balances all handles
        self.assertEqual(len(fake.open_handles) - len(fake.closed_handles), 1)
        run_job.close()
        self.assertEqual(sorted(fake.closed_handles), sorted(fake.open_handles))
        self.assertEqual(sorted(fake.local_frees), sorted(fake.local_allocs))

        self.assertIsNone(
            _win32.open_run_job_win(
                "/tmp/home_a",
                "run_missing",
                k32=fake,
                advapi=fake,
                get_last_error_fn=fake.get_last_error,
            )
        )
        fake.fail_open_job_error = w32.ERROR_ACCESS_DENIED
        with self.assertRaises(PermissionError):
            _win32.open_run_job_win(
                "/tmp/home_a",
                "run_abc",
                k32=fake,
                advapi=fake,
                get_last_error_fn=fake.get_last_error,
            )
        fake.fail_open_job_error = None

        self.assertEqual(sorted(fake.closed_handles), sorted(fake.open_handles))
        self.assertEqual(sorted(fake.local_frees), sorted(fake.local_allocs))

    def test_query_job_pids_resize_is_process_in_job_and_terminate(self) -> None:
        procs = {
            pid: _FakeProcObj(pid=pid, ppid=100, creation_time=1000 + pid, threads=[pid * 10])
            for pid in range(200, 360)
        }
        procs[999] = _FakeProcObj(pid=999, ppid=50, creation_time=9999)
        procs[998] = _FakeProcObj(pid=998, ppid=50, creation_time=9998, access_denied_open=True)
        fake = _FakeWinJobKernel(procs)

        job = _win32.create_ephemeral_job_win(
            k32=fake,
            get_last_error_fn=fake.get_last_error,
            set_last_error_fn=fake.set_last_error,
        )
        self.assertEqual(_win32.query_job_pids_win(job, k32=fake, get_last_error_fn=fake.get_last_error), [])

        # Assign 160 processes (> initial capacity 64) + exercise ERROR_MORE_DATA and short-list retries
        for pid in range(200, 360):
            _win32.assign_process_to_job_win(job, pid, k32=fake, get_last_error_fn=fake.get_last_error)

        fake.force_more_data_once = True
        pids = _win32.query_job_pids_win(
            job, k32=fake, get_last_error_fn=fake.get_last_error, initial_capacity=4
        )
        self.assertEqual(pids, list(range(200, 360)))

        fake.force_short_list_once = True
        pids2 = _win32.query_job_pids_win(
            job, k32=fake, get_last_error_fn=fake.get_last_error, initial_capacity=8
        )
        self.assertEqual(pids2, list(range(200, 360)))

        self.assertTrue(_win32.is_process_in_job_win(250, job, k32=fake, get_last_error_fn=fake.get_last_error))
        self.assertFalse(_win32.is_process_in_job_win(999, job, k32=fake, get_last_error_fn=fake.get_last_error))
        self.assertFalse(_win32.is_process_in_job_win(55555, job, k32=fake, get_last_error_fn=fake.get_last_error))
        with self.assertRaises(PermissionError):
            _win32.is_process_in_job_win(998, job, k32=fake, get_last_error_fn=fake.get_last_error)

        _win32.terminate_job_win(job, exit_code=137, k32=fake, get_last_error_fn=fake.get_last_error)
        self.assertTrue(all(not procs[pid].alive and procs[pid].exit_code == 137 for pid in range(200, 360)))
        self.assertTrue(procs[999].alive)
        self.assertEqual(_win32.query_job_pids_win(job, k32=fake, get_last_error_fn=fake.get_last_error), [])
        job.close()

        self.assertEqual(sorted(fake.closed_handles), sorted(fake.open_handles))

    def test_spawn_in_job_suspended_assign_resume_and_failure_cleanup(self) -> None:
        procs: dict[int, _FakeProcObj] = {}
        fake = _FakeWinJobKernel(procs)

        class _FakeHandleBox:
            def __init__(self, raw: int) -> None:
                self.raw = raw
                self.closed = False

            def __int__(self) -> int:
                return self.raw

            def Close(self) -> None:
                if not self.closed:
                    self.closed = True
                    fake.CloseHandle(self.raw)

        class _FakeStream:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        class _FakePopen:
            next_pid = 400

            def __init__(self, argv: list[str], **kwargs: Any) -> None:
                self.argv = argv
                self.kwargs = kwargs
                self.pid = _FakePopen.next_pid
                _FakePopen.next_pid += 1
                entry = _FakeProcObj(
                    pid=self.pid,
                    ppid=100,
                    creation_time=5000 + self.pid,
                    threads=[self.pid * 10 + 1],
                )
                procs[self.pid] = entry
                h = fake._alloc_handle("proc", (entry, w32.PROCESS_TERMINATE | w32.PROCESS_SET_QUOTA))
                self._handle = _FakeHandleBox(h)
                self.stdout = _FakeStream()
                self.waited = False
                fake.op_log.append(("Popen", self.pid, kwargs.get("creationflags")))

            def wait(self, timeout: float | None = None) -> int:
                _ = timeout
                self.waited = True
                return 0

        job = _win32.create_ephemeral_job_win(
            k32=fake, get_last_error_fn=fake.get_last_error, set_last_error_fn=fake.set_last_error
        )
        proc = _win32.spawn_in_job_win(
            ["python.exe", "-c", "pass"],
            job,
            popen_fn=_FakePopen,
            k32=fake,
            ntdll=fake,
            get_last_error_fn=fake.get_last_error,
            start_new_session=True,
        )
        self.assertIs(proc._mesh_job, job)
        self.assertEqual(proc._mesh_job_handle, job.handle)
        expected_flags = w32.CREATE_SUSPENDED | w32.CREATE_NEW_PROCESS_GROUP | w32.CREATE_NO_WINDOW
        self.assertEqual(proc.kwargs["creationflags"], expected_flags)
        self.assertNotIn("start_new_session", proc.kwargs)

        # Verify strict order: Popen(CREATE_SUSPENDED) -> AssignProcessToJobObject -> ResumeThread
        seq = [op[0] for op in fake.op_log if op[0] in ("Popen", "AssignProcessToJobObject", "ResumeThread")]
        self.assertEqual(seq, ["Popen", "AssignProcessToJobObject", "ResumeThread"])
        proc._handle.Close()

        # DETACHED_PROCESS must omit CREATE_NO_WINDOW, and Toolhelp32 fallback works without NtGetNextThread
        fake.op_log.clear()
        proc2 = _win32.spawn_in_job_win(
            ["python.exe", "-c", "pass"],
            job,
            popen_fn=_FakePopen,
            k32=fake,
            ntdll=_FakeNtNoGetNext(),
            get_last_error_fn=fake.get_last_error,
            creationflags=w32.DETACHED_PROCESS,
        )
        self.assertEqual(
            proc2.kwargs["creationflags"],
            w32.DETACHED_PROCESS | w32.CREATE_SUSPENDED | w32.CREATE_NEW_PROCESS_GROUP,
        )
        seq2 = [op[0] for op in fake.op_log if op[0] in ("Popen", "AssignProcessToJobObject", "ResumeThread")]
        self.assertEqual(seq2, ["Popen", "AssignProcessToJobObject", "ResumeThread"])
        proc2._handle.Close()

        # AssignProcessToJobObject failure must TerminateProcess + close streams without resuming
        fake.op_log.clear()
        fake.fail_assign_error = w32.ERROR_ACCESS_DENIED
        created_procs: list[_FakePopen] = []

        def _tracking_popen(*a: Any, **kw: Any) -> _FakePopen:
            p = _FakePopen(*a, **kw)
            created_procs.append(p)
            return p

        with self.assertRaises(PermissionError):
            _win32.spawn_in_job_win(
                ["python.exe"],
                job,
                popen_fn=_tracking_popen,
                k32=fake,
                ntdll=fake,
                get_last_error_fn=fake.get_last_error,
            )
        failed_p = created_procs[-1]
        self.assertFalse(procs[failed_p.pid].alive)
        self.assertTrue(failed_p.stdout.closed)
        self.assertTrue(failed_p.waited)
        self.assertNotIn("ResumeThread", [op[0] for op in fake.op_log])
        fake.fail_assign_error = None

        # ResumeThread failure must TerminateProcess + clean up
        fake.op_log.clear()
        fake.fail_resume_error = w32.ERROR_ACCESS_DENIED
        with self.assertRaises(PermissionError):
            _win32.spawn_in_job_win(
                ["python.exe"],
                job,
                popen_fn=_tracking_popen,
                k32=fake,
                ntdll=fake,
                get_last_error_fn=fake.get_last_error,
            )
        failed_p2 = created_procs[-1]
        self.assertFalse(procs[failed_p2.pid].alive)
        self.assertTrue(failed_p2.stdout.closed)
        fake.fail_resume_error = None

        # Missing/unusable proc._handle inside spawn_in_job_win must still terminate & clean up (P3-1)
        class _FakeNoHandlePopen:
            def __init__(self, pid: int, handle_val: Any = None, omit_attr: bool = False) -> None:
                self.pid = pid
                procs[pid] = _FakeProcObj(
                    pid=pid, ppid=100, creation_time=5000 + pid, threads=[pid * 10 + 1]
                )
                if not omit_attr:
                    self._handle = handle_val
                self.stdout = _FakeStream()
                self.waited = False

            def wait(self, timeout: float | None = None) -> int:
                _ = timeout
                self.waited = True
                return 0

        no_h_procs: list[_FakeNoHandlePopen] = []
        for idx, (omit_attr, h_val, exc_cls) in enumerate(
            [(True, None, AttributeError), (False, None, TypeError), (False, 0, OSError)]
        ):
            def _no_h_popen(*_a: Any, _pid: int = 480 + idx, _omit: bool = omit_attr, _hv: Any = h_val, **_kw: Any) -> _FakeNoHandlePopen:
                p = _FakeNoHandlePopen(_pid, handle_val=_hv, omit_attr=_omit)
                no_h_procs.append(p)
                return p

            with self.assertRaises(exc_cls):
                _win32.spawn_in_job_win(
                    ["python.exe"],
                    job,
                    popen_fn=_no_h_popen,
                    k32=fake,
                    ntdll=fake,
                    get_last_error_fn=fake.get_last_error,
                )
            bad_p = no_h_procs[-1]
            self.assertFalse(procs[bad_p.pid].alive)
            self.assertTrue(bad_p.stdout.closed)
            self.assertTrue(bad_p.waited)

        # NtGetNextThread failing with non-NOT_IMPLEMENTED status falls back to Toolhelp32 (P3-7)
        fake.op_log.clear()
        procs[490] = _FakeProcObj(pid=490, ppid=100, creation_time=5490, threads=[4901])
        h490 = fake.OpenProcess(w32.PROCESS_QUERY_INFORMATION, False, 490)
        fake.fail_nt_get_next_status = w32.STATUS_ACCESS_DENIED
        _win32.resume_process_main_thread_win(
            h490, 490, k32=fake, ntdll=fake, get_last_error_fn=fake.get_last_error
        )
        self.assertIn(("ResumeThread", 490, 4901), fake.op_log)
        # If Toolhelp32 also finds no threads, original NT failure raises (fail-closed)
        procs[491] = _FakeProcObj(pid=491, ppid=100, creation_time=5491, threads=[])
        h491 = fake.OpenProcess(w32.PROCESS_QUERY_INFORMATION, False, 491)
        with self.assertRaises(PermissionError):
            _win32.resume_process_main_thread_win(
                h491, 491, k32=fake, ntdll=fake, get_last_error_fn=fake.get_last_error
            )
        fake.fail_nt_get_next_status = None
        fake.CloseHandle(h490)
        fake.CloseHandle(h491)

        # resume_process_main_thread_win(None, pid) opens a handle or uses Toolhelp32 (P1-1)
        fake.op_log.clear()
        _win32.resume_process_main_thread_win(
            None, 490, k32=fake, ntdll=fake, get_last_error_fn=fake.get_last_error
        )
        self.assertIn(("ResumeThread", 490, 4901), fake.op_log)
        fake.op_log.clear()
        _win32.resume_process_main_thread_win(
            None, 490, k32=fake, ntdll=_FakeNtNoGetNext(), get_last_error_fn=fake.get_last_error
        )
        self.assertIn(("ResumeThread", 490, 4901), fake.op_log)

        job.close()
        self.assertEqual(sorted(fake.closed_handles), sorted(fake.open_handles))

    def test_handle_pinned_signal_and_kill_if_same_pid_reuse_guard(self) -> None:
        procs = {
            200: _FakeProcObj(pid=200, ppid=100, creation_time=9000),
            201: _FakeProcObj(pid=201, ppid=100, creation_time=9100),
            202: _FakeProcObj(pid=202, ppid=100, creation_time=9200, alive=False, exit_code=259),
            203: _FakeProcObj(pid=203, ppid=100, creation_time=9300, access_denied_open=True),
            204: _FakeProcObj(pid=204, ppid=100, creation_time=9400, access_denied_terminate=True),
        }
        fake = _FakeWinJobKernel(procs, current_pid=100)

        # Guards: pid <= 1, self pid, zero/negative starttime
        self.assertFalse(_win32.kill_if_same_win(0, 9000, k32=fake, get_last_error_fn=fake.get_last_error))
        self.assertFalse(_win32.kill_if_same_win(1, 9000, k32=fake, get_last_error_fn=fake.get_last_error))
        self.assertFalse(_win32.kill_if_same_win(100, 9000, k32=fake, get_last_error_fn=fake.get_last_error))
        self.assertFalse(_win32.kill_if_same_win(200, 0, k32=fake, get_last_error_fn=fake.get_last_error))

        # PID reuse guard: mismatched CreationTime must NOT call TerminateProcess
        self.assertFalse(
            _win32.kill_if_same_win(200, 9001, k32=fake, get_last_error_fn=fake.get_last_error)
        )
        self.assertTrue(procs[200].alive)
        self.assertNotIn("TerminateProcess", [op[0] for op in fake.op_log])

        # Probe signal 0 verifies identity without killing
        self.assertTrue(
            _win32.signal_if_same_win(200, 9000, 0, k32=fake, get_last_error_fn=fake.get_last_error)
        )
        self.assertTrue(procs[200].alive)

        # SIGTERM prefers named stop event when run_id + home exist
        with mock.patch("mesh.platform.paths.norm_cmp", side_effect=lambda p: p.lower()), \
                mock.patch("mesh.platform.paths.os.path.realpath", side_effect=lambda p: p):
            stop_ev = _win32.create_stop_event_win(
                "/tmp/h",
                "run_ev",
                k32=fake,
                advapi=fake,
                get_last_error_fn=fake.get_last_error,
                set_last_error_fn=fake.set_last_error,
            )
            self.assertTrue(
                _win32.signal_if_same_win(
                    200,
                    9000,
                    signal.SIGTERM,
                    run_id="run_ev",
                    home="/tmp/h",
                    k32=fake,
                    get_last_error_fn=fake.get_last_error,
                )
            )
            self.assertTrue(fake.events_by_name[stop_ev.name]["signalled"])
            stop_ev.close()

        # P2-5: SIGTERM without stop event returns False and NEVER invokes GenerateConsoleCtrlEvent
        fake.op_log.clear()
        self.assertFalse(
            _win32.signal_if_same_win(
                201, 9100, signal.SIGTERM, k32=fake, get_last_error_fn=fake.get_last_error
            )
        )
        self.assertNotIn("GenerateConsoleCtrlEvent", [op[0] for op in fake.op_log])
        self.assertNotIn("GetConsoleProcessList", [op[0] for op in fake.op_log])
        self.assertTrue(procs[201].alive)

        procs[50] = _FakeProcObj(pid=50, ppid=1, creation_time=8000)
        fake.op_log.clear()
        self.assertFalse(
            _win32.signal_if_same_win(
                50, 8000, signal.SIGTERM, k32=fake, get_last_error_fn=fake.get_last_error
            )
        )
        self.assertNotIn("GenerateConsoleCtrlEvent", [op[0] for op in fake.op_log])
        self.assertTrue(procs[50].alive)

        with mock.patch.object(_win32, "signal_run_stop_event_win", return_value=True) as m_stop_ev:
            self.assertTrue(
                _win32.signal_if_same_win(
                    201,
                    9100,
                    signal.SIGTERM,
                    run_id="run_stop",
                    home="/tmp/home_stop",
                    k32=fake,
                    get_last_error_fn=fake.get_last_error,
                )
            )
            m_stop_ev.assert_called_once()
        fake.op_log.clear()

        # Exact creation_time kill_if_same pins handle before verifying and terminates
        fake.op_log.clear()
        self.assertTrue(
            _win32.kill_if_same_win(200, 9000, k32=fake, get_last_error_fn=fake.get_last_error)
        )
        self.assertFalse(procs[200].alive)
        self.assertEqual(
            [op[0] for op in fake.op_log],
            ["OpenProcess", "WaitForSingleObject", "GetProcessTimes", "WaitForSingleObject", "TerminateProcess"],
        )

        # Dead process (exit code 259) and missing PID return False
        self.assertFalse(_win32.kill_if_same_win(202, 9200, k32=fake, get_last_error_fn=fake.get_last_error))
        self.assertFalse(_win32.kill_if_same_win(8888, 9200, k32=fake, get_last_error_fn=fake.get_last_error))

        # Access denied fails closed (returns False)
        self.assertFalse(_win32.kill_if_same_win(203, 9300, k32=fake, get_last_error_fn=fake.get_last_error))
        self.assertFalse(_win32.kill_if_same_win(204, 9400, k32=fake, get_last_error_fn=fake.get_last_error))

        self.assertEqual(sorted(fake.closed_handles), sorted(fake.open_handles))
        self.assertEqual(sorted(fake.local_frees), sorted(fake.local_allocs))

    def test_children_server_and_run_tier_win32_mocked(self) -> None:
        from mesh.daemon.server import DaemonServer
        from tests import run_tier

        events: list[tuple[Any, ...]] = []

        class _FakeJob:
            def __init__(self, kill_on_close: bool) -> None:
                self.kill_on_close = kill_on_close
                self.handle = 777
                self.closed = False
                self.terminated = False

            def close(self) -> None:
                self.closed = True
                events.append(("job_close", self.kill_on_close))

        class _FakePopen:
            def __init__(self, pid: int = 5150, timeout_on_first_comm: bool = False) -> None:
                self.pid = pid
                self._handle = 888
                self.returncode = 0
                self._timeout_first = timeout_on_first_comm
                self.killed = False
                self.stdout = None
                self.stderr = None
                self.stdin = None

            def poll(self) -> int | None:
                return 137 if self.killed else None

            def kill(self) -> None:
                self.killed = True
                self.returncode = 137

            def communicate(self, input: Any = None, timeout: float | None = None) -> tuple[Any, Any]:
                _ = (input, timeout)
                if self._timeout_first:
                    self._timeout_first = False
                    raise subprocess.TimeoutExpired(["git"], timeout)
                return ("ok", "")

        def fake_create_job(*, kill_on_close: bool = True, name: str | None = None) -> _FakeJob:
            _ = name
            events.append(("create_job", kill_on_close))
            return _FakeJob(kill_on_close)

        def fake_spawn_in_job(argv: list[str], job: _FakeJob, **kwargs: Any) -> _FakePopen:
            popen_fn = kwargs.pop("popen_fn", None)
            _ = popen_fn
            should_hang = any("hang" in str(a) for a in argv)
            p = _FakePopen(timeout_on_first_comm=should_hang)
            p._mesh_job = job
            events.append(("spawn_in_job", argv, job.kill_on_close))
            return p

        def fake_terminate_job(job: _FakeJob, exit_code: int = 137) -> None:
            job.terminated = True
            if hasattr(job, "_proc_ref"):
                job._proc_ref.killed = True
            events.append(("terminate_job", exit_code))

        fake_plat = mock.MagicMock()
        fake_plat.create_job.side_effect = fake_create_job
        fake_plat.create_ephemeral_job.side_effect = lambda: fake_create_job(kill_on_close=True)
        fake_plat.spawn_in_job.side_effect = fake_spawn_in_job
        fake_plat.terminate_job.side_effect = fake_terminate_job

        with (
            mock.patch.object(children.sys, "platform", "win32"),
            mock.patch.object(children, "_plat_proc", fake_plat),
            mock.patch.object(children, "get_proc_stat", return_value={"starttime": 12345}),
        ):
            # 1. Wrapper popen creates non-kill-on-close job (I8)
            w_proc = children.popen_registered("wrapper", ["python", "runwrap"], start_new_session=True)
            self.assertFalse(w_proc._mesh_job.kill_on_close)
            self.assertEqual(children.registered()[w_proc.pid], ("wrapper", 12345))
            children.unregister(w_proc.pid)

            # 2. run_registered timeout calls terminate_job and closes owned ephemeral job
            events.clear()
            with self.assertRaises(subprocess.TimeoutExpired):
                children.run_registered("git", ["git", "hang"], timeout=0.1, start_new_session=True)
            self.assertIn(("create_job", True), events)
            self.assertIn(("terminate_job", 137), events)
            self.assertIn(("job_close", True), events)
            self.assertEqual(children.registered_pids(), set())

            # 3. reap_orphans increments scans and returns [] without /proc
            before_scans = children.stats["scans"]
            self.assertEqual(children.reap_orphans(), [])
            self.assertEqual(children.stats["scans"], before_scans + 1)

            # 4. create_subprocess_exec_registered cleanup on register failure
            events.clear()

            class _FakeAsyncTransport:
                def __init__(self, handle: int) -> None:
                    self._proc = type("_P", (), {"_handle": handle})()

            class _FakeAsyncProc:
                def __init__(self) -> None:
                    self.pid = 6161
                    self._transport = _FakeAsyncTransport(999)
                    self.killed = False
                    self.waited = False

                def kill(self) -> None:
                    self.killed = True

                async def wait(self) -> int:
                    self.waited = True
                    return 137

            async def _run_async_fail() -> None:
                with (
                    mock.patch.object(children.asyncio, "create_subprocess_exec", return_value=_FakeAsyncProc()),
                    mock.patch.object(children, "register", side_effect=OSError("boom")),
                ):
                    with self.assertRaises(OSError):
                        await children.create_subprocess_exec_registered(
                            "probe", "python", "-c", "pass", start_new_session=True
                        )

            asyncio.run(_run_async_fail())
            fake_plat.assign_process_handle_to_job.assert_called_once()
            fake_plat.resume_process_main_thread.assert_called_once()
            self.assertIn(("terminate_job", 137), events)
            self.assertIn(("job_close", True), events)
            self.assertEqual(children.spawns_in_flight(), 0)

            # 4b. create_subprocess_exec_registered without _handle resumes via PID (P1-1)
            class _FakeNoHandleAsyncProc:
                def __init__(self, pid: int = 6262) -> None:
                    self.pid = pid
                    self._transport = object()
                    self.killed = False
                    self.waited = False

                def kill(self) -> None:
                    self.killed = True

                async def wait(self) -> int:
                    self.waited = True
                    return 0

            fake_plat.reset_mock()
            events.clear()
            no_h_proc = _FakeNoHandleAsyncProc(6262)

            async def _run_async_no_handle_ok() -> Any:
                with mock.patch.object(
                    children.asyncio, "create_subprocess_exec", return_value=no_h_proc
                ):
                    return await children.create_subprocess_exec_registered(
                        "probe", "python", "-c", "pass", start_new_session=True
                    )

            got_proc = asyncio.run(_run_async_no_handle_ok())
            self.assertIs(got_proc, no_h_proc)
            fake_plat.assign_process_to_job.assert_called_once()
            fake_plat.resume_process_main_thread.assert_called_once_with(None, 6262)
            self.assertEqual(children.registered()[6262], ("probe", 12345))
            children.unregister(6262)
            got_proc._mesh_job.close()
            self.assertEqual(children.spawns_in_flight(), 0)

            # 4c. create_subprocess_exec_registered without _handle cleans up if resume fails (P1-1)
            fake_plat.reset_mock()
            events.clear()
            no_h_fail_proc = _FakeNoHandleAsyncProc(6363)
            fake_plat.resume_process_main_thread.side_effect = OSError("cannot resume")

            async def _run_async_no_handle_fail() -> None:
                with mock.patch.object(
                    children.asyncio, "create_subprocess_exec", return_value=no_h_fail_proc
                ):
                    with self.assertRaises(OSError):
                        await children.create_subprocess_exec_registered(
                            "probe", "python", "-c", "pass", start_new_session=True
                        )

            asyncio.run(_run_async_no_handle_fail())
            fake_plat.assign_process_to_job.assert_called_once()
            fake_plat.resume_process_main_thread.assert_called_once_with(None, 6363)
            self.assertTrue(no_h_fail_proc.killed)
            self.assertTrue(no_h_fail_proc.waited)
            self.assertIn(("terminate_job", 137), events)
            self.assertIn(("job_close", True), events)
            self.assertEqual(children.spawns_in_flight(), 0)
            fake_plat.resume_process_main_thread.side_effect = None

        # 5. DaemonServer.setup_subreaper is a safe no-op on Windows
        srv = DaemonServer("/tmp/unused-home", testing_mode=True)
        with mock.patch("mesh.daemon.server.sys.platform", "win32"), \
                mock.patch("mesh.daemon.server.ctypes.CDLL", side_effect=AssertionError("must not load libc")):
            srv.setup_subreaper()

        # 6. tests/run_tier._run_single_test uses ephemeral job + terminate_job on Windows timeout
        events.clear()
        with (
            mock.patch.object(run_tier.sys, "platform", "win32"),
            mock.patch.object(run_tier, "_can_use_win_jobs", return_value=True),
            mock.patch.object(run_tier, "_platform_proc", fake_plat),
        ):
            tid, status, _dur, out = run_tier._run_single_test("tests.unit.hang", 1)
        self.assertEqual(tid, "tests.unit.hang")
        self.assertEqual(status, "ERROR")
        self.assertIn("timeout", out)
        self.assertIn(("create_job", True), events)
        self.assertIn(("terminate_job", 137), events)
        self.assertIn(("job_close", True), events)


def _read_line_bounded(pipe: Any, timeout_s: float = 15.0) -> str:
    q: queue.Queue[str] = queue.Queue(maxsize=1)

    def _reader() -> None:
        try:
            q.put(pipe.readline())
        except Exception:
            q.put("")

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    try:
        return q.get(timeout=timeout_s).strip()
    except queue.Empty as exc:
        raise AssertionError(f"Child failed to emit line within {timeout_s:g}s") from exc


@unittest.skipUnless(sys.platform == "win32", "Windows real-process Job Object tests")
class TestWin32JobsRealProcesses(unittest.TestCase):
    """Real Windows process tests for Job Objects, breakaway resistance, and pinned kill."""

    def _wait_dead(self, pid: int, starttime: int, timeout_s: float = 10.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline and plat_proc.is_process_alive(pid, starttime):
            time.sleep(0.05)
        self.assertFalse(plat_proc.is_process_alive(pid, starttime))

    def _force_kill_pid(self, pid: int | None) -> None:
        if pid is None or not plat_proc.is_tgid_alive(pid):
            return
        k32 = w32.get_kernel32()
        h = k32.OpenProcess(w32.PROCESS_TERMINATE, False, int(pid))
        if not w32.is_null_or_invalid_handle(h):
            try:
                k32.TerminateProcess(h, 137)
            finally:
                k32.CloseHandle(h)

    def test_win_real_ephemeral_job_kills_detached_wiped_env_grandchild_tree(self) -> None:
        grandchild_code = "import time; time.sleep(60)"
        child_code = (
            "import subprocess, sys, time\n"
            "flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP\n"
            "gc = subprocess.Popen(\n"
            "    [sys.executable, '-c', sys.argv[1]],\n"
            "    env={},\n"
            "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
            "    creationflags=flags,\n"
            ")\n"
            "print(gc.pid, flush=True)\n"
            "time.sleep(60)\n"
        )
        parent_code = (
            "import subprocess, sys, time\n"
            "ch = subprocess.Popen(\n"
            "    [sys.executable, '-c', sys.argv[1], sys.argv[2]],\n"
            "    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,\n"
            "    text=True,\n"
            ")\n"
            "gc_pid = ch.stdout.readline().strip()\n"
            "print(f'{ch.pid} {gc_pid}', flush=True)\n"
            "time.sleep(60)\n"
        )

        job = plat_proc.create_ephemeral_job()
        parent = None
        child_pid = None
        gc_pid = None
        try:
            parent = plat_proc.spawn_in_job(
                [sys.executable, "-c", parent_code, child_code, grandchild_code],
                job,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            line = _read_line_bounded(parent.stdout)
            parts = line.split()
            self.assertEqual(len(parts), 2, f"Unexpected child output: {line!r}")
            child_pid, gc_pid = int(parts[0]), int(parts[1])

            p_stat = plat_proc.get_proc_stat(parent.pid)
            c_stat = plat_proc.get_proc_stat(child_pid)
            g_stat = plat_proc.get_proc_stat(gc_pid)
            self.assertIsNotNone(p_stat)
            self.assertIsNotNone(c_stat)
            self.assertIsNotNone(g_stat)
            p_st = p_stat["starttime"]
            c_st = c_stat["starttime"]
            g_st = g_stat["starttime"]

            job_pids = plat_proc.query_job_pids(job)
            self.assertIn(parent.pid, job_pids)
            self.assertIn(child_pid, job_pids)
            self.assertIn(gc_pid, job_pids)
            self.assertTrue(plat_proc.is_process_in_job(parent, job))
            self.assertTrue(plat_proc.is_process_in_job(child_pid, job))
            self.assertTrue(plat_proc.is_process_in_job(gc_pid, job))

            plat_proc.terminate_job(job, 137)
            parent.wait(timeout=10)
            self._wait_dead(parent.pid, p_st)
            self._wait_dead(child_pid, c_st)
            self._wait_dead(gc_pid, g_st)
            self.assertEqual(plat_proc.query_job_pids(job), [])
        finally:
            job.close()
            if parent is not None:
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(timeout=5)
                if parent.stdout is not None:
                    parent.stdout.close()
            self._force_kill_pid(child_pid)
            self._force_kill_pid(gc_pid)

        # Also verify KILL_ON_JOB_CLOSE automatically kills tree when ephemeral JobHandle closes
        eph2 = plat_proc.create_ephemeral_job()
        p2 = None
        try:
            p2 = plat_proc.spawn_in_job(
                [sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(60)"],
                eph2,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            self.assertEqual(_read_line_bounded(p2.stdout), "ready")
            p2_stat = plat_proc.get_proc_stat(p2.pid)
            self.assertIsNotNone(p2_stat)
            eph2.close()
            p2.wait(timeout=10)
            self._wait_dead(p2.pid, p2_stat["starttime"])
        finally:
            eph2.close()
            if p2 is not None:
                if p2.poll() is None:
                    p2.kill()
                    p2.wait(timeout=5)
                if p2.stdout is not None:
                    p2.stdout.close()

    def test_win_real_named_run_job_survives_handle_close_and_reattaches(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            run_id = f"run_win_t07_{os.getpid()}_{uuid.uuid4().hex[:8]}"
            job_name = plat_proc.run_job_name(home, run_id)
            gc_code = "import time; time.sleep(60)"
            child_code = (
                "import subprocess, sys, time\n"
                "from mesh.platform import _win32_ctypes as w32\n"
                "k32 = w32.get_kernel32()\n"
                "h_job = k32.OpenJobObjectW(w32.JOB_OBJECT_QUERY, False, sys.argv[1])\n"
                "assert not w32.is_null_or_invalid_handle(h_job)\n"
                "gc = subprocess.Popen(\n"
                "    [sys.executable, '-c', sys.argv[2]],\n"
                "    env={},\n"
                "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
                "    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,\n"
                ")\n"
                "print(gc.pid, flush=True)\n"
                "time.sleep(60)\n"
            )
            repo_root = str(pathlib.Path(__file__).resolve().parents[2])
            env = dict(os.environ, PYTHONPATH=repo_root)

            job = plat_proc.create_run_job(home, run_id)
            child = None
            gc_pid = None
            reopened = None
            try:
                child = plat_proc.spawn_in_job(
                    [sys.executable, "-c", child_code, job_name, gc_code],
                    job,
                    cwd=repo_root,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
                line = _read_line_bounded(child.stdout)
                self.assertTrue(line.isdigit(), f"Expected grandchild PID, got {line!r}")
                gc_pid = int(line)
                c_stat = plat_proc.get_proc_stat(child.pid)
                g_stat = plat_proc.get_proc_stat(gc_pid)
                self.assertIsNotNone(c_stat)
                self.assertIsNotNone(g_stat)
                c_st = c_stat["starttime"]
                g_st = g_stat["starttime"]

                # Simulate meshd stop/crash closing parent's Named Run Job handle: processes MUST survive
                job.close()
                time.sleep(0.2)
                self.assertTrue(plat_proc.is_process_alive(child.pid, c_st))
                self.assertTrue(plat_proc.is_process_alive(gc_pid, g_st))

                # Re-attach from fresh OpenJobObjectW handle and kill entire tree
                reopened = plat_proc.open_run_job(home, run_id, terminate=True)
                self.assertIsNotNone(reopened)
                pids = plat_proc.query_job_pids(reopened)
                self.assertIn(child.pid, pids)
                self.assertIn(gc_pid, pids)

                plat_proc.terminate_job(reopened, 137)
                child.wait(timeout=10)
                self._wait_dead(child.pid, c_st)
                self._wait_dead(gc_pid, g_st)
            finally:
                job.close()
                if reopened is not None:
                    reopened.close()
                if child is not None:
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=5)
                    if child.stdout is not None:
                        child.stdout.close()
                self._force_kill_pid(gc_pid)

    def test_win_real_handle_pinned_kill_and_stop_event(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            run_id = f"run_stop_{os.getpid()}_{uuid.uuid4().hex[:8]}"
            ev_name = plat_proc.run_stop_event_name(home, run_id)
            ev = plat_proc.create_stop_event(home, run_id)
            repo_root = str(pathlib.Path(__file__).resolve().parents[2])
            env = dict(os.environ, PYTHONPATH=repo_root)
            wait_code = (
                "import sys\n"
                "from mesh.platform import _win32_ctypes as w32\n"
                "k32 = w32.get_kernel32()\n"
                "h = k32.OpenEventW(w32.SYNCHRONIZE, False, sys.argv[1])\n"
                "assert not w32.is_null_or_invalid_handle(h)\n"
                "print('ready', flush=True)\n"
                "k32.WaitForSingleObject(h, 30000)\n"
                "k32.CloseHandle(h)\n"
                "sys.exit(42)\n"
            )
            p1 = subprocess.Popen(
                [sys.executable, "-c", wait_code, ev_name],
                cwd=repo_root,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            p2 = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            try:
                self.assertEqual(_read_line_bounded(p1.stdout), "ready")
                st1 = plat_proc.get_proc_stat(p1.pid)["starttime"]
                st2 = plat_proc.get_proc_stat(p2.pid)["starttime"]

                # Wrong starttime must never signal or kill
                self.assertFalse(plat_proc.signal_if_same(p1.pid, st1 + 1, signal.SIGTERM, run_id=run_id, home=home))
                self.assertFalse(plat_proc.kill_if_same(p2.pid, st2 + 1))
                self.assertIsNone(p2.poll())

                # Graceful stop event via signal_if_same(..., SIGTERM)
                self.assertTrue(plat_proc.signal_if_same(p1.pid, st1, signal.SIGTERM, run_id=run_id, home=home))
                self.assertEqual(p1.wait(timeout=10), 42)
                self.assertFalse(plat_proc.signal_if_same(p1.pid, st1, signal.SIGTERM, run_id=run_id, home=home))

                # Handle-pinned hard kill
                self.assertTrue(plat_proc.kill_if_same(p2.pid, st2))
                p2.wait(timeout=10)
                self._wait_dead(p2.pid, st2)
                self.assertFalse(plat_proc.kill_if_same(p2.pid, st2))
            finally:
                ev.close()
                for p in (p1, p2):
                    if p.poll() is None:
                        p.kill()
                        p.wait(timeout=5)
                if p1.stdout is not None:
                    p1.stdout.close()

    def test_win_real_children_run_registered_timeout_and_noop_reaper(self) -> None:
        from mesh.daemon.server import DaemonServer

        with tempfile.TemporaryDirectory() as td:
            pid_file = os.path.join(td, "gc.pid")
            gc_code = "import time; time.sleep(60)"
            child_code = (
                "import pathlib, subprocess, sys, time\n"
                "gc = subprocess.Popen(\n"
                "    [sys.executable, '-c', sys.argv[2]],\n"
                "    env={},\n"
                "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
                "    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,\n"
                ")\n"
                "pathlib.Path(sys.argv[1]).write_text(str(gc.pid), encoding='utf-8')\n"
                "time.sleep(60)\n"
            )
            gc_pid = None
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    children.run_registered(
                        "git",
                        [sys.executable, "-c", child_code, pid_file, gc_code],
                        timeout=1.5,
                        capture_output=True,
                        start_new_session=True,
                    )
                self.assertEqual(children.registered_pids(), set())
                self.assertTrue(os.path.exists(pid_file), "Child did not spawn grandchild before timeout")
                gc_pid = int(pathlib.Path(pid_file).read_text(encoding="utf-8").strip())
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline and plat_proc.is_tgid_alive(gc_pid):
                    time.sleep(0.05)
                self.assertFalse(plat_proc.is_tgid_alive(gc_pid))
            finally:
                self._force_kill_pid(gc_pid)

            before_scans = children.stats["scans"]
            self.assertEqual(children.reap_orphans(), [])
            self.assertEqual(children.stats["scans"], before_scans + 1)
            DaemonServer(td, testing_mode=True).setup_subreaper()

    def test_win_real_create_subprocess_exec_registered_without_handle_resumes(self) -> None:
        # Real-Windows regression test for P1-1: resume_process_main_thread(None, pid)
        # and create_subprocess_exec_registered when transport exposes no _handle.
        p_susp = subprocess.Popen(
            [sys.executable, "-c", "print('ok', flush=True)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            creationflags=w32.CREATE_SUSPENDED | w32.CREATE_NEW_PROCESS_GROUP | w32.CREATE_NO_WINDOW,
        )
        try:
            plat_proc.resume_process_main_thread(None, p_susp.pid)
            out, _ = p_susp.communicate(timeout=10)
            self.assertEqual(p_susp.returncode, 0)
            self.assertEqual(out.strip(), "ok")
        finally:
            if p_susp.poll() is None:
                p_susp.kill()
                p_susp.wait(timeout=5)
            if p_susp.stdout is not None:
                p_susp.stdout.close()

        orig_exec = asyncio.create_subprocess_exec

        class _NoHandleProcProxy:
            def __init__(self, real_proc: Any) -> None:
                self._real = real_proc

            def __getattr__(self, name: str) -> Any:
                if name == "_handle":
                    return None
                return getattr(self._real, name)

        class _NoHandleTransportProxy:
            def __init__(self, real_transport: Any) -> None:
                self._real = real_transport

            def __getattr__(self, name: str) -> Any:
                if name == "_proc":
                    real_proc = getattr(self._real, "_proc", None)
                    return _NoHandleProcProxy(real_proc) if real_proc is not None else None
                return getattr(self._real, name)

        async def _patched_exec(*args: Any, **kwargs: Any) -> Any:
            proc = await orig_exec(*args, **kwargs)
            proc._transport = _NoHandleTransportProxy(proc._transport)
            return proc

        async def _run_real_no_handle() -> tuple[int, bytes]:
            with mock.patch.object(children.asyncio, "create_subprocess_exec", side_effect=_patched_exec):
                proc = await asyncio.wait_for(
                    children.create_subprocess_exec_registered(
                        "probe",
                        sys.executable,
                        "-c",
                        "print('resumed', flush=True)",
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                        start_new_session=True,
                    ),
                    timeout=10.0,
                )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
                return proc.returncode, stdout
            finally:
                children.unregister(proc.pid)
                job = getattr(proc, "_mesh_job", None)
                if job is not None:
                    job.close()

        rc, out_b = asyncio.run(_run_real_no_handle())
        self.assertEqual(rc, 0)
        self.assertEqual(out_b.decode("utf-8").strip(), "resumed")
        self.assertEqual(children.spawns_in_flight(), 0)

    def test_win_real_console_ctrl_break_skips_own_group_leader(self) -> None:
        # Real-Windows regression test for P2: subchild sharing leader's console and
        # console group must refuse CTRL_BREAK_EVENT targeting leader.pid.
        repo_root = str(pathlib.Path(__file__).resolve().parents[2])
        env = dict(os.environ, PYTHONPATH=repo_root)
        sub_code = (
            "import signal, sys\n"
            "from mesh.platform import proc as plat_proc\n"
            "leader_pid = int(sys.argv[1])\n"
            "leader_st = int(sys.argv[2])\n"
            "ok = plat_proc.signal_if_same(leader_pid, leader_st, signal.SIGTERM)\n"
            "print('SIGNALLED' if ok else 'BLOCKED', flush=True)\n"
        )
        leader_code = (
            "import os, signal, subprocess, sys, time\n"
            "from mesh.platform import proc as plat_proc\n"
            "got_break = []\n"
            "if hasattr(signal, 'SIGBREAK'):\n"
            "    signal.signal(signal.SIGBREAK, lambda *a: got_break.append(1))\n"
            "st = plat_proc.get_proc_stat(os.getpid())['starttime']\n"
            "sub = subprocess.run(\n"
            "    [sys.executable, '-c', sys.argv[1], str(os.getpid()), str(st)],\n"
            "    capture_output=True, text=True, timeout=10,\n"
            ")\n"
            "time.sleep(0.2)\n"
            "print(f\"{sub.stdout.strip()}:{len(got_break)}\", flush=True)\n"
        )
        leader = subprocess.Popen(
            [sys.executable, "-c", leader_code, sub_code],
            cwd=repo_root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=w32.CREATE_NEW_PROCESS_GROUP | w32.CREATE_NO_WINDOW,
        )
        try:
            out, err = leader.communicate(timeout=15)
            self.assertEqual(leader.returncode, 0, f"stderr={err!r} stdout={out!r}")
            self.assertEqual(out.strip(), "BLOCKED:0")
        finally:
            if leader.poll() is None:
                leader.kill()
                leader.wait(timeout=5)
            if leader.stdout is not None:
                leader.stdout.close()
            if leader.stderr is not None:
                leader.stderr.close()


@unittest.skipUnless(sys.platform == "win32", "Windows-only Job Object owner SID verification")
class TestRealWinJobObjectsFinalReview(unittest.TestCase):
    def test_p1_1_real_open_run_job_verifies_owner_sid_on_windows(self) -> None:
        from mesh.platform import perms as _perms

        with tempfile.TemporaryDirectory() as home:
            run_id = f"run_own_{os.getpid()}_{uuid.uuid4().hex[:8]}"
            job = plat_proc.create_run_job(home, run_id)
            try:
                reopened = _win32.open_run_job_win(home, run_id, terminate=True)
                self.assertIsNotNone(reopened)
                reopened.close()

                with (
                    mock.patch.object(
                        _perms,
                        "get_current_user_and_owner_sids_win",
                        return_value=("S-1-5-21-0-0-0-9999", "S-1-5-21-0-0-0-9999"),
                    ),
                    mock.patch.object(_perms, "WELL_KNOWN_TRUSTED_OWNER_SIDS", frozenset()),
                ):
                    with self.assertRaises(PermissionError):
                        _win32.open_run_job_win(home, run_id, terminate=True)
            finally:
                job.close()


if __name__ == "__main__":
    unittest.main()
