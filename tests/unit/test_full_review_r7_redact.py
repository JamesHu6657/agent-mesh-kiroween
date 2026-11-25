"""Full review r7: credential formats the redactor missed (R1-P1-1) and `sk-` matched inside
ordinary words (R1-P2-1). Stream == batch and the fixed point are re-checked on the new rules."""

import json
import unittest

from mesh.core.redact import StreamRedactor, redact

NONE = ()


def _stream(text, jsonl=False, step=3):
    sr = StreamRedactor(NONE, jsonl=jsonl)
    out = [sr.feed(text[i:i + step]) for i in range(0, len(text), step)]
    out.append(sr.flush())
    return "".join(out)


class MissedFormatsTest(unittest.TestCase):
    SECRETS = {
        "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY": "wJalrXUtnFEMI",
        "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY": "wJalrXUtnFEMI",
        "STRIPE_SECRET_KEY=sk_" "live_51Hxxxxxxxxxxxxxxxxxxxxxxxx": "sk_live_51H",
        "PRIVATE_KEY=0xabcdef0123456789abcdef": "0xabcdef",
        "GH=github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz": "github_pat_11",
        "token glpat-abcdefghij0123456789xy": "glpat-abcdef",
        "token hf_" "abcdefghijklmnopqrstuvwxyzABCDEFGH": "hf_abcdef",
        "registry npm_abcdefghijklmnopqrstuvwxyz0123456789": "npm_abcdef",
        "Authorization: Basic dXNlcjpzdXBlcnNlY3JldHBhc3N3b3Jk": "dXNlcjpz",
        "refresh_token=1//0gabcdefghijklmnop": "1//0gabc",
    }

    def test_each_format_is_redacted_batch_and_stream(self):
        for text, fragment in self.SECRETS.items():
            for jsonl in (False, True):
                with self.subTest(text=text, jsonl=jsonl):
                    out = redact(text, NONE, jsonl=jsonl)
                    self.assertNotIn(fragment, out)
                    self.assertIn("[REDACTED]", out)
                    self.assertEqual(_stream(text, jsonl), out)
                    self.assertEqual(redact(out, NONE, jsonl=jsonl), out)  # fixed point

    def test_oauth_json_members_in_run_logs(self):
        line = '{"access_token": "ya29.a0AfH6SMBxxxxxxxxxxx", "refresh_token": "1//0gxxxxxxxx", "expires_in": 3599}'
        out = redact(line, NONE, jsonl=True)
        doc = json.loads(out)
        self.assertEqual(doc["access_token"], "[REDACTED]")
        self.assertEqual(doc["refresh_token"], "[REDACTED]")
        self.assertEqual(doc["expires_in"], 3599)

    def test_keyword_and_basic_stay_readable(self):
        self.assertEqual(redact("Authorization: Basic dXNlcjpwYXNzd29yZA==", NONE),
                         "Authorization: Basic [REDACTED]")
        self.assertEqual(redact("AWS_SECRET_ACCESS_KEY=abcdefgh12345678", NONE),
                         "AWS_SECRET_ACCESS_KEY=[REDACTED]")
        # "basic" is not a credential on its own.
        self.assertEqual(redact("a basic internationalization_framework_x", NONE),
                         "a basic internationalization_framework_x")


# SkInsideWordsTest dropped: an sk- left boundary lets glued real keys leak
# (test_blind_review_unit never-leak property tests); redaction stays fail-closed.

if __name__ == "__main__":
    unittest.main()
