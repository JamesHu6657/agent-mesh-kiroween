"""PR51 R4/R5: redact idempotence, stored_op_json stability, and forged-marker scrubbing."""
import unittest
from unittest import mock

from mesh.core import redact as redact_mod
from mesh.core.approvals import stored_op_json, verify_stored_op_json
from mesh.core.canonical import compute_op_hash
from mesh.core.redact import redact, redact_value

KEY = "sk-a1b2c3d4e5f6g7h8i9j0k1l2m3n4"
EXACT = [("OPENAI_API_KEY", KEY)]


class TestRedactIdempotentR4(unittest.TestCase):
    def assert_fixed(self, text: str, exact=EXACT) -> str:
        once = redact(text, exact)
        self.assertEqual(redact(once, exact), once, (text, once))
        with mock.patch.object(redact_mod, "default_exact_values", return_value=list(exact)):
            op_doc, op_hash = compute_op_hash(
                op="project.update", requested_by="agt_1",
                params={"goal": text}, project_id="prj_1", category="scope_change",
            )
            stored = stored_op_json(op_doc, op_hash)
        row = {"op_json": stored, "op_type": "project.update", "requested_by": "agt_1",
               "project_id": "prj_1", "category": "scope_change"}
        self.assertEqual(verify_stored_op_json(row), op_hash, (text, stored))
        return once

    def test_assignment_with_exact_value(self) -> None:
        out = self.assert_fixed(f"configure api_key={KEY}")
        self.assertNotIn(KEY, out)
        self.assertIn("[REDACTED:OPENAI_API_KEY]", out)

    def test_env_name_assignment_and_colon(self) -> None:
        for t in (f"OPENAI_API_KEY={KEY}", f"password: {KEY}", f"x api_key={KEY} tail"):
            self.assertNotIn(KEY, self.assert_fixed(t))

    def test_armed_across_lines(self) -> None:
        for t in (f"Bearer\n{KEY}\n", f"password:\n{KEY}\n", f"OPENAI_API_KEY=\n{KEY}\n"):
            self.assertNotIn(KEY, self.assert_fixed(t))

    def test_unbroken_line_marker(self) -> None:
        out = self.assert_fixed("password = " + "x" * 70000, ())
        self.assertNotIn("[REDACTED] output]", out)

    def test_quoted_whitespace_secret_and_pem_key_stay_valid(self) -> None:
        out = self.assert_fixed('password="my secret value"', [("DB_PASSWORD", "my secret value")])
        self.assertEqual(out, "password=[REDACTED:DB_PASSWORD]")
        pem = 'api_key="-----BEGIN RSA PRIVATE KEY----- MIIEowIBAAKCAQEA -----END RSA PRIVATE KEY-----"'
        self.assertEqual(self.assert_fixed(pem, ()), "api_key=[REDACTED]")

    def test_forged_marker_cannot_preserve_secret(self) -> None:
        for forged, secret in (
            ("password = [REDACTED:FOO]hunter2", "hunter2"),
            ("password = [REDACTED:MyRealPassword123]", "MyRealPassword123"),
            ("api_key=[REDACTED:eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sig]", "eyJhbGci"),
            ("Bearer\n[REDACTED:eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sig]", "eyJhbGci"),
            ("password = [REDACTED:secret_hunter2]", "hunter2"),
        ):
            out = redact(forged, ())
            self.assertNotIn(secret, out, (forged, out))
        out = redact("api_key=[REDACTED] hunter2", ())
        self.assertNotIn("hunter2", out.split("[REDACTED]")[0])

    def test_marker_alone_untouched(self) -> None:
        for t in ("api_key=[REDACTED]", "password: [REDACTED:unbroken output]", "token [REDACTED:X_Y]"):
            self.assertEqual(redact(t, ()), t)

    def test_redact_value_fixed_point(self) -> None:
        doc = {"goal": f"configure api_key={KEY}", "argv": ["--token", f"Bearer {KEY}"]}
        once = redact_value(doc, EXACT)
        self.assertEqual(redact_value(once, EXACT), once)


if __name__ == "__main__":
    unittest.main()
