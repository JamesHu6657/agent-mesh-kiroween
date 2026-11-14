#!/usr/bin/env bash
# Coordinator merge of wt/<run> into main (hard review check, James/main 2026-11-09 21:40).
# Refuses unless docs/coordination/reviews/<review>.md exists, is committed, and its first line is "VERDICT: ACCEPT"
# (Quest reviewer, Kiro, or Linear review saved verbatim; Linear: first line added by Quest from "结论: ACCEPT").
# Usage: bash scripts/coord/merge.sh <run> <review-name> "<merge message>"   then run the full suite, then sync-github.sh.
set -euo pipefail
cd "$(dirname "$0")/../.."
N="${1:?run}"; RV="${2:?review name}"; MSG="${3:?message}"
[ "$(git branch --show-current)" = main ] || { echo "REFUSED: not on main"; exit 2; }
F="docs/coordination/reviews/$RV.md"
[ -f "$F" ] || { echo "REFUSED: no review on record ($F)"; exit 3; }
git ls-files --error-unmatch "$F" >/dev/null 2>&1 || { echo "REFUSED: review file not committed"; exit 3; }
# Linear full review D3: the verdict is the committed one; local edits to the review refuse.
git diff --quiet HEAD -- "$F" || { echo "REFUSED: review file has uncommitted changes"; exit 3; }
[ "$(git show "HEAD:$F" | head -1 | tr -d '\r')" = "VERDICT: ACCEPT" ] || { echo "REFUSED: review is not ACCEPT"; exit 3; }
# Linear full review D4: never touch a merge this invocation did not start.
git rev-parse -q --verify MERGE_HEAD >/dev/null && { echo "REFUSED: a merge is already in progress on main; finish or abort it first"; exit 2; }
H=$(git rev-parse "wt/$N")
grep -q "${H:0:7}" "$F" || echo "WARN: review does not mention branch head ${H:0:7}; confirm it covers the final commit"
git merge --no-ff "wt/$N" -m "$MSG (review: $RV)" || { git merge --abort; echo "MERGE FAILED (conflict): aborted, main unchanged. Merge main into wt/$N first."; exit 1; }
echo "merged wt/$N ($H); now run the full suite on main, then sync-github.sh"
