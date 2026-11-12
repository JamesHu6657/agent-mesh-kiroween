"""Unit tests for canonical JSON and op hash calculation.

ACC: G22
"""

import unittest
from mesh.core.canonical import (
    canonical_json,
    check_no_floats,
    compute_op_hash,
    hash_canonical,
)


class TestCanonical(unittest.TestCase):
    """Test canonical JSON serialization and op hashing. ACC: G22"""

    def test_key_order_independence(self):
        """Verify key-order-independent serialization. ACC: G22"""
        dict_a = {"b": 1, "a": 2, "c": {"y": 10, "x": 20}}
        dict_b = {"a": 2, "c": {"x": 20, "y": 10}, "b": 1}

        self.assertEqual(canonical_json(dict_a), canonical_json(dict_b))
        self.assertEqual(hash_canonical(dict_a), hash_canonical(dict_b))

    def test_unicode_preservation(self):
        """Verify non-ASCII characters are preserved without unicode escaping. ACC: G22"""
        data = {"greeting": "héllo wörld", "emoji": "🚀"}
        serialized = canonical_json(data)
        # ensure_ascii=False should keep raw UTF-8 bytes
        self.assertNotIn(b"\\u", serialized)
        self.assertEqual(serialized.decode("utf-8"), '{"emoji":"🚀","greeting":"héllo wörld"}')

    def test_compact_separators(self):
        """Verify no whitespace around separators. ACC: G22"""
        data = {"key1": "val1", "key2": [1, 2, 3]}
        serialized = canonical_json(data)
        self.assertEqual(serialized, b'{"key1":"val1","key2":[1,2,3]}')

    def test_float_rejection(self):
        """Verify floats are rejected at all nesting levels. ACC: G22"""
        # Top-level float
        with self.assertRaises(ValueError):
            canonical_json(1.5)

        # Float in dict value
        with self.assertRaises(ValueError):
            canonical_json({"score": 9.9})

        # Float in list
        with self.assertRaises(ValueError):
            canonical_json([1, 2, 3.14])

        # Float in nested dict
        with self.assertRaises(ValueError):
            canonical_json({"user": {"profile": {"rate": 0.05}}})

        # Direct check_no_floats
        with self.assertRaises(ValueError):
            check_no_floats(4.0)

        # Valid ints and bools must pass
        check_no_floats({"count": 42, "active": True, "items": [1, 2, 3]})

    def test_stable_op_hash_vectors(self):
        """Verify stable op_hash vectors per plan §7.4. ACC: G22"""
        # Test vector 1: known fixed op
        cmd = "task.create"
        requested_by = "usr_0000000001abcdefghijklmn"
        project = "prj_0000000002abcdefghijklmn"
        target = None
        params = {
            "title": "Fix bug in parser",
            "spec": "Address float validation",
            "priority": 1,
            # Excluded keys must be stripped
            "idempotency_key": "k-12345",
            "dry_run": True,
            "approval": "apr_123",
            "format": "json",
            "if_version": 1,
        }

        op_hash, canonical_bytes = compute_op_hash(
            cmd=cmd,
            requested_by=requested_by,
            project=project,
            target=target,
            params=params,
        )

        self.assertTrue(op_hash.startswith("sha256:"))
        self.assertEqual(len(op_hash), 7 + 64)

        # Ensure excluded keys were stripped from canonical_bytes
        self.assertNotIn(b"idempotency_key", canonical_bytes)
        self.assertNotIn(b"dry_run", canonical_bytes)
        self.assertNotIn(b"approval", canonical_bytes)
        self.assertNotIn(b"format", canonical_bytes)
        self.assertNotIn(b"if_version", canonical_bytes)

        # Recompute with params in different order and excluded keys changed
        params2 = {
            "priority": 1,
            "spec": "Address float validation",
            "title": "Fix bug in parser",
            "idempotency_key": "different-key",
            "dry_run": False,
        }
        op_hash2, canonical_bytes2 = compute_op_hash(
            cmd=cmd,
            requested_by=requested_by,
            project=project,
            target=target,
            params=params2,
        )

        self.assertEqual(op_hash, op_hash2)
        self.assertEqual(canonical_bytes, canonical_bytes2)

    def test_op_hash_hardcoded_vectors(self):
        """Hard-coded op_hash vectors built by hand from plan §7.4 (independent of the canonicalizer). ACC: G22"""
        import hashlib

        # Vector 1: canonical bytes written literally per plan §7.4 (sort_keys, "," ":" separators,
        # excluded keys dropped, null project/target kept).
        expected_bytes_1 = (
            b'{"op":"task.create","params":{"priority":1,"spec":"Address float validation",'
            b'"title":"Fix bug in parser"},"project":"prj_0000000002abcdefghijklmn",'
            b'"requested_by":"usr_0000000001abcdefghijklmn","target":null,"v":1}'
        )
        expected_hash_1 = "sha256:d128c9a424401d28819ad9bb1607233c1944d825203797c21a09a54e9439839b"
        self.assertEqual("sha256:" + hashlib.sha256(expected_bytes_1).hexdigest(), expected_hash_1)
        op_hash, canonical_bytes = compute_op_hash(
            cmd="task.create",
            requested_by="usr_0000000001abcdefghijklmn",
            project="prj_0000000002abcdefghijklmn",
            target=None,
            params={
                "title": "Fix bug in parser",
                "spec": "Address float validation",
                "priority": 1,
                "idempotency_key": "k-12345",
                "dry_run": True,
                "approval": "apr_123",
                "format": "json",
                "if_version": 1,
            },
        )
        self.assertEqual(canonical_bytes, expected_bytes_1)
        self.assertEqual(op_hash, expected_hash_1)

        # Vector 2: non-ASCII kept raw (ensure_ascii=False), list order preserved, bound category.
        expected_bytes_2 = (
            '{"category":"spend_money","op":"external.spend_money","params":{"amount":100,'
            '"note":"café ✓","tags":["b","a"]},"project":null,"requested_by":"usr_test",'
            '"target":"tsk_9","v":1}'
        ).encode("utf-8")
        expected_hash_2 = "sha256:766c92dc306650f533044f4f8d03004e15e8850436f3e0cb3d1bb0027d0debaa"
        self.assertEqual("sha256:" + hashlib.sha256(expected_bytes_2).hexdigest(), expected_hash_2)
        op_doc, op_hash_2 = compute_op_hash(
            op="external.spend_money",
            requested_by="usr_test",
            params={"tags": ["b", "a"], "note": "café ✓", "amount": 100, "category": "x"},
            target_id="tsk_9",
            category="spend_money",
        )
        self.assertEqual(canonical_json(op_doc), expected_bytes_2)
        self.assertEqual(op_hash_2, expected_hash_2)

        # Floats in params are rejected rather than hashed.
        with self.assertRaises(ValueError):
            compute_op_hash(cmd="task.create", requested_by="usr_x", params={"w": 1.0})

    def test_params_category_cannot_change_bound_category(self):
        """Verify caller-supplied params.category cannot alter the bound category (6i N1)."""
        op_doc, op_hash = compute_op_hash(
            op="external.spend_money",
            requested_by="usr_test",
            params={"category": "malicious_override", "amount": 100},
            category="spend_money",
        )
        self.assertEqual(op_doc["category"], "spend_money")
        self.assertNotIn("category", op_doc["params"])
        self.assertEqual(op_doc["params"], {"amount": 100})


if __name__ == "__main__":
    unittest.main()
