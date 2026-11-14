"""Unit tests for ID generation and validation per plan §5.4."""

import re
import unittest
from mesh.core.ids import BASE32_ALPHABET, PREFIXES, generate_id, is_valid_id, parse_id


class TestIDs(unittest.TestCase):
    """Test deterministic ID generation and validation."""

    def test_prefixes(self):
        """Verify all 14 expected prefixes are accepted and generated."""
        expected_prefixes = {
            "prj", "tsk", "run", "apr", "agt", "usr", "cred",
            "evt", "msg", "req", "boot", "lead", "mrg", "crit"
        }
        self.assertEqual(PREFIXES, expected_prefixes)

        for pfx in expected_prefixes:
            id_val = generate_id(pfx)
            self.assertTrue(id_val.startswith(f"{pfx}_"))
            self.assertTrue(is_valid_id(id_val, prefix=pfx))

        with self.assertRaises(ValueError):
            generate_id("invalid_pfx")

    def test_id_structure_and_length(self):
        """Verify ID structure: <prefix>_<26 chars> (10 hex + 16 base32)."""
        pattern = re.compile(r"^([a-z]+)_([0-9a-f]{10})([a-z2-7]{16})$")

        for pfx in PREFIXES:
            id_val = generate_id(pfx, ts_ms=1794038019047)
            m = pattern.match(id_val)
            self.assertIsNotNone(m, f"ID {id_val} did not match pattern")
            assert m is not None

            self.assertEqual(m.group(1), pfx)
            self.assertEqual(len(m.group(2)), 10)
            self.assertEqual(len(m.group(3)), 16)
            self.assertEqual(len(id_val), len(pfx) + 1 + 26)

            # Check base32 chars
            for char in m.group(3):
                self.assertIn(char, BASE32_ALPHABET)

    def test_validation_and_parsing(self):
        """Test is_valid_id and parse_id helpers."""
        good_id = generate_id("tsk")
        self.assertTrue(is_valid_id(good_id))
        self.assertTrue(is_valid_id(good_id, prefix="tsk"))
        self.assertFalse(is_valid_id(good_id, prefix="prj"))

        parsed = parse_id(good_id)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["prefix"], "tsk")
        self.assertEqual(len(parsed["time_hex"]), 10)
        self.assertEqual(len(parsed["random"]), 16)

        self.assertFalse(is_valid_id("tsk_123"))
        self.assertFalse(is_valid_id("invalid"))
        self.assertFalse(is_valid_id(12345))  # type: ignore
        self.assertIsNone(parse_id("bad_id"))

    def test_uniqueness(self):
        """Verify 10,000 generated IDs have zero collisions."""
        ids = {generate_id("tsk") for _ in range(10000)}
        self.assertEqual(len(ids), 10000)


if __name__ == "__main__":
    unittest.main()
