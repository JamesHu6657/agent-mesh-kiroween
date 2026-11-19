"""Regression tests for full2-r2 Group E findings (P1-7, P2-5) in mesh/core/redact.py."""

import json
import unittest

from mesh.core.approvals import stored_op_json, verify_stored_op_json
from mesh.core.canonical import compute_op_hash
from mesh.core.redact import (
    _MAX_JSON_DEPTH,
    StreamRedactor,
    redact,
    redact_json_text,
    redact_value,
    register_secret,
    unregister_secret,
)


def _both(text: str, exact=None, *, jsonl: bool = True) -> str:
    exact = list(exact or [])
    batch = redact(text, exact, jsonl=jsonl)
    for step in (1, 3, 17):
        sr = StreamRedactor(exact, jsonl=jsonl)
        streamed = "".join(sr.feed(text[i:i + step]) for i in range(0, len(text), step)) + sr.flush()
        assert streamed == batch, (step, streamed, batch)
    return batch


class TestP1_7JsonStateArming(unittest.TestCase):
    """P1-7: cross-element / cross-line 'kw' and 'tok' state in JSON/JSONL redaction."""

    def test_kw_armed_across_json_elements(self) -> None:
        cases = (
            '["password", ": secret123"]',
            '["password", "   ", ": secret123"]',
            '["password", ":", "secret123"]',
            '["password", "=secret123"]',
            '{"a": "password", "b": ": secret123"}',
            '{"a": "password", "password": "", "b": ": secret123"}',
        )
        for raw in cases:
            with self.subTest(raw=raw):
                out = _both(raw + "\n", jsonl=True)
                self.assertNotIn("secret123", out)
                json.loads(out)
                self.assertEqual(_both(out, jsonl=True), out)

    def test_kw_does_not_over_redact_non_assign_or_filled_field(self) -> None:
        safe1 = '["password", "hello", ": safe_val"]\n'
        self.assertEqual(_both(safe1, jsonl=True), safe1)
        safe2 = '{"a": "password", "password": "secret123", "b": ": safe_val"}\n'
        out2 = _both(safe2, jsonl=True)
        self.assertNotIn("secret123", out2)
        self.assertIn(": safe_val", out2)

    def test_cross_line_kw_and_tok_with_jsonl(self) -> None:
        cases = (
            'password\n[": secret123"]\n',
            'password\n{"v": ": secret123"}\n',
            'password\n[":"]\nsecret123\n',
            '["password"]\n[": secret123"]\n',
            '["password", ":"]\nsecret123\n',
            '["password"]\n: secret123\n',
            'password:\n{"v": "secret123"}\n',
        )
        for raw in cases:
            with self.subTest(raw=raw):
                out = _both(raw, jsonl=True)
                self.assertNotIn("secret123", out)
                for line in out.splitlines():
                    if line.lstrip().startswith(("{", "[")) and line.strip() != "[REDACTED]":
                        json.loads(line)
                self.assertEqual(_both(out, jsonl=True), out)

    def test_tok_armed_element_preserves_trailing_new_arming_or_key_block(self) -> None:
        cases = (
            '["password:", "tok1\\npassword:", "secret2"]',
            '["password:", "tok1 password:", "secret2"]',
            '["password:", "tok1\\npassword", ": secret2"]',
            '{"a": "password:", "b": "tok1 password:", "c": "secret2"}',
            '["password:", "tok1\\n-----BEGIN PRIVATE KEY-----\\nbody\\n-----END PRIVATE KEY-----\\npassword:", "secret2"]',
            '["-----BEGIN PRIVATE KEY-----", "tok1_body\\n-----END PRIVATE KEY-----\\npassword:", "secret2"]',
            '["password:", "{\\"a\\": \\"tok1\\", \\"b\\": \\"password:\\"}", "secret2"]',
            'password:\n{"a": "tok1\\npassword:"}\nsecret2\n',
        )
        for raw in cases:
            with self.subTest(raw=raw):
                out = _both(raw + "\n", jsonl=True)
                self.assertNotIn("tok1", out)
                self.assertNotIn("secret2", out)
                for line in out.splitlines():
                    if line.lstrip().startswith(("{", "[")) and line.strip() != "[REDACTED]":
                        json.loads(line)
                self.assertEqual(_both(out, jsonl=True), out)

    def test_max_json_depth_propagates_kw_state(self) -> None:
        inner = json.dumps([":"])
        for _ in range(_MAX_JSON_DEPTH):
            inner = json.dumps([inner])
        raw = f"password\n{inner}\nsecret_after_deep\n"
        out = _both(raw, jsonl=True)
        self.assertNotIn("secret_after_deep", out)
        self.assertEqual(_both(out, jsonl=True), out)

    def test_redact_json_text_handles_kw_and_trailing_arming(self) -> None:
        raw = json.dumps(["password", ": secret123"])
        out = redact_json_text(raw, exact_values=[])
        self.assertNotIn("secret123", out)
        self.assertEqual(json.loads(out), ["password", ": [REDACTED]"])

        raw2 = json.dumps(["password:", "tok1\npassword:", "secret2"])
        out2 = redact_json_text(raw2, exact_values=[])
        self.assertNotIn("tok1", out2)
        self.assertNotIn("secret2", out2)


