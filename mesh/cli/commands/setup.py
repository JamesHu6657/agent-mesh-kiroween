"""`mesh setup`: one command for the first run (productize).

Runs, in order and skipping what is already done: `mesh init`, `mesh daemon start`, an
inbox lead agent (created, given a token saved to a 0600 file outside every workspace,
and made `lead.default_agent`), then the `mesh doctor` environment checks. The
passphrase is read once (fd, file, env or a prompt, as for every `--user` command) and
handed to each step over a private pipe; it never appears in argv.
"""

import argparse
import hashlib
import json
import re
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

import mesh
import mesh.constants as c
from mesh.errors import MeshError


def register_setup_command(subparsers: argparse._SubParsersAction, parent_parser: argparse.ArgumentParser) -> None:
    p = subparsers.add_parser(
        "setup",
        parents=[parent_parser],
        help="First-run setup: init, start the daemon, create an inbox lead, check the environment",
        description=("First-run setup: init the home, start the daemon, create an inbox lead agent with a "
                     "token file, set it as the default lead, and run the environment checks. "
                     "Safe to re-run: finished steps are skipped."),
        epilog=("Examples:\n  mesh setup\n  mesh setup --passphrase-file ~/.config/agent-mesh/passphrase\n"
                "  MESH_PASSPHRASE_FILE=~/.config/agent-mesh/passphrase mesh setup --lead-name lead1\n"
                "  mesh setup --no-lead\n"
                "  mesh setup --approval-mode delegated\n"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--lead-name", default="lead", help="Name of the inbox lead agent to create (default: lead)")
    p.add_argument("--no-lead", action="store_true", default=False, help="Do not create a lead agent")
    p.add_argument("--token-dir", default=None,
                   help="Where to save the lead's token file (default: ~/.config/agent-mesh, mode 0700)")
    p.add_argument("--no-start", action="store_true", default=False, help="Do not start the daemon")
    p.add_argument("--approval-mode", choices=["passphrase", "delegated"], default=None,
                   help=("Who decides approvals: 'passphrase' (you, with the passphrase; the default) or "
                         "'delegated' (the lead asks you and decides with your quoted reply). "
                         "Left unchanged when omitted."))
    p.set_defaults(func=handle_setup)


def _mesh_cmd() -> List[str]:
    return [sys.executable, "-P", "-s", "-m", "mesh"]


def _mesh_env() -> Dict[str, str]:
    env = dict(os.environ)
    # Keep the caller's PYTHONPATH for launched agents, as bin/mesh does (Linear PR30 P2-6):
    # meshd restores it from this marker before starting any vendor process.
    if "_MESH_ORIG_PYTHONPATH" not in env:
        env["_MESH_ORIG_PYTHONPATH"] = os.environ.get("PYTHONPATH") or "__UNSET__"
    env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.abspath(mesh.__file__)))
    # The passphrase goes over a pipe; do not leave it in a child's environment.
    env.pop("MESH_PASSPHRASE", None)
    env.pop("MESH_PASSPHRASE_FILE", None)
    return env


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        view = view[n:]


def _run(home: str, argv: List[str], passphrase: Optional[str] = None) -> Tuple[int, Dict[str, Any]]:
    cmd = _mesh_cmd() + ["--home", home, "--format", "json"] + argv
    pass_fds: Tuple[int, ...] = ()
    r = w = None
    if passphrase is not None:
        r, w = os.pipe()
        data = passphrase.encode("utf-8")

        def _feed() -> None:
            # A writer thread, so a passphrase longer than the pipe buffer cannot
            # block setup before the child starts reading (Linear round 3 P3).
            try:
                _write_all(w, data)
            except OSError:
                pass
            finally:
                os.close(w)

        import threading
        feeder = threading.Thread(target=_feed, daemon=True)
        feeder.start()
        cmd += ["--passphrase-fd", str(r)]
        pass_fds = (r,)
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              env=_mesh_env(), pass_fds=pass_fds, timeout=300)
    finally:
        if r is not None:
            os.close(r)
            feeder.join(timeout=5)
    try:
        body = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except ValueError:
        body = {"raw": proc.stdout.strip()}
    if proc.returncode != 0 and not body.get("error"):
        body["error"] = {"message": (proc.stderr or proc.stdout).strip()[-500:]}
    return proc.returncode, body


_LEAD_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
DEFAULT_TOKEN_DIR = "~/.config/agent-mesh"


def _usage(msg: str, _prefix: str = "mesh setup", **details: Any) -> MeshError:
    return MeshError(code=c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message=f"{_prefix}: {msg}",
                     details=details or None)


