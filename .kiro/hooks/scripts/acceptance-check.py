#!/usr/bin/env python3
"""acceptance-check: are the checklist ids and test names cited in specs and gate reports real?

Usage (from anywhere; paths are resolved against the repo root):
    python3 .kiro/hooks/scripts/acceptance-check.py [PATH ...]

With no PATH it checks docs/*gate-report*.md and .kiro/specs/**/*.md. A PATH may be a
file or a directory (all *.md below it).

Checks:
  1. Checklist ids. Ids are read from the table rows of docs/acceptance-checklist.md and of
     the coordinator addendum docs/coordination/m3-acceptance-addendum.md (N18-N27).
     Cited ids are taken from `ACC:` lists (ranges such as K1-K14 are expanded) and from
     the first column of markdown table rows (`| K4 | ...`). Tokens whose letter is not a
     checklist section letter (M3, Q2, R1, T5, ...) are not ids and are ignored.
  2. Test names. Every `test_<name>` token must be a `def test_<name>` in tests/**/*.py
     or a module tests/**/test_<name>.py; `test_<prefix>*` matches by prefix. Tokens that
     occur as identifiers in mesh/**/*.py (test_command_json, test_timeout_s, ...) are
     field names, not tests, and are skipped. Every cited path tests/...*.py must exist.
     On a line that contains "(planned" (e.g. "(planned)" or "(planned; m3-r6)") a
     missing test or path is a warning only.

Read-only: it never writes files, runs tests or calls git.
Exit: 0 consistent, 1 at least one error, 2 checklist missing.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CHECKLIST = ROOT / "docs" / "acceptance-checklist.md"
ADDENDUM = ROOT / "docs" / "coordination" / "m3-acceptance-addendum.md"

NON_CHECKLIST_TABLE = re.compile(r"^\|\s*(Record|Decision|Ruling|Deviation)s?\s*\|", re.I)
ROW_ID = re.compile(r"^\|\s*([A-Z])(\d{1,2})\s*\|")
ACC_SPAN = re.compile(r"ACC:\s*([^)\]_|\n]*)")
PAREN_NOT_ACC = re.compile(r"\((?!\s*ACC:)[^()]*\)")
RANGE = re.compile(r"\b([A-Z])(\d{1,2})\s*[-\u2013]\s*([A-Z])?(\d{1,2})\b")
SINGLE = re.compile(r"\b([A-Z])(\d{1,2})\b")
TEST_TOKEN = re.compile(r"(?<![A-Za-z0-9_])(test_[A-Za-z0-9_]+)(\*?)")
TEST_PATH = re.compile(r"(?<![A-Za-z0-9_./-])(tests/[A-Za-z0-9_./-]+\.py)\b")
DEF_TEST = re.compile(r"^\s*(?:async\s+)?def\s+(test_[A-Za-z0-9_]+)\s*\(", re.M)
MESH_IDENT = re.compile(r"\b(test_[A-Za-z0-9_]+)")
PLANNED = re.compile(r"\(planned\b")
QUALIFIED = re.compile(r"(?<![A-Za-z0-9_./-])(tests/[A-Za-z0-9_./-]+\.py)::(?:([A-Za-z_][A-Za-z0-9_]*)::)?(test_[A-Za-z0-9_]+)")
INLINE_FMT = re.compile(r"\*\*|`")


def module_tests(path, cache={}):
    """{(class or None, name)} of the test functions defined in one test module."""
    if path not in cache:
        import ast
        found = set()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            tree = None
        for node in (tree.body if tree else []):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.add((None, node.name))
            elif isinstance(node, ast.ClassDef):
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        found.add((node.name, sub.name))
        cache[path] = found
    return cache[path]


def load_ids():
    ids = {}
    for source in (CHECKLIST, ADDENDUM):
        if not source.is_file():
            continue
        for line in source.read_text(encoding="utf-8").splitlines():
            m = ROW_ID.match(line)
            if m:
                ids.setdefault(m.group(1) + m.group(2), source.relative_to(ROOT).as_posix())
    return ids


def load_tests():
    names, modules = set(), set()
    for path in (ROOT / "tests").rglob("*.py"):
        if path.name.startswith("test_"):
            modules.add(path.stem)
        try:
            names.update(DEF_TEST.findall(path.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            pass
    return names, modules


def load_mesh_identifiers():
    found = set()
    for path in (ROOT / "mesh").rglob("*.py"):
        try:
            found.update(MESH_IDENT.findall(path.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            pass
    return found


def ids_in_span(span, letters):
    out = []

    def expand(m):
        a, lo, b, hi = m.group(1), int(m.group(2)), m.group(3), int(m.group(4))
        if (b is None or b == a) and lo <= hi <= lo + 40:  # explicit ACC: spans: every id is checked, known letter or not
            out.extend(f"{a}{n}" for n in range(lo, hi + 1))
            return " "
        return m.group(0)

    rest = RANGE.sub(expand, span)
    out.extend(a + n for a, n in SINGLE.findall(rest))
    return out


def cited_ids(line, letters, row_ids=True):
    # Linear full review D9: **K4** and `K4` are the same citation as K4.
    line = INLINE_FMT.sub("", line)
    found = []
    m = ROW_ID.match(line)
    if row_ids and m and m.group(1) in letters:
        found.append(m.group(1) + m.group(2))
    stripped = line
    while True:
        new = PAREN_NOT_ACC.sub(" ", stripped)
        if new == stripped:
            break
        stripped = new
    for span in ACC_SPAN.findall(stripped):
        found.extend(ids_in_span(span, letters))
    return found


def targets(argv):
    """Return (files, missing_count)."""
    if argv:
        out, missing = [], 0
        for arg in argv:
            p = Path(arg)
            p = p if p.is_absolute() else ROOT / p
            if p.is_dir():
                out.extend(sorted(p.rglob("*.md")))
            elif p.is_file():
                out.append(p)
            else:
                print(f"ERR  {arg}: no such file or directory")
                missing += 1
        return out, missing
    files = sorted((ROOT / "docs").glob("*gate-report*.md")) + sorted((ROOT / ".kiro" / "specs").rglob("*.md"))
    return files, 0


def rel(path):
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def main(argv):
    if not CHECKLIST.is_file():
        print(f"acceptance-check: REFUSED: {CHECKLIST.relative_to(ROOT)} not found")
        return 2
    ids = load_ids()
    letters = {i[0] for i in ids}
    test_names, test_modules = load_tests()
    mesh_idents = load_mesh_identifiers()
    files, missing = targets(argv)
    n_from_addendum = sum(1 for s in ids.values() if s.endswith("m3-acceptance-addendum.md"))
    print(f"acceptance-check: {len(ids)} known ids ({len(ids) - n_from_addendum} checklist, "
          f"{n_from_addendum} addendum), {len(test_names)} test functions, {len(files)} file(s)")

    errors, warnings = missing, 0
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        n_ids = n_tests = 0
        file_errors = []
        file_warnings = []
        row_ids = True  # first-column ids count as checklist ids, except in record tables
        for lineno, line in enumerate(text.splitlines(), 1):
            if not line.startswith("|"):
                row_ids = True  # the record-table skip ends with its table
            elif NON_CHECKLIST_TABLE.match(line):
                row_ids = False  # e.g. "| Record | Ruling ...": D01.. are decision records, not checklist D ids
            planned = PLANNED.search(line) is not None
            for cid in cited_ids(line, letters, row_ids):
                n_ids += 1
                if cid not in ids:
                    file_errors.append(f"{lineno}: unknown checklist id {cid}")
            for name, star in TEST_TOKEN.findall(line):
                if name in mesh_idents or name.endswith("_"):
                    if not star:
                        continue
                n_tests += 1
                if star:
                    ok = any(t.startswith(name) for t in test_names | test_modules)
                else:
                    ok = name in test_names or name in test_modules
                if not ok:
                    msg = f"{lineno}: test {name}{star} not found under tests/"
                    (file_warnings if planned else file_errors).append(msg + (" (planned)" if planned else ""))
            # Linear full review D10: path::[Class::]test_name must name a test of that module.
            for qpath, qcls, qname in QUALIFIED.findall(line):
                mod = ROOT / qpath
                if not mod.is_file():
                    continue  # reported as a missing path below
                defs = module_tests(mod)
                ok = (qcls, qname) in defs if qcls else any(n == qname for _, n in defs)
                if not ok:
                    where = f"{qpath}::{qcls + '::' if qcls else ''}{qname}"
                    msg = f"{lineno}: test {where} is not defined in that module"
                    (file_warnings if planned else file_errors).append(msg + (" (planned)" if planned else ""))
            for tpath in TEST_PATH.findall(line):
                if "*" in tpath or "<" in tpath:
                    continue
                if not (ROOT / tpath).is_file():
                    msg = f"{lineno}: path {tpath} does not exist"
                    (file_warnings if planned else file_errors).append(msg + (" (planned)" if planned else ""))
        status = "ERR " if file_errors else "OK  "
        print(f"{status} {rel(path)}: {n_ids} id citation(s), {n_tests} test citation(s)")
        for e in file_errors:
            print(f"     ERR  {e}")
        for w in file_warnings:
            print(f"     WARN {w}")
        errors += len(file_errors)
        warnings += len(file_warnings)

    print(f"acceptance-check: {errors} error(s), {warnings} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
