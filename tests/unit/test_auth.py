"""Unit tests for authentication, cryptographic verification, and run-session classifier."""

import os
import unittest
from unittest.mock import patch

import mesh.constants as c
from mesh.core.auth import (
    classify_peer_run_session,
    derive_user_keys,
    generate_agent_token,
    generate_run_token,
    parse_token_lookup,
    verify_token_hash,
    verify_user_passphrase,
)
from mesh.core.proc import parse_proc_stat
from mesh.errors import MeshError


class TestAuth(unittest.TestCase):
    """Test cryptographic authentication primitives and run-session classifier."""

    def test_scrypt_params(self) -> None:
        """ACC: C13 - Verifies scrypt derivation with exact parameters including maxmem."""
        passphrase = "test-passphrase-at-least-12-chars"
        verifier, k_user, salt, kdf_params = derive_user_keys(passphrase)

        self.assertEqual(len(verifier), 32)
        self.assertEqual(len(k_user), 32)
        self.assertEqual(len(salt), 16)
        self.assertEqual(kdf_params["n"], 32768)
        self.assertEqual(kdf_params["r"], 8)
        self.assertEqual(kdf_params["p"], 1)
        self.assertEqual(kdf_params["dklen"], 64)
        self.assertEqual(kdf_params["maxmem"], 64 * 1024 * 1024)

        # Verification passes with correct passphrase
        valid, derived_k_user = verify_user_passphrase(passphrase, salt, kdf_params, verifier, testing_mode=False)
        self.assertTrue(valid)
        self.assertEqual(derived_k_user, k_user)

        # Verification fails with wrong passphrase
        invalid, bad_k_user = verify_user_passphrase("wrong-passphrase-here", salt, kdf_params, verifier, testing_mode=False)
        self.assertFalse(invalid)
        self.assertIsNone(bad_k_user)

        # Passphrase shorter than 12 chars rejected
        with self.assertRaises(MeshError) as ctx:
            derive_user_keys("short123")
        self.assertEqual(ctx.exception.code, c.INVALID_ARGUMENT)

    def test_token_generation_and_verification(self) -> None:
        """ACC: C9 - Test agent and run token formatting and hash verification."""
        tok, lookup, secret_hash = generate_agent_token()
        self.assertTrue(tok.startswith("mesh_at_"))
        self.assertEqual(parse_token_lookup(tok), lookup)
        self.assertEqual(len(lookup), 10)
        self.assertEqual(len(secret_hash), 32)
        self.assertTrue(verify_token_hash(tok, secret_hash))
        self.assertFalse(verify_token_hash(tok + "x", secret_hash))

        rtok, rlookup, rsecret_hash = generate_run_token()
        self.assertTrue(rtok.startswith("mesh_rt_"))
        self.assertEqual(parse_token_lookup(rtok), rlookup)
        self.assertEqual(len(rlookup), 10)
        self.assertTrue(verify_token_hash(rtok, rsecret_hash))

    def test_proc_stat_parsing(self) -> None:
        """Test /proc/<pid>/stat parsing per plan §11.1 (including complex comm names)."""
        # Simple comm
        sample_stat = "1234 (meshd) S 1 1234 1234 0 -1 4194304 100 0 0 0 10 20 0 0 20 0 1 0 1234567 1000 200"
        parsed = parse_proc_stat(sample_stat)
        self.assertEqual(parsed["state"], "S")
        self.assertEqual(parsed["ppid"], 1)
        self.assertEqual(parsed["pgrp"], 1234)
        self.assertEqual(parsed["session"], 1234)
        self.assertEqual(parsed["starttime"], 1234567)

        # comm containing spaces and parentheses: e.g. "a) (b"
        complex_stat = "5678 (python3 (worker)) R 1234 5000 5000 0 -1 0 0 0 0 0 1 2 0 0 20 0 1 0 9876543 2000 300"
        parsed_complex = parse_proc_stat(complex_stat)
        self.assertEqual(parsed_complex["state"], "R")
        self.assertEqual(parsed_complex["ppid"], 1234)
        self.assertEqual(parsed_complex["pgrp"], 5000)
        self.assertEqual(parsed_complex["session"], 5000)
        self.assertEqual(parsed_complex["starttime"], 9876543)

    def test_run_session_classifier(self) -> None:
        """Plan §7.1, §17.2 test_run_session_classifier.

        A peer whose sid is an active run's, whose ancestry reaches a wrapper,
        or whose environ has the MESH_RUN_ID of any known run (active or ended)
        is 'inside run'; other peers are not.
        """
        active_runs = {"run_active_1": 2000, "run_active_2": 3000}
        known_run_ids = {"run_active_1", "run_active_2", "run_ended_3"}
        meshd_pid = 1000

        # Case 1: Session ID equals active run's wrapper pid
        with patch("mesh.core.auth.get_proc_stat", return_value={"session": 2000, "ppid": 500}), \
             patch("mesh.core.auth.get_proc_ancestry", return_value=[500]), \
             patch("mesh.core.auth.get_proc_environ", return_value={}):
            result = classify_peer_run_session(501, active_runs, known_run_ids, meshd_pid)
            self.assertEqual(result, "run_active_1")

        # Case 2: Ancestry reaches wrapper pid
        with patch("mesh.core.auth.get_proc_stat", return_value={"session": 9999, "ppid": 2500}), \
             patch("mesh.core.auth.get_proc_ancestry", return_value=[2500, 3000]), \
             patch("mesh.core.auth.get_proc_environ", return_value={}):
            result = classify_peer_run_session(2501, active_runs, known_run_ids, meshd_pid)
            self.assertEqual(result, "run_active_2")

        # Case 3: Environ contains MESH_RUN_ID for an ended run
        with patch("mesh.core.auth.get_proc_stat", return_value={"session": 9999, "ppid": 1}), \
             patch("mesh.core.auth.get_proc_ancestry", return_value=[]), \
             patch("mesh.core.auth.get_proc_environ", return_value={"MESH_RUN_ID": "run_ended_3"}):
            result = classify_peer_run_session(4000, active_runs, known_run_ids, meshd_pid)
            self.assertEqual(result, "run_ended_3")

        # Case 4: Normal peer outside any run
        with patch("mesh.core.auth.get_proc_stat", return_value={"session": 9999, "ppid": 100}), \
             patch("mesh.core.auth.get_proc_ancestry", return_value=[100]), \
             patch("mesh.core.auth.get_proc_environ", return_value={"USER": "box"}):
            result = classify_peer_run_session(105, active_runs, known_run_ids, meshd_pid)
            self.assertIsNone(result)

        # Case 5: Cannot read /proc stat
        with patch("mesh.core.auth.get_proc_stat", return_value=None):
            with self.assertRaises(MeshError) as ctx:
                classify_peer_run_session(999, active_runs, known_run_ids, meshd_pid)
            self.assertEqual(ctx.exception.code, c.CREDENTIAL_NOT_ALLOWED_IN_RUN)
            self.assertEqual(ctx.exception.exit_code, c.EXIT_FORBIDDEN)

    def test_peer_cred_check_mismatched_uid(self) -> None:
        """ACC: C6 - Unit test of the peer check with a different SO_PEERCRED uid."""
        from unittest.mock import AsyncMock, MagicMock
        from mesh.daemon.server import DaemonServer

        server = DaemonServer(home="/tmp/fake-home")
        mock_sock = MagicMock()
        mock_writer = MagicMock()
        mock_writer.get_extra_info.return_value = mock_sock
        mock_writer.wait_closed = AsyncMock()
        mock_reader = MagicMock()

        # Mock get_peer_credentials to return a different UID than current process
        diff_uid = os.getuid() + 999
        with patch("mesh.daemon.server.get_peer_credentials", return_value=(12345, diff_uid, diff_uid)):
            import asyncio
            asyncio.run(server.handle_client(mock_reader, mock_writer))

        # Must close writer immediately without reading line or dispatching
        mock_writer.close.assert_called_once()
        mock_reader.readline.assert_not_called()


if __name__ == "__main__":
    unittest.main()
