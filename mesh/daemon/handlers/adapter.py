"""Handlers for adapter resource commands: adapter.list, adapter.show, adapter.probe, adapter.models."""

import asyncio
from typing import Any, Dict, List, Optional, Tuple

import mesh.constants as c
from mesh.adapters.base import Adapter, Probe, record_adapter_probe
from mesh.adapters.evidence import apply_to_probe, effective_launch
from mesh.adapters.registry import get_adapter, list_adapters
from mesh.core.authz import check_authz
from mesh.daemon.handlers.base import HandlerContext, HandlerResult
from mesh.errors import MeshError


def _extract_adapter_id(ctx: HandlerContext, required: bool) -> Optional[str]:
    adapter_id = ctx.args.get("adapter_id") or ctx.args.get("id") or ctx.args.get("adapter")
    if required and not adapter_id:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Adapter identifier is required",
            details={"param": "adapter_id"},
        )
    if adapter_id is not None and (not isinstance(adapter_id, str) or not adapter_id.strip()):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Adapter identifier must be a non-empty string",
            details={"param": "adapter_id"},
        )
    return adapter_id


def _extract_binary_arg(ctx: HandlerContext, allow_on_probe: bool) -> Optional[str]:
    """Validate `--binary` / `binary_path`.

    Plan §13.3 + M3 plan: `--binary` is only valid on `adapter.probe`, and executing an
    arbitrary caller-chosen binary requires direct user authentication (`user` + `direct`).
    """
    has_binary = "binary" in ctx.args and ctx.args.get("binary") is not None
    has_binary_path = "binary_path" in ctx.args and ctx.args.get("binary_path") is not None
    if not has_binary and not has_binary_path:
        return None

    if not allow_on_probe:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"'--binary' is not accepted by '{ctx.cmd}'",
            details={"param": "binary", "cmd": ctx.cmd},
        )

    if not (ctx.actor_kind == "user" and ctx.via == "direct"):
        raise MeshError(
            code=c.CAPABILITY_MISSING,
            exit_code=c.EXIT_FORBIDDEN,
            message="Command 'adapter.probe --binary' requires direct user authentication",
            details={"cmd": "adapter.probe", "param": "binary", "required": "user_direct"},
        )

    raw_bin = ctx.args.get("binary") if has_binary else ctx.args.get("binary_path")
    if not isinstance(raw_bin, str) or not raw_bin.strip() or "\x00" in raw_bin:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Field 'binary' must be a non-empty path string",
            details={"param": "binary"},
        )
    return raw_bin


def _authorize_adapter_cmd(ctx: HandlerContext) -> None:
    check_authz(
        ctx.conn,
        ctx.cmd,
        ctx.principal,
        ctx.actor_kind,
        ctx.via,
        relayed_by=ctx.relayed_by,
    )


def _probe_many_off_db(
    adapters: List[Adapter],
    binary_path: Optional[str] = None,
) -> List[Tuple[Adapter, Probe]]:
    return [(a, a.probe(binary_path=binary_path, conn=None)) for a in adapters]


def _probe_one_off_db(
    adapter: Adapter,
    binary_path: Optional[str] = None,
) -> Tuple[Adapter, Probe]:
    return adapter, adapter.probe(binary_path=binary_path, conn=None)


