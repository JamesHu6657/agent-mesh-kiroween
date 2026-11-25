# Real CLI fixtures (captured by the coordinator, read-only)
Captured: 2026-11-07T06:47:40Z (UTC) on the build box. Sources: `--help`/model-list commands only (no files modified), plus sanitized metadata from two minimal real runs:
- droid/1.3.0/exec-init-event-opus55-high.json: first stream-json event of `droid exec --model claude-opus-5-5 --reasoning-effort high` (account login, no API key).
- kiro/3.1.0/effort-high-run-metadata.jsonl: ACP metadata of `kiro-cli chat --no-interactive --model claude-opus-5.5 --effort high`; an invalid model id exits 1 with "failed to set effort ... model does not support additional fields".
Versions: droid 1.3.0, agy 1.5.2, kiro-cli 3.1.0, vibe 3.1.0. Hermes/OpenClaw: not installed (see not-installed.txt).
NOTE: "reasoning effort high" (model thinking level chosen by the user) is a different concept from a Mesh "permission profile high" (tool/autonomy permissions granted to a CLI). Do not conflate them.
