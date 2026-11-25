"""Help-text flag diff for real adapters (plan §14.10, §14.11; m3-r1, ACC: N10, N22).

Pure functions: no subprocess, no vendor binary, no network. ``newest_fixture_dir`` and
``fixture_root`` read the committed fixture tree; they are for the probe tier and tests,
never for meshd at runtime (base.py: runtime code must not read tests/fixtures).

The flag token rule is the probe's (``base.is_flag_present``, used by ``flag_diff`` in
droid.py and kiro.py): a flag is ``-x`` or ``--long-name`` preceded by start of line,
whitespace, ``,``, ``[`` or ``(`` and followed by whitespace, ``=``, ``,``, ``]``, ``)``,
``:`` or end of line. Every token ``flag_tokens`` returns satisfies ``is_flag_present``.
"""

import os
import re
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from mesh.adapters.base import (
    EffortNotAllowed,
    EffortUnsupported,
    ProfileUnsupported,
    PromptTooLong,
    RunRequest,
    is_flag_present,
)

FLAG_TOKEN_RE = re.compile(r"(?:^|(?<=[\s,\[(]))(--?[A-Za-z0-9][A-Za-z0-9_-]*)(?=[\s=,\]):]|$)", re.MULTILINE)

PROFILES: Tuple[str, ...] = ("readonly", "edit", "dev", "elevated", "unrestricted")
RESUME: Tuple[Optional[str], ...] = (None, "sess_helpdiff")
EFFORTS: Tuple[Optional[str], ...] = ("high", None)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def fixture_root() -> Path:
    """`<repo>/tests/fixtures/real` of this checkout."""
    return _REPO_ROOT / "tests" / "fixtures" / "real"


def flag_tokens(text: str) -> Set[str]:
    """Every ``-x`` / ``--long`` flag token in help text (the probe's boundary rule)."""
    if not text:
        return set()
    return {m.group(1) for m in FLAG_TOKEN_RE.finditer(text) if is_flag_present(m.group(1), text)}


def diff(old_text: str, new_text: str) -> Dict[str, List[str]]:
    """Flags added and removed between two help texts, each list sorted."""
    old, new = flag_tokens(old_text), flag_tokens(new_text)
    return {"added": sorted(new - old), "removed": sorted(old - new)}


def _argv_flags(argv: List[str]) -> Set[str]:
    out: Set[str] = set()
    for tok in argv[1:]:
        if tok.startswith("-") and len(tok) > 1 and not tok[1:].replace(".", "").isdigit():
            out.add(tok.split("=", 1)[0])
    return out


MODELS: Tuple[Optional[str], ...] = (None, "mesh-helpdiff-model")
OPTION_VARIANTS: Tuple[Dict[str, object], ...] = (
    {},
    {"trust_workdir": True},
    {"sandbox": True},
    {"agent": "mesh-helpdiff"},
    {"max_turns": 1, "max_price_usd": 1, "max_tokens": 1},
)


def emitted_flags(adapter) -> Set[str]:
    """Every flag ``adapter.build()`` can emit over the matrix model (None, a named model) x
    options (base, ``trust_workdir``, ``sandbox``, ``agent``, ``max_turns``/``max_price_usd``/
    ``max_tokens``; each merged with ``run_id``) x prompt (short, ``max_argv_prompt_bytes + 1``
    bytes) x profile (5) x resume (2) x effort (``high``, none). Combinations the adapter
    refuses (``ProfileUnsupported``, ``EffortUnsupported``, ``EffortNotAllowed``,
    ``PromptTooLong``) are skipped. Prompts are files in a private temp dir, removed afterwards."""
    flags: Set[str] = set()
    with tempfile.TemporaryDirectory(prefix="mh-helpdiff-") as tmp:
        prompts = [os.path.join(tmp, "prompt.md")]
        with open(prompts[0], "w", encoding="utf-8") as f:
            f.write("helpdiff prompt\n")
        limit = getattr(adapter, "max_argv_prompt_bytes", None)
        if isinstance(limit, int) and limit > 0:
            prompts.append(os.path.join(tmp, "prompt-long.md"))
            with open(prompts[1], "w", encoding="utf-8") as f:
                f.write("x" * (limit + 1))
        for prompt in prompts:
            for model in MODELS:
                for extra in OPTION_VARIANTS:
                    for profile in PROFILES:
                        for resume in RESUME:
                            for effort in EFFORTS:
                                options = dict(extra)
                                options["run_id"] = "run_helpdiff"
                                req = RunRequest(prompt_path=prompt, workspace=tmp, model=model, effort=effort,
                                                 profile=profile, resume_session_id=resume, options=options)
                                try:
                                    spec = adapter.build(req)
                                except (ProfileUnsupported, EffortUnsupported, EffortNotAllowed, PromptTooLong):
                                    continue
                                flags |= _argv_flags(list(spec.argv))
    return flags


def _version_key(name: str) -> Optional[Tuple[int, ...]]:
    m = re.fullmatch(r"(\d+(?:\.\d+)*)", name)
    if not m:
        return None
    return tuple(int(p) for p in m.group(1).split("."))


def newest_fixture_dir(adapter_id: str, root: Optional[Path] = None) -> Optional[Path]:
    """The highest numeric version dir under ``<root>/<adapter_id>/`` (None if there is none).
    Versions compare numerically (1.10.0 > 1.9.2); non-version names are ignored."""
    base = Path(root) if root is not None else fixture_root()
    d = base / adapter_id
    if not d.is_dir():
        return None
    best: Optional[Tuple[Tuple[int, ...], Path]] = None
    for child in d.iterdir():
        if not child.is_dir():
            continue
        key = _version_key(child.name)
        if key is None:
            continue
        if best is None or key > best[0]:
            best = (key, child)
    return best[1] if best else None
