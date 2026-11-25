"""Daemon-side structured tool-call activity extraction across all adapters (Batch 3 B3).

Pure local file read (via ``open_run_file_nofollow`` to refuse symlinks) with
server-side redaction (redacting *before* truncation so secrets are never sliced
into unmatchable halves) and strict byte/entry caps.

Supports all six vendor adapters (``claude``, ``codex``, ``agy``, ``droid``,
``kiro``, ``vibe``) plus generic JSONL tool events and Lead ``mesh-actions``
blocks.

Normalizes adapter tool events into a compact action taxonomy (``read``,
``edit``, ``command``, ``search``, ``mesh-action``, ``thought``, ``other``)
for desktop activity summaries.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from mesh.adapters.vendor_stream import LOG_CAP_MARKER
from mesh.core.redact import redact
from mesh.platform.fs import open_regular_nofollow

MAX_ACTIVITY_SCAN_BYTES: int = 4 * 1024 * 1024  # 4 MiB scan cap
MAX_ACTIVITY_ENTRIES: int = 200
MAX_DETAIL_CHARS: int = 1600
MAX_TARGET_CHARS: int = 180
MAX_NAME_CHARS: int = 80

ACTIVITY_KINDS: Tuple[str, ...] = (
    "read",
    "edit",
    "command",
    "search",
    "mesh-action",
    "thought",
    "other",
)

_MESH_ACTIONS_RE = re.compile(r"```mesh-actions\s*([\s\S]*?)```")

_READ_NAMES = (
    "read", "read_file", "view_file", "cat", "open", "list_dir", "ls",
    "fs_read", "file_read", "readfile", "view_file_outline", "view_code_item",
)
_EDIT_NAMES = (
    "edit", "write", "write_file", "edit_file", "notebookedit", "apply_patch",
    "file_change", "replace_file_content", "multi_replace_file_content",
    "fs_write", "create_file", "delete_file", "str_replace", "patch",
)
_COMMAND_NAMES = (
    "bash", "sh", "shell", "execute", "exec", "command", "command_execution",
    "run_command", "terminal", "file_system.bash", "powershell",
)
_SEARCH_NAMES = (
    "grep", "glob", "search", "grep_search", "find_by_name", "websearch",
    "web_search", "webfetch", "read_url_content", "search_web", "ripgrep",
)


def _redact_and_clip(text: Any, limit: int) -> str:
    """Redact first, then clip, so secrets are never cut in half before redaction."""
    if text is None:
        return ""
    raw = text if isinstance(text, str) else str(text)
    cleaned = redact(raw)
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit] + "...[truncated]"


def _safe_json_detail(obj: Any, limit: int = MAX_DETAIL_CHARS) -> str:
    try:
        rendered = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)
    except Exception:
        rendered = str(obj)
    return _redact_and_clip(rendered, limit)


def classify_tool_kind(name: str, raw_type: str = "") -> str:
    """Map an adapter tool name / event type into Mesh's normalized action taxonomy."""
    n = (name or "").strip().lower()
    t = (raw_type or "").strip().lower()

    if t in ("thinking", "reasoning", "thought", "agent_thought_chunk") or n in ("thinking", "reasoning"):
        return "thought"
    if t == "mesh-action":
        return "mesh-action"
    if t == "command_execution" or any(k == n or n.endswith("." + k) for k in _COMMAND_NAMES):
        return "command"
    if t == "file_change" or any(k == n or n.endswith("." + k) for k in _EDIT_NAMES):
        return "edit"
    if any(k == n or n.endswith("." + k) for k in _SEARCH_NAMES):
        return "search"
    if any(k == n or n.endswith("." + k) for k in _READ_NAMES):
        return "read"

    # Substring fallback for custom/MCP tools
    if any(tok in n for tok in ("bash", "shell", "exec", "command", "terminal")):
        return "command"
    if any(tok in n for tok in ("write", "edit", "patch", "replace", "create", "delete", "apply")):
        return "edit"
    if any(tok in n for tok in ("grep", "glob", "search", "find", "fetch")):
        return "search"
    if any(tok in n for tok in ("read", "view", "list", "inspect", "cat", "open")):
        return "read"
    return "other"


