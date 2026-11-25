"""Secret redaction per plan §15.2 (PR #40 r5: line-based, stream == batch by design).

Text is redacted line by line by one state machine (``_LineRedactor``) that both
``redact`` (the whole text) and ``StreamRedactor`` (pieces) feed with the same
complete lines, so streamed output equals batch output for any chunking.

Per line, every rule is applied independently to the whole line and the union of the
matched spans is replaced (no sequential state inside a line, so no nesting bugs):

* exact values: every live token known to the process, plus env values whose names
  match ``(?i)(key|token|secret|passw|cookie|credential|auth)`` and are at least 8
  characters -> ``[REDACTED:<name>]``. A value spanning lines is matched on a window
  of the last K lines (K = its newline count + 1); each of its lines of 8+ characters
  is also matched on its own;
* credential tokens (``sk-``, ``fk-``, ``mesh_at_``/``mesh_rt_``, ``gh?_``, ``xox?-``,
  ``AIza``) to the end of their run, however long;
* ``password``/``passwd``/``secret``/``api_key`` ``[:=]`` value (to the next whitespace)
  and ``bearer`` token; the keyword is kept, the value becomes ``[REDACTED]``.

Across lines, only line-granular state: a ``BEGIN ... PRIVATE KEY`` line starts a key
block whose lines are redacted whole up to the END marker (or the end of the text); a
keyword or bearer with its value missing at the end of a line arms the next non-blank
line's first token. A line longer than ``LINE_CAP`` characters is withheld whole as
``[REDACTED:unbroken output]`` (fail closed).
"""

import bisect
from collections import deque
import json
import os
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

SECRET_ENV_NAME_RE = re.compile(r"(?i)(key|token|secret|passw|passphrase|cookie|credential|auth)")
MIN_EXACT_VALUE_LEN = 8

#: A line longer than this (without its newline) is withheld whole (fail closed).
LINE_CAP = 64 * 1024
UNBROKEN = "[REDACTED:unbroken output]"
#: Of a withheld line, at most this much of its start is kept (up to its last
#: whitespace, redacted like any line, and only outside a key block or armed value).
OVER_PREFIX = 256


def _over_prefix(body: str) -> str:
    head = body[:OVER_PREFIX]
    cut = max(head.rfind(" "), head.rfind("\t"))
    return head[:cut + 1] if cut >= 0 else ""

_TOKEN_RE = re.compile(
    r"(?:"
    r"mesh_(?:at|rt)_[A-Za-z0-9_-]{20,}"
    r"|fk-[A-Za-z0-9_-]{16,}"
    r"|sk-[A-Za-z0-9_-]{16,}"
    r"|AIza[0-9A-Za-z_-]{35,}"
    r"|gh[pousr]_[A-Za-z0-9]{30,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|glpat-[A-Za-z0-9_-]{16,}"
    r"|(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"
    r"|hf_[A-Za-z0-9]{20,}"
    r"|npm_[A-Za-z0-9]{20,}"
    r"|AKIA[0-9A-Z]{16}(?![0-9A-Za-z])"
    r"|xox[abpr]-[A-Za-z0-9-]{10,}"
    r")")
#: A token or bearer credential is redacted on to the next whitespace (fail closed).
_TO_WS_RE = re.compile(r"\S*")
_KW = r"(?:password|passwd|passw|passphrase|secret|token|credential|(?:api|access|private|secret)[_-]?key)"
_KW_BOUND = r"(?<![A-Za-z0-9])" + _KW
_ASSIGN_RE = re.compile(r"(?i)" + _KW + r"\s*[:=]\s*(\S+)")
_BEARER_RE = re.compile(
    r"(?i)bearer\s+"
    r"((?:\[REDACTED[^\r\n\]]{0,256}\]\S*|[A-Za-z0-9._~+/=-]{16,}\S*))")
_AUTH_BASIC_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])(?:proxy[-_])?authorization\s*[:=]\s*(?:basic|bearer|token|apikey|digest)\s+"
    r"((?:\[REDACTED[^\r\n\]]{0,256}\]\S*|\S+))")
#: Value missing at the end of the line: the next non-blank line's first token is it.
_ARM_RE = re.compile(
    r"(?i)(?:" + _KW_BOUND + r"\s*[:=]|(?<![A-Za-z0-9_-])(?:bearer|(?:proxy[-_])?authorization\s*[:=]\s*(?:basic|bearer|token|apikey|digest)))\s*$"
)
#: Keyword at the end of the line with no ``:``/``=`` yet: armed if the next
#: non-blank line starts with one.
_KW_END_RE = re.compile(r"(?i)" + _KW_BOUND + r"\s*$")
_MESH_FENCE_RE = re.compile(r"\s*`{3,}mesh-(?:actions|deliverable)[ \t]*\r?$")
_LEAD_ASSIGN_RE = re.compile(r"\s*[:=]\s*(\S*)")
_FIRST_TOKEN_RE = re.compile(r"\s*(\S+)")
_BEGIN_RE = re.compile(r"-----BEGIN [A-Z ]{0,64}PRIVATE KEY-----")
_END_RE = re.compile(r"-----END [A-Z ]{0,64}PRIVATE KEY-----")
_CAND_MARKER_RE = re.compile(r"\[REDACTED(?::([^\r\n\]]{1,256}))?\]")  # bounded: r4 P1-4
_RUN_TOKEN_LABEL_RE = re.compile(r"run_token:[A-Za-z0-9_-]+\Z")
_MARKER_TRAIL_PUNCT = frozenset('"\'`,;)]}')
_MARKER_WS_ESC_RE = re.compile(
    r"(?:\\+[bfnrt]|\\+u(?:00(?:[01][0-9a-fA-F]|20|85|[aA]0)|202[89]))"
)
_MARKER_PUNCT_UNIT_RE = re.compile(
    r"""(?:["'`,;)\]}]|\\["'\\/]|\\\\|\\u00(?:22|27|2[cC]|3[bB]|29|5[dD]|7[dD]|60|5[cC]|2[fF]))"""
)
#: Full review r1 P1-1 (``jsonl`` rules, run logs only): the password of a URL's userinfo
#: (``scheme://user:<password>@``) and the string value of a JSON member whose key names a
#: credential (``"api_key": "<value>"``); only the value is replaced, the JSON stays valid.
#: r4 P1-4: the scheme is bounded (an unbounded `[a-z0-9+.-]*` rescanned a long
#: `sk-sk-...` run from every word start: quadratic).
_URI_CRED_RE = re.compile(r"(?i)\b[a-z][a-z0-9+.-]{0,31}://[^/\s:@\"'\\]{0,256}:([^/\s@\"'\\]+)@")
_JSON_KW = r"(?:" + _KW + r"|(?:proxy[-_])?authorization|(?:set[-_])?cookie)"
_JSON_KW_KEY_RE = re.compile(r"(?i)" + _JSON_KW)
_JSON_MEMBER_RE = re.compile(r'(?i)"[^"\\\n]{0,64}' + _JSON_KW + r'[^"\\\n]{0,64}"\s*:\s*"((?:[^"\\\n]|\\.)+)"')
#: Full review r3 P1-2 (jsonl only): a member whose key holds a credential keyword
#: (bounded, so the overlapping scan stays linear per position). Its value is redacted:
#: a plain string to its closing quote, anything else (a container, an escaped string,
#: text in a non-JSON line) to the end of the line (fail closed).
_KW_MEMBER_RE = re.compile(r'(?i)' + _JSON_KW + r'[^"\\\n]{0,64}"\s*:\s*')
#: A whole JSON scalar literal (r4 P1-5: not just its first character).
_JSON_LITERAL_RE = re.compile(r'(?:true|false|null|-?\d{1,64}(?:\.\d{1,64})?(?:[eE][+-]?\d{1,8})?)(?=[,\s}\]]|$)')
_PLAIN_STR_RE = re.compile(r'"((?:[^"\\\n]|\\.){0,65536})"')
_ESC_RE = re.compile(r'\\(u[0-9a-fA-F]{4}|.)')
#: r5 P1-2: the real characters (an exact value may hold a tab or newline); the
#: single-line rules match on a copy with newlines folded to spaces.
_ESC_CHARS = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
#: r5 P0-1: decoded strings holding JSON are decoded again at most this deep.
_MAX_JSON_DEPTH = 4


