"""Regression tests for full2-r3 Group E findings (P1-8, P2-10 redact._ser)."""

import json
import unittest

from mesh.core.approvals import stored_op_json, verify_stored_op_json
from mesh.core.canonical import compute_op_hash
from mesh.core.redact import (
    LINE_CAP,
    StreamRedactor,
    collect_exact_values,
    redact,
    redact_json_text,
    redact_value,
    register_secret,
    unregister_secret,
)
from mesh.errors import MeshError


def _both(text: str, exact=None, *, jsonl: bool = True) -> str:
    exact = list(exact or [])
    batch = redact(text, exact, jsonl=jsonl)
    for step in (1, 3, 17, 1024):
        sr = StreamRedactor(exact, jsonl=jsonl)
        streamed = "".join(sr.feed(text[i:i + step]) for i in range(0, len(text), step)) + sr.flush()
        assert streamed == batch, (step, streamed[:200], batch[:200])
    return batch


class TestP1_8WalkJsonDocSingleTokenShortCircuit(unittest.TestCase):
    """P1-8 (1): _walk_json_doc single-token short-circuit must preserve trailing re-arm."""

    def test_compact_nested_json_and_escaped_nl_rearm_next_element(self) -> None:
        cases = (
            ('["password:", "{\\"a\\":\\"tok1\\",\\"b\\":\\"password:\\"}", "SECRET_2"]', ("tok1", "SECRET_2")),
            ('["password:", "{\\"a\\":\\"tok1\\",\\"b\\":\\"password\\"}", ": SECRET_2"]', ("tok1", "SECRET_2")),
            ('["password:", "tok1\\\\npassword:", "SECRET_3"]', ("tok1", "SECRET_3")),
            ('["password:", "tok1\\\\u0020password", ": SECRET_4"]', ("tok1", "SECRET_4")),
            ('{"a": "password:", "b": "{\\"x\\":\\"tok1\\",\\"y\\":\\"password:\\"}", "c": "SECRET_5"}', ("tok1", "SECRET_5")),
        )
        for raw, secrets in cases:
            with self.subTest(raw=raw):
                out = _both(raw + "\n", jsonl=True)
                for sec in secrets:
                    self.assertNotIn(sec, out)
                json.loads(out)
                self.assertEqual(_both(out, jsonl=True), out)

    def test_single_token_with_escaped_unclosed_key_block_arms_inkey(self) -> None:
        begin_esc = "-----BEGIN\\u0020PRIVATE\\u0020KEY-----"
        end_line = "-----END " + "PRIVATE KEY-----"
        raw = f'["password:", "tok1\\n{begin_esc}", "BODY_SECRET", "{end_line}", "safe_after"]\n'
        out = _both(raw, jsonl=True)
        self.assertNotIn("tok1", out)
        self.assertNotIn("BODY_SECRET", out)
        self.assertIn("safe_after", out)
        json.loads(out)

    def test_redact_json_text_preserves_trailing_rearm_on_single_token(self) -> None:
        raw = json.dumps(["password:", '{"a":"tok1","b":"password:"}', "SECRET_2"])
        out = redact_json_text(raw, exact_values=[])
        self.assertNotIn("tok1", out)
        self.assertNotIn("SECRET_2", out)
        json.loads(out)