def _extract_target(obj: Any) -> str:
    """Extract a concise human-readable target (path, command, query, url) from a tool payload."""
    if not isinstance(obj, dict):
        return ""
    candidates: List[Dict[str, Any]] = [obj]
    for sub_key in ("input", "parameters", "args", "arguments", "detail", "tool_info", "rawInput"):
        sub = obj.get(sub_key)
        if isinstance(sub, dict):
            candidates.append(sub)
            nested_params = sub.get("parameters") or sub.get("input") or sub.get("args")
            if isinstance(nested_params, dict):
                candidates.append(nested_params)

    target_keys = (
        "command", "cmd", "CommandLine",
        "file_path", "path", "Path", "TargetFile", "AbsolutePath", "SearchPath",
        "pattern", "query", "Query", "url", "Url", "summary", "title", "description",
    )
    for src in candidates:
        for key in target_keys:
            val = src.get(key)
            if isinstance(val, str) and val.strip():
                first_line = val.strip().splitlines()[0].strip()
                return _redact_and_clip(first_line, MAX_TARGET_CHARS)
        changes = src.get("changes")
        if isinstance(changes, list) and changes:
            paths = [
                str(ch.get("path"))
                for ch in changes
                if isinstance(ch, dict) and ch.get("path")
            ]
            if paths:
                joined = ", ".join(paths[:4]) + (f" (+{len(paths) - 4})" if len(paths) > 4 else "")
                return _redact_and_clip(joined, MAX_TARGET_CHARS)
    return ""


def _normalize_status(raw: Any, is_error: bool = False) -> Optional[str]:
    if is_error:
        return "error"
    if isinstance(raw, str):
        low = raw.strip().lower()
        if low in ("error", "failed", "denied", "cancelled"):
            return "error"
        if low in ("done", "completed", "success", "ok", "succeeded"):
            return "ok"
        if low in ("active", "running", "in_progress", "pending", "started"):
            return "running"
    return None


def _make_entry(
    kind: str,
    name: str,
    target: str,
    detail: str,
    *,
    status: Optional[str] = None,
    key: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "kind": kind if kind in ACTIVITY_KINDS else "other",
        "name": _redact_and_clip(name or "tool", MAX_NAME_CHARS),
        "target": _redact_and_clip(target or "", MAX_TARGET_CHARS),
        "status": status,
        "detail": detail,
        "_key": key,
    }


def _extract_mesh_actions_from_text(
    text: str,
    emit: Callable[[Dict[str, Any]], bool],
    seen_actions: set[str],
) -> None:
    if "mesh-actions" not in text:
        return
    for match in _MESH_ACTIONS_RE.finditer(text):
        body = match.group(1).strip()
        if not body:
            continue
        try:
            parsed = json.loads(body)
        except (ValueError, RecursionError):
            continue
        actions = parsed if isinstance(parsed, list) else []
        for act in actions:
            if not isinstance(act, dict) or not isinstance(act.get("cmd"), str):
                continue
            cmd = act["cmd"].strip()
            args = act.get("args") if isinstance(act.get("args"), dict) else {}
            sig = f"{cmd}:{json.dumps(args, sort_keys=True, ensure_ascii=False)[:200]}"
            if sig in seen_actions:
                continue
            seen_actions.add(sig)
            raw_target = args.get("title") or args.get("text") or args.get("plan_id") or args.get("task_id") or ""
            accepted = emit(
                _make_entry(
                    "mesh-action",
                    cmd,
                    str(raw_target),
                    _safe_json_detail(act),
                    status="ok",
                )
            )
            if not accepted:
                return