def _unescape_view(text: str) -> Tuple[str, List[int], List[int]]:
    """One JSON-unescape level of ``text`` (real characters, r5 P1-2), with the raw
    start/end offsets of every view character (r3 P1-2: escaped credentials in a line
    that is not itself JSON, e.g. `log={\"api\\u005fkey\": [...]}`)."""
    out: List[str] = []
    starts: List[int] = []
    ends: List[int] = []
    pos = 0
    matches = list(_ESC_RE.finditer(text))
    i = 0
    while i < len(matches):
        m = matches[i]
        for k in range(pos, m.start()):
            out.append(text[k]); starts.append(k); ends.append(k + 1)
        g = m.group(1)
        end = m.end()
        if len(g) == 5:
            code = int(g[1:], 16)
            nxt = matches[i + 1] if i + 1 < len(matches) else None
            if (0xD800 <= code <= 0xDBFF and nxt is not None and nxt.start() == end
                    and len(nxt.group(1)) == 5 and 0xDC00 <= int(nxt.group(1)[1:], 16) <= 0xDFFF):
                # r4 P1-2: a UTF-16 surrogate pair is one character (json.dumps default).
                code = 0x10000 + ((code - 0xD800) << 10) + (int(nxt.group(1)[1:], 16) - 0xDC00)
                end = nxt.end()
                i += 1
            ch = chr(code)
        else:
            ch = _ESC_CHARS.get(g, g)
        out.append(ch); starts.append(m.start()); ends.append(end)
        pos = end
        i += 1
    for k in range(pos, len(text)):
        out.append(text[k]); starts.append(k); ends.append(k + 1)
    return "".join(out), starts, ends


def _views(text: str, levels: int = 3) -> List[str]:
    """``text`` and up to ``levels`` JSON-unescape levels of it (r4 P1-2: every escape
    form of an exact value, ``\\uXXXX``, surrogate pairs, ``\\/``, shows in one of them)."""
    out = [text]
    for _ in range(levels):
        if "\\" not in out[-1]:
            break
        view = _unescape_view(out[-1])[0]
        if view == out[-1]:
            break
        out.append(view)
    return out


def _unclosed_key(text: str) -> bool:
    """``text`` opens a private key block it does not close."""
    b = _BEGIN_RE.search(text)
    while b is not None:
        e = _END_RE.search(text, b.end())
        if e is None:
            return True
        b = _BEGIN_RE.search(text, e.end())
    return False


class _Pairs(list):
    """A decoded JSON object as its (key, value) pairs: duplicate keys are kept (r3 P1-1)."""


_LONE_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def _json_dumps_utf8(v: Any) -> str:
    """JSON-serialize a scalar with UTF-8 output while escaping lone surrogates as \\udXXX."""
    s = json.dumps(v, ensure_ascii=False)
    if isinstance(v, str) and _LONE_SURROGATE_RE.search(s):
        s = _LONE_SURROGATE_RE.sub(lambda m: f"\\u{ord(m.group(0)):04x}", s)
    return s


def _ser(v: Any, compact: bool = False) -> str:
    if isinstance(v, _Pairs):
        sep, col = (",", ":") if compact else (", ", ": ")
        return "{" + sep.join(_json_dumps_utf8(k) + col + _ser(x, compact) for k, x in v) + "}"
    if isinstance(v, list):
        return "[" + (", " if not compact else ",").join(_ser(x, compact) for x in v) + "]"
    # P2-10: keep non-ASCII UTF-8 compact while escaping lone surrogates so UTF-8 encoding works.
    return _json_dumps_utf8(v)


def _ser_pretty(v: Any, level: int = 0) -> str:
    if isinstance(v, _Pairs):
        if not v:
            return "{}"
        pad = "  " * (level + 1)
        end_pad = "  " * level
        return "{\n" + ",\n".join(
            f"{pad}{_json_dumps_utf8(k)}: {_ser_pretty(x, level + 1)}" for k, x in v
        ) + f"\n{end_pad}}}"
    if isinstance(v, list):
        if not v:
            return "[]"
        pad = "  " * (level + 1)
        end_pad = "  " * level
        return "[\n" + ",\n".join(f"{pad}{_ser_pretty(x, level + 1)}" for x in v) + f"\n{end_pad}]"
    return _json_dumps_utf8(v)


_FAIL_CLOSED_JSON = json.dumps("[REDACTED]")

Span = Tuple[int, int, str]


def _marker_prefix_re(word: str) -> "re.Pattern[str]":
    """Fully matches every non-empty proper prefix of a ``-----<word> ... PRIVATE KEY-----``
    marker (text that may still grow into one)."""
    def chain(chars: str, last: str = "") -> str:
        out = last
        for ch in reversed(chars):
            out = re.escape(ch) + (f"(?:{out})?" if out else "")
        return out
    tail = chain("PRIVATE KEY-----")
    head = chain(f"-----{word} ", f"[A-Z ]{{0,64}}(?:{tail})?")
    return re.compile(head)


_MARKER_PREFIX = {"BEGIN": _marker_prefix_re("BEGIN"), "END": _marker_prefix_re("END")}


class _KeyScan:
    """Key-block state across a line that is too long to keep (PR #40 r5): scans it in
    pieces with the batch's sequential rule (BEGIN, then END, ...), accepting a marker
    only when no earlier position can still grow into one, so any chunking gives the
    batch result. Run once per possible starting state."""

    def __init__(self, in_key: bool) -> None:
        self.in_key = in_key
        self.s = ""

    def feed(self, text: str, final: bool = False) -> None:
        s = self.s + text
        pos = 0
        while True:
            word = "END" if self.in_key else "BEGIN"
            m = (_END_RE if self.in_key else _BEGIN_RE).search(s, pos)
            u = None
            if not final:
                for q in range(max(pos, len(s) - 100), len(s)):
                    if _MARKER_PREFIX[word].fullmatch(s, q):
                        u = q
                        break
            if m is not None and (u is None or m.start() < u):
                self.in_key = not self.in_key
                pos = m.end()
                continue
            break
        keep = len(s) if u is None else u
        self.s = "" if final else s[max(keep, pos):]


def _every(rx: "re.Pattern[str]", text: str, pos: int):
    """Every match of ``rx`` at every start position (overlapping): a match nested in
    another's value is still found, so the union covers both."""
    m = rx.search(text, pos)
    while m is not None:
        yield m
        m = rx.search(text, m.start() + 1)


_WS_RESUME_BACK = 32  # > longest keyword/header (`proxy-authorization:apikey`) plus `[:=]`


def _every_to_ws(rx: "re.Pattern[str]", text: str, pos: int, skip):
    """r4 P1-4: the matches of a rule whose span runs on to the next whitespace. A later
    match starting before that whitespace lies inside the (whitespace-extended) span, so
    the search resumes there (less a keyword's length): linear, where every start position was quadratic.
    ``skip(m)`` -> True for a match that is not used (the next position is tried)."""
    m = rx.search(text, pos)
    while m is not None:
        if skip(m):
            m = rx.search(text, m.start() + 1)
            continue
        yield m
        # A keyword ending this run (`...password:` + space + value) starts a match whose
        # value is past the whitespace: resume a keyword's length before it.
        nxt = _TO_WS_RE.match(text, m.end()).end()
        m = rx.search(text, max(nxt - _WS_RESUME_BACK, m.start() + 1))


def _merge(spans: List[Span], n: int) -> List[Span]:
    """Union of spans (clipped to [0, n)); an exact-value label wins in a merged span."""
    out: List[Span] = []
    clipped = [(max(0, a), min(n, b), lab) for a, b, lab in spans if b > 0 and a < n]
    for a, b, label in sorted(clipped, key=lambda s: (s[0], -(s[1] - s[0]), s[2] == "[REDACTED]")):
        if a >= b:
            continue
        if out and a <= out[-1][1]:
            pa, pb, pl = out[-1]
            keep = pl if pl != "[REDACTED]" else label
            out[-1] = (pa, max(pb, b), keep)
        else:
            out.append((a, b, label))
    return out


def _apply_merged(text: str, merged: List[Span]) -> str:
    parts, pos = [], 0
    for a, b, label in merged:
        parts.append(text[pos:a])
        parts.append(label)
        pos = b
    parts.append(text[pos:])
    return "".join(parts)


def _apply(text: str, spans: List[Span]) -> str:
    return _apply_merged(text, _merge(spans, len(text)))