class TestP1_8PassphraseHeadersAndRedactValue(unittest.TestCase):
    """P1-8 (2): passphrase, auth schemes, split JSON headers, and redact_value(..., jsonl=True)."""

    def test_mesh_passphrase_env_and_keyword_redacted(self) -> None:
        phrase = "my_test_" + "phrase_value_00"
        pairs = collect_exact_values(env={"MESH_PASSPHRASE": phrase})
        self.assertEqual(pairs, [("MESH_PASSPHRASE", phrase)])
        out_env = _both(f"using {phrase} ok", exact=pairs, jsonl=False)
        self.assertIn("[REDACTED:MESH_PASSPHRASE]", out_env)
        self.assertNotIn(phrase, out_env)

        for jsonl in (False, True):
            for text in (
                "passphrase=my_secret_pass_123",
                "passphrase: my_secret_pass_123",
                "passphrase:\nmy_secret_pass_123\n",
                "passphrase\n: my_secret_pass_123\n",
            ):
                with self.subTest(jsonl=jsonl, text=text):
                    out = _both(text, jsonl=jsonl)
                    self.assertNotIn("my_secret_pass_123", out)
                    self.assertIn("[REDACTED]", out)

        jout = _both('{"passphrase": "my_secret_pass_123", "ok": 1}\n', jsonl=True)
        self.assertEqual(json.loads(jout), {"passphrase": "[REDACTED]", "ok": 1})

    def test_auth_schemes_and_proxy_authorization_inline_and_cross_line(self) -> None:
        samples = (
            ("Authorization: Token deadbeef12345678", "deadbeef12345678", "Authorization: Token [REDACTED]"),
            ("Authorization: ApiKey mykey_secret_1234", "mykey_secret_1234", "Authorization: ApiKey [REDACTED]"),
            ("Authorization: Digest dXNlcjpyZWFsbQ==", "dXNlcjpyZWFsbQ==", "Authorization: Digest [REDACTED]"),
            ("Authorization: Bearer short6", "short6", "Authorization: Bearer [REDACTED]"),
            ("Proxy-Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz", "Proxy-Authorization: Basic [REDACTED]"),
            ("Proxy-Authorization: Bearer proxytok1234567890", "proxytok1234567890", "Proxy-Authorization: Bearer [REDACTED]"),
            ("Authorization: Token\ndeadbeef12345678\n", "deadbeef12345678", "Authorization: Token\n[REDACTED]\n"),
            ("Authorization: ApiKey\nmykey_secret_1234\n", "mykey_secret_1234", "Authorization: ApiKey\n[REDACTED]\n"),
            ("Authorization: Digest\ndXNlcjpyZWFsbQ==\n", "dXNlcjpyZWFsbQ==", "Authorization: Digest\n[REDACTED]\n"),
            ("Proxy-Authorization: Basic\ndXNlcjpwYXNz\n", "dXNlcjpwYXNz", "Proxy-Authorization: Basic\n[REDACTED]\n"),
        )
        for raw, secret, expected in samples:
            for jsonl in (False, True):
                with self.subTest(raw=raw, jsonl=jsonl):
                    out = _both(raw, jsonl=jsonl)
                    self.assertNotIn(secret, out)
                    self.assertEqual(out, expected)
                    self.assertEqual(_both(out, jsonl=jsonl), out)

        # Boundary checks still reject non-boundary prefixes.
        for safe in ("X-Authorization: Basic\nhello\n", "myproxy-authorization: Basic\nhello\n"):
            self.assertEqual(_both(safe, jsonl=True), safe)

        # Glued second Proxy-Authorization after long token must still be caught.
        glued = "Proxy-Authorization: Basic firsttok;Proxy-Authorization:Digest secondsecret\n"
        out_glued = _both(glued, jsonl=False)
        self.assertNotIn("firsttok", out_glued)
        self.assertNotIn("secondsecret", out_glued)

    def test_json_split_headers_and_redact_value_jsonl(self) -> None:
        headers_doc = {
            "Authorization": "Basic dXNlcjpwYXNz",
            "Proxy-Authorization": "Token deadbeef1234",
            "Cookie": "sid=supersecretcookie",
            "Set-Cookie": "sid=supersecretsetcookie; HttpOnly",
            "passphrase": "user_passphrase_val",
            "api_key": ["arr_secret_1", {"nested": "arr_secret_2"}],
            "empty_cred": {"password": ""},
            "valid_run": {"token": "[REDACTED:run_token:run_1]"},
            "forged": {"password": "[REDACTED:forged_bad]"},
            "count_cred": {"token": 42, "active": False},
            "ok": "safe_value",
        }
        line = json.dumps(headers_doc) + "\n"
        out = _both(line, jsonl=True)
        for sec in (
            "dXNlcjpwYXNz",
            "deadbeef1234",
            "supersecretcookie",
            "supersecretsetcookie",
            "user_passphrase_val",
            "arr_secret_1",
            "arr_secret_2",
            "forged_bad",
        ):
            self.assertNotIn(sec, out)
        self.assertIn("safe_value", out)
        parsed = json.loads(out)
        self.assertEqual(parsed["Authorization"], "[REDACTED]")
        self.assertEqual(parsed["Proxy-Authorization"], "[REDACTED]")
        self.assertEqual(parsed["Cookie"], "[REDACTED]")
        self.assertEqual(parsed["Set-Cookie"], "[REDACTED]")
        self.assertEqual(parsed["passphrase"], "[REDACTED]")
        self.assertEqual(parsed["api_key"], ["[REDACTED]", {"nested": "[REDACTED]"}])
        self.assertEqual(parsed["empty_cred"], {"password": ""})
        self.assertEqual(parsed["valid_run"], {"token": "[REDACTED:run_token:run_1]"})
        self.assertEqual(parsed["forged"], {"password": "[REDACTED]"})
        self.assertEqual(parsed["count_cred"], {"token": 42, "active": False})

        # Prefixed embedded JSON line with split headers under jsonl=True.
        prefixed = "INFO payload=" + json.dumps({
            "Authorization": "Basic dXNlcjpwYXNz",
            "Proxy-Authorization": "Token deadbeef1234",
            "Cookie": "sid=supersecretcookie",
            "Set-Cookie": "sid=supersecretsetcookie; HttpOnly",
            "passphrase": "user_passphrase_val",
            "ok": "safe_value",
        }) + "\n"
        out_prefixed = _both(prefixed, jsonl=True)
        for sec in ("dXNlcjpwYXNz", "deadbeef1234", "supersecretcookie", "supersecretsetcookie", "user_passphrase_val"):
            self.assertNotIn(sec, out_prefixed)
        self.assertIn("safe_value", out_prefixed)
        json.loads(out_prefixed[len("INFO payload="):])

        red_val = redact_value(headers_doc, exact_values=(), jsonl=True)
        self.assertEqual(red_val["Authorization"], "[REDACTED]")
        self.assertEqual(red_val["Proxy-Authorization"], "[REDACTED]")
        self.assertEqual(red_val["Cookie"], "[REDACTED]")
        self.assertEqual(red_val["Set-Cookie"], "[REDACTED]")
        self.assertEqual(red_val["passphrase"], "[REDACTED]")
        self.assertEqual(red_val["api_key"], ["[REDACTED]", {"nested": "[REDACTED]"}])
        self.assertEqual(red_val["empty_cred"], {"password": ""})
        self.assertEqual(red_val["valid_run"], {"token": "[REDACTED:run_token:run_1]"})
        self.assertEqual(red_val["forged"], {"password": "[REDACTED]"})
        self.assertEqual(red_val["count_cred"], {"token": 42, "active": False})
        self.assertEqual(red_val["ok"], "safe_value")

        # Default redact_value(jsonl=False) does not blank plain values merely by dict key name.
        self.assertEqual(redact_value({"Cookie": "plain_val"}, exact_values=()), {"Cookie": "plain_val"})


