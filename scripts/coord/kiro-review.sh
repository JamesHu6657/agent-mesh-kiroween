#!/usr/bin/env bash
# Kiro review of agy work. Usage: bash scripts/coord/kiro-review.sh <name> <base-ref> [focus...]
#  Reviews git diff <base-ref>..HEAD plus any uncommitted changes against requirements/plan/checklist.
#  Output: docs/coordination/reviews/<name>.md, first line "VERDICT: ACCEPT" or "VERDICT: REVISE".
source "$(dirname "$(readlink -f "$0")")/kiro-run-common.sh"
N="${1:?name}"; BASE="${2:?base ref}"; shift 2; FOCUS="${*:-the current plan step}"
[[ "$N" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "bad name"; exit 2; }
git rev-parse --verify -q "$BASE^{commit}" >/dev/null || { echo "bad base ref"; exit 2; }
OUT=docs/coordination/reviews/$N.md
P="You are the INDEPENDENT REVIEWER of agy's implementation work (Agent Mesh v1). Do not modify any file except writing your report to $OUT. Do not write code, do not commit, do not dispatch anything.
Scope: the changes in \`git diff $BASE..HEAD\` (see also \`git log $BASE..HEAD\`) plus any uncommitted changes (\`git status\`, \`git diff\`). Focus: $FOCUS.
Read docs/HANDOFF.md, the relevant parts of docs/requirements.md, docs/plan.md (milestones §16) and docs/acceptance-checklist.md (§0.1), the latest docs/impl-notes-*.md and the latest prompts/agy-*.md for this step. Read every changed file fully.
Verify: run \`bash scripts/test.sh\` (must be rc 0, tripwire empty) and any focused \`python3 -m unittest ...\`; check ACC tags (\`grep -rn 'ACC: <ID>' tests\`) for every gate item this step claims; where useful run real \`bin/mesh --home /tmp/mh-<name> ...\` commands in throwaway homes and remove only the /tmp/mh-* dirs you created and stop daemons you started.
Judge: correctness, spec/plan conformance, every claimed checklist item really covered (PASS/FAIL/NOT-COVERED with test name), security (authz/approval cannot be forged, no secrets in logs, loopback/Unix socket only), process supervision/recovery correctness, test quality (no tests weakened, no fixtures edited, nothing in the never-edit list touched), and regressions.
Write $OUT: first line exactly \`VERDICT: ACCEPT\` or \`VERDICT: REVISE\`; then a test-run summary (command, rc, counts); then a per-item table; then numbered findings, each with severity (BLOCKER/MAJOR/MINOR), evidence (file:line or command output) and a concrete required change; end with a section 'Fix prompt notes' listing exactly what agy must change if REVISE. ACCEPT only with no BLOCKER or MAJOR findings."
run_kiro "$OUT" "$P" "review-$N"