def _merge_ws(text: str, spans: List[Span], ws_starts: List[int]) -> List[Span]:
    r"""Merge spans and extend any span absorbing a ``\S+`` rule on to whitespace."""
    merged = _merge(spans, len(text))
    if not ws_starts:
        return merged
    starts = sorted(ws_starts)
    while True:
        changed = False
        ext: List[Span] = []
        for a, b, lab in merged:
            k = bisect.bisect_left(starts, a)
            if k < len(starts) and starts[k] < b:
                nb = _TO_WS_RE.match(text, b).end()
                if nb > b:
                    b = nb
                    changed = True
            ext.append((a, b, lab))
        merged = _merge(ext, len(text))
        if not changed:
            return merged


def _is_valid_label_for(label: Optional[str], allowed_labels: Set[str]) -> bool:
    """True for unlabelled markers, configured exact labels, or standard run_token labels."""
    return (
        label is None
        or label in allowed_labels
        or _RUN_TOKEN_LABEL_RE.fullmatch(label) is not None
    )


def _is_valid_marker_for(text: str, allowed_labels: Set[str]) -> bool:
    m = _CAND_MARKER_RE.fullmatch(text)
    return m is not None and _is_valid_label_for(m.group(1), allowed_labels)


def _is_clean_marker_tail(tail: str) -> bool:
    pos = 0
    n = len(tail)
    while pos < n:
        if _MARKER_WS_ESC_RE.match(tail, pos) is not None:
            return True
        m = _MARKER_PUNCT_UNIT_RE.match(tail, pos)
        if m is None:
            return False
        pos = m.end()
    return True


class _MarkerIndex:
    """Precomputed candidate redaction markers in a single line for O(log M) lookup."""

    def __init__(self, text: str, start: int, allowed_labels: Set[str]) -> None:
        self.text = text
        self.cand = [
            (m.start(), m.end(), _is_valid_label_for(m.group(1), allowed_labels))
            for m in _CAND_MARKER_RE.finditer(text, start)
        ]
        self.cand_starts = [a for a, _b, _ok in self.cand]
        self.cand_map = {a: (b, ok) for a, b, ok in self.cand}

    def containing(self, pos: int) -> Optional[Tuple[int, int, bool]]:
        k = bisect.bisect_right(self.cand_starts, pos) - 1
        if k >= 0 and pos < self.cand[k][1]:
            return self.cand[k]
        return None

    def in_valid(self, pos: int) -> bool:
        hit = self.containing(pos)
        return hit is not None and hit[2]

    def inspect(self, pos: int, end_pos: Optional[int] = None) -> Tuple[str, int, int]:
        """Classify a value starting at ``pos`` as 'valid', 'scrub', or 'none'."""
        hit = self.cand_map.get(pos)
        if hit is None:
            return "none", pos, pos
        mv_end, is_valid = hit
        if end_pos is not None and mv_end <= end_pos:
            tail = self.text[mv_end:end_pos]
        else:
            tail = _TO_WS_RE.match(self.text, mv_end).group(0)
        if not _is_clean_marker_tail(tail):
            return "none", pos, pos
        return ("valid" if is_valid else "scrub"), pos, mv_end


class _MultiState:
    """Mutable cursor for feeding lines sequentially into ``_MultiExactMatcher``."""

    __slots__ = ("st", "states")

    def __init__(self) -> None:
        self.st: int = 0
        self.states: Optional[Set[int]] = None


class _MultiExactMatcher:
    """O(N) line-level Aho-Corasick automaton for multi-line exact secrets of any length."""

    def __init__(self, items: Sequence[Tuple[str, Tuple[str, ...]]]) -> None:
        self.goto: List[Dict[str, int]] = [{}]
        self.depth: List[int] = [0]
        self.first_len: List[int] = [0]
        self.parent: List[int] = [0]
        self.in_edge: List[str] = [""]
        self.fail: List[int] = [0]
        self.exits_map: List[Dict[str, Tuple[str, int, int]]] = [{}]
        self.exit_link: List[int] = [0]

        self.rev_goto: List[Dict[str, int]] = [{}]
        self.rev_node: List[int] = [0]

        self.lt_goto: List[Dict[str, int]] = [{}]
        self.lt_term: List[Optional[str]] = [None]

        self.middle_edges: Set[str] = set()

        for name, pieces in items:
            if len(pieces) < 2:
                continue
            u = 0
            first_l = len(pieces[0])
            for idx, p in enumerate(pieces[:-1]):
                nxt = self.goto[u].get(p)
                if nxt is None:
                    nxt = len(self.goto)
                    self.goto[u][p] = nxt
                    self.goto.append({})
                    self.depth.append(idx + 1)
                    self.first_len.append(first_l)
                    self.parent.append(u)
                    self.in_edge.append(p)
                    self.fail.append(0)
                    self.exits_map.append({})
                    self.exit_link.append(0)
                if idx > 0:
                    self.middle_edges.add(p)
                u = nxt
            last_p = pieces[-1]
            if last_p not in self.exits_map[u]:
                self.exits_map[u][last_p] = (name, len(pieces), first_l)
            ls = 0
            for ch in last_p:
                lnxt = self.lt_goto[ls].get(ch)
                if lnxt is None:
                    lnxt = len(self.lt_goto)
                    self.lt_goto[ls][ch] = lnxt
                    self.lt_goto.append({})
                    self.lt_term.append(None)
                ls = lnxt
            self.lt_term[ls] = last_p

        for p0, nid in self.goto[0].items():
            rs = 0
            for ch in reversed(p0):
                rnxt = self.rev_goto[rs].get(ch)
                if rnxt is None:
                    rnxt = len(self.rev_goto)
                    self.rev_goto[rs][ch] = rnxt
                    self.rev_goto.append({})
                    self.rev_node.append(0)
                rs = rnxt
            self.rev_node[rs] = nid

        order = sorted(
            range(1, len(self.goto)),
            key=lambda nid: (self.depth[nid], self.first_len[nid]),
        )
        for u in order:
            if self.depth[u] == 1:
                self.fail[u] = self._longest_p0_node(self.in_edge[u], strict=True)
            else:
                r = self.parent[u]
                w = self.in_edge[u]
                f = self.fail[r]
                while f and w not in self.goto[f]:
                    f = self.fail[f]
                if w in self.goto[f]:
                    self.fail[u] = self.goto[f][w]
                else:
                    self.fail[u] = self._longest_p0_node(w)
            f = self.fail[u]
            self.exit_link[u] = f if self.exits_map[f] else self.exit_link[f]

    def _longest_p0_node(self, s: str, strict: bool = False) -> int:
        st = 0
        best = 0
        limit = len(s) - 1 if strict else len(s)
        rev_goto = self.rev_goto
        rev_node = self.rev_node
        for idx, ch in enumerate(reversed(s)):
            nxt = rev_goto[st].get(ch)
            if nxt is None:
                break
            st = nxt
            if idx < limit and rev_node[st]:
                best = rev_node[st]
        return best

    def _matched_last_pieces(self, line: str) -> List[str]:
        matched: List[str] = []
        st = 0
        lt_goto = self.lt_goto
        lt_term = self.lt_term
        for ch in line:
            nxt = lt_goto[st].get(ch)
            if nxt is None:
                break
            st = nxt
            lp = lt_term[st]
            if lp is not None:
                matched.append(lp)
        return matched

    def _collect_exits_for_node(
        self,
        u: int,
        matched_last: List[str],
        matches: List[Tuple[str, int, int, int]],
    ) -> None:
        max_end = len(matched_last[-1])
        best_last_len = 0
        cur = u if self.exits_map[u] else self.exit_link[u]
        while cur:
            ex_map = self.exits_map[cur]
            for lp in reversed(matched_last):
                lp_len = len(lp)
                if lp_len <= best_last_len:
                    break
                hit = ex_map.get(lp)
                if hit is not None:
                    label, m, first_l = hit
                    matches.append((label, m, first_l, lp_len))
                    best_last_len = lp_len
                    break
            if best_last_len >= max_end:
                break
            cur = self.exit_link[cur]

    def _collect_exits_for_over(
        self,
        u: int,
        matches: List[Tuple[str, int, int, int]],
    ) -> None:
        cur = u if self.exits_map[u] else self.exit_link[u]
        if cur:
            label, m, first_l = next(iter(self.exits_map[cur].values()))
            matches.append((label, m, first_l, 1))

    def _step_normal(self, st: int, body: str) -> int:
        goto = self.goto
        fail = self.fail
        while st and body not in goto[st]:
            st = fail[st]
        nxt = goto[st].get(body)
        if nxt is not None:
            return nxt
        return self._longest_p0_node(body)

    def _prune_and_set(self, state: _MultiState, new_states: Set[int]) -> None:
        if len(new_states) > 1:
            dominated: Set[int] = set()
            fail = self.fail
            for u in new_states:
                f = fail[u]
                while f and f not in dominated:
                    dominated.add(f)
                    f = fail[f]
            new_states.difference_update(dominated)
        if not new_states:
            state.st = 0
            state.states = None
        elif len(new_states) == 1:
            state.st = next(iter(new_states))
            state.states = None
        else:
            state.st = 0
            state.states = new_states

    def step(
        self,
        state: _MultiState,
        line: str,
        over: bool,
        nl: bool,
    ) -> Tuple[List[Tuple[str, int, int, int]], int]:
        matches: List[Tuple[str, int, int, int]] = []
        if over:
            if state.states is None:
                if state.st:
                    self._collect_exits_for_over(state.st, matches)
            else:
                for u in state.states:
                    if u:
                        self._collect_exits_for_over(u, matches)
        else:
            if state.states is not None or state.st:
                matched_last = self._matched_last_pieces(line)
                if matched_last:
                    if state.states is None:
                        self._collect_exits_for_node(state.st, matched_last, matches)
                    else:
                        for u in sorted(
                            state.states,
                            key=lambda nid: (-self.depth[nid], -self.first_len[nid]),
                        ):
                            if u:
                                self._collect_exits_for_node(u, matched_last, matches)

        if not nl:
            state.st = 0
            state.states = None
            return matches, 0

        if over:
            base_states = {state.st} if state.states is None else state.states
            closure = set(base_states)
            stack = [u for u in base_states if u]
            while stack:
                u = stack.pop()
                f = self.fail[u]
                if f and f not in closure:
                    closure.add(f)
                    stack.append(f)
            closure.add(0)
            new_states: Set[int] = set()
            for u in closure:
                new_states.update(self.goto[u].values())
            self._prune_and_set(state, new_states)
        else:
            body = line[:-1] if line.endswith("\n") else line
            if state.states is None:
                state.st = self._step_normal(state.st, body)
            elif body not in self.middle_edges:
                state.st = self._longest_p0_node(body)
                state.states = None
            else:
                new_states = {self._step_normal(u, body) for u in state.states}
                new_states.discard(0)
                self._prune_and_set(state, new_states)

        if state.states is None:
            active_depth = self.depth[state.st]
        else:
            active_depth = max((self.depth[u] for u in state.states), default=0)
        return matches, active_depth

    def find_text_spans(self, text: str) -> List[Span]:
        if "\n" not in text:
            return []
        parts = text.split("\n")
        offs: List[int] = []
        pos = 0
        for part in parts:
            offs.append(pos)
            pos += len(part) + 1
        spans: List[Span] = []
        st = 0
        last_idx = len(parts) - 1
        for i, body in enumerate(parts):
            if st:
                matched_last = self._matched_last_pieces(body)
                if matched_last:
                    matches: List[Tuple[str, int, int, int]] = []
                    self._collect_exits_for_node(st, matched_last, matches)
                    for label, m, first_l, last_l in matches:
                        s_i = i - m + 1
                        start = offs[s_i] + len(parts[s_i]) - first_l
                        end = offs[i] + last_l
                        spans.append((start, end, f"[REDACTED:{label}]"))
            if i < last_idx:
                st = self._step_normal(st, body)
        return spans

    def contains_any_text(self, text: str) -> bool:
        if "\n" not in text:
            return False
        parts = text.split("\n")
        st = 0
        last_idx = len(parts) - 1
        for i, body in enumerate(parts):
            if st:
                matched_last = self._matched_last_pieces(body)
                if matched_last:
                    matches: List[Tuple[str, int, int, int]] = []
                    self._collect_exits_for_node(st, matched_last, matches)
                    if matches:
                        return True
            if i < last_idx:
                st = self._step_normal(st, body)
        return False


