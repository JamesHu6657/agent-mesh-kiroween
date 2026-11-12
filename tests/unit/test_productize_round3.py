"""Linear Agent chat re-review round 3 (@043b143) regressions.

P2 setup refused token files only under <home>/runs and <home>/workspaces: it now also
   refuses the configured workspaces.root, registered project repositories and any git
   work tree.
P2 package installs shared one launcher shim: it is now namespaced per installation.
P3 setup blocked on passphrases longer than the pipe buffer.
P3 malformed evidence ("cases" not a list) raised instead of evidence_invalid.
P3 doctor reported a non-working git as ok.
"""

import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from mesh.cli.commands import setup as msetup
from mesh.errors import MeshError


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mh-r3-")
        self.addCleanup(shutil.rmtree, self.tmp, True)


class TestTokenDestination(_Tmp):
    def _db(self, home, ws_root=None, repo=None):
        os.makedirs(home, exist_ok=True)
        conn = sqlite3.connect(os.path.join(home, "mesh.db"))
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, "
                     "updated_at TEXT NOT NULL, updated_by TEXT NOT NULL);")
        conn.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, repo_path TEXT);")
        if ws_root:
            conn.execute("INSERT INTO settings VALUES ('workspaces.root', ?, 'now', 'test');", (json.dumps(ws_root),))
        if repo:
            conn.execute("INSERT INTO projects VALUES ('prj_1', ?);", (repo,))
        conn.commit()
        conn.close()

    def test_refuses_custom_workspaces_root(self):
        home, ws = os.path.join(self.tmp, "home"), os.path.join(self.tmp, "ws")
        os.makedirs(ws)
        self._db(home, ws_root=ws)
        with self.assertRaises(MeshError) as cm:
            msetup._check_token_destination(home, os.path.join(ws, "tok", "lead.token"))
        self.assertIn("workspace root or project repository", cm.exception.message)

    def test_refuses_registered_repo_and_any_git_worktree(self):
        home, repo = os.path.join(self.tmp, "home"), os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(repo, ".git"))
        self._db(home, repo=repo)
        with self.assertRaises(MeshError):
            msetup._check_token_destination(home, os.path.join(repo, "lead.token"))
        other = os.path.join(self.tmp, "other")
        os.makedirs(os.path.join(other, ".git"))
        os.makedirs(os.path.join(other, "sub"))
        with self.assertRaises(MeshError) as cm:
            msetup._check_token_destination(home, os.path.join(other, "sub", "lead.token"))
        self.assertIn("git work tree", cm.exception.message)

    def test_allows_plain_directory(self):
        home = os.path.join(self.tmp, "home")
        self._db(home)
        msetup._check_token_destination(home, os.path.join(self.tmp, "cfg", "lead.token"))


class TestShimPerInstall(_Tmp):
    def test_two_installs_get_two_shims(self):
        from mesh.core import paths

        dirs = []
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": self.tmp}), \
                mock.patch.object(paths, "get_install_root", return_value=self.tmp):
            for exe in ("/opt/venv-a/bin/python3", "/opt/venv-b/bin/python3"):
                with mock.patch.object(sys, "executable", exe):
                    dirs.append(paths.get_mesh_bin_dir())
        self.assertNotEqual(dirs[0], dirs[1])
        for d, exe in zip(dirs, ("venv-a", "venv-b")):
            with open(os.path.join(d, "mesh"), encoding="utf-8") as f:
                self.assertIn(exe, f.read())


class TestLongPassphrase(_Tmp):
    def test_passphrase_longer_than_pipe_buffer(self):
        reader = os.path.join(self.tmp, "reader.py")
        with open(reader, "w", encoding="utf-8") as f:
            f.write("import os,sys,json\n"
                    "fd=int(sys.argv[sys.argv.index('--passphrase-fd')+1])\n"
                    "data=b''\n"
                    "while True:\n"
                    "    b=os.read(fd,65536)\n"
                    "    if not b: break\n"
                    "    data+=b\n"
                    "print(json.dumps({'ok': True, 'n': len(data)}))\n")
        secret = "x" * 200000
        with mock.patch.object(msetup, "_mesh_cmd", return_value=[sys.executable, reader]):
            rc, body = msetup._run(self.tmp, ["noop"], secret)
        self.assertEqual(rc, 0, body)
        self.assertEqual(body["n"], len(secret))


class TestMalformedEvidence(_Tmp):
    def test_cases_not_a_list_is_evidence_invalid(self):
        from mesh.adapters import evidence, smoke_schema

        path = os.path.join(self.tmp, "droid-1.6.0.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"adapter": "droid", "version": "1.6.0", "cases": 1}, f)
        with mock.patch.object(evidence, "results_dir", return_value=self.tmp), \
                mock.patch.object(smoke_schema, "load_latest", return_value=path), \
                mock.patch.object(smoke_schema, "parse_results_name", return_value=("1.6.0",)), \
                mock.patch.object(smoke_schema, "validate", return_value=[]), \
                mock.patch.object(smoke_schema, "_check_sidecar", return_value=None):
            ev = evidence.load_evidence("droid", False, "1.6.0")
        self.assertFalse(ev.ok)
        self.assertEqual(ev.reason, "evidence_invalid")


class TestDoctorBrokenGit(_Tmp):
    def test_git_that_fails_is_not_ok(self):
        from mesh.cli.commands import doctor_env

        fake = os.path.join(self.tmp, "git")
        with open(fake, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\nexit 127\n")
        os.chmod(fake, stat.S_IRWXU)
        with mock.patch.dict(os.environ, {"PATH": self.tmp + os.pathsep + os.environ.get("PATH", "")}):
            checks = doctor_env.environment_checks(self.tmp, False)
        git = [c for c in checks if c["name"] == "git"][0]
        self.assertFalse(git["ok"], git)


if __name__ == "__main__":
    unittest.main()
