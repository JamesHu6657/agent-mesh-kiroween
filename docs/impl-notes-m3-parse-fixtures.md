# m3-parse-fixtures: real agy / vibe output for parse() work (coordinator, 2026-11-10)

Verbatim copies of earlier coordinator dispatch logs (gitignored `logs/raw/`), promoted with
`python3 -m tests.tierlib promote`. `captured_at_utc` = log mtime. Prompts are not recorded,
so `command` uses placeholders (`<prompt>`, `<repo>`, ...); commands are reconstructed from
`scripts/coord/dispatch-agy.sh` / `dispatch-vibe.sh` at the time.

| Fixture | Source | What it shows |
|---|---|---|
| agy/1.5.3/stream-step1-denied.jsonl (+ empty -stderr.txt) | agy-m1a-step1-denied.jsonl / .err | init, step_update, result SUCCESS with a denied tool step |
| agy/1.5.3/stream-fixC-denied.jsonl | agy-m1a-fixC-denied.jsonl | second denied-tool run |
| agy/1.5.3/stream-step6-network-error.jsonl (+ -stderr.txt) | agy-m1b-step6.jsonl / .err | long run ending in a network error; stderr has `AGY_ERROR: {json}` |
| vibe/3.1.0/streaming-terminated.jsonl (+ -stderr.txt) | vibe-m2-r1b.jsonl / .err | `--output streaming` message/effect lines; run stopped by the coordinator (stderr `Stopped: terminated`) |
| vibe/3.1.0/export-terminated.json | vibe-m2-r1b-export/export.json | `--output-dir` export: outcome `terminated`, session_id, usage, cost_usd, exit_code 3 |

agy 1.5.3: these runs were on the Paseo cloud PC before the 2026-11-07 move, where agy 1.5.3
was installed (log.md line 10: binaries copied, sha256 identical). Contents checked: no
credentials; paths `/home/box`, `/home/node` and `example.invalid` test addresses only.