class TestP1_8SecondaryRedactionJsonPreservation(unittest.TestCase):
    """P1-8 (3): escaped tails and untrusted env-secret markers must not corrupt JSON."""

    def test_escaped_tails_after_valid_marker_preserve_json(self) -> None:
        tok = "BearerTokValue0123456789"
        for prefix in ("Bearer ", "Authorization: Basic ", "Authorization: Token ", "Proxy-Authorization: Basic ", "password="):
            doc = {"op": {"hdr": f"{prefix}{tok}\nretry"}, "ok": 1}
            once_val = redact_value(doc, exact_values=())
            self.assertNotIn(tok, json.dumps(once_val))
            serialized = json.dumps(once_val)
            for jl in (False, True):
                for line in (serialized, "INFO payload=" + serialized):
                    with self.subTest(prefix=prefix, jl=jl, line=line):
                        twice = _both(line, exact=(), jsonl=jl)
                        self.assertEqual(twice, line)
                        json_part = twice[len("INFO payload="):] if twice.startswith("INFO ") else twice
                        self.assertEqual(json.loads(json_part), once_val)

        # Nested JSON string ending with escaped quote and newline.
        nested = json.dumps({"inner": json.dumps({"msg": "Bearer [REDACTED]\nretry"})})
        self.assertEqual(_both(nested, exact=(), jsonl=False), nested)
        self.assertEqual(json.loads(nested)["inner"], json.dumps({"msg": "Bearer [REDACTED]\nretry"}))

        # Control unicode escape \u000a after marker is preserved, whereas \u0073 ('s') is scrubbed.
        ctrl_json = '{"msg": "Bearer [REDACTED]\\u000aretry", "ok": 1}'
        self.assertEqual(_both(ctrl_json, exact=(), jsonl=False), ctrl_json)
        self.assertNotIn("secret", _both('{"msg": "password=[REDACTED]\\u0073ecret"}', exact=(), jsonl=False))
        self.assertNotIn("hunter2", _both('password=[REDACTED]\\"hunter2', exact=(), jsonl=False))

    def test_stored_op_json_with_newline_after_bearer_verifies(self) -> None:
        op_doc, op_hash = compute_op_hash(
            op="project.update",
            requested_by="agt_1",
            params={"goal": "Authorization: Bearer " + "B" * 24 + "\nretry"},
            project_id="prj_1",
            category="scope_change",
        )
        stored = stored_op_json(op_doc, op_hash)
        row = {
            "op_json": stored,
            "op_type": "project.update",
            "requested_by": "agt_1",
            "project_id": "prj_1",
            "category": "scope_change",
        }
        self.assertEqual(verify_stored_op_json(row), op_hash)
        re_redacted = redact(stored, exact_values=(), jsonl=False)
        self.assertEqual(re_redacted, stored)
        json.loads(re_redacted)

    def test_untrusted_env_label_scrubbed_without_eating_json_delimiters(self) -> None:
        mesh_tok = "mesh_rt_" + "Z" * 24
        exact = [("MESH_TOKEN", mesh_tok)]
        cases = (
            json.dumps({"type": "tool", "msg": f"token={mesh_tok}", "ok": 1}),
            json.dumps({"type": "tool", "msg": f"Bearer {mesh_tok}\nretry", "ok": 1}),
            json.dumps({"type": "tool", "msg": f"Authorization: Basic {mesh_tok}", "ok": 1}),
            json.dumps({"type": "tool", "msg": f"Proxy-Authorization: Token {mesh_tok}", "ok": 1}),
        )
        for raw in cases:
            first = _both(raw + "\n", exact=exact, jsonl=True)
            self.assertIn("[REDACTED:MESH_TOKEN]", first)
            self.assertNotIn(mesh_tok, first)
            for jl2 in (False, True):
                for candidate in (first, "INFO " + first):
                    with self.subTest(jl2=jl2, candidate=candidate):
                        second = _both(candidate, exact=(), jsonl=jl2)
                        self.assertNotIn("MESH_TOKEN", second)
                        json_part = second[len("INFO "):] if second.startswith("INFO ") else second
                        doc = json.loads(json_part)
                        self.assertEqual(doc["ok"], 1)
                        self.assertEqual(_both(second, exact=(), jsonl=jl2), second)

        # Prefixed embedded JSON line with double-escaped newline after untrusted label.
        dbl = 'INFO payload={"msg": "Bearer [REDACTED:MESH_TOKEN]\\\\nretry", "ok": 1}\n'
        dbl_out = _both(dbl, exact=(), jsonl=True)
        self.assertEqual(dbl_out, 'INFO payload={"msg": "Bearer [REDACTED]\\\\nretry", "ok": 1}\n')
        self.assertEqual(json.loads(dbl_out[len("INFO payload="):])["ok"], 1)

        # Forged marker with glued non-punctuation content must still redact the glued secret.
        for forged, secret in (
            ("password=[REDACTED:MESH_TOKEN]hunter2", "hunter2"),
            ("password=[REDACTED:SECRET_XYZ]hunter2", "hunter2"),
            ("password=[REDACTED:SECRET_XYZ]", "SECRET_XYZ"),
            ("password=[REDACTED:secret_hunter2]", "hunter2"),
        ):
            out = _both(forged, exact=(), jsonl=False)
            self.assertNotIn(secret, out)


