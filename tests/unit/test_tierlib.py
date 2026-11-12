"""Unit tests for tests/tierlib.py: capture writer, sidecars, promotion (m3-r1; ACC: N21).

Every path is under /tmp/mh-*; the fake repo is a temp dir, never the checkout. The last
test checks the committed trees read-only.
"""

import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout

from tests import tierlib

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# The M1 fixtures (plan §14.11, test_fixture_hashes) predate sidecars; every other file
# under tests/fixtures/real or tests/smoke/results must carry one (N21).
PRE_M3_FIXTURES = {
    "README.md", "droid/1.3.0/help.txt", "droid/1.3.0/exec-help.txt", "droid/1.3.0/exec-init-event-opus55-high.json",
    "agy/1.5.2/help.txt", "agy/1.5.2/models.txt", "kiro/3.1.0/help.txt", "kiro/3.1.0/chat-help.txt",
    "kiro/3.1.0/list-models.json", "kiro/3.1.0/effort-high-run-metadata.jsonl", "vibe/3.1.0/help.txt",
    "hermes/not-installed.txt", "openclaw/not-installed.txt",
}


class TierCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mh-tier-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.assertTrue(self.tmp.startswith("/tmp/mh-"))
        self.staging = os.path.join(self.tmp, "staging")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, "tests", "fixtures", "real", "droid", "1.3.0"))
        with open(os.path.join(self.repo, "tests", "fixtures", "real", "droid", "1.3.0", "help.txt"), "wb") as f:
            f.write(b"old\n")


