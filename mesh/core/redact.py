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

import os
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

SECRET_ENV_NAME_RE = re.compile(r"(?i)(key|token|secret|passw|cookie|credential|auth)")
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
    r"mesh_(?:at|rt)_[A-Za-z0-9_-]{20,}|fk-[A-Za-z0-9_-]{16,}|sk-[A-Za-z0-9_-]{16,}"
    r"|AIza[0-9A-Za-z_-]{35,}|gh[pousr]_[A-Za-z0-9]{30,}|xox[abpr]-[A-Za-z0-9-]{10,}")
#: A token or bearer credential is redacted on to the next whitespace (fail closed).
_TO_WS_RE = re.compile(r"\S*")
_KW = r"(?:password|passwd|secret|api[_-]?key)"
_ASSIGN_RE = re.compile(r"(?i)" + _KW + r"\s*[:=]\s*(\S+)")
_BEARER_RE = re.compile(r"(?i)bearer\s+([A-Za-z0-9._~+/-]{16,}\S*)")
#: Value missing at the end of the line: the next non-blank line's first token is it.
_ARM_RE = re.compile(r"(?i)(?:" + _KW + r"\s*[:=]|bearer)\s*$")
#: Keyword at the end of the line with no ``:``/``=`` yet: armed if the next
#: non-blank line starts with one.
_KW_END_RE = re.compile(r"(?i)" + _KW + r"\s*$")
_LEAD_ASSIGN_RE = re.compile(r"\s*[:=]\s*(\S*)")
_FIRST_TOKEN_RE = re.compile(r"\s*(\S+)")
_BEGIN_RE = re.compile(r"-----BEGIN [A-Z ]{0,64}PRIVATE KEY-----")
_END_RE = re.compile(r"-----END [A-Z ]{0,64}PRIVATE KEY-----")

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


