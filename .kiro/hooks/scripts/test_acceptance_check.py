"""Regression tests for acceptance-check.py (run: python3 .kiro/hooks/scripts/test_acceptance_check.py)."""
import subprocess, sys, tempfile, unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("acceptance-check.py")
ROOT = SCRIPT.parents[3]


def run(md):
    with tempfile.NamedTemporaryFile("w", suffix=".md", dir="/tmp", delete=False) as f:
        f.write(md)
    p = subprocess.run([sys.executable, str(SCRIPT), f.name], cwd=ROOT, capture_output=True, text=True)
    Path(f.name).unlink()
    return p.returncode, p.stdout + p.stderr


class T(unittest.TestCase):
    def test_record_table_ids_skipped(self):
        rc, out = run("## Rulings\n\n| Record | Ruling |\n|---|---|\n| D54 | x |\n")
        self.assertNotIn("unknown checklist id D54", out, out)

    def test_acceptance_table_after_record_table_same_section_checked(self):
        md = ("## Rulings\n\n| Record | Ruling |\n|---|---|\n| D54 | x |\n\n"
              "| ID | Result |\n|---|---|\n| D99 | PASS |\n")
        rc, out = run(md)
        self.assertNotIn("unknown checklist id D54", out, out)
        self.assertIn("unknown checklist id D99", out, out)
        self.assertNotEqual(rc, 0, out)

    def test_unknown_letter_in_acc_span_flagged(self):
        rc, out = run("Something (ACC: Z99)\n")
        self.assertIn("Z99", out, out)


if __name__ == "__main__":
    unittest.main()