def token_path_for(home: str, lead: str, token_dir: Optional[str]) -> str:
    """Token file for ``lead`` of ``home``. The default home keeps ``<lead>.token``; any
    other home gets a home-specific suffix so two installs never share (and overwrite)
    one credential file (Linear PR30 P2-1)."""
    tdir = os.path.abspath(os.path.expanduser(token_dir or DEFAULT_TOKEN_DIR))
    default_home = os.path.realpath(os.path.expanduser("~/.agent-mesh"))
    if os.path.realpath(home) == default_home:
        name = f"{lead}.token"
    else:
        name = f"{lead}-{hashlib.sha256(os.path.realpath(home).encode()).hexdigest()[:8]}.token"
    return os.path.join(tdir, name)


def _configured_roots(home: str, prefix: str = "mesh setup") -> List[str]:
    """The configured workspaces.root and every registered project repository (realpaths).
    Fails closed when an existing database cannot be inspected (Linear round 4 P2)."""
    from mesh.core.token_roots import configured_roots

    try:
        ws, repos = configured_roots(home)
    except MeshError as exc:
        raise _usage(f"cannot check token destination: {exc.message}", _prefix=prefix)
    return [os.path.realpath(p) for p in ([ws] if ws else []) + repos]


def _enclosing_git_worktree(directory: str) -> Optional[str]:
    """The nearest ancestor of ``directory`` (inclusive) holding a ``.git`` entry, or None."""
    cur = os.path.realpath(directory)
    while True:
        if os.path.lexists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def _check_token_destination(home: str, path: str, prefix: str = "mesh setup") -> None:
    """Refuse destinations the client would later reject, or that are not plain files we
    own (Linear PR30 P2-1, P2-3). Called before a token is issued."""
    from mesh.core.paths import is_path_equal_or_parent
    tdir = os.path.dirname(path)
    if os.path.lexists(tdir) and not os.path.isdir(tdir):
        raise _usage(_prefix=prefix, msg=f"token directory {tdir} exists and is not a directory", token_dir=tdir)
    real = os.path.realpath(path)
    for name in ("runs", "workspaces"):
        bad = os.path.realpath(os.path.join(home, name))
        if is_path_equal_or_parent(bad, real):
            raise _usage(_prefix=prefix, msg=f"token file {path} would be inside {bad}; Mesh refuses token files there. "
                         "Use a directory outside the Mesh home's runs/ and workspaces/ (default ~/.config/agent-mesh)",
                         token_file=path, conflict=bad)
    for bad in _configured_roots(home, prefix):
        if is_path_equal_or_parent(bad, real):
            raise _usage(_prefix=prefix, msg=f"token file {path} would be inside {bad} (a workspace root or project repository "
                         "agents can read); choose a token directory outside it (default ~/.config/agent-mesh)",
                         token_file=path, conflict=bad)
    git_top = _enclosing_git_worktree(tdir)
    if git_top:
        raise _usage(_prefix=prefix, msg=f"token file {path} would be inside the git work tree {git_top}; a credential there can be "
                     "committed or read by agents. Choose a token directory outside any repository",
                     token_file=path, conflict=git_top)
    if os.path.islink(path):
        raise _usage(_prefix=prefix, msg=f"token file {path} is a symlink; refusing to write through it", token_file=path)
    if os.path.lexists(path) and not os.path.isfile(path):
        raise _usage(_prefix=prefix, msg=f"token file {path} exists and is not a regular file", token_file=path)


def _token_works(home: str, path: str, lead: str) -> bool:
    """True only for a working, unscoped agent token of ``lead`` itself: a valid token of
    another agent in the lead's file is replaced (Linear PR30 re-review P2)."""
    if not os.path.isfile(path) or os.path.islink(path):
        return False
    rc, body = _run(home, ["whoami", "--token-file", path])
    d = body.get("data") or {}
    return rc == 0 and d.get("kind") == "agent" and d.get("name") == lead and d.get("via") == "token"


