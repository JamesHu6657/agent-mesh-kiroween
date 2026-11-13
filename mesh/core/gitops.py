"""Git operations allow-list and wrapper per plan §7.5, §10.5, §11.3."""

import subprocess
from typing import List, Optional

from mesh.constants import build_external_env

# Repository-selecting variables (git(1) ENVIRONMENT). An inherited value (a git hook or a
# wrapper that exported GIT_DIR before starting meshd) would make every Mesh git call act on
# that repository instead of the explicit `cwd`, so run_git drops them. Identity variables
# (GIT_AUTHOR_*/GIT_COMMITTER_*) and config variables are left as the user set them: Mesh
# never sets git identity or config (§7.5), and K8 depends on that.
REPO_LOCATION_ENV = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_PREFIX",
)
# GIT_CEILING_DIRECTORIES / GIT_DISCOVERY_ACROSS_FILESYSTEM only narrow discovery and are kept.

FORBIDDEN_SUBCOMMANDS = {
    "push", "rebase", "reset", "checkout", "switch", "fetch", "pull",
    "clone", "config", "stash", "clean", "rm", "mv",
}

# The explicit allow-list (plan §10.5). Anything not listed here is rejected.
ALLOWED_SUBCOMMANDS = {
    "rev-parse", "status", "diff", "log", "merge-base", "show-ref", "cat-file",
    "branch", "worktree", "merge", "commit", "update-ref",
}

FORBIDDEN_TOKENS = {
    "-X", "--strategy-option", "--force", "-f",
}

# Flags that make git write to a caller-chosen file (diff/log --output).
FORBIDDEN_OUTPUT_FLAGS = {"--output", "-o"}


def _is_output_flag(arg: str) -> bool:
    """True for --output=<file>, --output <file>, -o<file> and -o <file>."""
    if arg in FORBIDDEN_OUTPUT_FLAGS:
        return True
    return arg.startswith("--output=") or (arg.startswith("-o") and not arg.startswith("--"))


def _check_ref(ref: str, subcmd: str) -> None:
    """A ref must be a plain revision name: no leading '-' and no whitespace."""
    if not ref or ref.startswith("-") or any(ch.isspace() for ch in ref):
        raise AssertionError(f"Disallowed ref '{ref}' for git {subcmd}")


def validate_git_args(args: List[str]) -> None:
    """Validate git arguments against the strict allow-list. Raise AssertionError on any disallowed form."""
    if not args:
        raise AssertionError("git arguments list cannot be empty")

    for token in args:
        if token in FORBIDDEN_TOKENS or any(token.startswith(f) for f in ("-X", "--strategy-option=")):
            raise AssertionError(f"Forbidden git token: {token}")

    subcmd = args[0]
    rest = args[1:]

    if subcmd in FORBIDDEN_SUBCOMMANDS:
        raise AssertionError(f"Forbidden git subcommand: {subcmd}")

    if subcmd not in ALLOWED_SUBCOMMANDS:
        raise AssertionError(f"Disallowed git subcommand: {subcmd}")

    if subcmd in ("rev-parse", "status", "diff", "log", "merge-base", "show-ref", "cat-file"):
        for arg in rest:
            if arg in FORBIDDEN_TOKENS or arg.startswith("-X"):
                raise AssertionError(f"Disallowed flag '{arg}' for git {subcmd}")
            if subcmd in ("diff", "log") and _is_output_flag(arg):
                raise AssertionError(f"Disallowed output flag '{arg}' for git {subcmd}")
        return

    if subcmd == "branch":
        # Create: ["branch", "branch_name"] or ["branch", "branch_name", "start_point"].
        # Delete: exactly ["branch", "-d", <ref>] — the §10.7 prune form. `-D`,
        # `--delete --force`, `-df`, `-m`/`-M` and every other flag stay rejected: Mesh
        # never deletes unmerged work (§10.7, Q7).
        if rest and rest[0] == "-d":
            if len(rest) != 2:
                raise AssertionError(f"git branch -d takes exactly one ref: {args}")
            _check_ref(rest[1], "branch")
            return
        for arg in rest:
            if arg.startswith("-"):
                raise AssertionError(f"git branch flags are disallowed (create or -d only): {arg}")
        if not rest:
            raise AssertionError("git branch requires branch name to create")
        return

    if subcmd == "worktree":
        if not rest:
            raise AssertionError("git worktree requires a sub-verb")
        wt_verb = rest[0]
        if wt_verb not in ("add", "list", "remove", "prune"):
            raise AssertionError(f"Disallowed git worktree verb: {wt_verb}")
        for arg in rest[1:]:
            if (arg in FORBIDDEN_TOKENS or arg.startswith("--force")
                    or (arg.startswith("-") and not arg.startswith("--") and "f" in arg[1:])):
                raise AssertionError(f"Disallowed flag '{arg}' for git worktree {wt_verb}")
        return

    if subcmd == "merge":
        # Exactly three allowed forms (plan §10.5):
        # 1. ["merge", "--abort"]
        # 2. ["merge", "--ff-only", <ref>]
        # 3. ["merge", "--no-ff", "--no-commit", <ref>]
        if rest == ["--abort"]:
            return
        if len(rest) == 2 and rest[0] == "--ff-only":
            _check_ref(rest[1], "merge")
            return
        if len(rest) == 3 and rest[0] == "--no-ff" and rest[1] == "--no-commit":
            _check_ref(rest[2], "merge")
            return
        raise AssertionError(f"Disallowed git merge invocation: {args}")

    if subcmd == "commit":
        for arg in rest:
            if arg in ("--amend", "--fixup"):
                raise AssertionError(f"Disallowed git commit flag: {arg}")
        return

    if subcmd == "update-ref":
        # Require compare-and-swap: no deletion, stdin batch, or unconditional update.
        if len(rest) != 3:
            raise AssertionError("git update-ref requires <ref> <new> <old>")
        for arg in rest:
            _check_ref(arg, "update-ref")
        return

    raise AssertionError(f"Disallowed git subcommand: {subcmd}")


def run_git(
    args: List[str],
    cwd: Optional[str] = None,
    timeout_s: float = 30.0,
    check: bool = False,
    capture_output: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess:
    """Run an allow-listed git command with external environment sanitization."""
    validate_git_args(args)
    cmd = ["git"] + list(args)
    env = build_external_env()
    for key in REPO_LOCATION_ENV:
        env.pop(key, None)
    from mesh.daemon.children import run_registered  # meshd child registry (§11.4)

    return run_registered(
        "git",
        cmd,
        cwd=cwd,
        env=env,
        timeout=timeout_s,
        check=check,
        capture_output=capture_output,
        text=text,
    )
