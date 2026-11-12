# Release requirements (recorded 2026-11-07, UTC+8)

Authorization (from the user, relayed by the coordinating agent): only AFTER the whole project passes final acceptance,
create a NEW public GitHub repository and push this project. Never reuse or push to any older AgentMesh repository.
Until then: no remote, no push, no publishing. M1a is only the first sub-milestone and does not trigger this.

Roles: code review is done by the user's Quest workflow; Grok Bot (parent) performs acceptance against the agreed
gate criteria and coordinates the final release.

Before the public push (checklist for the release coordinator):
1. Exclude secrets: scan the full git history (not just HEAD) for API keys (e.g. `fk-...`), OAuth tokens, Mesh tokens
   (`mesh_at_`/`mesh_rt_`), passphrases, cookies, private keys.
2. Exclude private logs and account info: `logs/raw/` is git-ignored and must stay unpublished; review committed
   `logs/*.jsonl` / `logs/*.txt` summaries for emails, account ids, session ids, local usernames/paths, and decide
   whether to drop them from the published history (publish from a sanitized history if needed).
3. Review `prompts/` and `docs/reviews/` for personal/account details before publishing.
4. Do not delete local original materials on the box as part of release preparation; sanitize a separate copy/history.
5. Add a LICENSE and README only with the user's choice of license.
