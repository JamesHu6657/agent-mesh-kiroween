"""Hermetic git fixtures for the M2 workspace/merge tests (checklist section K, fixture K0).

Safety rule (hard, enforced by assertions in this module):

- Never point any helper at the Mesh checkout (`/workspace/mesh-fresh-v1`,
  `/home/user/workspace/mesh-fresh-v1`, any `mesh-wt-*`) or at any directory outside
  `/tmp/mh-*`. Every git call made here has an explicit `cwd` under the fixture base
  directory. Before a process starts, `git()` also refuses the repository-selecting
  options `-C`, `--git-dir`/`--git-dir=*`, `--work-tree`/`--work-tree=*` and any
  *absolute* path argument that is not under `/tmp/mh-`. Relative arguments and `-c`
  values are not inspected, so the `cwd` guard above is what keeps git inside the
  fixture.
- Tests never push. The bare "remote" is created with `git clone --bare` from the local
  repo, so `git ls-remote origin` works without any network or push.
- The git environment is hermetic: every inherited `GIT_*` variable is dropped, then an
  empty global config file, `GIT_CONFIG_NOSYSTEM=1`, no terminal prompt and
  `GIT_CEILING_DIRECTORIES=<base>` are set, so an unknown global git config on the box
  (plan risk 6), an inherited `GIT_DIR`/`GIT_WORK_TREE` and auto-detected identities cannot
  influence a test. `GIT_AUTHOR_*`/`GIT_COMMITTER_*` are never set here; commits use
  `-c user.name=... -c user.email=...` or the repo-local identity that `make_k0` writes
  (a test fixture config, not Mesh config).

This module is stdlib only and is not collected as a test (its name does not start with
`test_`).
"""

import fnmatch
import os
import secrets
import shutil
import subprocess
from typing import Dict, List, Optional

from tests.helpers import MeshHarness

BASE_PREFIX = "/tmp/mh-"

CALC_PY = "def add(a, b):\n    return a + b\n"

TEST_CALC_PY = (
    "import unittest\n"
    "\n"
    "from calc import add\n"
    "\n"
    "\n"
    "class TestCalc(unittest.TestCase):\n"
    "    def test_add(self):\n"
    "        self.assertEqual(add(1, 2), 3)\n"
    "\n"
    "\n"
    "if __name__ == '__main__':\n"
    "    unittest.main()\n"
)


