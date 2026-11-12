"""Unit test verifying real fixture hashes against plan §14.11.

ACC: N15
"""

import hashlib
import os
import unittest

FIXTURE_HASHES = {
    "droid/1.3.0/help.txt": "1df2e88e997f1488d22fa59340bc79c13198a143363981ee8825a641260143f3",
    "droid/1.3.0/exec-help.txt": "61880b6d4c40e86e497653e95f01beb3a7cdad223a4fd98010d53d6dce30449a",
    "droid/1.3.0/exec-init-event-opus55-high.json": "e03f9ec3ee4114856c9828a83be463ef8f4d58b41e5a131740d77699d22bd535",
    "agy/1.5.2/help.txt": "e92946f85cec63eca61f93014ca49f53b1e84277eac7954e7fac7d8d712940fd",
    "agy/1.5.2/models.txt": "4bdb4f0f081a521804aca90f92132965597e365f10a2afad10f7d5e70d3a4b83",
    "kiro/3.1.0/help.txt": "353d58ab2ccd44f36372cf16a00a3d051519ccaf4778b5269d6dded4622d17d5",
    "kiro/3.1.0/chat-help.txt": "56bf95560094d7bee17372d18b8fc40ff834ff6b72c338863f91428719b00eeb",
    "kiro/3.1.0/list-models.json": "bdc99135b5a3de929c857c392b25c9c0fe548b56f1bdd7a9eb12dc5a7533dbae",
    "kiro/3.1.0/effort-high-run-metadata.jsonl": "41957ed021f3994b1b2c50ba2c293b0a39db505e83ae407063fd043be0ee65fc",
    "vibe/3.1.0/help.txt": "15d858a3434d0664ee8af445933ef438bc7a6efac9961de31351013c0d2ee158",
    "hermes/not-installed.txt": "2a149e1329f638ffd7e6e5799cffac9ac6f671d92e56b24dff3926739503a154",
    "openclaw/not-installed.txt": "a102d301e2adc9402c676263c6807ee218d02a667a79f69e0962c985a980775e",
}


class TestFixtureHashes(unittest.TestCase):
    """Test fixture hashes match plan §14.11 exactly. ACC: N15"""

    def test_fixture_hashes(self):
        """Verify each committed fixture file matches plan §14.11 sha256. ACC: N15"""
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        fixtures_dir = os.path.join(repo_root, "tests", "fixtures", "real")

        mismatches = []
        for rel_path, expected_hash in FIXTURE_HASHES.items():
            full_path = os.path.join(fixtures_dir, rel_path)
            if not os.path.isfile(full_path):
                mismatches.append((rel_path, expected_hash, f"FILE_MISSING: {full_path}"))
                continue
            with open(full_path, "rb") as f:
                content = f.read()
            actual_hash = hashlib.sha256(content).hexdigest()
            if actual_hash != expected_hash:
                mismatches.append((rel_path, expected_hash, actual_hash))

        if mismatches:
            report_lines = ["FIXTURE HASH MISMATCH:"]
            for rel_path, expected, actual in mismatches:
                report_lines.append(f"  File: {rel_path}\n    Expected: {expected}\n    Actual:   {actual}")
            msg = "\n".join(report_lines)
            self.fail(f"Stop and report: fixture hash test failed!\n{msg}")


if __name__ == "__main__":
    unittest.main()
