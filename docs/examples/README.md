# Example agent configs

One file per supported agent CLI. Each is a short shell snippet you can copy; run the
lines one by one (the `--user` commands ask for your passphrase, or read it from
`--passphrase-file` / `MESH_PASSPHRASE_FILE`).

| File | Agent | How it gets work |
|---|---|---|
| [inbox-agent.sh](inbox-agent.sh) | any agent that can run shell commands (an interactive Claude Code / Cursor / Codex session, a bot, a script) | it polls its inbox with `mesh inbox wait` |
| [droid.sh](droid.sh) | Factory droid | Mesh launches `droid exec` per task |
| [kiro.sh](kiro.sh) | Kiro CLI (`kiro-cli`) | Mesh launches `kiro-cli chat --no-interactive` per task |
| [agy.sh](agy.sh) | Antigravity CLI (`agy`) | Mesh launches it per task |
| [vibe.sh](vibe.sh) | Mistral Vibe (`vibe`) | Mesh launches it per task |

Working as an inbox worker, step by step: [the inbox-worker guide](../inbox-workers.md). A runnable
wait/start/heartbeat/submit loop: [`scripts/inbox-worker-example.sh`](../../scripts/inbox-worker-example.sh).

Hermes and OpenClaw have stub adapters only: `agent create --adapter hermes|openclaw`
is refused until their command builders exist. Connect them as inbox agents instead.

The snippets are templates: set `MODEL_ID` from the model list and task/run/fence
variables from actual Mesh responses. Do not paste `<...>` placeholders into a shell.
Never run administrative `--user` examples as an inbox worker on a live home.
For a temporary demo, follow [the isolation steps](../usage.md#reading-and-trying-the-examples).

Model ids differ per account and CLI version. List what the installed CLI offers with
`mesh adapter models <adapter>` and use those ids for `--allowed-model`.

Launch evidence: by default (`adapter.evidence_mode = "warn"`) a launch agent works even
when the repository has no smoke evidence for your CLI version; `mesh adapter show
<adapter>` and `mesh doctor` show the gap. Set `"strict"` to require matching evidence:

```sh
mesh config set adapter.evidence_mode '"strict"' --user
```
