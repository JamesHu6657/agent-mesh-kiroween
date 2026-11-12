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
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, workspace_path TEXT);")
        conn.execute("CREATE TABLE events (seq INTEGER PRIMARY KEY, type TEXT, payload_json TEXT);")
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


class TestRound4(_Tmp):
    """Linear Agent chat round 4 (@21f165c)."""

    def _db(self, home, ws_root=None, repo=None):
        TestTokenDestination._db(self, home, ws_root=ws_root, repo=repo)

    def test_home_with_uri_characters_still_protected(self):
        home, ws = os.path.join(self.tmp, "h?mode=memory#x"), os.path.join(self.tmp, "ws")
        os.makedirs(ws)
        self._db(home, ws_root=ws)
        with self.assertRaises(MeshError):
            msetup._check_token_destination(home, os.path.join(ws, "lead.token"))

    def test_uninspectable_db_fails_closed(self):
        from mesh.core.token_roots import configured_roots

        home = os.path.join(self.tmp, "home")
        os.makedirs(home)
        with open(os.path.join(home, "mesh.db"), "wb") as f:
            f.write(b"not a database" * 100)
        with self.assertRaises(MeshError):
            configured_roots(home)
        with self.assertRaises(MeshError):
            msetup._check_token_destination(home, os.path.join(self.tmp, "cfg", "lead.token"))

    def test_client_refuses_token_in_configured_root_and_repo(self):
        from mesh.client import MeshClient

        home, ws, repo = (os.path.join(self.tmp, n) for n in ("home", "ws", "repo"))
        os.makedirs(ws)
        os.makedirs(repo)
        self._db(home, ws_root=ws, repo=repo)
        for d in (ws, repo):
            tok = os.path.join(d, "a.token")
            with open(tok, "w", encoding="utf-8") as f:
                f.write("tok")
            os.chmod(tok, 0o600)
            client = MeshClient.__new__(MeshClient)
            client.home = home
            with self.assertRaises(MeshError) as cm:
                _client_token(client, tok)
            self.assertEqual(cm.exception.code, "TOKEN_FILE_INSECURE")

    def test_shim_differs_for_shared_package_envs(self):
        from mesh.core import paths

        dirs = []
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": self.tmp}), \
                mock.patch.object(paths, "get_install_root", return_value=self.tmp), \
                mock.patch.object(os.path, "realpath", side_effect=lambda p: "/usr/bin/python3.11"
                                  if "python" in str(p) else os.path.abspath(p)):
            for venv in ("/opt/env-a", "/opt/env-b"):
                with mock.patch.object(sys, "executable", venv + "/bin/python3"), \
                        mock.patch.object(sys, "prefix", venv):
                    dirs.append(paths.get_mesh_bin_dir())
        self.assertNotEqual(dirs[0], dirs[1])

    def test_nested_malformed_case_id(self):
        from mesh.adapters import smoke_schema

        for bad in ([], {"x": 1}, 3):
            errs = smoke_schema.validate({"adapter": "droid", "version": "1.6.0", "cases": [{"id": bad}]}, self.tmp)
            self.assertTrue(any("case id must be a string" in e for e in errs), errs)

    def test_doctor_non_utf8_git(self):
        from mesh.cli.commands import doctor_env

        fake = os.path.join(self.tmp, "git")
        with open(fake, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\nprintf 'git version \\377\\n'\n")
        os.chmod(fake, stat.S_IRWXU)
        with mock.patch.dict(os.environ, {"PATH": self.tmp + os.pathsep + os.environ.get("PATH", "")}):
            checks = doctor_env.environment_checks(self.tmp, False)
        self.assertTrue(any(c["name"] == "platform" for c in checks))
        self.assertTrue(any(c["name"] == "git" for c in checks))


class TestRound5(_Tmp):
    """Linear Agent chat round 5 (@60604b0)."""

    def _db(self, home, ws_value=None, task_paths=()):
        os.makedirs(home, exist_ok=True)
        conn = sqlite3.connect(os.path.join(home, "mesh.db"))
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, "
                     "updated_at TEXT NOT NULL, updated_by TEXT NOT NULL);")
        conn.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, repo_path TEXT);")
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, workspace_path TEXT);")
        conn.execute("CREATE TABLE events (seq INTEGER PRIMARY KEY, type TEXT, payload_json TEXT);")
        if ws_value is not None:
            conn.execute("INSERT INTO settings VALUES ('workspaces.root', ?, 'now', 'test');", (json.dumps(ws_value),))
        for i, wp in enumerate(task_paths):
            conn.execute("INSERT INTO tasks VALUES (?, ?);", (f"tsk_{i}", wp))
        conn.commit()
        conn.close()

    def test_root_normalised_like_runtime(self):
        from mesh.core.token_roots import configured_roots

        home, ws = os.path.join(self.tmp, "home"), os.path.join(self.tmp, "custom-ws")
        os.makedirs(ws)
        self._db(home, ws_value=ws + " ")
        root, _paths = configured_roots(home)
        self.assertEqual(root, ws)
        with self.assertRaises(MeshError):
            msetup._check_token_destination(home, os.path.join(ws, "prj_1", "tasks", "tsk_1", "lead.token"))

    def test_workspaces_under_an_earlier_root_stay_protected(self):
        old = os.path.join(self.tmp, "rootA")
        wp = os.path.join(old, "prj_1", "tasks", "tsk_1") + "/"
        os.makedirs(wp)
        home = os.path.join(self.tmp, "home")
        self._db(home, ws_value=os.path.join(self.tmp, "rootB"), task_paths=[wp])
        with self.assertRaises(MeshError):
            msetup._check_token_destination(home, os.path.join(wp, "lead.token"))
        with self.assertRaises(MeshError):
            msetup._check_token_destination(home, os.path.join(old, "prj_1", "lead", "agt", "lead.token"))

    def test_unreadable_evidence_is_evidence_invalid(self):
        from mesh.adapters import evidence, smoke_schema

        with mock.patch.object(evidence, "results_dir", return_value=self.tmp), \
                mock.patch.object(smoke_schema, "load_latest", side_effect=PermissionError(13, "denied")):
            ev = evidence.load_evidence("droid", False, "1.6.0")
        self.assertEqual((ev.ok, ev.reason), (False, "evidence_invalid"))
        path = os.path.join(self.tmp, "droid-1.6.0.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"adapter": "droid", "version": "1.6.0", "cases": []}, f)
        with mock.patch.object(evidence, "results_dir", return_value=self.tmp), \
                mock.patch.object(smoke_schema, "load_latest", return_value=path), \
                mock.patch.object(smoke_schema, "validate", side_effect=PermissionError(13, "denied")):
            ev = evidence.load_evidence("droid", False, "1.6.0")
        self.assertEqual((ev.ok, ev.reason), (False, "evidence_invalid"))


class TestRound6(TestRound5):
    """Linear Agent chat round 6 (@a36ff84)."""

    def _event(self, home, etype, payload):
        conn = sqlite3.connect(os.path.join(home, "mesh.db"))
        conn.execute("INSERT INTO events (type, payload_json) VALUES (?, ?);", (etype, json.dumps(payload)))
        conn.commit()
        conn.close()

    def test_lead_only_workspace_under_earlier_root(self):
        home, a, b = (os.path.join(self.tmp, n) for n in ("home", "rootA", "rootB"))
        lead = os.path.join(a, "prj_1", "lead", "agt_1")
        os.makedirs(lead)
        self._db(home, ws_value=b)
        self._event(home, "config.updated", {"key": "workspaces.root", "value": a})
        self._event(home, "config.updated", {"key": "workspaces.root", "value": b})
        with self.assertRaises(MeshError):
            msetup._check_token_destination(home, os.path.join(lead, "lead.token"))

    def test_earlier_attempt_workspace_stays_protected(self):
        home, a = os.path.join(self.tmp, "home"), os.path.join(self.tmp, "rootA")
        old = os.path.join(a, "p", "x", "tsk_1")
        os.makedirs(old)
        self._db(home, ws_value=os.path.join(self.tmp, "rootB"),
                 task_paths=[os.path.join(self.tmp, "rootB", "prj_1", "tasks", "tsk_1.a2") + "/"])
        self._event(home, "task.retried", {"workspace_path": old + "/"})
        with self.assertRaises(MeshError):
            msetup._check_token_destination(home, os.path.join(old, "lead.token"))

    def test_symlinked_workspace_target_protected(self):
        home = os.path.join(self.tmp, "home")
        target = os.path.join(self.tmp, "elsewhere")
        os.makedirs(target)
        ws = os.path.join(self.tmp, "ws")
        os.makedirs(os.path.join(ws, "prj_1", "tasks"))
        link = os.path.join(ws, "prj_1", "tasks", "tsk_1")
        os.symlink(target, link)
        self._db(home, ws_value=ws, task_paths=[link + "/"])
        with self.assertRaises(MeshError):
            msetup._check_token_destination(home, os.path.join(target, "lead.token"))

    def test_unrelated_location_still_allowed(self):
        home = os.path.join(self.tmp, "home")
        self._db(home, ws_value=os.path.join(self.tmp, "ws"), task_paths=["/weird"])
        msetup._check_token_destination(home, os.path.join(self.tmp, "cfg", "lead.token"))


def _client_token(client, tok):
    import argparse

    args = argparse.Namespace(user=False, token_file=tok, passphrase_fd=None, passphrase_file=None)
    return client._resolve_auth_uncached(args)


if __name__ == "__main__":
    unittest.main()