def extract_activity_from_lines(
    lines: Iterable[str],
    *,
    adapter: Optional[str] = None,
    limit: int = MAX_ACTIVITY_ENTRIES,
) -> Dict[str, Any]:
    """Extract normalized, redacted, bounded tool-call activity from log lines.

    Pure function (no I/O). Handles Claude, Codex, Agy, Droid, Kiro, Vibe,
    generic tool JSONL streams, and ``mesh-actions`` blocks.
    """
    cap = max(1, min(int(limit), MAX_ACTIVITY_ENTRIES))
    entries: List[Dict[str, Any]] = []
    by_key: Dict[str, int] = {}
    seen_actions: set[str] = set()
    raw_text_for_actions: List[str] = []
    log_capped = False
    truncated = False
    lines_scanned = 0
    adapter_norm = (adapter or "").strip().lower()

    def _upsert(entry: Dict[str, Any]) -> bool:
        nonlocal truncated
        ekey = entry.get("_key")
        if ekey and ekey in by_key:
            idx = by_key[ekey]
            prev = entries[idx]
            # Preserve earlier target/detail if update omits them
            if not entry["target"] and prev["target"]:
                entry["target"] = prev["target"]
            if not entry["detail"] and prev["detail"]:
                entry["detail"] = prev["detail"]
            entries[idx] = entry
            return True
        if len(entries) >= cap:
            truncated = True
            return False
        if ekey:
            by_key[ekey] = len(entries)
        entries.append(entry)
        return True

    for raw_line in lines:
        lines_scanned += 1
        line = raw_line.strip()
        if not line:
            continue
        if line == LOG_CAP_MARKER:
            log_capped = True
            continue
        if "mesh-actions" in line or len(raw_text_for_actions) < 64:
            raw_text_for_actions.append(raw_line)

        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(obj, dict):
            continue

        obj_type = str(obj.get("type") or "")
        ev_name = str(obj.get("event") or "")

        # 1. Claude Code stream-json: assistant message content[] (tool_use / thinking)
        if obj_type == "assistant" and isinstance(obj.get("message"), dict):
            content = obj["message"].get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "tool_use":
                        tname = str(block.get("name") or "tool")
                        tid = str(block.get("id") or "")
                        _upsert(
                            _make_entry(
                                classify_tool_kind(tname, "tool_use"),
                                tname,
                                _extract_target(block),
                                _safe_json_detail(block.get("input") or block),
                                status="ok",
                                key=f"claude:{tid}" if tid else None,
                            )
                        )
                    elif btype == "thinking" and isinstance(block.get("thinking"), str) and block["thinking"].strip():
                        txt = block["thinking"].strip()
                        _upsert(
                            _make_entry(
                                "thought",
                                "thinking",
                                txt.splitlines()[0],
                                _redact_and_clip(txt, MAX_DETAIL_CHARS),
                            )
                        )
                    elif btype == "text" and isinstance(block.get("text"), str) and "mesh-actions" in block["text"]:
                        _extract_mesh_actions_from_text(block["text"], _upsert, seen_actions)
            continue

        if obj_type == "result" and isinstance(obj.get("result"), str) and "mesh-actions" in obj["result"]:
            _extract_mesh_actions_from_text(obj["result"], _upsert, seen_actions)
            continue

        # 2. Codex CLI exec --json: item.started / item.completed
        if obj_type in ("item.completed", "item.started") and isinstance(obj.get("item"), dict):
            it = obj["item"]
            it_type = str(it.get("type") or "")
            if it_type in ("command_execution", "file_change", "mcp_tool_call", "web_search", "reasoning"):
                tname = str(it.get("tool") or it.get("name") or it_type)
                target = _extract_target(it)
                if not target and isinstance(it.get("text"), str) and it["text"].strip():
                    target = it["text"].strip().splitlines()[0]
                is_err = (
                    (isinstance(it.get("exit_code"), int) and it["exit_code"] != 0)
                    or it.get("status") in ("failed", "error")
                )
                status = _normalize_status(it.get("status") or ("running" if obj_type == "item.started" else "ok"), is_error=is_err)
                item_id = str(it.get("id") or "")
                _upsert(
                    _make_entry(
                        classify_tool_kind(tname, it_type),
                        tname,
                        target,
                        _safe_json_detail(it),
                        status=status,
                        key=f"codex:{item_id}" if item_id else None,
                    )
                )
            elif it_type == "agent_message" and isinstance(it.get("text"), str) and "mesh-actions" in it["text"]:
                _extract_mesh_actions_from_text(it["text"], _upsert, seen_actions)
            continue

        # 3. Antigravity (agy) stream-json: {"event":"step_update","step_update":{"step_type":"tool",...}}
        if ev_name == "step_update" and isinstance(obj.get("step_update"), dict):
            su = obj["step_update"]
            st_type = str(su.get("step_type") or "")
            if st_type == "tool":
                tinfo = su.get("tool_info") if isinstance(su.get("tool_info"), dict) else {}
                tname = str(su.get("tool_name") or tinfo.get("name") or "tool")
                step_idx = su.get("step_index")
                is_err = str(su.get("state") or "").upper() == "ERROR" or bool(tinfo.get("error"))
                status = _normalize_status(su.get("state"), is_error=is_err)
                _upsert(
                    _make_entry(
                        classify_tool_kind(tname, "tool"),
                        tname,
                        _extract_target(tinfo or su),
                        _safe_json_detail(tinfo or su),
                        status=status,
                        key=f"agy:{step_idx}" if step_idx is not None else None,
                    )
                )
            continue

        if ev_name == "result" and isinstance(obj.get("result"), dict):
            resp = obj["result"].get("response")
            if isinstance(resp, str) and "mesh-actions" in resp:
                _extract_mesh_actions_from_text(resp, _upsert, seen_actions)
            continue

        # 4. Factory Droid stream-json: tool_call / tool_result / completion
        if obj_type == "tool_call":
            tname = str(obj.get("toolName") or obj.get("toolId") or obj.get("name") or "tool")
            tid = str(obj.get("id") or "")
            _upsert(
                _make_entry(
                    classify_tool_kind(tname, "tool_call"),
                    tname,
                    _extract_target(obj),
                    _safe_json_detail(obj.get("parameters") or obj),
                    status="running",
                    key=f"droid:{tid}" if tid else None,
                )
            )
            continue

        if obj_type == "tool_result":
            tid = str(obj.get("id") or "")
            k = f"droid:{tid}" if tid else None
            if k and k in by_key:
                idx = by_key[k]
                is_err = bool(obj.get("isError")) or bool(obj.get("error"))
                entries[idx]["status"] = "error" if is_err else "ok"
                err_raw = obj.get("error")
                err_msg = (
                    err_raw.get("message")
                    if isinstance(err_raw, dict)
                    else (err_raw if isinstance(err_raw, str) else None)
                )
                if is_err and isinstance(err_msg, str) and err_msg.strip():
                    # Prepend error text before existing detail so prior large detail cannot clip it away
                    err_block = f"[error]\n{err_msg.strip()}"
                    prev_detail = entries[idx]["detail"]
                    combined = f"{err_block}\n\n{prev_detail}" if prev_detail else err_block
                    entries[idx]["detail"] = _redact_and_clip(combined, MAX_DETAIL_CHARS)
            continue

        if obj_type == "completion" and isinstance(obj.get("finalText"), str) and "mesh-actions" in obj["finalText"]:
            _extract_mesh_actions_from_text(obj["finalText"], _upsert, seen_actions)
            continue

        # 5. Kiro CLI (ACP stream-json): sessionUpdate -> tool_call / tool_call_update / agent_thought_chunk
        if obj_type == "sessionUpdate" and isinstance(obj.get("data"), dict):
            upd = obj["data"].get("update")
            if isinstance(upd, dict):
                su_kind = str(upd.get("sessionUpdate") or "")
                if su_kind in ("tool_call", "tool_call_update"):
                    tname = str(upd.get("title") or upd.get("name") or upd.get("kind") or "tool")
                    tcid = str(upd.get("toolCallId") or "")
                    status = _normalize_status(upd.get("status"))
                    _upsert(
                        _make_entry(
                            classify_tool_kind(tname, str(upd.get("kind") or su_kind)),
                            tname,
                            _extract_target(upd),
                            _safe_json_detail(upd),
                            status=status,
                            key=f"kiro:{tcid}" if tcid else None,
                        )
                    )
                elif su_kind == "agent_thought_chunk":
                    content = upd.get("content")
                    if isinstance(content, dict) and isinstance(content.get("text"), str) and content["text"].strip():
                        txt = content["text"].strip()
                        _upsert(
                            _make_entry(
                                "thought",
                                "thought",
                                txt.splitlines()[0],
                                _redact_and_clip(txt, MAX_DETAIL_CHARS),
                            )
                        )
            continue

        if obj_type == "runFinished" and isinstance(obj.get("data"), dict):
            ft = obj["data"].get("finalText")
            if isinstance(ft, str) and "mesh-actions" in ft:
                _extract_mesh_actions_from_text(ft, _upsert, seen_actions)
            continue

        # 6. Mistral Vibe streaming JSONL: {"type":"effect","title":"file_system.bash","detail":{...}}
        if obj_type == "effect":
            tname = str(obj.get("title") or obj.get("name") or "effect")
            eid = str(obj.get("id") or "")
            status = _normalize_status(obj.get("status") or obj.get("generationStatus"))
            _upsert(
                _make_entry(
                    classify_tool_kind(tname, "effect"),
                    tname,
                    _extract_target(obj),
                    _safe_json_detail(obj.get("detail") or obj),
                    status=status,
                    key=f"vibe:{eid}" if eid else None,
                )
            )
            continue

        # 7. Generic fallback for tool_use / function_call JSONL lines
        low_t = (obj_type or ev_name).lower()
        if low_t in ("tool_use", "function_call") or obj.get("tool_name") or (
            isinstance(obj.get("tool"), str) and obj["tool"]
        ):
            tname = str(obj.get("tool_name") or obj.get("tool") or obj.get("name") or low_t or "tool")
            _upsert(
                _make_entry(
                    classify_tool_kind(tname, low_t),
                    tname,
                    _extract_target(obj),
                    _safe_json_detail(obj),
                    status=_normalize_status(obj.get("status")),
                )
            )

    # Also scan plain-text stdout (e.g. fake/plain-text leads) for mesh-actions blocks
    if raw_text_for_actions and (len(entries) < cap or not truncated):
        joined_raw = "".join(raw_text_for_actions)
        if "mesh-actions" in joined_raw:
            _extract_mesh_actions_from_text(joined_raw, _upsert, seen_actions)

    counts: Dict[str, int] = {k: 0 for k in ACTIVITY_KINDS}
    public_entries: List[Dict[str, Any]] = []
    for idx, item in enumerate(entries):
        kind = item["kind"]
        counts[kind] = counts.get(kind, 0) + 1
        public_entries.append(
            {
                "index": idx,
                "kind": kind,
                "name": item["name"],
                "target": item["target"],
                "status": item["status"],
                "detail": item["detail"],
            }
        )

    return {
        "adapter": adapter_norm or None,
        "source": "structured" if public_entries else "empty",
        "entries": public_entries,
        "counts": counts,
        "total_entries": len(public_entries),
        "lines_scanned": lines_scanned,
        "log_capped": log_capped,
        "truncated": truncated or log_capped,
    }


