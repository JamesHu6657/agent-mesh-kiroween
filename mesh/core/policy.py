"""Policy validation helpers for concurrency, models, and effort."""

import re
from typing import Any, Union
import mesh.constants as c
from mesh.errors import MeshError

MAX_INT64 = 9223372036854775807
POSITIVE_INT_RE = re.compile(r"^[1-9][0-9]*$")


def parse_positive_int(val: Any, param_name: str = "max_concurrency", allow_str: bool = False) -> int:
    """Validate and parse a positive integer in range 1..9223372036854775807 per plan §9.2.

    CLI string: must match ^[1-9][0-9]*$ and be <= 9223372036854775807 (when allow_str=True).
    JSON input: must be a strict JSON integer (not bool, float, or string) in 1..9223372036854775807.
    Rejected examples: 0, -1, 1.5, abc, '', 1e3, 0x10, +3, ' 2', 9223372036854775808,
                       JSON true, JSON "2", JSON 2.0.

    Raises MeshError(INVALID_CONCURRENCY, exit_code 2) on validation failure.
    """
    err_msg = f"{param_name} must be a positive integer (1..9223372036854775807); got '{val}'"

    # Reject booleans (in Python bool is a subclass of int: isinstance(True, int) == True!)
    if isinstance(val, bool):
        raise MeshError(
            code=c.INVALID_CONCURRENCY,
            exit_code=c.EXIT_USAGE,
            message=err_msg,
            details={"param": param_name, "got": val},
        )

    # Reject float
    if isinstance(val, float):
        raise MeshError(
            code=c.INVALID_CONCURRENCY,
            exit_code=c.EXIT_USAGE,
            message=err_msg,
            details={"param": param_name, "got": val},
        )

    if val is None:
        raise MeshError(
            code=c.INVALID_CONCURRENCY,
            exit_code=c.EXIT_USAGE,
            message=err_msg,
            details={"param": param_name, "got": val},
        )

    if isinstance(val, int):
        if 1 <= val <= MAX_INT64:
            return val
        raise MeshError(
            code=c.INVALID_CONCURRENCY,
            exit_code=c.EXIT_USAGE,
            message=err_msg,
            details={"param": param_name, "got": val},
        )

    if isinstance(val, str) and allow_str:
        if not POSITIVE_INT_RE.match(val):
            raise MeshError(
                code=c.INVALID_CONCURRENCY,
                exit_code=c.EXIT_USAGE,
                message=err_msg,
                details={"param": param_name, "got": val},
            )
        try:
            int_val = int(val)
        except ValueError:
            raise MeshError(
                code=c.INVALID_CONCURRENCY,
                exit_code=c.EXIT_USAGE,
                message=err_msg,
                details={"param": param_name, "got": val},
            )
        if 1 <= int_val <= MAX_INT64:
            return int_val
        raise MeshError(
            code=c.INVALID_CONCURRENCY,
            exit_code=c.EXIT_USAGE,
            message=err_msg,
            details={"param": param_name, "got": val},
        )

    raise MeshError(
        code=c.INVALID_CONCURRENCY,
        exit_code=c.EXIT_USAGE,
        message=err_msg,
        details={"param": param_name, "got": val},
    )


def parse_cli_positive_int(val: Any, param_name: str = "max_concurrency") -> int:
    """Parse positive integer from CLI string flag."""
    return parse_positive_int(val, param_name=param_name, allow_str=True)