class _SingleExactMatcher:
    """O(N) Aho-Corasick multi-pattern matcher for single-line exact secrets."""

    def __init__(self, items: Sequence[Tuple[str, str]]) -> None:
        self.items = list(items)
        self.goto: List[Dict[str, int]] = [{}]
        self.fail: List[int] = [0]
        self.out: List[Optional[Tuple[int, str]]] = [None]
        self.rx: Optional["re.Pattern[str]"] = None
        if not self.items:
            return
        unique_vals = sorted({v for _n, v in self.items}, key=len, reverse=True)
        self.rx = re.compile("|".join(re.escape(v) for v in unique_vals))
        for name, val in self.items:
            st = 0
            for ch in val:
                nxt = self.goto[st].get(ch)
                if nxt is None:
                    nxt = len(self.goto)
                    self.goto[st][ch] = nxt
                    self.goto.append({})
                    self.fail.append(0)
                    self.out.append(None)
                st = nxt
            if self.out[st] is None:
                self.out[st] = (len(val), f"[REDACTED:{name}]")
        q: deque = deque()
        for nxt in self.goto[0].values():
            q.append(nxt)
        while q:
            r = q.popleft()
            for ch, s in self.goto[r].items():
                q.append(s)
                f = self.fail[r]
                while f and ch not in self.goto[f]:
                    f = self.fail[f]
                self.fail[s] = self.goto[f].get(ch, 0)
                if self.out[s] is None:
                    self.out[s] = self.out[self.fail[s]]

    def find_spans(self, text: str) -> List[Span]:
        if self.rx is None or not text or self.rx.search(text) is None:
            return []
        spans: List[Span] = []
        st = 0
        goto = self.goto
        fail = self.fail
        out = self.out
        for idx, ch in enumerate(text):
            while st and ch not in goto[st]:
                st = fail[st]
            st = goto[st].get(ch, 0)
            hit = out[st]
            if hit is not None:
                length, label = hit
                end = idx + 1
                spans.append((end - length, end, label))
        return spans

    def contains_any(self, text: str) -> bool:
        return self.rx is not None and self.rx.search(text) is not None


def _exact_table(pairs: Sequence[Tuple[str, str]]) -> List[Tuple[str, str]]:
    table: List[Tuple[str, str]] = []
    for name, value in pairs:
        # PR #40 r7 P1: newlines at either end delimit the value, they are not part of a
        # match ("a\nb\n" split gave a final empty piece that waited for one more line,
        # so the value leaked at EOF). Match the value without them.
        value = value.strip("\n") if "\n" in value else value
        if not value:
            continue
        table.append((name, value))
        if "\n" in value:
            for piece in value.split("\n"):
                if len(piece) >= MIN_EXACT_VALUE_LEN:
                    table.append((name, piece))
    return table