async def prepare_adapter_command(ctx: HandlerContext, testing_mode: bool) -> None:
    """Run adapter subprocess probes/model queries in a worker thread outside any DB tx.

    On `dry_run`, validates arguments and authz only: spawns no thread and no subprocess.
    """
    _authorize_adapter_cmd(ctx)
    cmd = ctx.cmd

    if cmd == "adapter.list":
        _extract_binary_arg(ctx, allow_on_probe=False)
        adapters = list_adapters(testing_mode=testing_mode)
        if ctx.dry_run:
            return
        pairs = await asyncio.to_thread(_probe_many_off_db, adapters, None)
        setattr(ctx, "_prepared_adapter", ("list", pairs, None))
        return

    if cmd == "adapter.show":
        _extract_binary_arg(ctx, allow_on_probe=False)
        adapter_id = _extract_adapter_id(ctx, required=True)
        assert adapter_id is not None
        adapter = get_adapter(adapter_id, testing_mode=testing_mode)
        if ctx.dry_run:
            return
        pair = await asyncio.to_thread(_probe_one_off_db, adapter, None)
        setattr(ctx, "_prepared_adapter", ("show", pair, None))
        return

    if cmd == "adapter.probe":
        binary_path = _extract_binary_arg(ctx, allow_on_probe=True)
        adapter_id = _extract_adapter_id(ctx, required=False)
        if adapter_id:
            adapter = get_adapter(adapter_id, testing_mode=testing_mode)
            if ctx.dry_run:
                return
            pair = await asyncio.to_thread(_probe_one_off_db, adapter, binary_path)
            setattr(ctx, "_prepared_adapter", ("probe_one", pair, binary_path))
            return
        adapters = list_adapters(testing_mode=testing_mode)
        if ctx.dry_run:
            return
        pairs = await asyncio.to_thread(_probe_many_off_db, adapters, binary_path)
        setattr(ctx, "_prepared_adapter", ("probe_many", pairs, binary_path))
        return

    if cmd == "adapter.models":
        _extract_binary_arg(ctx, allow_on_probe=False)
        adapter_id = _extract_adapter_id(ctx, required=True)
        assert adapter_id is not None
        adapter = get_adapter(adapter_id, testing_mode=testing_mode)
        if ctx.dry_run:
            return
        models = await asyncio.to_thread(adapter.list_models)
        setattr(ctx, "_prepared_adapter", ("models", (adapter_id, models), None))
        return


def handle_adapter_list(ctx: HandlerContext, testing_mode: bool) -> HandlerResult:
    """List all registered adapters (ACC: N1, N3)."""
    _authorize_adapter_cmd(ctx)
    _extract_binary_arg(ctx, allow_on_probe=False)
    adapters = list_adapters(testing_mode=testing_mode)
    if ctx.dry_run:
        return HandlerResult(data={"dry_run": True, "adapters": [a.id for a in adapters]})

    prepared = getattr(ctx, "_prepared_adapter", None)
    if prepared and prepared[0] == "list":
        pairs: List[Tuple[Adapter, Probe]] = prepared[1]
    else:
        pairs = _probe_many_off_db(adapters, None)

    items = []
    for a, probe in pairs:
        record_adapter_probe(ctx.conn, a.id, probe, probe.binary_path)
        vendor = None if a.kind in ("test_double", "none") else a.id
        note = "not a vendor integration" if a.kind == "test_double" else ""
        # Effective launch from probe + smoke evidence (ACC: N19, N20, N23)
        pdata = apply_to_probe(probe.to_dict(), a, effective_launch(ctx.conn, a, testing_mode))
        items.append({
            "id": a.id,
            "kind": a.kind,
            "vendor": vendor,
            "version": probe.version,
            "status": pdata["status"],
            "launch_enabled": pdata["launch_enabled"],
            "launch_milestone": pdata["launch_milestone"],
            "launch_reason": pdata["launch_reason"],
            "results_file": pdata["results_file"],
            "note": note,
        })
    return HandlerResult(data={"adapters": items, "items": items})


