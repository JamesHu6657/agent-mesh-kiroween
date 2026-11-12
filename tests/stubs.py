"""Test stub generators for external vendor CLI adapters.

Injects isolated stub binaries for unit and integration tests so tests NEVER
execute real vendor CLIs and default suite remains strictly offline.
"""

import os
import stat
from typing import Optional


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES_ROOT = os.path.join(REPO_ROOT, "tests", "fixtures", "real")


def make_executable(path: str) -> None:
    current = os.stat(path).st_mode
    os.chmod(path, current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def create_test_stubs(tmpdir: str, tripwire_file: Optional[str] = None,
                      droid_version: str = "1.6.0") -> str:
    """Create executable stubs in tmpdir for droid, agy, kiro-cli, vibe.

    F7 (m4-p3-cleanup): droid defaults to 1.6.0, the version with committed passing smoke
    evidence. Tests that need a droid with no committed evidence pass ``droid_version="1.3.0"``.
    """
    os.makedirs(tmpdir, exist_ok=True)
    trip_path = tripwire_file or "/tmp/mesh-stub-tripwire.log"

    # 1. Droid stub
    droid_help = os.path.join(FIXTURES_ROOT, "droid", droid_version, "help.txt")
    droid_exec_help = os.path.join(FIXTURES_ROOT, "droid", droid_version, "exec-help.txt")
    droid_stub = os.path.join(tmpdir, "droid")
    with open(droid_stub, "w", encoding="utf-8") as f:
        f.write(f"""#!/usr/bin/env python3
import sys, os

args = sys.argv[1:]
if args == ["--version"]:
    print("droid {droid_version}")
    sys.exit(0)
elif args == ["--help"]:
    with open({droid_help!r}, "rb") as fp:
        sys.stdout.buffer.write(fp.read())
    sys.exit(0)
elif args == ["exec", "--help"]:
    with open({droid_exec_help!r}, "rb") as fp:
        sys.stdout.buffer.write(fp.read())
    sys.exit(0)
else:
    trip = os.environ.get("MESH_TRIPWIRE_FILE") or {trip_path!r}
    with open(trip, "a", encoding="utf-8") as fp:
        fp.write(f"UNAUTHORIZED_DROID_INVOCATION: {{args}}\\n")
    sys.exit(99)
""")
    make_executable(droid_stub)

    # 2. Agy stub
    agy_help = os.path.join(FIXTURES_ROOT, "agy", "1.5.2", "help.txt")
    agy_models = os.path.join(FIXTURES_ROOT, "agy", "1.5.2", "models.txt")
    agy_stub = os.path.join(tmpdir, "agy")
    with open(agy_stub, "w", encoding="utf-8") as f:
        f.write(f"""#!/usr/bin/env python3
import sys, os

args = sys.argv[1:]
if args == ["--version"]:
    print("agy version 1.5.2")
    sys.exit(0)
elif args == ["--help"]:
    with open({agy_help!r}, "rb") as fp:
        sys.stdout.buffer.write(fp.read())
    sys.exit(0)
elif args == ["models"]:
    with open({agy_models!r}, "rb") as fp:
        sys.stdout.buffer.write(fp.read())
    sys.exit(0)
else:
    trip = os.environ.get("MESH_TRIPWIRE_FILE") or {trip_path!r}
    with open(trip, "a", encoding="utf-8") as fp:
        fp.write(f"UNAUTHORIZED_AGY_INVOCATION: {{args}}\\n")
    sys.exit(99)
""")
    make_executable(agy_stub)

    # 3. Kiro stub (kiro-cli)
    kiro_help = os.path.join(FIXTURES_ROOT, "kiro", "3.1.0", "help.txt")
    kiro_chat_help = os.path.join(FIXTURES_ROOT, "kiro", "3.1.0", "chat-help.txt")
    kiro_models = os.path.join(FIXTURES_ROOT, "kiro", "3.1.0", "list-models.json")
    kiro_stub = os.path.join(tmpdir, "kiro-cli")
    with open(kiro_stub, "w", encoding="utf-8") as f:
        f.write(f"""#!/usr/bin/env python3
import sys, os

args = sys.argv[1:]
if args == ["--version"]:
    print("kiro-cli 3.1.0")
    sys.exit(0)
elif args == ["--help"]:
    with open({kiro_help!r}, "rb") as fp:
        sys.stdout.buffer.write(fp.read())
    sys.exit(0)
elif args == ["chat", "--help"]:
    with open({kiro_chat_help!r}, "rb") as fp:
        sys.stdout.buffer.write(fp.read())
    sys.exit(0)
elif args == ["chat", "--list-models", "-f", "json"]:
    with open({kiro_models!r}, "rb") as fp:
        sys.stdout.buffer.write(fp.read())
    sys.exit(0)
else:
    trip = os.environ.get("MESH_TRIPWIRE_FILE") or {trip_path!r}
    with open(trip, "a", encoding="utf-8") as fp:
        fp.write(f"UNAUTHORIZED_KIRO_INVOCATION: {{args}}\\n")
    sys.exit(99)
""")
    make_executable(kiro_stub)

    # 4. Vibe stub
    vibe_help = os.path.join(FIXTURES_ROOT, "vibe", "3.1.0", "help.txt")
    vibe_stub = os.path.join(tmpdir, "vibe")
    with open(vibe_stub, "w", encoding="utf-8") as f:
        f.write(f"""#!/usr/bin/env python3
import sys, os

args = sys.argv[1:]
if args == ["--version"]:
    print("vibe 3.1.0")
    sys.exit(0)
elif args == ["--help"]:
    with open({vibe_help!r}, "rb") as fp:
        sys.stdout.buffer.write(fp.read())
    sys.exit(0)
else:
    trip = os.environ.get("MESH_TRIPWIRE_FILE") or {trip_path!r}
    with open(trip, "a", encoding="utf-8") as fp:
        fp.write(f"UNAUTHORIZED_VIBE_INVOCATION: {{args}}\\n")
    sys.exit(99)
""")
    make_executable(vibe_stub)

    return tmpdir


def create_failing_stub(tmpdir: str, binary_name: str, exit_code: int = 1) -> str:
    """Create a stub binary that fails with exit_code."""
    os.makedirs(tmpdir, exist_ok=True)
    stub_path = os.path.join(tmpdir, binary_name)
    with open(stub_path, "w", encoding="utf-8") as f:
        f.write(f"""#!/usr/bin/env python3
import sys
sys.stderr.write("Simulated binary failure\\n")
sys.exit({exit_code})
""")
    make_executable(stub_path)
    return stub_path


def create_hanging_stub(tmpdir: str, binary_name: str, sleep_s: float = 10.0) -> str:
    """Create a stub binary that hangs / sleeps."""
    os.makedirs(tmpdir, exist_ok=True)
    stub_path = os.path.join(tmpdir, binary_name)
    with open(stub_path, "w", encoding="utf-8") as f:
        f.write(f"""#!/usr/bin/env python3
import time, sys
time.sleep({sleep_s})
sys.exit(0)
""")
    make_executable(stub_path)
    return stub_path


def create_garbage_stub(tmpdir: str, binary_name: str, text: str = "GARBAGE_NO_VERSION_INFO") -> str:
    """Create a stub binary that prints unparseable output."""
    os.makedirs(tmpdir, exist_ok=True)
    stub_path = os.path.join(tmpdir, binary_name)
    with open(stub_path, "w", encoding="utf-8") as f:
        f.write(f"""#!/usr/bin/env python3
import sys
print({text!r})
sys.exit(0)
""")
    make_executable(stub_path)
    return stub_path


def create_stale_stub(tmpdir: str, binary_name: str, version: str = "99.0.0") -> str:
    """Create a stub binary that reports a different version."""
    os.makedirs(tmpdir, exist_ok=True)
    stub_path = os.path.join(tmpdir, binary_name)
    with open(stub_path, "w", encoding="utf-8") as f:
        f.write(f"""#!/usr/bin/env python3
import sys
args = sys.argv[1:]
if args == ["--version"]:
    print({version!r})
    sys.exit(0)
else:
    print("Generic help text for stale version")
    sys.exit(0)
""")
    make_executable(stub_path)
    return stub_path


def create_modified_help_stub(tmpdir: str, binary_name: str, version: str = "1.5.2", help_text: str = "Modified help text") -> str:
    """Create a stub binary that reports matching version but different help text (hash mismatch)."""
    os.makedirs(tmpdir, exist_ok=True)
    stub_path = os.path.join(tmpdir, binary_name)
    with open(stub_path, "w", encoding="utf-8") as f:
        f.write(f"""#!/usr/bin/env python3
import sys
args = sys.argv[1:]
if args == ["--version"]:
    print({version!r})
    sys.exit(0)
elif "--help" in args:
    print({help_text!r})
    sys.exit(0)
else:
    sys.exit(0)
""")
    make_executable(stub_path)
    return stub_path


def create_stderr_help_stub(
    tmpdir: str,
    binary_name: str,
    version: str = "1.5.2",
    with_locale_warning: bool = True,
    with_ansi: bool = True,
) -> str:
    """Create an agy stub binary that prints its help output exclusively to STDERR, with exit code 0.
    
    Includes a locale-warning line ('bash: warning: setlocale...') and ANSI escape sequences
    to verify that normalization strips both and computes the canonical fixture SHA-256.
    """
    os.makedirs(tmpdir, exist_ok=True)
    stub_path = os.path.join(tmpdir, binary_name)
    agy_help = os.path.join(FIXTURES_ROOT, "agy", "1.5.2", "help.txt")
    agy_models = os.path.join(FIXTURES_ROOT, "agy", "1.5.2", "models.txt")

    with open(stub_path, "w", encoding="utf-8") as f:
        f.write(f"""#!/usr/bin/env python3
import sys, os

args = sys.argv[1:]
if args == ["--version"]:
    print("agy version {version}")
    sys.exit(0)
elif args == ["--help"]:
    # stdout is empty; stderr contains help text
    if {with_locale_warning!r}:
        sys.stderr.write("bash: warning: setlocale: LC_ALL: cannot change locale (en_US.UTF-8): No such file or directory\\n")
    with open({agy_help!r}, "r", encoding="utf-8") as fp:
        content = fp.read()
    if {with_ansi!r}:
        # Insert ANSI escape sequences (e.g. bold header, color reset)
        content = "\\x1b[1m" + content + "\\x1b[0m"
    sys.stderr.write(content)
    sys.exit(0)
elif args == ["models"]:
    with open({agy_models!r}, "rb") as fp:
        sys.stdout.buffer.write(fp.read())
    sys.exit(0)
else:
    sys.exit(0)
""")
    make_executable(stub_path)
    return stub_path


def create_missing_flag_stub(
    tmpdir: str,
    binary_name: str,
    version: str = "1.5.2",
    missing_flag: str = "--dangerously-skip-permissions",
    to_stderr: bool = True,
) -> str:
    """Create an agy stub binary whose help output omits a specific flag."""
    os.makedirs(tmpdir, exist_ok=True)
    stub_path = os.path.join(tmpdir, binary_name)
    agy_help = os.path.join(FIXTURES_ROOT, "agy", "1.5.2", "help.txt")
    agy_models = os.path.join(FIXTURES_ROOT, "agy", "1.5.2", "models.txt")

    with open(stub_path, "w", encoding="utf-8") as f:
        f.write(f"""#!/usr/bin/env python3
import sys, os

args = sys.argv[1:]
if args == ["--version"]:
    print("agy version {version}")
    sys.exit(0)
elif args == ["--help"]:
    with open({agy_help!r}, "r", encoding="utf-8") as fp:
        content = fp.read()
    # Replace the missing flag with a dummy non-matching string
    content = content.replace({missing_flag!r}, "--removed-flag")
    out = sys.stderr if {to_stderr!r} else sys.stdout
    out.write(content)
    sys.exit(0)
elif args == ["models"]:
    with open({agy_models!r}, "rb") as fp:
        sys.stdout.buffer.write(fp.read())
    sys.exit(0)
else:
    sys.exit(0)
""")
    make_executable(stub_path)
    return stub_path