class _LineRedactor:
    """The one per-line state machine behind ``redact`` and ``StreamRedactor``.

    ``push(line, over)`` takes a complete line (with its ``\n``, or the last line
    without one); ``over`` marks a line longer than ``LINE_CAP`` whose content is not
    kept. Output lags only while an active multi-line exact secret prefix could still
    complete on upcoming lines."""

    def __init__(self, pairs: Sequence[Tuple[str, str]], jsonl: bool = False,
                 depth: int = 0,
                 _shared: Optional[Tuple[Any, ...]] = None) -> None:
        self.pairs = pairs if isinstance(pairs, list) else list(pairs)
        self.jsonl = jsonl
        self.depth = depth
        if _shared is not None:
            (self.exact, self.allowed_labels, self.matcher, self.multi_matcher) = _shared
        else:
            self.exact = _exact_table(self.pairs)
            self.allowed_labels = {"unbroken output"} | {name for name, _ in self.pairs if name}
            single = [(n, v) for n, v in self.exact if "\n" not in v]
            self.matcher = _SingleExactMatcher(single)
            multi = [(n, tuple(v.split("\n"))) for n, v in self.exact if "\n" in v]
            self.multi_matcher = _MultiExactMatcher(multi) if multi else None
        self.pending: deque = deque()             # [line_no, line, over, had_nl, key_after, start_matches]
        self.next_line_no: int = 0
        self.multi_state = _MultiState() if self.multi_matcher is not None else None
        self.full_queue: deque = deque()          # [(e_line_no, label)] ascending by e_line_no
        self.end_caps: Dict[int, Tuple[int, str]] = {}  # e_line_no -> (max_last_len, label)
        self.in_key = False
        self.armed = ""                            # "", "tok" (first token), "kw" (needs [:=])

    def push(self, line: str, over: bool = False, nl: bool = True,
             key_after: Tuple[bool, bool] = (False, True)) -> str:
        """``key_after``: for an over-long line, the key-block state after it when it
        starts outside / inside a block (from ``_KeyScan``)."""
        line_no = self.next_line_no
        self.next_line_no += 1
        entry: List[Any] = [line_no, line, over, nl, key_after, None]
        self.pending.append(entry)
        active_depth = 0
        if self.multi_matcher is not None:
            assert self.multi_state is not None
            matches, active_depth = self.multi_matcher.step(
                self.multi_state, line, over, nl if over else line.endswith("\n")
            )
            if matches:
                plen = len(self.pending)
                for label, m, first_l, last_l in matches:
                    s_entry = self.pending[plen - m]
                    sm = s_entry[5]
                    if sm is None:
                        s_entry[5] = [(first_l, label, line_no, last_l)]
                    else:
                        sm.append((first_l, label, line_no, last_l))
        out = []
        while len(self.pending) > active_depth:
            out.append(self._finalize())
        return "".join(out)

    def finish(self) -> str:
        out = []
        while self.pending:
            out.append(self._finalize())
        return "".join(out)

    def _finalize(self) -> str:
        line_no, line, over, had_nl, key_after, start_matches = self.pending.popleft()
        nl = "\n" if (over and had_nl) else ""
        body = line if over else (line[:-1] if line.endswith("\n") else line)
        tail = "" if over else line[len(body):]

        spans: List[Span] = []
        carried_in = bool(self.full_queue) or (line_no in self.end_caps)
        while self.full_queue and self.full_queue[0][0] <= line_no:
            self.full_queue.popleft()
        if not over and self.full_queue and body:
            spans.append((0, len(body), self.full_queue[0][1]))

        end_hit = self.end_caps.pop(line_no, None)
        if not over and end_hit is not None:
            last_l, lab = end_hit
            if last_l > 0:
                spans.append((0, last_l, lab))

        if start_matches:
            for first_l, name, e_line_no, last_l in start_matches:
                lab = f"[REDACTED:{name}]"
                if not over:
                    start_off = max(0, len(body) - first_l)
                    if start_off < len(body):
                        spans.append((start_off, len(body), lab))
                if e_line_no > line_no:
                    if not self.full_queue or e_line_no > self.full_queue[-1][0]:
                        if self.full_queue and self.full_queue[-1][1] == lab:
                            self.full_queue[-1] = (e_line_no, lab)
                        else:
                            self.full_queue.append((e_line_no, lab))
                    prev_end = self.end_caps.get(e_line_no)
                    if prev_end is None or last_l > prev_end[0]:
                        self.end_caps[e_line_no] = (last_l, lab)

        carries_out = bool(self.full_queue)
        if over:
            shown = ""
            if line and not self.in_key and not self.armed and not carried_in:
                shown = self._safe_prefix(line)
                if len(shown) + len(UNBROKEN) > LINE_CAP:
                    shown = ""
            self.in_key = key_after[1] if self.in_key else key_after[0]
            # Its end is unknown: it may have ended in a keyword (fail closed). "over"
            # arms like "tok", except a complete JSON event line (r4 P1-5).
            self.armed = "over"
            return shown + UNBROKEN + nl

        spans.extend(self.matcher.find_spans(line))
        spans = [(a, b, lab) for a, b, lab in spans if a < len(body)]
        # r4 P1-5: an armed line may still be a JSON event (its first non-blank string
        # value is then the armed value); a key block or a carried exact value may not.
        plain_start = not carried_in and not carries_out and not self.in_key
        pre_armed = self.armed
        l_spans, ws_starts = self._line_spans(body)
        spans += l_spans
        merged = _merge_ws(body, spans, ws_starts)
        if not self.in_key and not self.armed:
            tail_start = merged[-1][1] if merged else 0
            suffix = body[tail_start:]
            # Value missing at the end of the line (only in the unredacted suffix).
            if _ARM_RE.search(suffix):
                self.armed = "tok"
            elif _KW_END_RE.search(suffix):
                self.armed = "kw"
        out = _apply_merged(body, merged)
        if self.jsonl and plain_start and body.lstrip().startswith(("{", "[")):
            # Full review r1 P1-1: an adapter's JSONL event stays one valid JSON line; its
            # strings are redacted decoded (the line rules would cut through the quotes).
            # r2 P1-A: every JSON line is decoded, not only one the raw rules matched:
            # escaped credentials (`\"api_key\": ...`, `Bearer\t...`, `https:\/\/`) and
            # exact values holding `"`/`\\` only show once decoded.
            jout = self._json_line(body, out, pre_armed)
            if jout is not None:
                return jout + tail
        if len(out) > LINE_CAP:
            if not self.in_key:
                self.armed = "over"
            return UNBROKEN + tail
        return out + tail

    def _valid_label(self, label: Optional[str]) -> bool:
        """True for unlabelled markers, configured exact labels, or standard run_token labels."""
        return _is_valid_label_for(label, self.allowed_labels)

    def _valid_marker(self, text: str) -> bool:
        """``text`` is a whole redaction marker this redactor could have written (a forged
        `[REDACTED:<anything>]` value is not kept as one)."""
        return _is_valid_marker_for(text, self.allowed_labels)

    def _leaks(self, text: str) -> bool:
        """Some exact value, in any escape form (up to 3 levels), is in ``text``."""
        if not self.exact:
            return False
        views = _views(text)
        if any(self.matcher.contains_any(view) for view in views):
            return True
        if self.multi_matcher is not None:
            if any(self.multi_matcher.contains_any_text(view) for view in views):
                return True
        return False

    def _json_line(self, body: str, line_out: str = "", pre_armed: str = "") -> Optional[str]:
        """``body`` (a JSON object/array line) redacted string by string and re-serialized,
        or None when it is not JSON (the line rules apply). Full review r4: the line is
        decoded by ``json.loads`` (every escape and surrogate resolved, duplicate keys
        kept), each decoded string is redacted with the full rules, and the result is
        re-serialized with ASCII escapes. A line with nothing to redact is returned byte
        for byte. Any doubt (an exact value across JSON syntax, any exact value in any
        escape form of the output) or any error gives the line-rule result when that
        holds no exact value, else a whole-line ``"[REDACTED]"`` (fail closed)."""
        if not body.lstrip().startswith(("{", "[")):
            return None
        try:
            doc = json.loads(body, object_pairs_hook=_Pairs)
        except (ValueError, RecursionError):
            return None
        if self.depth >= _MAX_JSON_DEPTH:
            try:
                self._sync_doc_state(doc, pre_armed)
            except Exception:  # noqa: BLE001 - conservative fail-closed state
                self.in_key = True
                self.armed = "tok"
            return _FAIL_CLOSED_JSON  # r5 P0-1: JSON nested this deep in strings is not kept
        try:
            return self._json_doc(body, doc, line_out, pre_armed)
        except Exception:  # noqa: BLE001 - fail closed, conservative cross-line state
            self.in_key = True
            self.armed = "tok"
            return _FAIL_CLOSED_JSON

    def _sync_doc_state(self, doc: Any, pre_armed: str) -> None:
        """Linearly scan decoded ``doc`` at ``_MAX_JSON_DEPTH`` to preserve ``in_key``/``armed``."""
        armed = [pre_armed == "tok"]
        armed_kw = [pre_armed == "kw"]
        inkey = [self.in_key]

        def has_value(v: Any) -> bool:
            if isinstance(v, str):
                return bool(v.strip())
            if isinstance(v, _Pairs):
                return any(has_value(x) for _k, x in v)
            if isinstance(v, list):
                return any(has_value(x) for x in v)
            return False

        def scan_line(part: str, key: bool) -> None:
            if inkey[0]:
                e = _END_RE.search(part)
                if e is None:
                    return
                part = part[e.end():]
                inkey[0] = False
            if armed[0] and not key and part.strip():
                armed[0] = False
                armed_kw[0] = False
            elif armed_kw[0] and not key and part.strip():
                m = _LEAD_ASSIGN_RE.match(part)
                armed_kw[0] = False
                if m is not None and not m.group(1):
                    armed[0] = True
            views = _views(part)
            if any(_unclosed_key(vw) for vw in views):
                inkey[0] = True
                armed[0] = False
                armed_kw[0] = False
                return
            last = views[-1]
            # Deeper JSON or unresolved unicode escapes beyond max depth: take conservative state.
            if "\\" in last and ("u" in last or "U" in last):
                inkey[0] = True
                armed[0] = True
                return
            if last.lstrip().startswith(("{", "[")):
                inkey[0] = inkey[0] or ("PRIVATE KEY" in last)
                armed[0] = True
                return
            if not key:
                for vw in views:
                    if _ARM_RE.search(vw):
                        armed[0] = True
                        armed_kw[0] = False
                        break
                    if _KW_END_RE.search(vw):
                        armed_kw[0] = True

        def scan_str(s: str, key: bool = False) -> None:
            for part in s.split("\n"):
                scan_line(part, key)

        def walk_blank(v: Any) -> None:
            if isinstance(v, str):
                for part in v.split("\n"):
                    if inkey[0]:
                        e = _END_RE.search(part)
                        if e is None:
                            continue
                        part = part[e.end():]
                        inkey[0] = False
                    views = _views(part)
                    if any(_unclosed_key(vw) for vw in views):
                        inkey[0] = True
                    elif "\\" in views[-1] and ("u" in views[-1] or "U" in views[-1]):
                        inkey[0] = True
            elif isinstance(v, _Pairs):
                for k, x in v:
                    scan_str(k, key=True)
                    walk_blank(x)
            elif isinstance(v, list):
                for x in v:
                    walk_blank(x)

        def walk_state(v: Any) -> None:
            if isinstance(v, str):
                scan_str(v, key=False)
            elif isinstance(v, _Pairs):
                for k, x in v:
                    scan_str(k, key=True)
                    if _JSON_KW_KEY_RE.search(k) and isinstance(x, (str, list, _Pairs)):
                        if has_value(x):
                            armed[0] = False
                            armed_kw[0] = False
                        walk_blank(x)
                    else:
                        walk_state(x)
            elif isinstance(v, list):
                for x in v:
                    walk_state(x)

        walk_state(doc)
        self.in_key = inkey[0]
        self.armed = "tok" if armed[0] else ("kw" if armed_kw[0] else "")

    def _walk_json_doc(
        self, doc: Any, pre_armed: str
    ) -> Tuple[Any, List[str], bool, bool, bool]:
        shared = (self.exact, self.allowed_labels, self.matcher, self.multi_matcher)
        cur_armed = [pre_armed if pre_armed in ("tok", "kw") else ""]
        inkey = [False]
        leaves: List[str] = []

        def leaf(text: str, key: bool = False) -> str:
            leaves.append(text)
            if not text:
                return text
            was_tok_armed = (not key) and cur_armed[0] == "tok" and bool(text.strip())
            single_tok_leaf = (
                was_tok_armed and "\n" not in text and not any(ch.isspace() for ch in text.strip())
            )
            red, sub_inkey, sub_armed = _redact_state(
                text,
                self.pairs,
                True,
                self.depth + 1,
                shared,
                init_in_key=inkey[0],
                init_armed="" if key else cur_armed[0],
            )
            if sub_inkey or _unclosed_key(text):
                inkey[0] = True
                cur_armed[0] = ""
            else:
                inkey[0] = False
                if not key:
                    if sub_armed in ("tok", "over") or _ARM_RE.search(red):
                        cur_armed[0] = "tok"
                    elif sub_armed == "kw" or _KW_END_RE.search(red):
                        cur_armed[0] = "kw"
                    elif text.strip():
                        cur_armed[0] = ""
            if single_tok_leaf:
                return text if self._valid_marker(text.strip()) else "[REDACTED]"
            return red

        def has_value(v: Any) -> bool:
            if isinstance(v, str):
                return bool(v.strip())
            if isinstance(v, _Pairs):
                return any(has_value(x) for _k, x in v)
            if isinstance(v, list):
                return any(has_value(x) for x in v)
            return False

        def blank(v: Any) -> Any:
            """The value of a credential key: every string in it is a credential."""
            if isinstance(v, str):
                leaves.append(v)
                if any(_unclosed_key(vw) for vw in _views(v)):
                    inkey[0] = True
                return v if (not v or self._valid_marker(v)) else "[REDACTED]"
            if isinstance(v, _Pairs):
                return _Pairs((leaf(k, key=True), blank(x)) for k, x in v)
            if isinstance(v, list):
                return [blank(x) for x in v]
            return v

        def walk(v: Any) -> Any:
            if isinstance(v, str):
                return leaf(v)
            if isinstance(v, _Pairs):
                out_d = _Pairs()
                for k, x in v:
                    nk = leaf(k, key=True)
                    if _JSON_KW_KEY_RE.search(k) and isinstance(x, (str, list, _Pairs)):
                        if has_value(x):
                            cur_armed[0] = ""  # an empty credential field is not the value
                        out_d.append((nk, blank(x)))
                    else:
                        out_d.append((nk, walk(x)))
                return out_d
            if isinstance(v, list):
                return [walk(x) for x in v]
            return v

        red = walk(doc)
        return red, leaves, inkey[0], cur_armed[0] == "tok", cur_armed[0] == "kw"

    def _spans_json_syntax(self, raw_text: str, leaves: List[str]) -> bool:
        if not self.exact:
            return False
        b_views = _views(raw_text)
        if not any(self.matcher.contains_any(vw) for vw in b_views) and not (
            self.multi_matcher is not None and any(self.multi_matcher.contains_any_text(vw) for vw in b_views)
        ):
            return False
        body_hits = [value for _n, value in self.exact if any(value in vw for vw in b_views)]
        if not body_hits:
            return False
        return any(
            not any(val in lv for x in leaves if len(x) >= len(val) for lv in _views(x))
            for val in body_hits
        )

    def _json_doc(self, body: str, doc: Any, line_out: str, pre_armed: str) -> str:
        # r4 P1-5: a `password:`/`bearer` armed by the line before (in text) arms this
        # line's first non-blank string value; an over-long line before ("over": its end
        # is unknown) does not arm a complete JSON event (its strings still get every rule).
        red, leaves, inkey, is_armed, is_armed_kw = self._walk_json_doc(doc, pre_armed)
        same = red == doc and not inkey
        # r4 P1-2: an exact value in some escape form of the raw line but in no single
        # decoded string runs across JSON syntax (evaluated lazily only when body hits).
        spans_syntax = self._spans_json_syntax(body, leaves)
        text = body if same else _ser(red)
        self.in_key = inkey
        self.armed = "tok" if is_armed else ("kw" if is_armed_kw else "")
        # r5 P1-1: any doubt gives the whole line, never the line-rule output (which can
        # miss structural credentials and break the JSON); r5 P0-1: never grow past LINE_CAP.
        if len(text) > LINE_CAP or spans_syntax or self._leaks(text):
            if not self.in_key and not self.armed:
                self.armed = "over" if len(text) > LINE_CAP else "tok"
            return _FAIL_CLOSED_JSON
        return text

    def _safe_prefix(self, prefix: str) -> str:
        """The kept start of a withheld line, redacted; cut before any tail that could be
        the start of an exact value continuing past it."""
        cut = len(prefix)
        for _name, value in self.exact:
            for k in range(min(len(value) - 1, len(prefix)), 0, -1):
                if prefix.endswith(value[:k]):
                    cut = min(cut, len(prefix) - k)
                    break
        prefix = prefix[:cut]
        spans: List[Span] = self.matcher.find_spans(prefix)
        saved = (self.in_key, self.armed)
        l_spans, ws_starts = self._line_spans(prefix)
        spans += l_spans
        self.in_key, self.armed = saved
        return _apply_merged(prefix, _merge_ws(prefix, spans, ws_starts))

    def _kw_member_spans(self, body: str, start: int) -> List[Span]:
        """r3 P1-2 (jsonl): the value of every credential-keyword member: a plain string
        to its closing quote (kept when it is a whole valid marker), anything else up to
        the end of the line (fail closed)."""
        spans: List[Span] = []
        for m in _KW_MEMBER_RE.finditer(body, start):
            v = m.end()
            if v >= len(body):
                continue
            if body[v] == '"':
                sm = _PLAIN_STR_RE.match(body, v)
                if sm is not None:
                    if not self._valid_marker(sm.group(1)) and sm.group(1):
                        spans.append((sm.start(1), sm.end(1), "[REDACTED]"))
                    continue
            elif _JSON_LITERAL_RE.match(body, v) or body.startswith(("[]", "{}"), v):
                continue
            mk = _CAND_MARKER_RE.match(body, v)
            if mk is not None and self._valid_marker(mk.group(0)):
                continue
            spans.append((v, len(body), "[REDACTED]"))
        return spans

    def _scan_pattern_rules(
        self,
        text: str,
        start: int,
        mx: _MarkerIndex,
        ws_starts: Optional[List[int]],
    ) -> List[Span]:
        spans: List[Span] = []

        def _skip_token(m: "re.Match[str]") -> bool:
            hit = mx.containing(m.start())
            if hit is None:
                return False
            ca, cb, ok = hit
            if ok:
                return True
            tail_ws = _TO_WS_RE.match(text, cb).group(0)
            if _is_clean_marker_tail(tail_ws):
                spans.append((ca, cb, "[REDACTED]"))
                return True
            return False

        for m in _every_to_ws(_TOKEN_RE, text, start, _skip_token):
            spans.append((m.start(), _TO_WS_RE.match(text, m.end()).end(), "[REDACTED]"))
            if ws_starts is not None:
                ws_starts.append(m.start())

        def _skip_val(m: "re.Match[str]") -> bool:
            if mx.in_valid(m.start()):
                return True
            kind, ma, mb = mx.inspect(m.start(1), m.end(1))
            if kind == "valid":
                return True
            if kind == "scrub":
                spans.append((ma, mb, "[REDACTED]"))
                return True
            return False

        for rx in (_ASSIGN_RE, _BEARER_RE, _AUTH_BASIC_RE):
            for m in _every_to_ws(rx, text, start, _skip_val):
                spans.append((m.start(1), m.end(1), "[REDACTED]"))
                if ws_starts is not None:
                    ws_starts.append(m.start(1))
        return spans

    def _escaped_spans(self, body: str, start: int) -> List[Span]:
        """r3 P1-2 (jsonl): the rules again on up to three JSON-unescape levels of the
        line, mapped back to raw offsets: escaped credentials in a line that is not JSON
        (or a decoded string holding escaped JSON) are found too."""
        spans: List[Span] = []
        text = body[start:]
        s_map = list(range(len(text)))
        e_map = [k + 1 for k in range(len(text))]
        for _level in range(3):
            view, vs, ve = _unescape_view(text)
            if view == text:
                break
            s_map = [s_map[vs[k]] for k in range(len(view))]
            e_map = [e_map[ve[k] - 1] for k in range(len(view))]
            text = view
            flat = text.replace("\r", " ").replace("\n", " ")  # 1:1, single-line rules
            mx = _MarkerIndex(flat, 0, self.allowed_labels)
            found: List[Tuple[int, int]] = [
                (a, b) for a, b, _lab in self._scan_pattern_rules(flat, 0, mx, None)
            ]
            for rx in (_URI_CRED_RE, _JSON_MEMBER_RE):
                for m in rx.finditer(flat):
                    if not mx.in_valid(m.start(1)) and not self._valid_marker(m.group(1)):
                        found.append((m.start(1), m.end(1)))
            for a, b, _lab in self._kw_member_spans(flat, 0):
                found.append((a, b))
            for a, b, _lab in self.matcher.find_spans(text):
                found.append((a, b))
            if self.multi_matcher is not None and "\n" in text:
                for a, b, _lab in self.multi_matcher.find_text_spans(text):
                    found.append((a, b))
            b0 = _BEGIN_RE.search(text)
            if b0 is not None:
                found.append((b0.start(), len(text)))
            if _unclosed_key(text):
                self.in_key = True
            elif not self.in_key and not self.armed:
                if _ARM_RE.search(text):
                    self.armed = "tok"
                elif _KW_END_RE.search(text):
                    self.armed = "kw"
            for a, b in found:
                if b > a:
                    spans.append((start + s_map[a], start + e_map[b - 1], "[REDACTED]"))
        return spans

    def _line_spans(self, body: str) -> Tuple[List[Span], List[int]]:
        spans: List[Span] = []
        ws_starts: List[int] = []
        start = 0
        if self.in_key:
            m = _END_RE.search(body)
            if m is None:
                return ([(0, len(body), "[REDACTED]")] if body else []), ws_starts
            spans.append((0, m.end(), "[REDACTED]"))
            self.in_key = False
            start = m.end()
        rest = body[start:]
        if self.armed and _MESH_FENCE_RE.match(rest):
            # Full review: a mesh-actions/mesh-deliverable fence header is never the
            # value of an armed keyword; redacting it would drop the whole block.
            self.armed = ""
        mx = _MarkerIndex(body, start, self.allowed_labels)

        if self.armed and rest.strip():
            if self.armed in ("tok", "over"):
                m = _FIRST_TOKEN_RE.match(rest)
                vpos = start + m.start(1)
                kind, ma, mb = mx.inspect(vpos)
                if kind == "scrub":
                    spans.append((ma, mb, "[REDACTED]"))
                elif kind == "none":
                    spans.append((vpos, start + m.end(1), "[REDACTED]"))
                    ws_starts.append(vpos)
                self.armed = ""
            else:  # "kw": the line must start with [:=]
                m = _LEAD_ASSIGN_RE.match(rest)
                self.armed = ""
                if m is not None:
                    if m.group(1):
                        vpos = start + m.start(1)
                        kind, ma, mb = mx.inspect(vpos)
                        if kind == "scrub":
                            spans.append((ma, mb, "[REDACTED]"))
                        elif kind == "none":
                            spans.append((vpos, start + m.end(1), "[REDACTED]"))
                            ws_starts.append(vpos)
                    else:
                        self.armed = "tok"
        spans.extend(self._scan_pattern_rules(body, start, mx, ws_starts))
        if self.jsonl:
            for rx in (_URI_CRED_RE, _JSON_MEMBER_RE):
                # Values hold no unescaped quote or whitespace: no match nests in another's
                # value, so the plain (non-overlapping) scan is complete (r4 P1-4).
                for m in rx.finditer(body, start):
                    if not mx.in_valid(m.start(1)) and not self._valid_marker(m.group(1)):
                        spans.append((m.start(1), m.end(1), "[REDACTED]"))
            spans.extend(self._kw_member_spans(body, start))
            if "\\" in body[start:]:
                spans.extend(self._escaped_spans(body, start))
        b = _BEGIN_RE.search(body, start)
        while b is not None:
            e = _END_RE.search(body, b.end())
            if e is None:
                spans.append((b.start(), len(body), "[REDACTED]"))
                self.in_key = True
                break
            spans.append((b.start(), e.end(), "[REDACTED]"))
            b = _BEGIN_RE.search(body, e.end())
        return spans, ws_starts