def _write_token(path: str, token: str) -> None:
    """Atomic 0600 write: a private temp file in the same directory, then rename over the
    old file. rename replaces the directory entry, so it never follows a symlink."""
    tdir = os.path.dirname(path)
    os.makedirs(tdir, mode=0o700, exist_ok=True)
    tmp = os.path.join(tdir, f".{os.path.basename(path)}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _fail(step: str, rc: int, body: Dict[str, Any], done: List[str]) -> MeshError:
    err = body.get("error") or {}
    return MeshError(
        code=err.get("code") or c.INTERNAL_ERROR,
        exit_code=rc or c.EXIT_INTERNAL,
        message=f"mesh setup: step '{step}' failed: {err.get('message', 'unknown error')}",
        details={"step": step, "completed": done, "error": err},
    )


def handle_setup(args: argparse.Namespace) -> Dict[str, Any]:
    from mesh.cli.commands.doctor_env import environment_checks
    from mesh.core.passphrase import read_passphrase
    from mesh.core.paths import resolve_home

    home = getattr(args, "home", None) or os.environ.get("MESH_HOME") or os.path.expanduser("~/.agent-mesh")
    home = resolve_home(home)
    if getattr(args, "dry_run", False):
        raise _usage("--dry-run is not supported: setup only runs steps that are not done yet "
                     "(re-running it is safe); run `mesh doctor` to see the current state")
    lead_name = None if args.no_lead else args.lead_name
    if lead_name is not None and not _LEAD_NAME_RE.match(lead_name):
        raise _usage(f"invalid --lead-name {lead_name!r}: use letters, digits, '-' or '_' (max 64)",
                     lead_name=lead_name)
    token_file_planned = token_path_for(home, lead_name, args.token_dir) if lead_name else None
    if token_file_planned:
        _check_token_destination(home, token_file_planned)
    done: List[str] = []
    skipped: List[str] = []
    initialized = os.path.exists(os.path.join(home, "mesh.db"))
    need_pass = (not initialized) or not args.no_lead or bool(getattr(args, "approval_mode", None))
    passphrase = read_passphrase(args, "Mesh user passphrase (>= 12 chars): ") if need_pass else None
    if passphrase is not None and not initialized and (getattr(args, "passphrase_fd", None) is None
                                                       and not getattr(args, "passphrase_file", None)
                                                       and not os.environ.get("MESH_PASSPHRASE_FILE")
                                                       and not os.environ.get("MESH_PASSPHRASE")):
        again = read_passphrase(argparse.Namespace(), "Repeat the passphrase: ")
        if again != passphrase:
            raise MeshError(code=c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE,
                            message="mesh setup: the two passphrases differ; nothing was changed")

    if initialized:
        skipped.append("init (home already initialized)")
    else:
        rc, body = _run(home, ["init"], passphrase)
        if rc != 0:
            raise _fail("init", rc, body, done)
        done.append("init")

    if args.no_start:
        skipped.append("daemon start (--no-start)")
    else:
        rc, body = _run(home, ["daemon", "status"])
        if rc == 0 and (body.get("data") or {}).get("running", True):
            skipped.append("daemon start (already running)")
        else:
            rc, body = _run(home, ["daemon", "start"])
            if rc != 0:
                raise _fail("daemon start", rc, body, done)
            done.append("daemon start")

    token_file = None
    lead = None
    if args.no_lead:
        skipped.append("lead agent (--no-lead)")
    else:
        lead = lead_name
        token_file = token_file_planned
        rc, body = _run(home, ["agent", "show", lead, "--user"], passphrase)
        if rc == 0:
            skipped.append(f"lead agent '{lead}' (exists)")
        else:
            rc, body = _run(home, ["agent", "create", "--user", "--name", lead, "--adapter", "none",
                                   "--delivery", "inbox", "--can-lead", "--max-concurrency", "1"], passphrase)
            if rc != 0:
                raise _fail("agent create", rc, body, done)
            done.append(f"agent create {lead}")
        # Credential provisioning is separate from agent creation, so an interrupted
        # setup or a deleted token file is repaired on the next run (Linear PR30 P2-2).
        if _token_works(home, token_file, lead):
            skipped.append(f"token file {token_file} (valid)")
        else:
            _check_token_destination(home, token_file)
            rc, body = _run(home, ["agent", "token", "issue", lead, "--user"], passphrase)
            if rc != 0:
                raise _fail("agent token issue", rc, body, done)
            token = (body.get("data") or {}).get("token")
            if not token:
                raise _fail("agent token issue", 1, {"error": {"message": "no token in response"}}, done)
            _write_token(token_file, token)
            done.append(f"token file {token_file}")
        rc, body = _run(home, ["config", "set", "lead.default_agent", json.dumps(lead), "--user"], passphrase)
        if rc != 0:
            raise _fail("config set lead.default_agent", rc, body, done)
        done.append(f"lead.default_agent = {lead}")

    mode = getattr(args, "approval_mode", None)
    if mode:
        rc, body = _run(home, ["config", "set", "approval.mode", json.dumps(mode), "--user"], passphrase)
        if rc != 0:
            raise _fail("config set approval.mode", rc, body, done)
        done.append(f"approval.mode = {mode}")

    checks = environment_checks(home, os.path.exists(os.path.join(home, "mesh.db")))
    tok = token_file or "<token file>"
    next_steps = [
        "mesh status",
        f"mesh intake submit --user --title \"First project\" --goal \"...\" --criterion \"...\"  (lead: {lead or 'lead'})",
        f"mesh inbox list --token-file {tok}   (what the lead sees)",
        "docs/usage.md: the full workflow; docs/examples/: adding a CLI agent (droid, kiro, agy, vibe, hermes, openclaw)",
    ]
    return {
        "home": home,
        "done": done,
        "skipped": skipped,
        "lead": lead,
        "token_file": token_file,
        "checks": checks,
        "problems": [f"{x['name']}: {x['detail']} -> {x.get('hint', '')}" for x in checks if not x["ok"]],
        "next_steps": next_steps,
    }
