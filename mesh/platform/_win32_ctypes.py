"""Pure-stdlib ctypes definitions and lazy Win32 bindings.

Nothing in this module touches ``ctypes.windll``, ``msvcrt``, ``_winapi``, or
``nt`` at import time so importing it on Linux is always safe.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import errno
import functools
import sys
from typing import Any, Optional

INVALID_HANDLE_VALUE: int = wintypes.HANDLE(-1).value

# Access & sharing flags
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
GENERIC_ALL = 0x10000000
DELETE = 0x00010000
READ_CONTROL = 0x00020000
WRITE_DAC = 0x00040000
WRITE_OWNER = 0x00080000
FILE_READ_ATTRIBUTES = 0x0080
FILE_WRITE_DATA = 0x0002
FILE_APPEND_DATA = 0x0004
FILE_WRITE_EA = 0x0010
FILE_DELETE_CHILD = 0x0040
FILE_WRITE_ATTRIBUTES = 0x0100

FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004

# Creation dispositions
CREATE_NEW = 1
CREATE_ALWAYS = 2
OPEN_EXISTING = 3
OPEN_ALWAYS = 4
TRUNCATE_EXISTING = 5

# File attributes & flags
FILE_ATTRIBUTE_READONLY = 0x00000001
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_NORMAL = 0x00000080
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400

FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000

# Native NT open / directory query constants (ntdll.dll)
SYNCHRONIZE = 0x00100000
FILE_LIST_DIRECTORY = 0x0001
FILE_TRAVERSE = 0x0020
OBJ_CASE_INSENSITIVE = 0x00000040
FILE_DIRECTORY_FILE = 0x00000001
FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
FILE_OPEN_FOR_BACKUP_INTENT = 0x00004000
FILE_OPEN_REPARSE_POINT = 0x00200000
FILE_NAMES_INFORMATION_CLASS = 12

STATUS_SUCCESS = 0
STATUS_BUFFER_OVERFLOW = -2147483643  # 0x80000005
STATUS_NO_MORE_FILES = -2147483642  # 0x80000006

# File types (GetFileType)
FILE_TYPE_UNKNOWN = 0x0000
FILE_TYPE_DISK = 0x0001
FILE_TYPE_CHAR = 0x0002
FILE_TYPE_PIPE = 0x0003

# SetFilePointerEx move methods
FILE_BEGIN = 0

# LockFileEx flags
LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
LOCKFILE_EXCLUSIVE_LOCK = 0x00000002

# Security / Token / ACL constants
TOKEN_QUERY = 0x0008
TokenUser = 1
TokenOwner = 4

SE_FILE_OBJECT = 1
OWNER_SECURITY_INFORMATION = 0x00000001
GROUP_SECURITY_INFORMATION = 0x00000002
DACL_SECURITY_INFORMATION = 0x00000004
PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
SDDL_REVISION_1 = 1

ACCESS_ALLOWED_ACE_TYPE = 0
ACCESS_DENIED_ACE_TYPE = 1
ACCESS_ALLOWED_OBJECT_ACE_TYPE = 5
ACCESS_DENIED_OBJECT_ACE_TYPE = 6
ACCESS_ALLOWED_CALLBACK_ACE_TYPE = 9
ACCESS_DENIED_CALLBACK_ACE_TYPE = 10
ACCESS_ALLOWED_CALLBACK_OBJECT_ACE_TYPE = 11
ACCESS_DENIED_CALLBACK_OBJECT_ACE_TYPE = 12
INHERIT_ONLY_ACE = 0x08

# Common Win32 error codes
ERROR_SUCCESS = 0
ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_HANDLE = 6
ERROR_NO_MORE_FILES = 18
ERROR_BAD_LENGTH = 24
ERROR_SHARING_VIOLATION = 32
ERROR_LOCK_VIOLATION = 33
ERROR_HANDLE_EOF = 38
ERROR_FILE_EXISTS = 80
ERROR_INVALID_PARAMETER = 87
ERROR_BROKEN_PIPE = 109
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_ALREADY_EXISTS = 183
ERROR_PIPE_BUSY = 231
ERROR_NO_DATA = 232
ERROR_PIPE_NOT_CONNECTED = 233
ERROR_MORE_DATA = 234
ERROR_PARTIAL_COPY = 299
ERROR_PIPE_CONNECTED = 535
ERROR_PIPE_LISTENING = 536
ERROR_OPERATION_ABORTED = 995
ERROR_IO_INCOMPLETE = 996
ERROR_IO_PENDING = 997
ERROR_NOT_FOUND = 1168

# Named Pipe constants
PIPE_ACCESS_INBOUND = 0x00000001
PIPE_ACCESS_OUTBOUND = 0x00000002
PIPE_ACCESS_DUPLEX = 0x00000003
FILE_FLAG_OVERLAPPED = 0x40000000
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000

PIPE_TYPE_BYTE = 0x00000000
PIPE_READMODE_BYTE = 0x00000000
PIPE_WAIT = 0x00000000
PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
PIPE_UNLIMITED_INSTANCES = 255

# Process & Toolhelp32 constants
PROCESS_TERMINATE = 0x0001
PROCESS_VM_READ = 0x0010
PROCESS_SET_QUOTA = 0x0100
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

WAIT_OBJECT_0 = 0x00000000
WAIT_ABANDONED_0 = 0x00000080
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF
INFINITE = 0xFFFFFFFF
MAXIMUM_WAIT_OBJECTS = 64
STILL_ACTIVE = 259

TH32CS_SNAPPROCESS = 0x00000002
TH32CS_SNAPTHREAD = 0x00000004
MAX_PATH = 260

THREAD_SUSPEND_RESUME = 0x0002
THREAD_RESUME_FAILED = 0xFFFFFFFF

# Job Object constants & access rights
JOB_OBJECT_ASSIGN_PROCESS = 0x0001
JOB_OBJECT_SET_ATTRIBUTES = 0x0002
JOB_OBJECT_QUERY = 0x0004
JOB_OBJECT_TERMINATE = 0x0008
JOB_OBJECT_ALL_ACCESS = 0x001F001F

JobObjectBasicLimitInformation = 2
JobObjectBasicProcessIdList = 3
JobObjectExtendedLimitInformation = 9

JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

# Process creation flags & console/event signals
CREATE_SUSPENDED = 0x00000004
DETACHED_PROCESS = 0x00000008
CREATE_NEW_CONSOLE = 0x00000010
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000

EVENT_MODIFY_STATE = 0x0002
CTRL_C_EVENT = 0
CTRL_BREAK_EVENT = 1

STD_INPUT_HANDLE = 0xFFFFFFF6
STD_OUTPUT_HANDLE = 0xFFFFFFF5
STD_ERROR_HANDLE = 0xFFFFFFF4
SW_HIDE = 0

ProcessBasicInformation = 0
ProcessWow64Information = 26

IMAGE_FILE_MACHINE_UNKNOWN = 0x0000
IMAGE_FILE_MACHINE_I386 = 0x014C
IMAGE_FILE_MACHINE_ARM = 0x01C0
IMAGE_FILE_MACHINE_THUMB = 0x01C2
IMAGE_FILE_MACHINE_ARMNT = 0x01C4

STATUS_ACCESS_DENIED = -1073741790  # 0xC0000022
STATUS_PROCESS_IS_TERMINATING = -1073741558  # 0xC000010A
STATUS_PARTIAL_COPY = -2147483635  # 0x8000000D
STATUS_NO_MORE_ENTRIES = -2147483622  # 0x8000001A

# Named Pipe, overlapped I/O, and security QoS constants
PIPE_ACCESS_INBOUND = 0x00000001
PIPE_ACCESS_OUTBOUND = 0x00000002
PIPE_ACCESS_DUPLEX = 0x00000003
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
FILE_FLAG_OVERLAPPED = 0x40000000

PIPE_TYPE_BYTE = 0x00000000
PIPE_READMODE_BYTE = 0x00000000
PIPE_WAIT = 0x00000000
PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
PIPE_UNLIMITED_INSTANCES = 255
NMPWAIT_USE_DEFAULT_WAIT = 0x00000000

SECURITY_ANONYMOUS = 0x00000000
SECURITY_IDENTIFICATION = 0x00010000
SECURITY_SQOS_PRESENT = 0x00100000

SE_KERNEL_OBJECT = 6

ERROR_BROKEN_PIPE = 109
ERROR_SEM_TIMEOUT = 121
ERROR_PIPE_BUSY = 231
ERROR_NO_DATA = 232
ERROR_PIPE_NOT_CONNECTED = 233
ERROR_PIPE_CONNECTED = 535
ERROR_OPERATION_ABORTED = 995
ERROR_IO_PENDING = 997
ERROR_NOT_FOUND = 1168

INFINITE = 0xFFFFFFFF


class _OVERLAPPED_OFFSET(ctypes.Structure):
    _fields_ = [
        ("Offset", ctypes.c_uint32),
        ("OffsetHigh", ctypes.c_uint32),
    ]


class _OVERLAPPED_UNION(ctypes.Union):
    _anonymous_ = ("DUMMYSTRUCTNAME",)
    _fields_ = [
        ("DUMMYSTRUCTNAME", _OVERLAPPED_OFFSET),
        ("Pointer", ctypes.c_void_p),
    ]


class OVERLAPPED(ctypes.Structure):
    """Win32 OVERLAPPED struct for synchronous/asynchronous file range locking."""

    _anonymous_ = ("DUMMYUNIONNAME",)
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("DUMMYUNIONNAME", _OVERLAPPED_UNION),
        ("hEvent", ctypes.c_void_p),
    ]


class ACL(ctypes.Structure):
    """Win32 ACL header (8 bytes)."""

    _fields_ = [
        ("AclRevision", ctypes.c_ubyte),
        ("Sbz1", ctypes.c_ubyte),
        ("AclSize", ctypes.c_uint16),
        ("AceCount", ctypes.c_uint16),
        ("Sbz2", ctypes.c_uint16),
    ]


class ACE_HEADER(ctypes.Structure):
    """Win32 ACE_HEADER (4 bytes)."""

    _fields_ = [
        ("AceType", ctypes.c_ubyte),
        ("AceFlags", ctypes.c_ubyte),
        ("AceSize", ctypes.c_uint16),
    ]


class ACCESS_ALLOWED_ACE(ctypes.Structure):
    """Win32 ACCESS_ALLOWED_ACE (inline SID starts at offset 8)."""

    _fields_ = [
        ("Header", ACE_HEADER),
        ("Mask", ctypes.c_uint32),
        ("SidStart", ctypes.c_uint32),
    ]


class ACCESS_ALLOWED_OBJECT_ACE(ctypes.Structure):
    """Win32 ACCESS_ALLOWED_OBJECT_ACE fixed header (12 bytes before optional GUIDs + SID)."""

    _fields_ = [
        ("Header", ACE_HEADER),
        ("Mask", ctypes.c_uint32),
        ("Flags", ctypes.c_uint32),
    ]


class SID_AND_ATTRIBUTES(ctypes.Structure):
    """Win32 SID_AND_ATTRIBUTES."""

    _fields_ = [
        ("Sid", ctypes.c_void_p),
        ("Attributes", ctypes.c_uint32),
    ]


class TOKEN_USER(ctypes.Structure):
    """Win32 TOKEN_USER."""

    _fields_ = [("User", SID_AND_ATTRIBUTES)]


class TOKEN_OWNER(ctypes.Structure):
    """Win32 TOKEN_OWNER."""

    _fields_ = [("Owner", ctypes.c_void_p)]


class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    """Win32 BY_HANDLE_FILE_INFORMATION returned by GetFileInformationByHandle."""

    _fields_ = [
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", wintypes.FILETIME),
        ("ftLastAccessTime", wintypes.FILETIME),
        ("ftLastWriteTime", wintypes.FILETIME),
        ("dwVolumeSerialNumber", wintypes.DWORD),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("nNumberOfLinks", wintypes.DWORD),
        ("nFileIndexHigh", wintypes.DWORD),
        ("nFileIndexLow", wintypes.DWORD),
    ]


class UNICODE_STRING(ctypes.Structure):
    """Native NT UNICODE_STRING."""

    _fields_ = [
        ("Length", ctypes.c_uint16),
        ("MaximumLength", ctypes.c_uint16),
        ("Buffer", ctypes.c_void_p),
    ]


class OBJECT_ATTRIBUTES(ctypes.Structure):
    """Native NT OBJECT_ATTRIBUTES for handle-relative NtOpenFile."""

    _fields_ = [
        ("Length", ctypes.c_uint32),
        ("RootDirectory", ctypes.c_void_p),
        ("ObjectName", ctypes.POINTER(UNICODE_STRING)),
        ("Attributes", ctypes.c_uint32),
        ("SecurityDescriptor", ctypes.c_void_p),
        ("SecurityQualityOfService", ctypes.c_void_p),
    ]


class _IO_STATUS_UNION(ctypes.Union):
    _fields_ = [
        ("Status", ctypes.c_long),
        ("Pointer", ctypes.c_void_p),
    ]


class IO_STATUS_BLOCK(ctypes.Structure):
    """Native NT IO_STATUS_BLOCK."""

    _anonymous_ = ("u",)
    _fields_ = [
        ("u", _IO_STATUS_UNION),
        ("Information", ctypes.c_size_t),
    ]


class FILE_NAMES_INFORMATION(ctypes.Structure):
    """Native NT FILE_NAMES_INFORMATION header (FileName WCHARs start at offset 12)."""

    _fields_ = [
        ("NextEntryOffset", ctypes.c_uint32),
        ("FileIndex", ctypes.c_uint32),
        ("FileNameLength", ctypes.c_uint32),
    ]


class FILETIME(ctypes.Structure):
    """Win32 64-bit FILETIME (two 32-bit parts, always 8 bytes across LP64/LLP64)."""

    _fields_ = [
        ("dwLowDateTime", ctypes.c_uint32),
        ("dwHighDateTime", ctypes.c_uint32),
    ]


class PROCESSENTRY32W(ctypes.Structure):
    """Win32 Toolhelp32 PROCESSENTRY32W (568 bytes on 64-bit, 556 bytes on 32-bit)."""

    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_uint32),
        ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32),
        ("pcPriClassBase", ctypes.c_int32),
        ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_uint16 * MAX_PATH),
    ]


class PROCESS_BASIC_INFORMATION(ctypes.Structure):
    """Native NT PROCESS_BASIC_INFORMATION (48 bytes on 64-bit, 24 bytes on 32-bit)."""

    _fields_ = [
        ("ExitStatus", ctypes.c_int32),
        ("PebBaseAddress", ctypes.c_void_p),
        ("AffinityMask", ctypes.c_size_t),
        ("BasePriority", ctypes.c_int32),
        ("UniqueProcessId", ctypes.c_size_t),
        ("InheritedFromUniqueProcessId", ctypes.c_size_t),
    ]


class PEB64(ctypes.Structure):
    """64-bit PEB prefix through ``ProcessParameters`` (offset 0x20)."""

    _fields_ = [
        ("InheritedAddressSpace", ctypes.c_ubyte),
        ("ReadImageFileExecOptions", ctypes.c_ubyte),
        ("BeingDebugged", ctypes.c_ubyte),
        ("BitField", ctypes.c_ubyte),
        ("Padding0", ctypes.c_uint32),
        ("Mutant", ctypes.c_uint64),
        ("ImageBaseAddress", ctypes.c_uint64),
        ("Ldr", ctypes.c_uint64),
        ("ProcessParameters", ctypes.c_uint64),
    ]


class PEB32(ctypes.Structure):
    """32-bit / WOW64 PEB prefix through ``ProcessParameters`` (offset 0x10)."""

    _fields_ = [
        ("InheritedAddressSpace", ctypes.c_ubyte),
        ("ReadImageFileExecOptions", ctypes.c_ubyte),
        ("BeingDebugged", ctypes.c_ubyte),
        ("BitField", ctypes.c_ubyte),
        ("Mutant", ctypes.c_uint32),
        ("ImageBaseAddress", ctypes.c_uint32),
        ("Ldr", ctypes.c_uint32),
        ("ProcessParameters", ctypes.c_uint32),
    ]


class UNICODE_STRING64(ctypes.Structure):
    """64-bit NT UNICODE_STRING (16 bytes)."""

    _fields_ = [
        ("Length", ctypes.c_uint16),
        ("MaximumLength", ctypes.c_uint16),
        ("Padding0", ctypes.c_uint32),
        ("Buffer", ctypes.c_uint64),
    ]


class UNICODE_STRING32(ctypes.Structure):
    """32-bit NT UNICODE_STRING (8 bytes)."""

    _fields_ = [
        ("Length", ctypes.c_uint16),
        ("MaximumLength", ctypes.c_uint16),
        ("Buffer", ctypes.c_uint32),
    ]


class CURDIR64(ctypes.Structure):
    """64-bit NT CURDIR (24 bytes)."""

    _fields_ = [
        ("DosPath", UNICODE_STRING64),
        ("Handle", ctypes.c_uint64),
    ]


class CURDIR32(ctypes.Structure):
    """32-bit NT CURDIR (12 bytes)."""

    _fields_ = [
        ("DosPath", UNICODE_STRING32),
        ("Handle", ctypes.c_uint32),
    ]


class RTL_DRIVE_LETTER_CURDIR64(ctypes.Structure):
    """64-bit RTL_DRIVE_LETTER_CURDIR (24 bytes)."""

    _fields_ = [
        ("Flags", ctypes.c_uint16),
        ("Length", ctypes.c_uint16),
        ("TimeStamp", ctypes.c_uint32),
        ("DosPath", UNICODE_STRING64),
    ]


class RTL_DRIVE_LETTER_CURDIR32(ctypes.Structure):
    """32-bit RTL_DRIVE_LETTER_CURDIR (16 bytes)."""

    _fields_ = [
        ("Flags", ctypes.c_uint16),
        ("Length", ctypes.c_uint16),
        ("TimeStamp", ctypes.c_uint32),
        ("DosPath", UNICODE_STRING32),
    ]


class RTL_USER_PROCESS_PARAMETERS64(ctypes.Structure):
    """64-bit RTL_USER_PROCESS_PARAMETERS through ``EnvironmentSize`` (0x3F0)."""

    _fields_ = [
        ("MaximumLength", ctypes.c_uint32),
        ("Length", ctypes.c_uint32),
        ("Flags", ctypes.c_uint32),
        ("DebugFlags", ctypes.c_uint32),
        ("ConsoleHandle", ctypes.c_uint64),
        ("ConsoleFlags", ctypes.c_uint32),
        ("Padding0", ctypes.c_uint32),
        ("StandardInput", ctypes.c_uint64),
        ("StandardOutput", ctypes.c_uint64),
        ("StandardError", ctypes.c_uint64),
        ("CurrentDirectory", CURDIR64),
        ("DllPath", UNICODE_STRING64),
        ("ImagePathName", UNICODE_STRING64),
        ("CommandLine", UNICODE_STRING64),
        ("Environment", ctypes.c_uint64),
        ("StartingX", ctypes.c_uint32),
        ("StartingY", ctypes.c_uint32),
        ("CountX", ctypes.c_uint32),
        ("CountY", ctypes.c_uint32),
        ("CountCharsX", ctypes.c_uint32),
        ("CountCharsY", ctypes.c_uint32),
        ("FillAttribute", ctypes.c_uint32),
        ("WindowFlags", ctypes.c_uint32),
        ("ShowWindowFlags", ctypes.c_uint32),
        ("Padding1", ctypes.c_uint32),
        ("WindowTitle", UNICODE_STRING64),
        ("DesktopInfo", UNICODE_STRING64),
        ("ShellInfo", UNICODE_STRING64),
        ("RuntimeData", UNICODE_STRING64),
        ("CurrentDirectories", RTL_DRIVE_LETTER_CURDIR64 * 32),
        ("EnvironmentSize", ctypes.c_uint64),
    ]


class RTL_USER_PROCESS_PARAMETERS32(ctypes.Structure):
    """32-bit RTL_USER_PROCESS_PARAMETERS through ``EnvironmentSize`` (0x290)."""

    _fields_ = [
        ("MaximumLength", ctypes.c_uint32),
        ("Length", ctypes.c_uint32),
        ("Flags", ctypes.c_uint32),
        ("DebugFlags", ctypes.c_uint32),
        ("ConsoleHandle", ctypes.c_uint32),
        ("ConsoleFlags", ctypes.c_uint32),
        ("StandardInput", ctypes.c_uint32),
        ("StandardOutput", ctypes.c_uint32),
        ("StandardError", ctypes.c_uint32),
        ("CurrentDirectory", CURDIR32),
        ("DllPath", UNICODE_STRING32),
        ("ImagePathName", UNICODE_STRING32),
        ("CommandLine", UNICODE_STRING32),
        ("Environment", ctypes.c_uint32),
        ("StartingX", ctypes.c_uint32),
        ("StartingY", ctypes.c_uint32),
        ("CountX", ctypes.c_uint32),
        ("CountY", ctypes.c_uint32),
        ("CountCharsX", ctypes.c_uint32),
        ("CountCharsY", ctypes.c_uint32),
        ("FillAttribute", ctypes.c_uint32),
        ("WindowFlags", ctypes.c_uint32),
        ("ShowWindowFlags", ctypes.c_uint32),
        ("WindowTitle", UNICODE_STRING32),
        ("DesktopInfo", UNICODE_STRING32),
        ("ShellInfo", UNICODE_STRING32),
        ("RuntimeData", UNICODE_STRING32),
        ("CurrentDirectories", RTL_DRIVE_LETTER_CURDIR32 * 32),
        ("EnvironmentSize", ctypes.c_uint32),
    ]


class SECURITY_ATTRIBUTES(ctypes.Structure):
    """Win32 SECURITY_ATTRIBUTES (24 bytes on 64-bit, 12 bytes on 32-bit)."""

    _fields_ = [
        ("nLength", ctypes.c_uint32),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", ctypes.c_int32),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    """Win32 JOBOBJECT_BASIC_LIMIT_INFORMATION (64 bytes on 64-bit, 48 bytes on 32-bit)."""

    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class IO_COUNTERS(ctypes.Structure):
    """Win32 IO_COUNTERS (48 bytes across 32-bit and 64-bit)."""

    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    """Win32 JOBOBJECT_EXTENDED_LIMIT_INFORMATION (144 bytes on 64-bit, 112 bytes on 32-bit)."""

    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class JOBOBJECT_BASIC_PROCESS_ID_LIST(ctypes.Structure):
    """Win32 JOBOBJECT_BASIC_PROCESS_ID_LIST header + first ULONG_PTR slot."""

    _fields_ = [
        ("NumberOfAssignedProcesses", ctypes.c_uint32),
        ("NumberOfProcessIdsInList", ctypes.c_uint32),
        ("ProcessIdList", ctypes.c_size_t * 1),
    ]


class THREADENTRY32(ctypes.Structure):
    """Win32 Toolhelp32 THREADENTRY32 (28 bytes)."""

    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ThreadID", ctypes.c_uint32),
        ("th32OwnerProcessID", ctypes.c_uint32),
        ("tpBasePri", ctypes.c_int32),
        ("tpDeltaPri", ctypes.c_int32),
        ("dwFlags", ctypes.c_uint32),
    ]


def bind_kernel32(dll: Any) -> Any:
    """Attach argtypes and restype for Win32 file/handle functions on *dll*."""
    dll.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    dll.CreateFileW.restype = wintypes.HANDLE

    dll.CloseHandle.argtypes = [wintypes.HANDLE]
    dll.CloseHandle.restype = wintypes.BOOL

    dll.ReadFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPDWORD,
        ctypes.c_void_p,
    ]
    dll.ReadFile.restype = wintypes.BOOL

    dll.GetFileType.argtypes = [wintypes.HANDLE]
    dll.GetFileType.restype = wintypes.DWORD

    dll.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(BY_HANDLE_FILE_INFORMATION),
    ]
    dll.GetFileInformationByHandle.restype = wintypes.BOOL

    dll.SetFilePointerEx.argtypes = [
        wintypes.HANDLE,
        wintypes.LARGE_INTEGER,
        wintypes.PLARGE_INTEGER,
        wintypes.DWORD,
    ]
    dll.SetFilePointerEx.restype = wintypes.BOOL

    dll.SetEndOfFile.argtypes = [wintypes.HANDLE]
    dll.SetEndOfFile.restype = wintypes.BOOL
    return dll


def bind_kernel32_locks(dll: Any) -> Any:
    """Attach argtypes and restype for Win32 range locking, memory, and handle APIs."""
    dll.LockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(OVERLAPPED),
    ]
    dll.LockFileEx.restype = wintypes.BOOL

    dll.UnlockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(OVERLAPPED),
    ]
    dll.UnlockFileEx.restype = wintypes.BOOL

    dll.LocalFree.argtypes = [wintypes.HLOCAL]
    dll.LocalFree.restype = wintypes.HLOCAL

    dll.GetCurrentProcess.argtypes = []
    dll.GetCurrentProcess.restype = wintypes.HANDLE

    dll.ReOpenFile.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    dll.ReOpenFile.restype = wintypes.HANDLE
    return dll


def bind_kernel32_proc(dll: Any) -> Any:
    """Attach argtypes and restype for Win32 process, Toolhelp32, and VM read APIs."""
    dll.OpenProcess.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    dll.OpenProcess.restype = wintypes.HANDLE

    dll.GetCurrentProcessId.argtypes = []
    dll.GetCurrentProcessId.restype = wintypes.DWORD

    dll.WaitForSingleObject.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
    ]
    dll.WaitForSingleObject.restype = wintypes.DWORD

    dll.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        wintypes.LPDWORD,
    ]
    dll.GetExitCodeProcess.restype = wintypes.BOOL

    dll.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
    ]
    dll.GetProcessTimes.restype = wintypes.BOOL

    dll.TerminateProcess.argtypes = [
        wintypes.HANDLE,
        wintypes.UINT,
    ]
    dll.TerminateProcess.restype = wintypes.BOOL

    dll.CreateToolhelp32Snapshot.argtypes = [
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    dll.CreateToolhelp32Snapshot.restype = wintypes.HANDLE

    dll.Process32FirstW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(PROCESSENTRY32W),
    ]
    dll.Process32FirstW.restype = wintypes.BOOL

    dll.Process32NextW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(PROCESSENTRY32W),
    ]
    dll.Process32NextW.restype = wintypes.BOOL

    dll.ReadProcessMemory.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    dll.ReadProcessMemory.restype = wintypes.BOOL

    if hasattr(dll, "IsWow64Process2"):
        dll.IsWow64Process2.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ctypes.c_uint16),
            ctypes.POINTER(ctypes.c_uint16),
        ]
        dll.IsWow64Process2.restype = wintypes.BOOL
    return dll


def bind_kernel32_jobs(dll: Any) -> Any:
    """Attach argtypes and restype for Win32 Job Object, thread, event, and console APIs."""
    dll.CreateJobObjectW.argtypes = [
        ctypes.c_void_p,
        wintypes.LPCWSTR,
    ]
    dll.CreateJobObjectW.restype = wintypes.HANDLE

    dll.OpenJobObjectW.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    dll.OpenJobObjectW.restype = wintypes.HANDLE

    dll.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    dll.SetInformationJobObject.restype = wintypes.BOOL

    dll.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.LPDWORD,
    ]
    dll.QueryInformationJobObject.restype = wintypes.BOOL

    dll.AssignProcessToJobObject.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
    ]
    dll.AssignProcessToJobObject.restype = wintypes.BOOL

    dll.IsProcessInJob.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.BOOL),
    ]
    dll.IsProcessInJob.restype = wintypes.BOOL

    dll.TerminateJobObject.argtypes = [
        wintypes.HANDLE,
        wintypes.UINT,
    ]
    dll.TerminateJobObject.restype = wintypes.BOOL

    dll.OpenThread.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    dll.OpenThread.restype = wintypes.HANDLE

    dll.ResumeThread.argtypes = [wintypes.HANDLE]
    dll.ResumeThread.restype = wintypes.DWORD

    dll.Thread32First.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(THREADENTRY32),
    ]
    dll.Thread32First.restype = wintypes.BOOL

    dll.Thread32Next.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(THREADENTRY32),
    ]
    dll.Thread32Next.restype = wintypes.BOOL

    dll.CreateEventW.argtypes = [
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    dll.CreateEventW.restype = wintypes.HANDLE

    dll.OpenEventW.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    dll.OpenEventW.restype = wintypes.HANDLE

    dll.SetEvent.argtypes = [wintypes.HANDLE]
    dll.SetEvent.restype = wintypes.BOOL

    dll.GenerateConsoleCtrlEvent.argtypes = [
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    dll.GenerateConsoleCtrlEvent.restype = wintypes.BOOL

    dll.GetConsoleProcessList.argtypes = [
        wintypes.LPDWORD,
        wintypes.DWORD,
    ]
    dll.GetConsoleProcessList.restype = wintypes.DWORD
    return dll


def bind_kernel32_pipes(dll: Any) -> Any:
    """Attach argtypes and restype for Win32 Named Pipe and Overlapped I/O APIs."""
    dll.CreateNamedPipeW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    dll.CreateNamedPipeW.restype = wintypes.HANDLE

    dll.ConnectNamedPipe.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(OVERLAPPED),
    ]
    dll.ConnectNamedPipe.restype = wintypes.BOOL

    dll.GetOverlappedResult.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(OVERLAPPED),
        wintypes.LPDWORD,
        wintypes.BOOL,
    ]
    dll.GetOverlappedResult.restype = wintypes.BOOL

    dll.CancelIoEx.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(OVERLAPPED),
    ]
    dll.CancelIoEx.restype = wintypes.BOOL

    dll.ResetEvent.argtypes = [wintypes.HANDLE]
    dll.ResetEvent.restype = wintypes.BOOL

    dll.WaitForMultipleObjects.argtypes = [
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    dll.WaitForMultipleObjects.restype = wintypes.DWORD
    return dll


def bind_kernel32_console(dll: Any) -> Any:
    """Attach argtypes and restype for Win32 console and stdio-handle APIs."""
    dll.AllocConsole.argtypes = []
    dll.AllocConsole.restype = wintypes.BOOL

    dll.FreeConsole.argtypes = []
    dll.FreeConsole.restype = wintypes.BOOL

    dll.GetConsoleWindow.argtypes = []
    dll.GetConsoleWindow.restype = wintypes.HWND

    dll.GetStdHandle.argtypes = [wintypes.DWORD]
    dll.GetStdHandle.restype = wintypes.HANDLE

    dll.SetStdHandle.argtypes = [wintypes.DWORD, wintypes.HANDLE]
    dll.SetStdHandle.restype = wintypes.BOOL
    return dll


def bind_user32(dll: Any) -> Any:
    """Attach argtypes and restype for Win32 window visibility APIs."""
    dll.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    dll.ShowWindow.restype = wintypes.BOOL
    return dll


def bind_advapi32(dll: Any) -> Any:
    """Attach argtypes and restype for Win32 token, SID, SDDL, and ACL APIs on *dll*."""
    dll.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    dll.OpenProcessToken.restype = wintypes.BOOL

    dll.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPDWORD,
    ]
    dll.GetTokenInformation.restype = wintypes.BOOL

    dll.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    dll.ConvertSidToStringSidW.restype = wintypes.BOOL

    dll.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.ULONG),
    ]
    dll.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL

    dll.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    dll.GetSecurityDescriptorDacl.restype = wintypes.BOOL

    dll.GetSecurityInfo.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    dll.GetSecurityInfo.restype = wintypes.DWORD

    dll.SetSecurityInfo.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    dll.SetSecurityInfo.restype = wintypes.DWORD

    dll.GetAce.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    dll.GetAce.restype = wintypes.BOOL

    dll.IsValidSid.argtypes = [ctypes.c_void_p]
    dll.IsValidSid.restype = wintypes.BOOL
    return dll


def bind_kernel32_ipc(dll: Any) -> Any:
    """Attach argtypes and restype for Win32 Named Pipe and overlapped I/O APIs."""
    dll.CreateNamedPipeW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    dll.CreateNamedPipeW.restype = wintypes.HANDLE

    dll.ConnectNamedPipe.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
    ]
    dll.ConnectNamedPipe.restype = wintypes.BOOL

    dll.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
    dll.DisconnectNamedPipe.restype = wintypes.BOOL

    dll.WaitNamedPipeW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
    ]
    dll.WaitNamedPipeW.restype = wintypes.BOOL

    dll.GetNamedPipeClientProcessId.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.ULONG),
    ]
    dll.GetNamedPipeClientProcessId.restype = wintypes.BOOL

    dll.GetNamedPipeServerProcessId.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.ULONG),
    ]
    dll.GetNamedPipeServerProcessId.restype = wintypes.BOOL

    dll.WriteFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.LPDWORD,
        ctypes.c_void_p,
    ]
    dll.WriteFile.restype = wintypes.BOOL

    dll.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    dll.FlushFileBuffers.restype = wintypes.BOOL

    dll.GetOverlappedResult.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(OVERLAPPED),
        wintypes.LPDWORD,
        wintypes.BOOL,
    ]
    dll.GetOverlappedResult.restype = wintypes.BOOL

    dll.CancelIoEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
    ]
    dll.CancelIoEx.restype = wintypes.BOOL
    return dll


@functools.lru_cache(maxsize=1)
def get_kernel32() -> Any:
    """Lazily load and bind kernel32.dll with ``use_last_error=True`` on Windows."""
    if sys.platform != "win32":
        raise NotImplementedError("kernel32.dll is only available on Windows")
    win_dll = getattr(ctypes, "WinDLL", None)
    if win_dll is None:
        raise NotImplementedError("ctypes.WinDLL is unavailable on this platform")
    dll = win_dll("kernel32", use_last_error=True)
    return bind_kernel32_console(
        bind_kernel32_ipc(bind_kernel32_pipes(
            bind_kernel32_jobs(bind_kernel32_proc(bind_kernel32_locks(bind_kernel32(dll))))
        ))
    )


@functools.lru_cache(maxsize=1)
def get_user32() -> Any:
    """Lazily load and bind user32.dll with ``use_last_error=True`` on Windows."""
    if sys.platform != "win32":
        raise NotImplementedError("user32.dll is only available on Windows")
    win_dll = getattr(ctypes, "WinDLL", None)
    if win_dll is None:
        raise NotImplementedError("ctypes.WinDLL is unavailable on this platform")
    return bind_user32(win_dll("user32", use_last_error=True))


@functools.lru_cache(maxsize=1)
def get_advapi32() -> Any:
    """Lazily load and bind advapi32.dll with ``use_last_error=True`` on Windows."""
    if sys.platform != "win32":
        raise NotImplementedError("advapi32.dll is only available on Windows")
    win_dll = getattr(ctypes, "WinDLL", None)
    if win_dll is None:
        raise NotImplementedError("ctypes.WinDLL is unavailable on this platform")
    return bind_advapi32(win_dll("advapi32", use_last_error=True))


def bind_ntdll(dll: Any) -> Any:
    """Attach argtypes and restype for native NT file/directory APIs on *dll*."""
    dll.NtOpenFile.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        ctypes.POINTER(OBJECT_ATTRIBUTES),
        ctypes.POINTER(IO_STATUS_BLOCK),
        wintypes.ULONG,
        wintypes.ULONG,
    ]
    dll.NtOpenFile.restype = ctypes.c_long

    dll.NtQueryDirectoryFile.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(IO_STATUS_BLOCK),
        ctypes.c_void_p,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.BOOLEAN,
        ctypes.POINTER(UNICODE_STRING),
        wintypes.BOOLEAN,
    ]
    dll.NtQueryDirectoryFile.restype = ctypes.c_long

    dll.RtlNtStatusToDosError.argtypes = [ctypes.c_long]
    dll.RtlNtStatusToDosError.restype = wintypes.ULONG
    return dll


def bind_ntdll_proc(dll: Any) -> Any:
    """Attach argtypes and restype for native NT process inspection APIs on *dll*."""
    dll.NtQueryInformationProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    dll.NtQueryInformationProcess.restype = ctypes.c_int32
    return dll


def bind_ntdll_jobs(dll: Any) -> Any:
    """Attach argtypes and restype for native NT thread enumeration APIs on *dll*."""
    if hasattr(dll, "NtGetNextThread"):
        dll.NtGetNextThread.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.ULONG,
            wintypes.ULONG,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        dll.NtGetNextThread.restype = ctypes.c_int32
    return dll


@functools.lru_cache(maxsize=1)
def get_ntdll() -> Any:
    """Lazily load and bind ntdll.dll on Windows."""
    if sys.platform != "win32":
        raise NotImplementedError("ntdll.dll is only available on Windows")
    win_dll = getattr(ctypes, "WinDLL", None)
    if win_dll is None:
        raise NotImplementedError("ctypes.WinDLL is unavailable on this platform")
    return bind_ntdll_jobs(bind_ntdll_proc(bind_ntdll(win_dll("ntdll", use_last_error=True))))


def is_invalid_handle(handle: Any) -> bool:
    """Return True if *handle* is null or ``INVALID_HANDLE_VALUE``."""
    val = handle.value if hasattr(handle, "value") else handle
    return val is None or val == 0 or val == -1 or val == INVALID_HANDLE_VALUE


def is_null_or_invalid_handle(handle: Any) -> bool:
    """Return True if *handle* is null/0 or ``INVALID_HANDLE_VALUE``."""
    return not handle or is_invalid_handle(handle)


def get_last_error() -> int:
    """Return the thread-local Win32 last-error captured by ctypes."""
    fn = getattr(ctypes, "get_last_error", None)
    return int(fn()) if fn is not None else 0


def set_last_error(code: int = 0) -> None:
    """Set the thread-local Win32 last-error cached by ctypes (no-op on non-Windows)."""
    fn = getattr(ctypes, "set_last_error", None)
    if fn is not None:
        fn(int(code))


def win_error(code: int, filename: Optional[str] = None) -> OSError:
    """Construct an ``OSError`` from Win32 error *code*, preserving *filename*."""
    format_error = getattr(ctypes, "FormatError", None)
    descr = format_error(code).strip() if format_error is not None else f"WinError {code}"
    if sys.platform == "win32":
        return OSError(None, descr, filename, code)
    exc = OSError(errno.EIO, descr, filename)
    exc.winerror = code  # type: ignore[attr-defined]
    return exc