_registered_secrets: Dict[str, str] = {}
_env_cache: Optional[Tuple[Tuple[str, str], ...]] = None


def register_secret(name: str, value: str) -> None:
    """Make an in-memory secret (e.g. a run token) known to default redaction."""
    if name and value:
        _registered_secrets[name] = value


def unregister_secret(name: str) -> None:
    _registered_secrets.pop(name, None)


def clear_registered_secrets() -> None:
    _registered_secrets.clear()


_LABEL_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_]")


def _label_name(name: str) -> str:
    """Full review r1 P1-2: the `[REDACTED:<name>]` label of an exact value.

    Env names may hold `-`, `.`, `/`, `:`, `]` or newlines (npm's
    `npm_config_//registry.npmjs.org/:_authToken`); such a label would not be a stored
    marker that approvals can normalize, and `]`/newline would break the marker itself.
    The name is reduced to an identifier; it is a label only, the value is what is matched.
    """
    safe = _LABEL_UNSAFE_RE.sub("_", name)
    if not safe or safe[0].isdigit():
        safe = "_" + safe
    return safe


def collect_exact_values(
    env: Optional[Mapping[str, str]] = None,
    extra: Optional[Iterable[Tuple[str, str]]] = None,
) -> List[Tuple[str, str]]:
    """Return (name, value) pairs that must be redacted exactly, longest first."""
    source = os.environ if env is None else env
    pairs: List[Tuple[str, str]] = []
    for name, value in source.items():
        if not value or len(value) < MIN_EXACT_VALUE_LEN:
            continue
        if SECRET_ENV_NAME_RE.search(name or ""):
            pairs.append((_label_name(name), value))
    if extra:
        pairs.extend((_label_name(name), value) for name, value in extra if name and value)
    pairs.sort(key=lambda pair: len(pair[1]), reverse=True)
    return pairs


