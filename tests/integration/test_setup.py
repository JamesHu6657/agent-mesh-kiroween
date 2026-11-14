"""Productize: `mesh setup` (first run in one command, safe to re-run)."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN = os.path.join(REPO, "bin", "mesh")


class TestSetup(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mh-setup-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.pfile = os.path.join(self.tmp, "pass")
        with open(self.pfile, "w", encoding="utf-8") as f:
            f.write("setup-test-passphrase-123")
        os.chmod(self.pfile, 0o600)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("MESH_")}
        self.env["MESH_TESTING"] = "1"
        self.env["MESH_PASSPHRASE_FILE"] = self.pfile
        self.addCleanup(self._stop)

    def _mesh(self, *argv):
        p = subprocess.run([BIN, "--home", self.home, *argv], env=self.env, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=240)
        return p.returncode, (json.loads(p.stdout) if p.stdout.strip() else {"stderr": p.stderr})

    def _stop(self):
        if os.path.exists(os.path.join(self.home, "mesh.db")):
            self._mesh("daemon", "stop", "--user")

    def test_setup_then_rerun(self):
        tdir = os.path.join(self.tmp, "tok")
        rc, r = self._mesh("setup", "--token-dir", tdir)
        self.assertEqual(rc, 0, r)
        d = r["data"]
        self.assertEqual(d["done"][:3], ["init", "daemon start", "agent create lead"])
        tok = d["token_file"]
        self.assertEqual(os.stat(tok).st_mode & 0o777, 0o600)
        rc, who = self._mesh("whoami", "--token-file", tok)
        self.assertEqual(rc, 0, who)
        self.assertEqual(who["data"]["name"], "lead")
        rc, cfg = self._mesh("config", "get", "lead.default_agent", "--user")
        self.assertEqual(rc, 0, cfg)
        self.assertIn("lead", json.dumps(cfg["data"]))
        rc, r2 = self._mesh("setup", "--token-dir", tdir)
        self.assertEqual(rc, 0, r2)
        self.assertNotIn("init", r2["data"]["done"])
        self.assertTrue(any(s.startswith("init") for s in r2["data"]["skipped"]))
        self.assertTrue(any("exists" in s for s in r2["data"]["skipped"]))


    def test_token_recovered_after_delete(self):
        """Linear PR30 P2-2: a missing token file is re-issued on the next run."""
        tdir = os.path.join(self.tmp, "tok")
        rc, r = self._mesh("setup", "--token-dir", tdir)
        self.assertEqual(rc, 0, r)
        tok = r["data"]["token_file"]
        os.unlink(tok)
        rc, r2 = self._mesh("setup", "--token-dir", tdir)
        self.assertEqual(rc, 0, r2)
        self.assertEqual(r2["data"]["token_file"], tok)
        self.assertEqual(self._mesh("whoami", "--token-file", tok)[0], 0)

    def test_other_agents_token_is_replaced(self):
        """Linear PR30 re-review P2: a valid token of another agent in the lead's file."""
        tdir = os.path.join(self.tmp, "tok")
        rc, r = self._mesh("setup", "--token-dir", tdir)
        self.assertEqual(rc, 0, r)
        tok = r["data"]["token_file"]
        rc, _ = self._mesh("agent", "create", "--user", "--name", "worker1", "--adapter", "none",
                           "--delivery", "inbox", "--max-concurrency", "1")
        self.assertEqual(rc, 0)
        rc, w = self._mesh("agent", "token", "issue", "worker1", "--user")
        self.assertEqual(rc, 0, w)
        with open(tok, "w", encoding="utf-8") as f:
            f.write(w["data"]["token"] + "\n")
        rc, r2 = self._mesh("setup", "--token-dir", tdir)
        self.assertEqual(rc, 0, r2)
        rc, who = self._mesh("whoami", "--token-file", tok)
        self.assertEqual(rc, 0, who)
        self.assertEqual(who["data"]["name"], "lead")

    def test_refuses_bad_destinations_before_issuing(self):
        """Linear PR30 P2-1/P2-3/P2-4: bad names, symlinks, files as dirs, workspaces/, dry-run."""
        rc, r = self._mesh("setup", "--dry-run")
        self.assertNotEqual(rc, 0, r)
        self.assertFalse(os.path.exists(os.path.join(self.home, "mesh.db")))
        rc, r = self._mesh("setup", "--lead-name", "../victim", "--no-start")
        self.assertNotEqual(rc, 0, r)
        self.assertFalse(os.path.exists(os.path.join(self.home, "mesh.db")))
        rc, r = self._mesh("setup", "--token-dir", os.path.join(self.home, "workspaces", "t"))
        self.assertNotEqual(rc, 0, r)
        afile = os.path.join(self.tmp, "afile")
        open(afile, "w").close()
        rc, r = self._mesh("setup", "--token-dir", afile)
        self.assertNotEqual(rc, 0, r)
        sd = os.path.join(self.tmp, "sd")
        os.makedirs(sd)
        rc, r = self._mesh("setup", "--no-lead")
        self.assertEqual(rc, 0, r)
        import hashlib
        suffix = hashlib.sha256(os.path.realpath(self.home).encode()).hexdigest()[:8]
        victim = os.path.join(self.tmp, "victim")
        os.symlink(victim, os.path.join(sd, f"lead-{suffix}.token"))
        rc, r = self._mesh("setup", "--token-dir", sd)
        self.assertNotEqual(rc, 0, r)
        self.assertFalse(os.path.exists(victim))

    def test_two_homes_do_not_share_a_token_file(self):
        """Linear PR30 P2-1: two homes, same token dir, both tokens keep working."""
        tdir = os.path.join(self.tmp, "tok")
        rc, a = self._mesh("setup", "--token-dir", tdir)
        self.assertEqual(rc, 0, a)
        first_home = self.home
        self.home = os.path.join(self.tmp, "home2")
        try:
            rc, b = self._mesh("setup", "--token-dir", tdir)
            self.assertEqual(rc, 0, b)
            self.assertNotEqual(a["data"]["token_file"], b["data"]["token_file"])
            self.assertEqual(self._mesh("whoami", "--token-file", b["data"]["token_file"])[0], 0)
        finally:
            self._stop()
            self.home = first_home
        self.assertEqual(self._mesh("whoami", "--token-file", a["data"]["token_file"])[0], 0)


if __name__ == "__main__":
    unittest.main()