class TestP2_5RedactionIdempotence(unittest.TestCase):
    """P2-5: unregistered run_token:<id> markers and trailing JSON punctuation in _is_marker_val."""

    def test_unregistered_run_token_label_stays_idempotent(self) -> None:
        run_id = "run_0123456789abcdefghijklmnop"
        label = f"run_token:{run_id}"
        tok = "mesh_rt_supersecrettokenvalue123456"
        plain_tok = "tok_run_secret_value_123456"
        register_secret(label, tok)
        register_secret("run_token:run-alt_2", plain_tok)
        try:
            samples = [
                (f"password={tok}", False, label, tok),
                (f"Bearer {tok}", False, label, tok),
                (f"Authorization: Basic {tok}", False, label, tok),
                (json.dumps({"msg": f"set password={tok}"}), True, label, tok),
                (json.dumps({"auth": f"Bearer {tok}"}), True, label, tok),
                (json.dumps({"val": tok}), True, label, tok),
                (json.dumps({"url": f"https://u:{plain_tok}@example.com"}), True, "run_token:run-alt_2", plain_tok),
                (f'INFO payload={{"api_key": "{plain_tok}"}}', True, "run_token:run-alt_2", plain_tok),
            ]
            first_pass = [(redact(txt, jsonl=jl), jl, lab, secret) for txt, jl, lab, secret in samples]
        finally:
            unregister_secret(label)
            unregister_secret("run_token:run-alt_2")

        for once, jl, lab, secret in first_pass:
            self.assertNotIn(secret, once)
            self.assertIn(f"[REDACTED:{lab}]", once)
            # Re-redacting with default env (token unregistered) or empty exact_values
            # under both jsonl=True and jsonl=False must preserve the marker and valid JSON.
            for exact in (None, ()):
                for jl2 in (jl, False, True):
                    again = redact(once, exact_values=exact, jsonl=jl2)
                    self.assertEqual(again, once, (once, exact, jl2, again))
                    if jl and not once.startswith("INFO "):
                        json.loads(again)

    def test_run_token_marker_with_approvals_stored_op_json(self) -> None:
        label = "run_token:run_0123456789abcdefghijklmnop"
        tok = "mesh_rt_opjsonsecretvalue12345678"
        register_secret(label, tok)
        try:
            op_doc, op_hash = compute_op_hash(
                op="project.update",
                requested_by="agt_1",
                params={"goal": f"use password={tok}"},
                project_id="prj_1",
                category="scope_change",
            )
            stored = stored_op_json(op_doc, op_hash)
        finally:
            unregister_secret(label)
        self.assertNotIn(tok, stored)
        row = {
            "op_json": stored,
            "op_type": "project.update",
            "requested_by": "agt_1",
            "project_id": "prj_1",
            "category": "scope_change",
        }
        self.assertEqual(verify_stored_op_json(row), op_hash)
        # Re-redacting an already-redacted op_doc after unregister preserves the marker.
        inner = json.loads(stored)["op"]
        self.assertEqual(redact_value(inner, exact_values=()), inner)

    def test_forged_run_token_and_glued_secrets_still_redacted(self) -> None:
        for text, secret in (
            ("password=[REDACTED:run_token:run_1]hunter2", "hunter2"),
            ("password=[REDACTED:run_token:run_1]}hunter2", "hunter2"),
            ("password=[REDACTED]hunter2", "hunter2"),
            ('password=[REDACTED]"hunter2', "hunter2"),
            ("password=[REDACTED])hunter2", "hunter2"),
        ):
            out = redact(text, ())
            self.assertNotIn(secret, out, (text, out))

        for bad_label in (
            "password=[REDACTED:run_token:bad/slash]",
            "password=[REDACTED:run_token:]",
            "password=[REDACTED:custom_forged_label]",
        ):
            self.assertEqual(redact(bad_label, ()), "password=[REDACTED]", bad_label)

    def test_jsonl_to_default_preserves_trailing_closing_quotes_and_brackets(self) -> None:
        cases = (
            '{"type":"test","msg":"set password=hunter22"}',
            '{"msg":"Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"}',
            '{"msg":"Authorization: Basic dXNlcjpzZWNyZXQ="}',
            '{"msg":"password: hunter22", "ok": 1}',
        )
        for raw in cases:
            with self.subTest(raw=raw):
                once = _both(raw + "\n", jsonl=True)
                self.assertNotIn("hunter22", once)
                json.loads(once)
                # Default jsonl=False pass (e.g., MeshError / record_event / logfile) must not eat '"}'
                twice = _both(once, jsonl=False)
                self.assertEqual(twice, once)
                json.loads(twice)

        for punct_case in (
            "(password=[REDACTED])",
            "password=[REDACTED];",
            "password=[REDACTED], ok",
            "password=[REDACTED]`",
            '{"msg":"password: [REDACTED:unbroken output]"}',
        ):
            with self.subTest(punct_case=punct_case):
                self.assertEqual(_both(punct_case, jsonl=False), punct_case)

        run_tok_json = '["password:", "  [REDACTED:run_token:run_1]  "]\n'
        self.assertEqual(_both(run_tok_json, jsonl=True), run_tok_json)
        self.assertEqual(
            redact_json_text('{"msg": "password=[REDACTED:run_token:run_1]"}', exact_values=[]),
            '{"msg": "password=[REDACTED:run_token:run_1]"}',
        )


if __name__ == "__main__":
    unittest.main()