def default_exact_values() -> List[Tuple[str, str]]:
    """Env-derived exact values plus secrets registered in this process."""
    global _env_cache
    if _env_cache is None:
        _env_cache = tuple(collect_exact_values())
    extra = list(_registered_secrets.items())
    merged = list(_env_cache) + extra
    merged.sort(key=lambda pair: len(pair[1]), reverse=True)
    return merged


def _key_after(body: str) -> Tuple[bool, bool]:
    states = []
    for start in (False, True):
        ks = _KeyScan(start)
        ks.feed(body, final=True)
        states.append(ks.in_key)
    return states[0], states[1]


def redact(text: str, exact_values: Optional[Sequence[Tuple[str, str]]] = None,
           jsonl: bool = False) -> str:
    """Redact ``text``: every complete line through the line state machine (see the
    module doc), the last line at the end of the text. ``jsonl`` (run logs) adds the
    URL/JSON-member rules and keeps JSON lines valid (full review r1 P1-1); stored
    values (deliverables, op_json) keep the default rules, which approvals verify."""
    if not text:
        return text
    pairs = default_exact_values() if exact_values is None else list(exact_values)
    return _redact(text, pairs, jsonl, 0)


def _redact_state(
    text: str,
    pairs: Sequence[Tuple[str, str]],
    jsonl: bool,
    depth: int,
    _shared: Optional[Tuple[Any, ...]] = None,
    init_in_key: bool = False,
    init_armed: str = "",
) -> Tuple[str, bool, str]:
    if not text:
        return text, init_in_key, init_armed
    lr = _LineRedactor(pairs, jsonl=jsonl, depth=depth, _shared=_shared)
    lr.in_key = init_in_key
    lr.armed = init_armed
    if "\n" not in text:
        if len(text) > LINE_CAP:
            res = lr.push(_over_prefix(text), over=True, nl=False, key_after=_key_after(text)) + lr.finish()
        else:
            res = lr.push(text) + lr.finish()
        return res, lr.in_key, lr.armed
    out = []
    for line in re.findall(r"[^\n]*\n|[^\n]+$", text):
        body_len = len(line) - (1 if line.endswith("\n") else 0)
        if body_len > LINE_CAP:
            out.append(lr.push(_over_prefix(line), over=True, nl=line.endswith("\n"),
                               key_after=_key_after(line[:body_len])))
        else:
            out.append(lr.push(line))
    out.append(lr.finish())
    return "".join(out), lr.in_key, lr.armed