class GitFixture:
    """Owns a `/tmp/mh-<rand8>-git/` base dir holding K0 repos and their bare remotes."""

    def __init__(self) -> None:
        self.base = f"/tmp/mh-{secrets.token_hex(4)}-git"
        os.makedirs(self.base, mode=0o700, exist_ok=True)
        os.chmod(self.base, 0o700)
        self.empty_gitconfig = os.path.join(self.base, "empty.gitconfig")
        with open(self.empty_gitconfig, "w", encoding="utf-8") as f:
            f.write("")
        # Directories git calls may run in: the base dir, plus roots registered with
        # allow_root() (a GitMeshHarness registers its own home, where Mesh creates the
        # integration, lead and task worktrees).
        self.allowed_roots: List[str] = [self.base]

    def allow_root(self, root: str) -> None:
        """Register another `/tmp/mh-*` directory (an existing one) as a valid git cwd/path root.

        Refuses anything outside `/tmp/mh-` and any Mesh checkout; never widens beyond the
        given directory.
        """
        assert isinstance(root, str) and root.startswith(BASE_PREFIX), (
            f"git fixture refuses allowed root outside {BASE_PREFIX}: {root!r}"
        )
        real = os.path.realpath(root)
        assert real.startswith(os.path.realpath("/tmp") + os.sep + "mh-"), (
            f"git fixture refuses allowed root that resolves outside /tmp/mh-: {root!r} -> {real!r}"
        )
        assert "mesh-fresh-v1" not in real and "mesh-wt-" not in real, f"refusing Mesh checkout {real!r}"
        if root not in self.allowed_roots:
            self.allowed_roots.append(root)

    # --- guards ---------------------------------------------------------------

    def _assert_under_base(self, path: str) -> str:
        """Refuse any path that is not inside the base dir or an allowed root (before any spawn)."""
        assert isinstance(path, str) and path, f"git fixture path must be a non-empty string, got {path!r}"
        assert path.startswith(BASE_PREFIX), f"git fixture refuses path outside {BASE_PREFIX}: {path}"
        real = os.path.realpath(path)
        for root in self.allowed_roots:
            root_real = os.path.realpath(root)
            if real == root_real or real.startswith(root_real + os.sep):
                return path
        raise AssertionError(f"git fixture refuses path outside its roots {self.allowed_roots}: {path}")

    def path(self, *parts: str) -> str:
        """Join parts onto the base dir, refusing anything that escapes it."""
        return self._assert_under_base(os.path.join(self.base, *parts))

    # --- environment ----------------------------------------------------------

    def git_env_vars(self) -> Dict[str, str]:
        """Only the hermetic git keys (no copy of `os.environ`).

        Used both for fixture git calls and to extend a `MeshHarness` env without
        overwriting the harness' own `PATH`/`MESH_*` values.
        """
        return {
            "GIT_CONFIG_GLOBAL": self.empty_gitconfig,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CEILING_DIRECTORIES": self.base,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }

    def env(self) -> Dict[str, str]:
        """Hermetic git environment: inherited `GIT_*` dropped, then the hermetic keys."""
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(self.git_env_vars())
        return env

    # --- git ------------------------------------------------------------------

    def _assert_safe_args(self, args: List[str]) -> None:
        """Refuse repository-selecting options and absolute paths outside `/tmp/mh-`."""
        for arg in args:
            assert arg not in ("-C", "--git-dir", "--work-tree"), (
                f"git fixture refuses repository-selecting option {arg!r} (cwd must select the repo)"
            )
            assert not arg.startswith("--git-dir="), f"git fixture refuses {arg!r}"
            assert not arg.startswith("--work-tree="), f"git fixture refuses {arg!r}"
            if arg.startswith("/"):
                assert arg.startswith(BASE_PREFIX), (
                    f"git fixture refuses absolute path argument outside {BASE_PREFIX}: {arg}"
                )

    def git(
        self,
        repo: str,
        *args: str,
        check: bool = True,
        identity: bool = False,
    ) -> subprocess.CompletedProcess:
        """Run git with cwd=repo (must be under the base dir) and the hermetic env."""
        self._assert_under_base(repo)
        self._assert_safe_args(list(args))
        cmd: List[str] = ["git"]
        if identity:
            cmd.extend(["-c", "user.name=t", "-c", "user.email=t@example.invalid"])
        cmd.extend(args)
        return subprocess.run(
            cmd,
            cwd=repo,
            env=self.env(),
            check=check,
            capture_output=True,
            text=True,
        )

    # --- K0 -------------------------------------------------------------------

    def make_k0(self, name: str = "R", identity: bool = True) -> str:
        """Create the K0 repo: branch main, repo-local identity, passing unittest suite, bare origin."""
        repo = self.path(name)
        origin = self.path(f"{name}-origin.git")
        self.git(self.base, "init", "-b", "main", repo)
        if identity:
            self.git(repo, "config", "user.name", "t")
            self.git(repo, "config", "user.email", "t@example.invalid")
        with open(os.path.join(repo, "calc.py"), "w", encoding="utf-8") as f:
            f.write(CALC_PY)
        tests_dir = os.path.join(repo, "tests")
        os.makedirs(tests_dir, exist_ok=True)
        with open(os.path.join(tests_dir, "__init__.py"), "w", encoding="utf-8") as f:
            f.write("")
        with open(os.path.join(tests_dir, "test_calc.py"), "w", encoding="utf-8") as f:
            f.write(TEST_CALC_PY)
        # Keep the tree clean after a test run (bytecode would otherwise show up as untracked).
        with open(os.path.join(repo, ".gitignore"), "w", encoding="utf-8") as f:
            f.write("__pycache__/\n")
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-m", "init", identity=not identity)
        # Bare "remote" built locally: no push, no network, no remote creation outside the fixture.
        self.git(self.base, "clone", "--bare", repo, origin)
        self.git(repo, "remote", "add", "origin", origin)
        if not identity:
            self.git(repo, "config", "--unset", "user.name", check=False)
            self.git(repo, "config", "--unset", "user.email", check=False)
            self.git(repo, "config", "user.useConfigOnly", "true")
        return repo

    # --- queries --------------------------------------------------------------

    def remote_refs(self, repo: str) -> List[str]:
        """Sorted `git ls-remote origin` lines (read-only; never a push)."""
        cp = self.git(repo, "ls-remote", "origin")
        return sorted(line for line in cp.stdout.splitlines() if line.strip())

    def head(self, repo: str, ref: str = "HEAD") -> str:
        return self.git(repo, "rev-parse", ref).stdout.strip()

    def is_clean(self, repo: str) -> bool:
        return self.git(repo, "status", "--porcelain").stdout.strip() == ""

    def has_merge_head(self, repo: str) -> bool:
        cp = self.git(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False)
        return cp.returncode == 0

    def branches(self, repo: str, pattern: str = "*") -> List[str]:
        """Plain branch names matching `pattern` (worktree-checked-out branches included).

        `git branch --list` prefixes a branch checked out in another worktree with `+ `,
        which would corrupt the name; `--format=%(refname:short)` prints the bare name.
        The pattern is passed through unchanged and matched with `fnmatch` semantics, so a
        `*` crosses `/` and `mesh/*` also matches `mesh/<prj>/<tsk>`.
        """
        cp = self.git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/**")
        names = [line.strip() for line in cp.stdout.splitlines() if line.strip()]
        return sorted(name for name in names if fnmatch.fnmatchcase(name, pattern))

    def worktrees(self, repo: str) -> List[Dict[str, object]]:
        """Parse `git worktree list --porcelain` into dicts."""
        cp = self.git(repo, "worktree", "list", "--porcelain")
        entries: List[Dict[str, object]] = []
        current: Dict[str, object] = {}
        for line in cp.stdout.splitlines():
            if not line.strip():
                if current:
                    entries.append(current)
                    current = {}
                continue
            key, _, value = line.partition(" ")
            if key == "worktree":
                current["worktree"] = value
            elif key == "HEAD":
                current["head"] = value
            elif key == "branch":
                current["branch"] = value
            elif key == "detached":
                current["detached"] = True
        if current:
            entries.append(current)
        return entries

    # --- teardown -------------------------------------------------------------

    def cleanup(self) -> None:
        """Remove the base dir (re-checking the /tmp/mh- prefix first)."""
        if not self.base.startswith(BASE_PREFIX):
            raise AssertionError(f"refusing to remove non-/tmp/mh- path: {self.base}")
        shutil.rmtree(self.base, ignore_errors=True)


class GitMeshHarness(MeshHarness):
    """MeshHarness whose meshd (and every child it launches) sees the fixture's git env."""

    def __init__(self, fixture: GitFixture, **kwargs: object) -> None:
        self.git_fixture = fixture
        super().__init__(**kwargs)  # type: ignore[arg-type]
        # Integration, lead and task worktrees live under $H/workspaces: let the fixture's
        # git helpers (worktree queries, test-made commits) run there too.
        fixture.allow_root(self.home)

    def get_env(self) -> Dict[str, str]:
        """Harness env (shims on PATH, MESH_HOME, MESH_TESTING, tripwire) plus the git keys.

        Every inherited `GIT_*` key is dropped first, so an outer `GIT_DIR`/`GIT_WORK_TREE`
        (a git hook or wrapper exporting them) or an inherited `GIT_AUTHOR_*`/
        `GIT_COMMITTER_*` can never reach meshd or the git calls it makes. Only the
        hermetic git keys are added back: the raw `os.environ` is never re-applied, so the
        shims stay on `PATH` and the per-harness tripwire file is not replaced.
        """
        env = {k: v for k, v in super().get_env().items() if not k.startswith("GIT_")}
        env.update(self.git_fixture.git_env_vars())
        return env

    def teardown(self) -> None:
        """Harness teardown first (daemon, invariants, tripwire), then the git fixture."""
        try:
            super().teardown()
        finally:
            self.git_fixture.cleanup()
