"""Policy validation helpers for concurrency, models, and effort."""

import json
import re
from typing import Any, Iterable, List, Optional, Tuple
import mesh.constants as c
from mesh.errors import MeshError

MAX_INT64 = 9223372036854775807
POSITIVE_INT_RE = re.compile(r"\A[1-9][0-9]*\Z")
AGY_EFFORT_SUFFIXES = ("-low", "-medium", "-high", "-xhigh", "-max")
_UNSET = object()


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


def parse_json_str_list(raw: Optional[str]) -> List[str]:
    """Safely parse a JSON-encoded list column (e.g. allowed_models_json / allowed_efforts_json)."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def resolve_model_and_effort(
    *,
    model: Optional[str] = None,
    current_model: Optional[str] = None,
    default_model: Optional[str] = None,
    effort: Optional[str] = None,
    current_effort: Optional[str] = None,
    default_effort: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve effective model and effort from overrides, stored task values, and agent defaults."""
    resolved_model = model or current_model or default_model
    resolved_effort = effort or current_effort or default_effort
    return resolved_model, resolved_effort


def check_agy_model_effort_conflict(
    adapter_id: Optional[str],
    model: Optional[str],
    effort: Optional[str],
) -> None:
    """Reject AGY effort when resolved model id already encodes effort in its suffix (D17, §14.4)."""
    if adapter_id == "agy" and effort and model:
        for suffix in AGY_EFFORT_SUFFIXES:
            if model.endswith(suffix):
                from mesh.adapters.base import EffortNotAllowed

                raise EffortNotAllowed(
                    f"Model '{model}' already encodes reasoning effort; cannot specify effort '{effort}'",
                    details={"reason": "model_encodes_effort", "model": model, "effort": effort},
                )


def check_adapter_effort_values(adapter: Any, efforts: Iterable[str], param: str) -> None:
    """Reject effort values the adapter's CLI does not accept (codex/claude), at agent
    create/update, task create (AG step 5) and scheduler T6, not only at spawn."""
    supported = getattr(adapter, "supported_efforts", None)
    if not isinstance(supported, (tuple, list, set, frozenset)) or not supported:
        return  # None (no fixed list) or not a real list (e.g. a test double)
    for eff in efforts:
        if eff and eff not in supported:
            from mesh.adapters.base import EffortNotAllowed

            raise EffortNotAllowed(
                f"Adapter '{getattr(adapter, 'id', '')}' accepts reasoning effort {list(supported)}, not '{eff}'",
                details={"reason": "effort_not_supported_by_cli", "param": param, "effort": eff,
                         "allowed": list(supported)},
            )


def validate_resolved_effort(
    *,
    adapter_id: Optional[str],
    resolved_model: Optional[str],
    resolved_effort: Optional[str],
    allowed_efforts: Iterable[str],
    agent_name: Optional[str] = None,
    adapter: Any = _UNSET,
    testing_mode: bool = False,
) -> None:
    """Validate resolved effort against adapter capability, allowed_efforts, and AGY suffix rule."""
    if not resolved_effort:
        return

    if adapter is _UNSET and adapter_id:
        from mesh.adapters.registry import get_adapter
        adapter = get_adapter(adapter_id, testing_mode=testing_mode)
    if adapter is not None and adapter is not _UNSET:
        effort_cap = adapter.capabilities.get("reasoning_effort")
        if effort_cap and effort_cap.supported == "no":
            from mesh.adapters.base import EffortUnsupported

            raise EffortUnsupported(adapter_id or getattr(adapter, "id", ""))
        check_adapter_effort_values(adapter, [resolved_effort], "effort")

    allowed_list = list(allowed_efforts)
    if resolved_effort not in allowed_list:
        subject = f"agent '{agent_name}'" if agent_name else "agent"
        raise MeshError(
            code=c.EFFORT_NOT_ALLOWED,
            exit_code=c.EXIT_USAGE,
            message=f"Effort '{resolved_effort}' is not allowed for {subject}",
            details={"effort": resolved_effort, "allowed": allowed_list},
        )

    check_agy_model_effort_conflict(adapter_id, resolved_model, resolved_effort)