class WriteCaptureTest(TierCase):
    def test_sidecar_has_exactly_three_keys_and_the_hash(self) -> None:
        """ACC: N21 - write_capture writes the data and <file>.meta.json with exactly {command, captured_at_utc, sha256}."""
        data = b"Usage: droid exec [options]\n  --cwd <dir>\n"
        path = tierlib.write_capture(self.staging, "droid/1.6.0/exec-help.txt", data, ["droid", "exec", "--help"])
        self.assertEqual(path, os.path.join(self.staging, "droid", "1.6.0", "exec-help.txt"))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), data)
        with open(path + ".meta.json", "r", encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(set(meta), {"command", "captured_at_utc", "sha256"})
        self.assertEqual(meta["command"], ["droid", "exec", "--help"])
        self.assertEqual(meta["sha256"], hashlib.sha256(data).hexdigest())
        self.assertRegex(meta["captured_at_utc"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(tierlib.read_sidecar(path), meta)

    def test_never_overwrites(self) -> None:
        """ACC: N21 - a second write to the same path is refused and the first content is kept."""
        p = tierlib.write_capture(self.staging, "results/droid-1.6.0-2026-11-09.json", b"{}\n", ["harness"])
        with self.assertRaises(tierlib.TierError):
            tierlib.write_capture(self.staging, "results/droid-1.6.0-2026-11-09.json", b"[]\n", ["harness"])
        with open(p, "rb") as f:
            self.assertEqual(f.read(), b"{}\n")
        # A stray sidecar alone also blocks the write.
        side_only = os.path.join(self.staging, "droid", "1.6.0", "help.txt.meta.json")
        os.makedirs(os.path.dirname(side_only))
        with open(side_only, "w", encoding="utf-8") as f:
            f.write("{}")
        with self.assertRaises(tierlib.TierError):
            tierlib.write_capture(self.staging, "droid/1.6.0/help.txt", b"x", ["droid", "--help"])
        self.assertFalse(os.path.exists(os.path.join(self.staging, "droid", "1.6.0", "help.txt")))

    def test_refused_names_and_paths(self) -> None:
        """ACC: N21 - synthetic_* names, traversal, absolute paths, sidecar names and other layouts are refused."""
        bad = ["droid/1.6.0/synthetic_help.txt", "synthetic_droid/1.6.0/help.txt", "results/synthetic_r.json",
               "../droid/1.6.0/help.txt", "droid/../help.txt", "/tmp/mh-x/help.txt", "droid/1.6.0/help.txt.meta.json",
               "droid/help.txt", "droid/1.6.0/sub/help.txt", "results/a/b.json", "Droid/1.6.0/help.txt",
               "droid/v1/help.txt", "droid/1.6.0/.hidden", ""]
        for rel in bad:
            with self.subTest(rel=rel):
                with self.assertRaises(tierlib.TierError):
                    tierlib.write_capture(self.staging, rel, b"x", ["droid", "--help"])
        self.assertFalse(os.path.exists(self.staging))
        with self.assertRaises(tierlib.TierError):
            tierlib.write_capture("relative/staging", "droid/1.6.0/help.txt", b"x", ["droid"])
        with self.assertRaises(tierlib.TierError):
            tierlib.write_capture(self.staging, "droid/1.6.0/help.txt", b"x", [])


class PromoteTest(TierCase):
    def stage_two(self) -> None:
        tierlib.write_capture(self.staging, "droid/1.6.0/help.txt", b"help 1.6.0\n", ["droid", "--help"])
        tierlib.write_capture(self.staging, "results/droid-1.6.0-2026-11-09.json", b"{\"cases\": []}\n",
                              ["python3", "-m", "unittest", "tests.smoke.test_smoke"])

    def test_promote_copies_files_and_sidecars(self) -> None:
        """ACC: N21 - promote copies into tests/fixtures/real/<a>/<v>/ and tests/smoke/results/ only, with sidecars, re-verified."""
        self.stage_two()
        written = tierlib.promote(self.staging, self.repo)
        fx = os.path.join(self.repo, "tests", "fixtures", "real", "droid", "1.6.0", "help.txt")
        res = os.path.join(self.repo, "tests", "smoke", "results", "droid-1.6.0-2026-11-09.json")
        self.assertEqual(sorted(written), sorted([fx, res]))
        for p in written:
            meta = tierlib.read_sidecar(p)
            self.assertEqual(set(meta), {"command", "captured_at_utc", "sha256"})
        # The pre-existing fixture is untouched.
        with open(os.path.join(self.repo, "tests", "fixtures", "real", "droid", "1.3.0", "help.txt"), "rb") as f:
            self.assertEqual(f.read(), b"old\n")
        # Promoting again refuses: every destination exists now.
        with self.assertRaises(tierlib.TierError):
            tierlib.promote(self.staging, self.repo)

    def test_promote_refuses_existing_destination_before_writing_anything(self) -> None:
        """ACC: N21 - one existing destination refuses the whole promotion; nothing is written."""
        tierlib.write_capture(self.staging, "droid/1.3.0/help.txt", b"changed\n", ["droid", "--help"])
        tierlib.write_capture(self.staging, "droid/1.6.0/help.txt", b"new\n", ["droid", "--help"])
        with self.assertRaises(tierlib.TierError):
            tierlib.promote(self.staging, self.repo)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "tests", "fixtures", "real", "droid", "1.6.0")))
        with open(os.path.join(self.repo, "tests", "fixtures", "real", "droid", "1.3.0", "help.txt"), "rb") as f:
            self.assertEqual(f.read(), b"old\n")

    def test_promote_refuses_bad_staging(self) -> None:
        """ACC: N21 - tampered data, wrong sidecar keys, missing or orphan sidecars, synthetic names, symlinks and a non-checkout target are refused."""
        cases = {}

        def fresh(name):
            d = os.path.join(self.tmp, name)
            return d

        # tampered data (sha mismatch)
        s = fresh("s1")
        p = tierlib.write_capture(s, "droid/1.6.0/help.txt", b"a\n", ["droid", "--help"])
        with open(p, "ab") as f:
            f.write(b"tamper\n")
        cases["sha"] = s
        # extra sidecar key
        s = fresh("s2")
        p = tierlib.write_capture(s, "droid/1.6.0/help.txt", b"a\n", ["droid", "--help"])
        with open(p + ".meta.json", "r", encoding="utf-8") as f:
            meta = json.load(f)
        meta["note"] = "x"
        with open(p + ".meta.json", "w", encoding="utf-8") as f:
            json.dump(meta, f)
        cases["keys"] = s
        # data file without sidecar
        s = fresh("s3")
        os.makedirs(os.path.join(s, "droid", "1.6.0"))
        with open(os.path.join(s, "droid", "1.6.0", "help.txt"), "wb") as f:
            f.write(b"a\n")
        cases["no_sidecar"] = s
        # orphan sidecar
        s = fresh("s4")
        p = tierlib.write_capture(s, "droid/1.6.0/help.txt", b"a\n", ["droid", "--help"])
        shutil.copy(p + ".meta.json", os.path.join(s, "droid", "1.6.0", "models.txt.meta.json"))
        cases["orphan"] = s
        # synthetic name placed by hand
        s = fresh("s5")
        p = tierlib.write_capture(s, "droid/1.6.0/help.txt", b"a\n", ["droid", "--help"])
        os.rename(p, os.path.join(s, "droid", "1.6.0", "synthetic_help.txt"))
        os.rename(p + ".meta.json", os.path.join(s, "droid", "1.6.0", "synthetic_help.txt.meta.json"))
        cases["synthetic"] = s
        # symlink
        s = fresh("s6")
        p = tierlib.write_capture(s, "droid/1.6.0/help.txt", b"a\n", ["droid", "--help"])
        os.symlink(p, os.path.join(s, "droid", "1.6.0", "link.txt"))
        cases["symlink"] = s
        # other destination layout
        s = fresh("s7")
        os.makedirs(os.path.join(s, "elsewhere"))
        with open(os.path.join(s, "elsewhere", "x.txt"), "wb") as f:
            f.write(b"a\n")
        cases["layout"] = s
        # empty staging
        s = fresh("s8")
        os.makedirs(s)
        cases["empty"] = s
        for name, staging in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(tierlib.TierError):
                    tierlib.promote(staging, self.repo)
        self.assertEqual(os.listdir(os.path.join(self.repo, "tests", "fixtures", "real")), ["droid"])
        self.assertEqual(os.listdir(os.path.join(self.repo, "tests", "fixtures", "real", "droid")), ["1.3.0"])
        self.assertFalse(os.path.exists(os.path.join(self.repo, "tests", "smoke")))
        # Not a checkout, and a staging dir inside the target repo.
        self.stage_two()
        with self.assertRaises(tierlib.TierError):
            tierlib.promote(self.staging, os.path.join(self.tmp, "not-a-repo"))
        inside = os.path.join(self.repo, "stage")
        tierlib.write_capture(inside, "droid/1.6.0/help.txt", b"a\n", ["droid", "--help"])
        with self.assertRaises(tierlib.TierError):
            tierlib.promote(inside, self.repo)

    def test_symlinked_directories_refused(self) -> None:
        """ACC: N21 - a directory symlink inside staging, and staging reached through a symlinked alias of a dir inside the repo, are refused; nothing is written (Kiro r1 minors 3, 4)."""
        # (a) directory symlink inside staging
        s = os.path.join(self.tmp, "sdir")
        tierlib.write_capture(s, "droid/1.6.0/help.txt", b"a\n", ["droid", "--help"])
        elsewhere = os.path.join(self.tmp, "elsewhere", "1.7.0")
        tierlib.write_capture(os.path.join(self.tmp, "elsewhere-root"), "droid/1.7.0/help.txt", b"b\n", ["droid", "--help"])
        os.symlink(os.path.join(self.tmp, "elsewhere-root", "droid", "1.7.0"), os.path.join(s, "droid", "1.7.0"))
        with self.assertRaises(tierlib.TierError):
            tierlib.promote(s, self.repo)
        # (b) staging inside the repo, named through a symlinked alias outside it
        inside = os.path.join(self.repo, "stage")
        tierlib.write_capture(inside, "droid/1.6.0/help.txt", b"a\n", ["droid", "--help"])
        alias = os.path.join(self.tmp, "alias")
        os.symlink(self.repo, alias)
        with self.assertRaises(tierlib.TierError):
            tierlib.promote(os.path.join(alias, "stage"), self.repo)
        link_stage = os.path.join(self.tmp, "link-stage")
        os.symlink(inside, link_stage)
        with self.assertRaises(tierlib.TierError):
            tierlib.promote(link_stage, self.repo)
        self.assertEqual(os.listdir(os.path.join(self.repo, "tests", "fixtures", "real", "droid")), ["1.3.0"])
        self.assertFalse(os.path.exists(os.path.join(self.repo, "tests", "smoke")))
        self.assertFalse(os.path.exists(elsewhere))

    def test_destination_parent_symlink_refused(self) -> None:
        """ACC: N21 - a destination parent that resolves outside the allowed trees (symlinked fixtures/real/<adapter>) is refused before writing."""
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        os.symlink(outside, os.path.join(self.repo, "tests", "fixtures", "real", "agy"))
        tierlib.write_capture(self.staging, "agy/1.5.3/help.txt", b"a\n", ["agy", "--help"])
        with self.assertRaises(tierlib.TierError):
            tierlib.promote(self.staging, self.repo)
        self.assertEqual(os.listdir(outside), [])

    def test_cli_promote(self) -> None:
        """ACC: N21 - `python3 -m tests.tierlib promote --from S --to R` promotes and reports; a refusal returns 1."""
        self.stage_two()
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = tierlib.main(["promote", "--from", self.staging, "--to", self.repo])
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertEqual(len(json.loads(buf.getvalue())["written"]), 2)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = tierlib.main(["promote", "--from", self.staging, "--to", self.repo])
        self.assertEqual(rc, 1)
        self.assertFalse(json.loads(buf.getvalue())["ok"])


