"""Reading a flag's value from stdin when the flag is given as '-'.

stdin can be read only once per command: `--input -` and a '-' flag value (for example
`--spec -`) cannot both use it, so the second reader gets a clear usage error instead
of silently reading nothing.
"""

import sys
from typing import Optional

import mesh.constants as c
from mesh.errors import MeshError

_consumed_by: Optional[str] = None


def read_stdin(flag: str) -> str:
    """All of stdin for ``flag`` (e.g. ``"--spec"``); refuses a second read."""
    global _consumed_by
    if _consumed_by is not None:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"{flag} - cannot read stdin: it is already used by {_consumed_by} -",
            details={"param": flag.lstrip("-").replace("-", "_")},
        )
    _consumed_by = flag
    return sys.stdin.read()


def reset() -> None:
    """Forget an earlier read (each in-process `main()` call starts fresh)."""
    global _consumed_by
    _consumed_by = None
