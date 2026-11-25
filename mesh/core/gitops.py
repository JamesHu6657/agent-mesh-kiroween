"""Git operations allow-list and wrapper per plan §7.5, §10.5, §11.3."""

import os
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

from mesh.constants import build_external_env

# Repository-selecting variables (git(1) ENVIRONMENT). An inherited value (a git hook or a
# wrapper that exported GIT_DIR before starting meshd) would make every Mesh git call act on
# that repository instead of the explicit `cwd`, so run_git drops them. Identity variables
# (GIT_AUTHOR_*/GIT_COMMITTER_*) and config variables are left as the user set them: Mesh
# never sets git identity or config (§7.5), and K8 depends on that.
REPO_LOCATION_ENV = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_PREFIX",
    "GIT_CONFIG_PARAMETERS",
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
        # Exactly four allowed forms (plan §10.5):
        # 1. ["merge", "--abort"]
        # 2. ["merge", "--ff-only", <ref>]
        # 3. ["merge", "--ff-only", "--no-overwrite-ignore", <ref>] (the §10.6 deliver form:
        #    an ignored, untracked file in the base's worktree is never silently replaced)
        # 4. ["merge", "--no-ff", "--no-commit", <ref>]
        if rest == ["--abort"]:
            return
        if len(rest) == 2 and rest[0] == "--ff-only":
            _check_ref(rest[1], "merge")
            return
        if len(rest) == 3 and rest[0] == "--ff-only" and rest[1] == "--no-overwrite-ignore":
            _check_ref(rest[2], "merge")
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
        # `--no-deref` (detached HEAD restore in the merge pipeline) is the only flag.
        if rest and rest[0] == "--no-deref":
            rest = rest[1:]
        if len(rest) != 3:
            raise AssertionError("git update-ref requires <ref> <new> <old>")
        for arg in rest:
            _check_ref(arg, "update-ref")
        return

    raise AssertionError(f"Disallowed git subcommand: {subcmd}")


def _norm_dir_key(path: str) -> str:
    try:
        real = os.path.realpath(path)
    except OSError:
        real = os.path.abspath(path)
    norm = os.path.normpath(real)
    return norm.lower() if sys.platform == "win32" else norm


def _git_exe_in_dir(directory: str) -> Optional[str]:
    names = ("git.exe", "git") if sys.platform == "win32" else ("git", "git.exe")
    for name in names:
        cand = os.path.join(directory, name)
        if os.path.isfile(cand):
            return cand
    return None


def bundled_mingit_cmd_dir(executable: Optional[str] = None) -> Optional[str]:
    """Return ``<app_dir>/mingit/cmd`` if a bundled MinGit exists next to the portable/installed Python.

    Pure local filesystem inspection — never downloads anything at runtime.
    """
    exe = executable if executable is not None else sys.executable
    if not exe:
        return None
    exe_dir = os.path.dirname(os.path.abspath(exe))
    candidates: List[str] = []
    if os.path.basename(exe_dir).lower() == "python":
        candidates.append(os.path.join(os.path.dirname(exe_dir), "mingit", "cmd"))
    candidates.append(os.path.join(exe_dir, "mingit", "cmd"))
    seen = set()
    for cand in candidates:
        key = _norm_dir_key(cand)
        if key in seen:
            continue
        seen.add(key)
        if os.path.isdir(cand) and _git_exe_in_dir(cand) is not None:
            return os.path.normpath(cand)
    return None


def _get_path_key_and_val(env: Dict[str, str]) -> Tuple[str, str]:
    if sys.platform == "win32":
        for k, v in env.items():
            if k.upper() == "PATH":
                return k, v
    if "PATH" in env:
        return "PATH", env["PATH"]
    return "PATH", ""


def find_system_git(
    path_env: Optional[str] = None,
    *,
    bundled_dir: Optional[str] = None,
) -> Optional[str]:
    """Locate system ``git`` on PATH while excluding any bundled MinGit directory."""
    raw_path = os.environ.get("PATH", "") if path_env is None else path_env
    if not raw_path:
        return None
    if bundled_dir:
        bkey = _norm_dir_key(bundled_dir)
        entries = [
            entry for entry in raw_path.split(os.pathsep)
            if entry and _norm_dir_key(entry) != bkey
        ]
        filtered = os.pathsep.join(entries)
    else:
        filtered = raw_path
    if not filtered:
        return None
    return shutil.which("git", path=filtered)


def resolve_git_binary(
    path_env: Optional[str] = None,
    *,
    executable: Optional[str] = None,
) -> Tuple[Optional[str], str]:
    """Resolve ``(git_path, source)`` preferring shipped checksum-pinned MinGit when present.

    Prefers ``bundled_mingit`` over arbitrary ``PATH`` entries so a poisoned shell ``PATH``
    cannot hijack Git on portable/installed builds that ship pinned MinGit; falls back to
    system ``git`` on ``PATH`` when no bundled MinGit is installed.
    """
    bundled_dir = bundled_mingit_cmd_dir(executable=executable)
    if bundled_dir:
        bundled_git = _git_exe_in_dir(bundled_dir)
        if bundled_git:
            return bundled_git, "bundled_mingit"
    sys_git = find_system_git(path_env, bundled_dir=bundled_dir)
    if sys_git:
        return sys_git, "system"
    return None, "missing"


