# Kiro agent profiles (historical build profiles)

These are the Kiro CLI agent profiles used while Agent Mesh was being built. Their
prompts and path allow/deny lists assume the build machine layout: the repository at
`/workspace/mesh-fresh-v1` and coder worktrees at `/workspace/mesh-wt-<run>`. To reuse
them elsewhere, replace those prefixes with your checkout path. They reference a
coordination log and handoff notes (`docs/HANDOFF.md`, `docs/coordination/`) that are
private and not included in this public snapshot.

The profiles are soft guards, not a sandbox (see `.kiro/steering/workflow.md`).