class TestP1_8MeshErrorEnvelopeData(unittest.TestCase):
    """P1-8 (4): MeshError.to_envelope must redact self.data via redact_value."""

    def test_to_envelope_redacts_data_without_mutating_self_data(self) -> None:
        reg_val = "registered_err_" + "data_val_98765"
        register_secret("ERR_SECRET", reg_val)
        try:
            orig_data = {
                "cmd": "run passphrase=topsecret_val",
                "items": [f"Bearer {'X' * 24}", reg_val],
                "nested": {"hdr": "Proxy-Authorization: Basic dXNlcjpwYXNz"},
                "url": "https://bob:pw12345678@h/x",
            }
            err = MeshError(
                "INTERNAL_ERROR",
                message="fail password=msg_secret",
                details={"d": "password=det_secret"},
                data=orig_data,
            )
            env = err.to_envelope("req_e1")
        finally:
            unregister_secret("ERR_SECRET")

        dumped = json.dumps(env["data"])
        for sec in ("topsecret_val", "X" * 24, reg_val, "dXNlcjpwYXNz"):
            self.assertNotIn(sec, dumped)
        self.assertIn("[REDACTED:ERR_SECRET]", dumped)
        # Preserve r1 jsonl=False default-rule contract on non-jsonl URLs in data.
        self.assertEqual(env["data"]["url"], "https://bob:pw12345678@h/x")
        # Original self.data dict is not mutated in place.
        self.assertIn("topsecret_val", orig_data["cmd"])

        env_none = MeshError("INVALID_ARGUMENT", message="bad").to_envelope("req_e2")
        self.assertNotIn("data", env_none)


