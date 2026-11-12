"""Documentation and help-text checks for gate M1c step M1.9.

ACC: G23, G29, J7, N9, N17
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

from mesh.adapters.evidence import apply_to_probe, load_evidence
from mesh.adapters.registry import get_adapter

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN_MESH = os.path.join(ROOT, "bin", "mesh")

DISCLAIMER_SENTENCE = (
    "Mesh does not claim that executors cannot steal credentials, and does not claim that "
    "audit verify detects forgeries by a process that modified Mesh's code or captured the "
    "passphrase."
)
CONTRARY_CLAIMS = (
    "executors cannot steal",
    "Mesh sandboxes",
    "audit verify detects all",
)


def read_doc(relative_path):
    with open(os.path.join(ROOT, relative_path), encoding="utf-8") as handle:
        return handle.read()


def normalize(text):
    return " ".join(text.split())


def run_help(topic):
    home = tempfile.mkdtemp(prefix="mh-docs-")
    os.chmod(home, 0o700)
    try:
        proc = subprocess.run(
            [BIN_MESH, "--home", home, "help", topic],
            capture_output=True,
            text=True,
            timeout=120,
        )
        envelope = json.loads(proc.stdout)
        return proc.returncode, envelope
    finally:
        shutil.rmtree(home, ignore_errors=True)


class TestDocsM19(unittest.TestCase):
    """Required documentation sentences and help topics for M1.9."""

    def test_g23_relayed_approvals_and_no_sandbox_sentence(self):
        """ACC: G23 - approvals help says relayed statements are unverifiable and advises direct_only; auth help and usage.md say Mesh does not sandbox external CLIs."""
        rc_auth, auth_env = run_help("auth")
        rc_approvals, approvals_env = run_help("approvals")
        self.assertEqual(rc_auth, 0)
        self.assertEqual(rc_approvals, 0)

        auth_text = normalize(auth_env["data"]["text"])
        approvals_text = normalize(approvals_env["data"]["text"])
        usage_text = normalize(read_doc("docs/usage.md"))

        self.assertIn("Mesh cannot verify a relayed statement", approvals_text)
        self.assertIn("direct_only", approvals_text)
        self.assertIn("sensitive categories", approvals_text)

        self.assertIn("Mesh does not sandbox external CLIs", auth_text)
        self.assertIn("Mesh does not sandbox external CLIs", usage_text)

    def test_g29_same_uid_limits_and_no_false_claims(self):
        """ACC: G29 - auth help and usage.md state the same-uid limits, passphrase/token-file rules, read-only install, and do not claim executors are contained or audit verify catches code-level forgery."""
        rc_auth, auth_env = run_help("auth")
        self.assertEqual(rc_auth, 0)
        auth_text = normalize(auth_env["data"]["text"])
        usage_text = normalize(read_doc("docs/usage.md"))

        required_sentences = [
            "Mesh is not a security sandbox",
            "A same-uid process can read other agents' Mesh credentials (and forge relayed approvals) unless OS-level separation is used.",
            "Never keep the passphrase in a file and keep token files outside every workspace",
            "A same-uid process can modify Mesh's own code or read the passphrase or K_user from memory during a direct request, after which forged direct approvals carry valid MACs and audit verify cannot be trusted.",
            "Mesh recommends a read-only install outside every workspace",
            "Mesh does not claim that executors cannot steal credentials, and does not claim that audit verify detects forgeries by a process that modified Mesh's code or captured the passphrase.",
        ]
        for sentence in required_sentences:
            with self.subTest(sentence=sentence, source="help_auth"):
                self.assertIn(normalize(sentence), auth_text)
            with self.subTest(sentence=sentence, source="usage_md"):
                self.assertIn(normalize(sentence), usage_text)

        disclaimer = normalize(DISCLAIMER_SENTENCE)
        for source, text in (("help_auth", auth_text), ("usage_md", usage_text)):
            checked = text.replace(disclaimer, "").lower()
            for phrase in CONTRARY_CLAIMS:
                with self.subTest(phrase=phrase, source=source):
                    self.assertNotIn(phrase.lower(), checked)

    def test_j7_delivery_table_and_tui_limit(self):
        """ACC: J7 - usage.md has the launch/inbox/user delivery table and says Mesh cannot push into an interactive TUI."""
        usage_doc = read_doc("docs/usage.md")
        usage_text = normalize(usage_doc)

        self.assertIn("How each agent kind receives work", usage_text)
        self.assertIn("Agent kind", usage_text)
        self.assertIn("| **launch** executor", usage_text)
        self.assertIn("| **inbox** agent", usage_text)
        self.assertIn("| **user**", usage_text)

        launch_lead_rows = [
            line for line in usage_doc.splitlines() if "| **launch** lead" in line
        ]
        self.assertEqual(len(launch_lead_rows), 1)
        self.assertIn("M2", launch_lead_rows[0])
        self.assertIn("delivery=inbox", launch_lead_rows[0])
        self.assertIn("Available in M2", launch_lead_rows[0])
        self.assertIn("`fake` test double only", launch_lead_rows[0])
        self.assertIn("Vendor adapters cannot be launch-mode leads until M3", launch_lead_rows[0])
        self.assertIn("rc 13 `ADAPTER_UNSUPPORTED`", launch_lead_rows[0])
        self.assertIn("lead.turn_refused", launch_lead_rows[0])
        self.assertIn("lead for vendors", launch_lead_rows[0])

        self.assertIn("new headless run for every attempt", usage_text)
        self.assertIn("resumes the vendor session only if", usage_text)
        self.assertIn("mesh inbox wait --timeout N", usage_text)
        self.assertIn("mesh inbox list", usage_text)
        self.assertIn("mesh inbox ack <msg>", usage_text)
        self.assertIn("user inbox", usage_text)
        self.assertIn("origin agent's", usage_text)
        self.assertIn("Mesh cannot push into an interactive TUI", usage_text)

    def test_n9_status_table_for_six_vendor_clis(self):
        """ACC: N9 - adapters.md has one status word per vendor CLI, hermes/openclaw not_installed, and fake is never counted as vendor support."""
        doc = read_doc("docs/adapters.md")
        normalized = normalize(doc)

        # m3-r7b supersession (coordinator authorization 2026-11-10): droid status
        # partially_verified -> verified, from live `adapter show droid`.
        expected = {
            "droid": "verified",
            "agy": "partially_verified",
            "kiro": "partially_verified",
            "vibe": "partially_verified",
            "hermes": "not_installed",
            "openclaw": "not_installed",
        }
        expected_milestone = {
            "droid": "—",
            "agy": "M3",
            "kiro": "—",
            "vibe": "M3",
            "hermes": "M4",
            "openclaw": "M4",
        }
        allowed = {"verified", "partially_verified", "not_installed", "unsupported"}

        adapter_rows = {}
        for line in doc.splitlines():
            if not line.strip().startswith("|"):
                continue
            for adapter in expected:
                if f"(`{adapter}`)" in line:
                    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
                    adapter_rows[adapter] = cells

        self.assertEqual(set(adapter_rows), set(expected))
        for adapter, status in expected.items():
            cells = adapter_rows[adapter]
            self.assertIn(status, cells, f"{adapter} row lacks {status}: {cells}")
            status_cells = [cell for cell in cells if cell in allowed]
            self.assertEqual(
                status_cells,
                [status],
                f"{adapter} must have exactly one status word, got {status_cells}",
            )
            # m3-r7b supersession (named launch-column assertion): the milestone cell
            # now pins the live effective launch_milestone, not the class attribute;
            # "—" means adapter show printed null (launch enabled by evidence).
            with self.subTest(adapter=adapter, source="launch_milestone"):
                self.assertEqual(
                    cells[-1],
                    expected_milestone[adapter],
                    f"{adapter} row milestone does not match live adapter show: {cells}",
                )
            # m3-r7b2: tie the literal back to the code. Run the real evidence path
            # (load_evidence on the committed results + apply_to_probe, as `adapter show`
            # does) for the documented version; no launch rule is restated here.
            with self.subTest(adapter=adapter, source="effective_launch_milestone"):
                version = cells[2] if cells[2] != "—" else None
                ev = load_evidence(adapter, False, version)
                shown = apply_to_probe(
                    {"capabilities": {}, "status": status},
                    get_adapter(adapter, testing_mode=False),
                    ev,
                )
                self.assertEqual(
                    cells[-1],
                    shown["launch_milestone"] or "—",
                    f"{adapter} doc milestone != code effective milestone",
                )
                self.assertEqual(cells[4], "true" if shown["launch_enabled"] else "false")

        self.assertIn("never counted as vendor support", normalized)
        self.assertIn("test double", normalized)
        self.assertIn("not a vendor integration", normalized)

    def test_n17_inbox_participation_is_not_adapter_support(self):
        """ACC: N17 - adapters.md says Hermes/OpenClaw can join as adapter=none, delivery=inbox and labels that inbox participation, not adapter support, while their status stays not_installed."""
        normalized = normalize(read_doc("docs/adapters.md"))

        self.assertIn("Hermes", normalized)
        self.assertIn("OpenClaw", normalized)
        self.assertIn("adapter=none, delivery=inbox", normalized)
        self.assertIn("inbox participation, not adapter support", normalized)
        self.assertIn("status stays", normalized)
        self.assertIn("not_installed", normalized)

    def test_docs_describe_built_launch_execution(self):
        """M1c run 5: usage.md and adapters.md no longer say launch execution, run tokens or recovery are unbuilt, and still say vendor launching is not enabled."""
        for path in ("docs/usage.md", "docs/adapters.md"):
            text = normalize(read_doc(path))
            for stale in ("still being built in M1c", "not yet built", "do not exist yet",
                          "are not present yet", "does not issue or inject run tokens"):
                with self.subTest(path=path, phrase=stale):
                    self.assertNotIn(stale, text)
            with self.subTest(path=path, clause="fake only"):
                self.assertIn("test double", text)
                self.assertIn("ADAPTER_UNSUPPORTED", text)
        usage = normalize(read_doc("docs/usage.md"))
        self.assertIn("meshd injects a short-lived **run token**", usage)
        self.assertIn("crash recovery", usage)


if __name__ == "__main__":
    unittest.main()
