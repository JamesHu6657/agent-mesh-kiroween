#!/usr/bin/env bash
# Kiro planning: drafts the next agy dispatch prompt. Usage: bash scripts/coord/kiro-plan.sh <name> <goal...>
#  Output: docs/coordination/plans/<name>.md (a ready-to-use agy prompt draft). The coordinator (Quest) vets it and
#  copies it to prompts/agy-<...>.md before dispatch; Kiro never writes prompts/ itself.
source "$(dirname "$(readlink -f "$0")")/kiro-run-common.sh"
N="${1:?name}"; shift; GOAL="${*:?goal}"
[[ "$N" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "bad name"; exit 2; }
OUT=docs/coordination/plans/$N.md
P="You are the PLANNER for Agent Mesh v1. Do not modify any file except writing your plan to $OUT. Do not write code, do not commit, do not dispatch anything.
Goal: $GOAL
Read docs/HANDOFF.md, docs/requirements.md, docs/plan.md (esp. milestones §16 and the step(s) in scope), docs/acceptance-checklist.md (§0.1 gate table), docs/impl-notes-*.md, the tail of docs/coordination/log.md, any docs/coordination/reviews/*.md relevant to this step, prompts/agy-m1b-common.md and the most recent prompts/agy-*.md as style examples. Check the current code (\`git log\`, read the modules involved) so the plan matches reality.
Write $OUT as a complete agy implementer prompt in the same style as the existing prompts/agy-*.md: role and repo, what is already accepted, exact scope (one plan step, split into small sub-steps, commit per sub-step with author \`agy gemini-3.8-flash-high <agy@localhost>\`), the checklist items to cover with ACC tags, files likely touched, required tests and how to run them (\`bash scripts/test.sh\` rc 0), the never-edit list, permission/limit rules (no network in tests, /tmp/mh-* homes only, no trust-all, no push), and a clear done-definition including updating docs/impl-notes-*.md. Before the prompt, add a short header section 'Planner notes' (risks, open questions for the coordinator, anything that needs James). Keep it focused; do not expand scope beyond plan.md."
run_kiro "$OUT" "$P" "plan-$N"