class TestP2_10SerUtf8AndLoneSurrogates(unittest.TestCase):
    """P2-10: redact._ser uses ensure_ascii=False while escaping lone surrogates."""

    def test_cjk_and_emoji_jsonl_lines_do_not_inflate_past_line_cap(self) -> None:
        cjk_text = "中" * 12000
        raw_cjk = json.dumps({"type": "result", "result": cjk_text, "debug": "password=hunter2"}, ensure_ascii=False)
        self.assertLess(len(raw_cjk), LINE_CAP)
        out_cjk = _both(raw_cjk + "\n", jsonl=True)
        self.assertLessEqual(len(out_cjk), LINE_CAP)
        self.assertNotIn("hunter2", out_cjk)
        doc_cjk = json.loads(out_cjk)
        self.assertEqual(doc_cjk["result"], cjk_text)
        self.assertEqual(doc_cjk["debug"], "password=[REDACTED]")

        emoji_text = "\U0001f511" * 7000
        raw_emoji = json.dumps({"type": "result", "result": emoji_text, "api_key": "secret123"}, ensure_ascii=False)
        self.assertLess(len(raw_emoji), LINE_CAP)
        out_emoji = _both(raw_emoji + "\n", jsonl=True)
        self.assertLessEqual(len(out_emoji), LINE_CAP)
        self.assertNotIn("secret123", out_emoji)
        doc_emoji = json.loads(out_emoji)
        self.assertEqual(doc_emoji["result"], emoji_text)
        self.assertEqual(doc_emoji["api_key"], "[REDACTED]")
        out_emoji.encode("utf-8")

        # CJK keys also stay compact without \uXXXX inflation.
        raw_key = json.dumps({"键" * 12000: "ok", "password": "secret"}, ensure_ascii=False)
        out_key = _both(raw_key + "\n", jsonl=True)
        self.assertLessEqual(len(out_key), LINE_CAP)
        self.assertNotIn("secret", out_key)
        self.assertEqual(json.loads(out_key)["password"], "[REDACTED]")

    def test_lone_surrogates_in_keys_values_and_redact_json_text_stay_utf8_safe(self) -> None:
        raw = '{"msg": "中文 bad \\ud83d", "k_\\udc00": "ok", "api_key": "secret123"}\n'
        out = _both(raw, jsonl=True)
        out.encode("utf-8")
        self.assertNotIn("secret123", out)
        doc = json.loads(out)
        self.assertEqual(doc["api_key"], "[REDACTED]")
        self.assertEqual(doc["msg"], "中文 bad \ud83d")
        self.assertEqual(doc["k_\udc00"], "ok")

        raw_pretty = '{"msg": "中文 \\ud83d", "api_key": "secret123"}'
        out_pretty = redact_json_text(raw_pretty, exact_values=[])
        out_pretty.encode("utf-8")
        self.assertNotIn("secret123", out_pretty)
        self.assertIn("中文", out_pretty)
        self.assertEqual(json.loads(out_pretty)["api_key"], "[REDACTED]")


if __name__ == "__main__":
    unittest.main()
