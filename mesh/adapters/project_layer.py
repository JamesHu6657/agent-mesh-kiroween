"""Project-level vendor config layers that run code (full review r7, R4-P1-1).

Several vendor CLIs load configuration from the workspace they run in, and some of it
starts processes that no permission profile governs: MCP servers (a ``command`` the CLI
spawns at session start) and hooks (shell commands the CLI runs on its own events). In a
Mesh task worktree that content is task-controlled: an earlier run of the same task can
write it (an ``edit`` run that may only change files), or a merged task can carry it into
the integration branch every later task forks from. Below ``unrestricted`` such a layer is
refused, on every build, like ``codex._refuse_project_layer`` does for Codex.

Sources (checked 2026-11-18):

* Kiro CLI: MCP servers from ``.kiro/settings/mcp.json`` (workspace) merged over
  ``~/.kiro/settings/mcp.json``; agent configs (``mcpServers``, hooks) from
  ``.kiro/agents/`` (https://kiro.dev/docs/mcp/configuration/,
  https://kiro.dev/docs/configuration/).
* Factory droid: hooks from ``hooks.json`` / ``hooks/hooks.json`` / ``settings.json`` /
  ``settings.local.json`` in every ``.factory/`` from the cwd up to the git root; MCP servers
  from ``.factory/mcp.json`` in the project root and in any ancestor directory
  (https://docs.factory.com/harness/hooks, https://docs.factory.com/harness/mcp).

Instruction-only content (Kiro steering/specs, droid skills/droids/commands) is not refused:
it is text the model reads, not a process the CLI starts.
"""

import os
from typing import Iterable, List, Optional

from mesh.adapters.base import AdapterUnsupported, RunRequest


def _root_chain(workspace: str, root_markers: Iterable[str] = (".git",)) -> List[str]:
    """``workspace`` and its parents up to the nearest directory holding ``.git`` (a task
    worktree holds a ``.git`` file, so its chain is the worktree alone). Without a ``.git``
    above, only the workspace itself (the same rule as ``codex.project_layer_paths``)."""
    ws = os.path.realpath(workspace)
    chain = [ws]
    cur = ws
    markers = tuple(root_markers)
    while not any(os.path.lexists(os.path.join(cur, m)) for m in markers):
        parent = os.path.dirname(cur)
        if parent == cur:
            return [ws]
        cur = parent
        chain.append(cur)
    return chain


def _ancestors_above(path: str) -> List[str]:
    out = []
    cur = path
    while True:
        parent = os.path.dirname(cur)
        if parent == cur:
            return out
        out.append(parent)
        cur = parent


def project_layer_paths(workspace: str, config_dir: str, names: Iterable[str],
                        user_dir: str, ancestor_names: Iterable[str] = (),
                        root_markers: Iterable[str] = (".git",)) -> List[str]:
    """Existing ``<dir>/<config_dir>/<name>`` entries a vendor CLI would load for
    ``workspace``: ``names`` in every directory of the root chain, ``ancestor_names`` also in
    every directory above it. The user's own config directory (``user_dir``) is user config,
    not a project layer, and is skipped."""
    names = tuple(names)
    ancestor_names = tuple(ancestor_names)
    user_real = os.path.realpath(os.path.expanduser(user_dir))
    chain = _root_chain(workspace, root_markers)
    found: List[str] = []

    def scan(directory: str, wanted: Iterable[str]) -> None:
        base = os.path.join(directory, config_dir)
        if not os.path.lexists(base) or os.path.realpath(base) == user_real:
            return
        for name in wanted:
            p = os.path.join(base, name)
            if os.path.lexists(p):
                found.append(p)

    for d in chain:
        scan(d, names)
    if ancestor_names:
        for d in _ancestors_above(chain[-1]):
            scan(d, ancestor_names)
    return found


def refuse_project_layer(adapter_id: str, req: RunRequest, config_dir: str, names: Iterable[str],
                         user_dir: str, ancestor_names: Iterable[str] = ()) -> None:
    """Fail closed below unrestricted (see the module docstring)."""
    if req.profile == "unrestricted":
        return
    paths = project_layer_paths(req.workspace, config_dir, names, user_dir, ancestor_names)
    if not paths:
        return
    ws = os.path.realpath(req.workspace)
    shown = [os.path.relpath(p, ws) for p in paths]
    reason = f"{adapter_id}_project_layer_present"
    raise AdapterUnsupported(
        f"{reason}: {adapter_id} refuses profile '{req.profile}': the workspace has a project config "
        f"layer ({', '.join(shown)}) whose MCP servers/hooks would run outside the permission profile",
        reason=reason,
    )
