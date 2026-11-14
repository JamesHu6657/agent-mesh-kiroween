"""Plain-text rendering for ``--format text``.

JSON stays the default, machine-readable output. Text output is for people: lists are
small aligned tables, single objects are ``key: value`` lines (id/name/title/status
first), nested values are indented. The renderer never adds or removes data a JSON
caller would see; it only lays it out.
"""

import json
import unicodedata
from typing import Any, Dict, List, Sequence

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


def _char_width(ch: str) -> int:
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


_ZWJ = "\u200d"
_VS15 = "\ufe0e"  # text presentation
_VS16 = "\ufe0f"  # emoji presentation


def _is_regional(ch: str) -> bool:
    return 0x1F1E6 <= ord(ch) <= 0x1F1FF


def _is_skin_tone(ch: str) -> bool:
    return 0x1F3FB <= ord(ch) <= 0x1F3FF


def _is_pictographic(ch: str) -> bool:
    """Emoji-capable characters (an approximation of Extended_Pictographic, which
    unicodedata does not expose): the emoji planes and the BMP symbol blocks emoji use."""
    o = ord(ch)
    return (0x1F000 <= o <= 0x1FAFF or 0x2600 <= o <= 0x27BF or 0x2300 <= o <= 0x23FF
            or 0x2B00 <= o <= 0x2BFF or 0x2190 <= o <= 0x21FF or 0x25A0 <= o <= 0x25FF
            or o in (0x00A9, 0x00AE, 0x203C, 0x2049, 0x2122, 0x2139, 0x3030, 0x303D, 0x3297, 0x3299))


def _extends(ch: str) -> bool:
    """Characters that attach to the previous one in the same cluster."""
    return (ch in (_VS15, _VS16, _ZWJ) or _is_skin_tone(ch) or unicodedata.combining(ch) != 0
            or unicodedata.category(ch) in ("Mn", "Me") or 0xE0020 <= ord(ch) <= 0xE007F)


def clusters(s: str) -> List[str]:
    """``s`` split into user-perceived characters (a simple grapheme-cluster rule, enough
    for terminal tables): a base character with its combining marks, variation selectors,
    skin-tone modifiers, tag characters and ZWJs; a ZWJ joins the next character into the
    same cluster only between two pictographic (emoji) characters; two regional
    indicators form one flag. Non-emoji text around a ZWJ keeps its own boundaries."""
    out: List[str] = []
    i, n = 0, len(s)
    while i < n:
        j = i + 1
        if _is_regional(s[i]) and j < n and _is_regional(s[j]):
            j += 1
        while j < n:
            if (s[j] == _ZWJ and j + 1 < n and _is_pictographic(s[i])
                    and _is_pictographic(s[j + 1])):
                j += 2
            elif _extends(s[j]):
                j += 1
            else:
                break
        out.append(s[i:j])
        i = j
    return out


def _cluster_width(cl: str) -> int:
    if len(cl) == 1:
        return _char_width(cl)
    if _is_regional(cl[0]) and len(cl) > 1 and _is_regional(cl[1]):
        return 2  # flag
    if _VS15 in cl:
        return 1  # text presentation
    joined = any(cl[k] == _ZWJ and k + 1 < len(cl) and _is_pictographic(cl[k + 1])
                 for k in range(len(cl)))  # a dangling ZWJ joins nothing
    if _VS16 in cl or (_is_pictographic(cl[0]) and (joined or any(_is_skin_tone(ch) for ch in cl))):
        return 2  # emoji presentation, joined or toned emoji
    return _char_width(cl[0])  # marks, ZWJ and selectors add nothing


def display_width(s: str) -> int:
    """Terminal columns ``s`` takes, counted per user-perceived character: East Asian
    wide/fullwidth characters (CJK) and emoji sequences (VS16, ZWJ, skin tone, flags) count
    2, combining marks and format characters 0, everything else 1."""
    return sum(_cluster_width(cl) for cl in clusters(s))


def fit(s: str, limit: int) -> str:
    """``s`` cut to at most ``limit`` display columns, ending in ``...`` when cut. A cut
    never splits a cluster (an emoji sequence or a character and its marks)."""
    if display_width(s) <= limit:
        return s
    out, used = [], 0
    for cl in clusters(s):
        w = _cluster_width(cl)
        if used + w > limit - 3:
            break
        out.append(cl)
        used += w
    return "".join(out) + "..."


def pad(s: str, width: int) -> str:
    """``s`` padded with spaces to ``width`` display columns (``str.ljust`` counts code points)."""
    return s + " " * max(0, width - display_width(s))


def align_rows(rows: Sequence[Sequence[str]]) -> List[str]:
    """Rows of cells laid out in columns by display width; trailing spaces stripped."""
    if not rows:
        return []
    n = len(rows[0])
    widths = [max(display_width(r[i]) for r in rows) for i in range(n)]
    return ["  ".join(pad(r[i], widths[i]) for i in range(n)).rstrip() for r in rows]


def _cell(v: Any) -> str:
    return fit(" ".join(_scalar(v).split()), MAX_CELL)  # one line per row


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
    return align_rows(rows)


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