def _apply(text: str, spans: List[Span]) -> str:
    parts, pos = [], 0
    for a, b, label in _merge(spans, len(text)):
        parts.append(text[pos:a])
        parts.append(label)
        pos = b
    parts.append(text[pos:])
    return "".join(parts)


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
    kept. Output lags by K-1 lines when a multi-line exact value is registered."""

    def __init__(self, pairs: Sequence[Tuple[str, str]]) -> None:
        self.exact = _exact_table(pairs)
        self.k = 1 + max([v.count("\n") for _n, v in self.exact] or [0])
        self.pending: List[Tuple[str, bool, bool]] = []   # (line, over, over-line had "\n")
        self.carry: List[Span] = []                # exact spans from earlier lines (relative to pending[0])
        self.in_key = False
        self.armed = ""                            # "", "tok" (first token), "kw" (needs [:=])
        self.key_after: List[Tuple[bool, bool]] = []

    def push(self, line: str, over: bool = False, nl: bool = True,
             key_after: Tuple[bool, bool] = (False, True)) -> str:
        """``key_after``: for an over-long line, the key-block state after it when it
        starts outside / inside a block (from ``_KeyScan``)."""
        self.pending.append((line, over, nl))
        self.key_after.append(key_after)
        out = []
        while len(self.pending) >= self.k:
            out.append(self._finalize())
        return "".join(out)

    def finish(self) -> str:
        out = []
        while self.pending:
            out.append(self._finalize())
        return "".join(out)

    def _finalize(self) -> str:
        line, over, had_nl = self.pending[0]
        nl = "\n" if (over and had_nl) else ""
        joined = "".join(("" if o else l) + ("\n" if o and h else "") for l, o, h in self.pending)
        n = 0 if over else len(line)
        spans: List[Span] = list(self.carry)
        carried = bool(self.carry)  # an exact value runs into this line
        if over:
            # PR #40 r8: a withheld line's end is unknown, so it may end with the first
            # piece of a multi-line value; the following pieces are still redacted.
            for name, value in self.exact:
                if "\n" in value:
                    spans.extend(self._multi_spans(name, value))
        if not over:
            for name, value in self.exact:
                if "\n" in value:
                    spans.extend(self._multi_spans(name, value))
                    continue
                i = joined.find(value)
                while i != -1 and i < n:
                    spans.append((i, i + len(value), f"[REDACTED:{name}]"))
                    i = joined.find(value, i + 1)
        # Spans running past this line are carried into the next one.
        step = n + len(nl)
        self.carry = [(a - step, b - step, lab) for a, b, lab in spans if b > step]
        self.pending.pop(0)
        key_after = self.key_after.pop(0)
        if over:
            shown = ""
            if line and not self.in_key and not self.armed and not carried:
                shown = self._safe_prefix(line)
            self.in_key = key_after[1] if self.in_key else key_after[0]
            # Its end is unknown: it may have ended in a keyword (fail closed).
            self.armed = "tok"
            return shown + UNBROKEN + nl
        body = line[:-1] if line.endswith("\n") else line
        tail = line[len(body):]
        spans = [(a, b, lab) for a, b, lab in spans if a < len(body)]
        spans += self._line_spans(body)
        return _apply(body, spans) + tail

    def _multi_spans(self, name: str, value: str) -> List[Span]:
        """A multi-line exact value starting in the first pending line, matched line by
        line over the window (PR #40 r6). A withheld (over-long) line is unknown and
        counts as matching its piece (fail closed); so does an exhausted window only
        when the input ended inside a withheld line run, never otherwise."""
        pieces = value.split("\n")
        first, first_over, first_nl = self.pending[0]
        fbody = first[:-1] if first.endswith("\n") else first
        if first_over:
            if not first_nl:
                return []
            fbody = ""  # withheld: may end with pieces[0] (fail closed)
        elif not first.endswith("\n") or not fbody.endswith(pieces[0]):
            return []
        offs, pos = [], 0
        for l, o, h in self.pending:
            offs.append(pos)
            pos += (1 if h else 0) if o else len(l)
        start = 0 if first_over else len(fbody) - len(pieces[0])
        end = None
        for j in range(1, len(pieces)):
            if j >= len(self.pending):
                return []
            l, o, h = self.pending[j]
            last = j == len(pieces) - 1
            if o:
                if last or h:
                    # PR #40 r7 P2: a withheld line holding the last piece is covered too
                    # (end past its start), so its kept head is not released.
                    end = offs[j] + 1
                    if last:
                        break
                    continue
                return []
            body = l[:-1] if l.endswith("\n") else l
            if last:
                if not l.startswith(pieces[j]):
                    return []
                end = offs[j] + len(pieces[j])
            else:
                if body != pieces[j] or not l.endswith("\n"):
                    return []
                end = offs[j] + len(l)
        label = f"[REDACTED:{name}]"
        return [(start, end if end is not None else len(first), label)]

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
        spans: List[Span] = []
        for name, value in self.exact:
            i = prefix.find(value)
            while i != -1:
                spans.append((i, i + len(value), f"[REDACTED:{name}]"))
                i = prefix.find(value, i + 1)
        saved = (self.in_key, self.armed)
        spans += self._line_spans(prefix)
        self.in_key, self.armed = saved
        return _apply(prefix, spans)

    def _line_spans(self, body: str) -> List[Span]:
        spans: List[Span] = []
        start = 0
        if self.in_key:
            m = _END_RE.search(body)
            if m is None:
                return [(0, len(body), "[REDACTED]")] if body else []
            spans.append((0, m.end(), "[REDACTED]"))
            self.in_key = False
            start = m.end()
        rest = body[start:]
        if self.armed and rest.strip():
            if self.armed == "tok":
                m = _FIRST_TOKEN_RE.match(rest)
                spans.append((start + m.start(1), start + m.end(1), "[REDACTED]"))
                self.armed = ""
            else:  # "kw": the line must start with [:=]
                m = _LEAD_ASSIGN_RE.match(rest)
                self.armed = ""
                if m is not None:
                    if m.group(1):
                        spans.append((start + m.start(1), start + m.end(1), "[REDACTED]"))
                    else:
                        self.armed = "tok"
        for m in _every(_TOKEN_RE, body, start):
            spans.append((m.start(), _TO_WS_RE.match(body, m.end()).end(), "[REDACTED]"))
        for rx in (_ASSIGN_RE, _BEARER_RE):
            for m in _every(rx, body, start):
                spans.append((m.start(1), m.end(1), "[REDACTED]"))
        b = _BEGIN_RE.search(body, start)
        while b is not None:
            e = _END_RE.search(body, b.end())
            if e is None:
                spans.append((b.start(), len(body), "[REDACTED]"))
                self.in_key = True
                break
            spans.append((b.start(), e.end(), "[REDACTED]"))
            b = _BEGIN_RE.search(body, e.end())
        if not self.in_key and not self.armed:
            # Value missing at the end of the line (outside every redacted span).
            if _ARM_RE.search(body):
                self.armed = "tok"
            elif _KW_END_RE.search(body):
                self.armed = "kw"
        return spans


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
            pairs.append((name, value))
    if extra:
        pairs.extend((name, value) for name, value in extra if name and value)
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


def redact(text: str, exact_values: Optional[Sequence[Tuple[str, str]]] = None) -> str:
    """Redact ``text``: every complete line through the line state machine (see the
    module doc), the last line at the end of the text."""
    if not text:
        return text
    pairs = default_exact_values() if exact_values is None else list(exact_values)
    lr = _LineRedactor(pairs)
    out = []
    for line in re.findall(r"[^\n]*\n|[^\n]+$", text):
        body_len = len(line) - (1 if line.endswith("\n") else 0)
        if body_len > LINE_CAP:
            out.append(lr.push(_over_prefix(line), over=True, nl=line.endswith("\n"),
                               key_after=_key_after(line[:body_len])))
        else:
            out.append(lr.push(line))
    out.append(lr.finish())
    return "".join(out)


def redact_value(value: Any, exact_values: Optional[Sequence[Tuple[str, str]]] = None) -> Any:
    """Recursively redact the string leaves of JSON-like data."""
    if isinstance(value, str):
        return redact(value, exact_values)
    if isinstance(value, dict):
        return {key: redact_value(val, exact_values) for key, val in value.items()}
    if isinstance(value, list):
        return [redact_value(item, exact_values) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item, exact_values) for item in value)
    return value


class StreamRedactor:
    """Redact a text stream arriving in arbitrary pieces. Output equals ``redact`` of
    the whole stream for any chunking: lines are buffered until their newline and fed
    to the same ``_LineRedactor``; a line growing past ``LINE_CAP`` is withheld whole
    (its rest is discarded up to the newline)."""

    def __init__(self, exact_values: Optional[Sequence[Tuple[str, str]]] = None) -> None:
        pairs = default_exact_values() if exact_values is None else list(exact_values)
        self.lines = _LineRedactor(pairs)
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