def extract_run_activity(
    run_dir: str,
    *,
    adapter: Optional[str] = None,
    limit: int = MAX_ACTIVITY_ENTRIES,
    max_scan_bytes: int = MAX_ACTIVITY_SCAN_BYTES,
) -> Dict[str, Any]:
    """Read ``stdout.log`` inside *run_dir* safely (nofollow, bounded) and extract activity."""
    stdout_path = os.path.join(run_dir, "stdout.log")
    if not os.path.isfile(stdout_path):
        return {
            "available": False,
            "adapter": (adapter or "").strip().lower() or None,
            "source": "empty",
            "entries": [],
            "counts": {k: 0 for k in ACTIVITY_KINDS},
            "total_entries": 0,
            "lines_scanned": 0,
            "log_capped": False,
            "truncated": False,
        }

    scan_truncated = False
    lines: List[str] = []
    try:
        fd = open_regular_nofollow(stdout_path, os.O_RDONLY, label="run stdout.log")
        with os.fdopen(fd, "rb") as bf:
            try:
                size = os.fstat(bf.fileno()).st_size
            except OSError:
                size = 0
            if max_scan_bytes > 0 and size > max_scan_bytes:
                bf.seek(size - max_scan_bytes)
                bf.readline()  # discard partial first line after seek
                scan_truncated = True
            raw_bytes = bf.read(max_scan_bytes if max_scan_bytes > 0 else None)
        lines = raw_bytes.decode("utf-8", errors="replace").splitlines(keepends=True)
    except OSError:
        return {
            "available": False,
            "adapter": (adapter or "").strip().lower() or None,
            "source": "empty",
            "entries": [],
            "counts": {k: 0 for k in ACTIVITY_KINDS},
            "total_entries": 0,
            "lines_scanned": 0,
            "log_capped": False,
            "truncated": False,
        }

    res = extract_activity_from_lines(lines, adapter=adapter, limit=limit)
    res["available"] = True
    if scan_truncated:
        res["truncated"] = True
    return res