def augment_env_with_bundled_git(
    env: Dict[str, str],
    *,
    executable: Optional[str] = None,
) -> Dict[str, str]:
    """Return *env* with bundled MinGit prepended to PATH whenever bundled MinGit is installed."""
    bundled_dir = bundled_mingit_cmd_dir(executable=executable)
    if not bundled_dir:
        return env
    path_key, cur_path = _get_path_key_and_val(env)
    bkey = _norm_dir_key(bundled_dir)
    existing = [p for p in cur_path.split(os.pathsep) if p]
    if existing and _norm_dir_key(existing[0]) == bkey:
        return env
    rest = [p for p in existing if _norm_dir_key(p) != bkey]
    out = dict(env)
    out[path_key] = os.pathsep.join([bundled_dir] + rest) if rest else bundled_dir
    return out


def _prepare_git_cmd_and_env(args: List[str]) -> Tuple[List[str], Dict[str, str]]:
    """Validate git arguments and build the sanitized command + environment."""
    validate_git_args(args)
    env = build_external_env()
    for key in REPO_LOCATION_ENV:
        env.pop(key, None)
    env = augment_env_with_bundled_git(env)
    _pk, path_val = _get_path_key_and_val(env)
    git_bin, _git_source = resolve_git_binary(path_val)
    cmd = [git_bin or "git"] + list(args)
    # Full review r5 P1-3 / r6 P1-4: append `core.fsmonitor=false` (and on Windows,
    # `core.longpaths=true` so users never need `git config --global`) after any valid
    # caller-supplied GIT_CONFIG_COUNT entries so K8 env identity/config is preserved
    # while later-index env config still overrides any earlier `core.fsmonitor`.
    raw_cnt = env.get("GIT_CONFIG_COUNT")
    try:
        idx = int(raw_cnt) if raw_cnt not in (None, "") else 0
        if idx < 0 or idx > 1024:
            raise ValueError("out of range")
        for i in range(idx):
            key_i = env.get(f"GIT_CONFIG_KEY_{i}")
            if not key_i or "." not in key_i or f"GIT_CONFIG_VALUE_{i}" not in env:
                raise ValueError("invalid config entry")
    except ValueError:
        for k in [k for k in env if k.startswith("GIT_CONFIG_")]:
            env.pop(k, None)
        idx = 0
    inject_pairs = [("core.fsmonitor", "false")]
    if sys.platform == "win32":
        inject_pairs.append(("core.longpaths", "true"))
    env["GIT_CONFIG_COUNT"] = str(idx + len(inject_pairs))
    for offset, (cfg_key, cfg_val) in enumerate(inject_pairs):
        env[f"GIT_CONFIG_KEY_{idx + offset}"] = cfg_key
        env[f"GIT_CONFIG_VALUE_{idx + offset}"] = cfg_val
    return cmd, env


def run_git(
    args: List[str],
    cwd: Optional[str] = None,
    timeout_s: float = 30.0,
    check: bool = False,
    capture_output: bool = True,
    text: bool = True,
    input: Optional[str] = None,
) -> subprocess.CompletedProcess:
    """Run an allow-listed git command with external environment sanitization."""
    cmd, env = _prepare_git_cmd_and_env(args)
    from mesh.daemon.children import run_registered  # meshd child registry (§11.4)

    extra: Dict[str, Any] = {}
    if input is not None:
        extra["input"] = input
        extra["stdin"] = subprocess.PIPE
    return run_registered(
        "git",
        cmd,
        cwd=cwd,
        env=env,
        timeout=timeout_s,
        check=check,
        capture_output=capture_output,
        text=text,
        # PR #39 review: its own process group, so a timeout also kills hooks and helpers
        # git started (run_registered SIGKILLs the whole group).
        start_new_session=True,
        **extra,
    )


def run_git_bounded(
    args: List[str],
    cwd: Optional[str] = None,
    *,
    max_output_bytes: int,
    timeout_s: float = 30.0,
) -> Tuple[int, str, bool]:
    """Run an allow-listed git command while strictly bounding stdout RAM usage.

    Returns ``(returncode, stdout_text, output_capped)``. When stdout exceeds
    ``max_output_bytes``, the git process is terminated immediately and
    ``output_capped`` is ``True``.
    """
    cmd, env = _prepare_git_cmd_and_env(args)
    from mesh.daemon.children import run_registered_bounded

    rc, out_bytes, _err_bytes, capped = run_registered_bounded(
        "git",
        cmd,
        cwd=cwd,
        env=env,
        timeout=timeout_s,
        max_stdout_bytes=max_output_bytes,
        start_new_session=True,
    )
    return rc, out_bytes.decode("utf-8", errors="replace"), capped
