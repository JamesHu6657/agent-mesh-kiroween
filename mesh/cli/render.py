"""Plain-text rendering for ``--format text``.

JSON stays the default, machine-readable output. Text output is for people: lists are
small aligned tables, single objects are ``key: value`` lines (id/name/title/status
first), nested values are indented. The renderer never adds or removes data a JSON
caller would see; it only lays it out.
"""

import json
from typing import Any, Dict, List

#: Columns tried, in this order, for list tables (the first ones present are used).
TABLE_COLUMNS = (
    "seq", "id", "name", "key", "title", "status", "type", "kind", "category",
    "adapter", "delivery", "assignee", "project_id", "task_id", "version", "value",
    "launch_enabled", "launch_reason", "ok", "detail", "hint", "created_at",
)
MAX_COLUMNS = 7
MAX_CELL = 48
#: Keys shown first for a single object (fields that are null are not shown).
LEAD_KEYS = ("id", "name", "title", "status")
INDENT = "  "


def _scalar(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    return str(v)


def _cell(v: Any) -> str:
    s = " ".join(_scalar(v).split())  # one line per row
    return s if len(s) <= MAX_CELL else s[: MAX_CELL - 3] + "..."


def _is_table(items: Any) -> bool:
    return isinstance(items, list) and bool(items) and all(isinstance(i, dict) for i in items)


def _row_version(items: List[Dict[str, Any]]) -> bool:
    """True when ``version`` is the row's optimistic-concurrency counter (an int), not a
    product version (an adapter's CLI version is a string); the counter is not shown."""
    vals = [i.get("version") for i in items if i.get("version") is not None]
    return bool(vals) and all(isinstance(v, int) and not isinstance(v, bool) for v in vals)


def render_table(items: List[Dict[str, Any]]) -> List[str]:
    if not items:
        return ["(none)"]
    if not all(isinstance(i, dict) for i in items):
        return [_cell(i) for i in items]
    cols = [k for k in TABLE_COLUMNS if any(i.get(k) is not None for i in items)
            and not (k == "version" and _row_version(items))][:MAX_COLUMNS]
    if not cols:
        seen: List[str] = []
        for i in items:
            for k in i:
                if k not in seen:
                    seen.append(k)
        cols = seen[:MAX_COLUMNS]
    rows = [[c.upper() for c in cols]] + [[_cell(i.get(c)) for c in cols] for i in items]
    widths = [max(len(r[n]) for r in rows) for n in range(len(cols))]
    return ["  ".join(r[n].ljust(widths[n]) for n in range(len(cols))).rstrip() for r in rows]


def _ordered(d: Dict[str, Any]) -> List[str]:
    return [k for k in LEAD_KEYS if k in d] + [k for k in d if k not in LEAD_KEYS]


def render_object(d: Dict[str, Any], depth: int = 0) -> List[str]:
    pad = INDENT * depth
    out: List[str] = []
    for k in _ordered(d):
        v = d[k]
        if v is None:
            continue  # unset fields are left out of the text view (JSON keeps them)
        if isinstance(v, dict) and v and depth < 2:
            out.append(f"{pad}{k}:")
            out.extend(render_object(v, depth + 1))
        elif _is_table(v) and depth < 2:
            out.append(f"{pad}{k}:")
            out.extend(pad + INDENT + line for line in render_table(v))
        elif isinstance(v, list) and all(not isinstance(i, (dict, list)) for i in v):
            out.append(f"{pad}{k}: " + (", ".join(_scalar(i) for i in v) if v else "(none)"))
        elif isinstance(v, str) and "\n" in v:
            out.append(f"{pad}{k}:")
            out.extend(pad + INDENT + line for line in v.rstrip("\n").split("\n"))
        else:
            out.append(f"{pad}{k}: {_scalar(v)}")
    return out


def render_text(data: Any) -> str:
    """The text form of a command's ``data`` (no trailing newline)."""
    if not isinstance(data, dict):
        return _scalar(data)
    if isinstance(data.get("text"), str):
        return data["text"].rstrip("\n")
    if set(data) == {"version"}:  # `mesh version` / `mesh --version`
        return f"mesh {data['version']}"
    if isinstance(data.get("items"), list):
        items = data["items"]
        lines = render_table(items)
        # Other keys (e.g. a project's current_lead); aliases of the item list are skipped.
        rest = {k: v for k, v in data.items() if k != "items" and v is not None and v != items}
        if rest:
            lines.append("")
            lines.extend(render_object(rest))
        return "\n".join(lines)
    return "\n".join(render_object(data))