def _redact(text: str, pairs: Sequence[Tuple[str, str]], jsonl: bool, depth: int) -> str:
    return _redact_state(text, pairs, jsonl, depth)[0]


def redact_json_text(
    raw: str, exact_values: Optional[Sequence[Tuple[str, str]]] = None
) -> str:
    """Redact a standalone JSON/text file (e.g. vibe-out/export.json) with full JSONL rules.

    Preserves duplicate object keys via ``_Pairs`` so shadowed secrets cannot survive
    a ``rdoc == doc`` check, applies credential-key, URL-password, and nested-JSON rules,
    and falls back to line-based ``redact(..., jsonl=True)`` when ``raw`` is not JSON.
    """
    if not raw:
        return raw
    pairs = default_exact_values() if exact_values is None else list(exact_values)
    try:
        doc = json.loads(raw, object_pairs_hook=_Pairs)
    except (ValueError, RecursionError):
        return _redact(raw, pairs, True, 0)
    trailing_nl = "\n" if raw.endswith("\n") else ""
    lr = _LineRedactor(pairs, jsonl=True, depth=0)
    try:
        if isinstance(doc, (_Pairs, list)):
            red, leaves, inkey, _armed, _armed_kw = lr._walk_json_doc(doc, "")
            if lr._spans_json_syntax(raw, leaves):
                return _FAIL_CLOSED_JSON + trailing_nl
            if red == doc and not inkey and not lr._leaks(raw):
                return raw
            out = _ser_pretty(red)
            if lr._leaks(out):
                return _FAIL_CLOSED_JSON + trailing_nl
            return out + trailing_nl
        if isinstance(doc, str):
            r_str = _redact(doc, pairs, True, 1)
            out = _json_dumps_utf8(r_str)
            if lr._leaks(out):
                return _FAIL_CLOSED_JSON + trailing_nl
            return raw if (r_str == doc and not lr._leaks(raw)) else (out + trailing_nl)
        return _FAIL_CLOSED_JSON + trailing_nl if lr._leaks(raw) else raw
    except Exception:  # noqa: BLE001 - fail closed
        return _FAIL_CLOSED_JSON + trailing_nl


def _blank_json_value(v: Any, pairs: Sequence[Tuple[str, str]], allowed_labels: Set[str]) -> Any:
    """Redact all non-empty string leaves under a credential-named JSON key."""
    if isinstance(v, str):
        return v if (not v or _is_valid_marker_for(v, allowed_labels)) else "[REDACTED]"
    if isinstance(v, dict):
        return {
            (_redact(key, pairs, True, 0) if isinstance(key, str) else key): _blank_json_value(
                val, pairs, allowed_labels
            )
            for key, val in v.items()
        }
    if isinstance(v, list):
        return [_blank_json_value(item, pairs, allowed_labels) for item in v]
    if isinstance(v, tuple):
        return tuple(_blank_json_value(item, pairs, allowed_labels) for item in v)
    return v


def _redact_value_with_pairs(
    value: Any,
    pairs: Sequence[Tuple[str, str]],
    jsonl: bool,
    allowed_labels: Optional[Set[str]],
) -> Any:
    if isinstance(value, str):
        return _redact(value, pairs, jsonl, 0)
    if isinstance(value, dict):
        out: Dict[Any, Any] = {}
        for key, val in value.items():
            rk = _redact(key, pairs, jsonl, 0) if isinstance(key, str) else key
            if jsonl and isinstance(key, str) and _JSON_KW_KEY_RE.search(key):
                assert allowed_labels is not None
                rv = _blank_json_value(val, pairs, allowed_labels)
            else:
                rv = _redact_value_with_pairs(val, pairs, jsonl, allowed_labels)
            out[rk] = rv
        return out
    if isinstance(value, list):
        return [_redact_value_with_pairs(item, pairs, jsonl, allowed_labels) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value_with_pairs(item, pairs, jsonl, allowed_labels) for item in value)
    return value


def redact_value(
    value: Any,
    exact_values: Optional[Sequence[Tuple[str, str]]] = None,
    *,
    jsonl: bool = False,
) -> Any:
    """Recursively redact the string leaves of JSON-like data."""
    pairs = default_exact_values() if exact_values is None else list(exact_values)
    allowed_labels = ({"unbroken output"} | {name for name, _ in pairs if name}) if jsonl else None
    return _redact_value_with_pairs(value, pairs, jsonl, allowed_labels)


class StreamRedactor:
    """Redact a text stream arriving in arbitrary pieces. Output equals ``redact`` of
    the whole stream for any chunking: lines are buffered until their newline and fed
    to the same ``_LineRedactor``; a line growing past ``LINE_CAP`` is withheld whole
    (its rest is discarded up to the newline)."""

    def __init__(self, exact_values: Optional[Sequence[Tuple[str, str]]] = None,
                 jsonl: bool = False) -> None:
        pairs = default_exact_values() if exact_values is None else list(exact_values)
        self.lines = _LineRedactor(pairs, jsonl=jsonl)
        self.buf = ""
        self.discarding = False
        self.scans: List[_KeyScan] = []
        self.prefix = ""
        self.withheld = 0  # lines withheld for exceeding LINE_CAP

    def _over(self, nl: bool) -> str:
        for ks in self.scans:
            ks.feed("", final=True)
        after = (self.scans[0].in_key, self.scans[1].in_key)
        self.scans = []
        self.discarding = False
        return self.lines.push(self.prefix, over=True, nl=nl, key_after=after)

    def feed(self, text: str) -> str:
        out = []
        pos = 0
        while pos < len(text):
            i = text.find("\n", pos)
            if self.discarding:
                piece = text[pos:] if i == -1 else text[pos:i]
                for ks in self.scans:
                    ks.feed(piece)
                if i == -1:
                    return "".join(out)
                out.append(self._over(True))
                pos = i + 1
                continue
            if i == -1:
                self.buf += text[pos:]
                if len(self.buf) > LINE_CAP:
                    self.prefix = _over_prefix(self.buf)
                    self.scans = [_KeyScan(False), _KeyScan(True)]
                    for ks in self.scans:
                        ks.feed(self.buf)
                    self.buf = ""
                    self.discarding = True
                    self.withheld += 1
                break
            line = self.buf + text[pos:i + 1]
            self.buf = ""
            pos = i + 1
            if len(line) - 1 > LINE_CAP:
                self.withheld += 1
                out.append(self.lines.push(_over_prefix(line), over=True, key_after=_key_after(line[:-1])))
            else:
                out.append(self.lines.push(line))
        return "".join(out)

    def flush(self) -> str:
        out = []
        if self.discarding:
            out.append(self._over(False))
        elif self.buf:
            out.append(self.lines.push(self.buf))
            self.buf = ""
        out.append(self.lines.finish())
        return "".join(out)