def handle_adapter_show(ctx: HandlerContext, testing_mode: bool) -> HandlerResult:
    """Show details and capability matrix for a single adapter (ACC: N2)."""
    _authorize_adapter_cmd(ctx)
    _extract_binary_arg(ctx, allow_on_probe=False)
    adapter_id = _extract_adapter_id(ctx, required=True)
    assert adapter_id is not None
    adapter = get_adapter(adapter_id, testing_mode=testing_mode)
    if ctx.dry_run:
        return HandlerResult(data={"dry_run": True, "adapter": adapter.id})

    prepared = getattr(ctx, "_prepared_adapter", None)
    if prepared and prepared[0] == "show":
        adapter, probe = prepared[1]
    else:
        adapter, probe = _probe_one_off_db(adapter, None)

    record_adapter_probe(ctx.conn, adapter.id, probe, probe.binary_path)
    data = apply_to_probe(probe.to_dict(), adapter, effective_launch(ctx.conn, adapter, testing_mode))
    data["kind"] = adapter.kind
    data["vendor"] = None if adapter.kind in ("test_double", "none") else adapter.id
    if adapter.kind == "test_double":
        data["note"] = "not a vendor integration"
    return HandlerResult(data=data)


def handle_adapter_probe(ctx: HandlerContext, testing_mode: bool) -> HandlerResult:
    """Probe adapter live or return cached verification evidence (ACC: N2)."""
    _authorize_adapter_cmd(ctx)
    binary_path = _extract_binary_arg(ctx, allow_on_probe=True)
    adapter_id = _extract_adapter_id(ctx, required=False)

    if adapter_id:
        adapter = get_adapter(adapter_id, testing_mode=testing_mode)
        if ctx.dry_run:
            return HandlerResult(data={"dry_run": True, "adapter": adapter.id, "binary_path": binary_path})

        prepared = getattr(ctx, "_prepared_adapter", None)
        if prepared and prepared[0] == "probe_one":
            adapter, probe = prepared[1]
        else:
            adapter, probe = _probe_one_off_db(adapter, binary_path)

        record_adapter_probe(ctx.conn, adapter.id, probe, binary_path or probe.binary_path)
        data = apply_to_probe(
            probe.to_dict(),
            adapter,
            effective_launch(ctx.conn, adapter, testing_mode, binary_path),
        )
        data["kind"] = adapter.kind
        data["vendor"] = None if adapter.kind in ("test_double", "none") else adapter.id
        if adapter.kind == "test_double":
            data["note"] = "not a vendor integration"
        return HandlerResult(data=data)

    adapters = list_adapters(testing_mode=testing_mode)
    if ctx.dry_run:
        return HandlerResult(
            data={"dry_run": True, "adapters": [a.id for a in adapters], "binary_path": binary_path}
        )

    prepared = getattr(ctx, "_prepared_adapter", None)
    if prepared and prepared[0] == "probe_many":
        pairs: List[Tuple[Adapter, Probe]] = prepared[1]
    else:
        pairs = _probe_many_off_db(adapters, binary_path)

    items = []
    for a, probe in pairs:
        record_adapter_probe(ctx.conn, a.id, probe, binary_path or probe.binary_path)
        pdict = apply_to_probe(probe.to_dict(), a, effective_launch(ctx.conn, a, testing_mode, binary_path))
        pdict["kind"] = a.kind
        pdict["vendor"] = None if a.kind in ("test_double", "none") else a.id
        if a.kind == "test_double":
            pdict["note"] = "not a vendor integration"
        items.append(pdict)
    return HandlerResult(data={"probes": items, "items": items})


def handle_adapter_models(ctx: HandlerContext, testing_mode: bool) -> HandlerResult:
    """Query available models for an adapter (ACC: N7)."""
    _authorize_adapter_cmd(ctx)
    _extract_binary_arg(ctx, allow_on_probe=False)
    adapter_id = _extract_adapter_id(ctx, required=True)
    assert adapter_id is not None
    adapter = get_adapter(adapter_id, testing_mode=testing_mode)
    if ctx.dry_run:
        return HandlerResult(data={"dry_run": True, "adapter": adapter_id, "models": []})

    prepared = getattr(ctx, "_prepared_adapter", None)
    if prepared and prepared[0] == "models":
        _, models = prepared[1]
    else:
        models = adapter.list_models()
    return HandlerResult(data={"adapter": adapter_id, "models": models})