class CommittedTreesTest(unittest.TestCase):
    def test_m3_files_in_real_trees_have_matching_sidecars(self) -> None:
        """ACC: N21 - every file under tests/fixtures/real and tests/smoke/results beyond the M1 set has a sidecar with exactly {command, captured_at_utc, sha256} matching; no synthetic_* names (read-only)."""
        seen = 0
        for top, prefix in ((os.path.join(REPO_ROOT, "tests", "fixtures", "real"), ""),
                            (os.path.join(REPO_ROOT, "tests", "smoke", "results"), "results/")):
            if not os.path.isdir(top):
                continue
            for dirpath, _, filenames in os.walk(top):
                for name in filenames:
                    path = os.path.join(dirpath, name)
                    rel = prefix + os.path.relpath(path, top).replace(os.sep, "/")
                    self.assertNotIn("synthetic_", rel)
                    if rel in PRE_M3_FIXTURES:
                        continue
                    if name.endswith(tierlib.SIDECAR_SUFFIX):
                        self.assertTrue(os.path.isfile(path[: -len(tierlib.SIDECAR_SUFFIX)]), f"orphan sidecar {rel}")
                        continue
                    with self.subTest(file=rel):
                        tierlib.read_sidecar(path)
                        seen += 1
        for rel in PRE_M3_FIXTURES:
            self.assertTrue(os.path.isfile(os.path.join(REPO_ROOT, "tests", "fixtures", "real", rel)), rel)
        print(json.dumps({"m3_files_with_sidecars": seen}))


if __name__ == "__main__":
    unittest.main()
