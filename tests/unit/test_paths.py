"""Unit tests for path validation, install root isolation, and network-free code."""

import os
import shutil
import sys
import tempfile
import unittest

import mesh.constants as c
from mesh.constants import build_external_env
from mesh.core.paths import (
    check_home_permissions,
    check_install_path_secure,
    check_socket_path_length,
    check_token_file_secure,
    get_install_root,
)
from mesh.errors import MeshError


class TestPaths(unittest.TestCase):
    """Test path security checks and environment isolation."""

    def test_install_path_check(self) -> None:
        """ACC: C14 - Bidirectional install-path security check."""
        install_root = get_install_root()

        # Valid outside paths: should pass without error
        with tempfile.TemporaryDirectory() as tmpdir:
            home = os.path.join(tmpdir, "home")
            ws = os.path.join(tmpdir, "workspaces")
            os.makedirs(home, mode=0o700)
            os.makedirs(ws, mode=0o700)

            # Normal configuration outside install root: ok
            check_install_path_secure(
                install_root=install_root,
                home=home,
                workspaces_root=ws,
                repo_paths=[],
            )

            # Test direction 1: install root is inside workspaces.root
            inst_inside_ws = os.path.join(ws, "my_install")
            with self.assertRaises(MeshError) as ctx:
                check_install_path_secure(
                    install_root=inst_inside_ws,
                    home=home,
                    workspaces_root=ws,
                )
            self.assertEqual(ctx.exception.code, c.INSTALL_PATH_INSECURE)
            self.assertEqual(ctx.exception.exit_code, c.EXIT_USAGE)
            self.assertEqual(ctx.exception.details["install_root"], os.path.realpath(inst_inside_ws))
            self.assertEqual(ctx.exception.details["conflict"], os.path.realpath(ws))

            # Test direction 1: install root is inside runs
            runs_dir = os.path.join(home, "runs")
            os.makedirs(runs_dir, mode=0o700)
            inst_inside_runs = os.path.join(runs_dir, "my_install")
            with self.assertRaises(MeshError) as ctx:
                check_install_path_secure(
                    install_root=inst_inside_runs,
                    home=home,
                    workspaces_root=ws,
                )
            self.assertEqual(ctx.exception.code, c.INSTALL_PATH_INSECURE)
            self.assertEqual(ctx.exception.details["conflict"], os.path.realpath(runs_dir))

            # Test direction 1: install root is inside registered repo
            repo_dir = os.path.join(tmpdir, "my_repo")
            os.makedirs(repo_dir, mode=0o700)
            inst_inside_repo = os.path.join(repo_dir, "my_install")
            with self.assertRaises(MeshError) as ctx:
                check_install_path_secure(
                    install_root=inst_inside_repo,
                    home=home,
                    workspaces_root=ws,
                    repo_paths=[repo_dir],
                )
            self.assertEqual(ctx.exception.code, c.INSTALL_PATH_INSECURE)
            self.assertEqual(ctx.exception.details["conflict"], os.path.realpath(repo_dir))

            # Test direction 2: workspaces.root is inside install root
            ws_inside_inst = os.path.join(install_root, "some_ws")
            with self.assertRaises(MeshError) as ctx:
                check_install_path_secure(
                    install_root=install_root,
                    home=home,
                    workspaces_root=ws_inside_inst,
                )
            self.assertEqual(ctx.exception.code, c.INSTALL_PATH_INSECURE)
            self.assertEqual(ctx.exception.details["conflict"], os.path.realpath(ws_inside_inst))

            # Test direction 2: MESH_HOME is inside install root
            home_inside_inst = os.path.join(install_root, "some_home")
            with self.assertRaises(MeshError) as ctx:
                check_install_path_secure(
                    install_root=install_root,
                    home=home_inside_inst,
                    workspaces_root=ws,
                )
            self.assertEqual(ctx.exception.code, c.INSTALL_PATH_INSECURE)
            self.assertEqual(ctx.exception.details["conflict"], os.path.realpath(home_inside_inst))

            # Test direction 2: repo is inside install root
            repo_inside_inst = os.path.join(install_root, "some_repo")
            with self.assertRaises(MeshError) as ctx:
                check_install_path_secure(
                    install_root=install_root,
                    home=home,
                    workspaces_root=ws,
                    repo_paths=[repo_inside_inst],
                )
            self.assertEqual(ctx.exception.code, c.INSTALL_PATH_INSECURE)
            self.assertEqual(ctx.exception.details["conflict"], os.path.realpath(repo_inside_inst))

    def test_token_file_secure(self) -> None:
        """ACC: C10, C12 - Verify token file permissions and insecure location checks."""
        with tempfile.TemporaryDirectory() as tmpdir:
            home = os.path.join(tmpdir, "home")
            ws = os.path.join(home, "workspaces")
            runs = os.path.join(home, "runs")
            os.makedirs(home, mode=0o700)
            os.makedirs(ws, mode=0o700)
            os.makedirs(runs, mode=0o700)

            # Valid token file in outside config dir
            cfg_dir = os.path.join(tmpdir, "config")
            os.makedirs(cfg_dir, mode=0o700)
            valid_tok = os.path.join(cfg_dir, "agent.tok")
            with open(valid_tok, "w") as f:
                f.write("mesh_at_1234567890_secret")
            os.chmod(valid_tok, 0o600)

            check_token_file_secure(valid_tok, home=home, workspaces_root=ws)

            # Insecure mode 0644
            insecure_mode_tok = os.path.join(cfg_dir, "insecure.tok")
            with open(insecure_mode_tok, "w") as f:
                f.write("mesh_at_1234567890_secret")
            os.chmod(insecure_mode_tok, 0o644)
            with self.assertRaises(MeshError) as ctx:
                check_token_file_secure(insecure_mode_tok, home=home, workspaces_root=ws)
            self.assertEqual(ctx.exception.code, c.TOKEN_FILE_INSECURE)
            self.assertEqual(ctx.exception.exit_code, c.EXIT_UNAUTHENTICATED)

            # Insecure location inside workspaces (ACC: C12)
            tok_in_ws = os.path.join(ws, "g.tok")
            with open(tok_in_ws, "w") as f:
                f.write("mesh_at_1234567890_secret")
            os.chmod(tok_in_ws, 0o600)
            with self.assertRaises(MeshError) as ctx:
                check_token_file_secure(tok_in_ws, home=home, workspaces_root=ws)
            self.assertEqual(ctx.exception.code, c.TOKEN_FILE_INSECURE)
            self.assertEqual(ctx.exception.exit_code, c.EXIT_UNAUTHENTICATED)

            # Insecure location inside runs
            tok_in_runs = os.path.join(runs, "r.tok")
            with open(tok_in_runs, "w") as f:
                f.write("mesh_at_1234567890_secret")
            os.chmod(tok_in_runs, 0o600)
            with self.assertRaises(MeshError) as ctx:
                check_token_file_secure(tok_in_runs, home=home, workspaces_root=ws)
            self.assertEqual(ctx.exception.code, c.TOKEN_FILE_INSECURE)

    def test_home_permissions(self) -> None:
        """ACC: C4 - Permissions check for MESH_HOME."""
        with tempfile.TemporaryDirectory() as tmpdir:
            home = os.path.join(tmpdir, "home")
            os.makedirs(home, mode=0o700)
            check_home_permissions(home)

            os.chmod(home, 0o755)
            with self.assertRaises(MeshError) as ctx:
                check_home_permissions(home)
            self.assertEqual(ctx.exception.code, c.INSECURE_PERMISSIONS)
            self.assertEqual(ctx.exception.exit_code, c.EXIT_USAGE)

    def test_socket_path_length(self) -> None:
        """Socket path length check per plan §5.1."""
        short_path = "/tmp/mh-1234/run/mesh.sock"
        check_socket_path_length(short_path)

        long_path = "/tmp/" + "a" * 100 + "/run/mesh.sock"
        with self.assertRaises(MeshError) as ctx:
            check_socket_path_length(long_path)
        self.assertEqual(ctx.exception.code, c.SOCKET_PATH_TOO_LONG)
        self.assertEqual(ctx.exception.exit_code, c.EXIT_USAGE)

    def test_pythonpath_isolation(self) -> None:
        """Review-r3 Finding 1: verify build_external_env strips install-root PYTHONPATH and MESH_*."""
        fake_env = {
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": "/workspace/mesh-fresh-v1:/custom/libs",
            "_MESH_ORIG_PYTHONPATH": "/custom/libs",
            "MESH_HOME": "/tmp/mh-1234",
            "MESH_TOKEN": "mesh_rt_xxx",
            "MESH_RUN_ID": "run_123",
            "USER": "box",
        }
        cleaned = build_external_env(fake_env)
        self.assertNotIn("MESH_HOME", cleaned)
        self.assertNotIn("MESH_TOKEN", cleaned)
        self.assertNotIn("MESH_RUN_ID", cleaned)
        self.assertNotIn("_MESH_ORIG_PYTHONPATH", cleaned)
        self.assertEqual(cleaned["PYTHONPATH"], "/custom/libs")
        self.assertEqual(cleaned["USER"], "box")

        # When caller originally had no PYTHONPATH
        fake_env_unset = {
            "PYTHONPATH": "/workspace/mesh-fresh-v1",
            "_MESH_ORIG_PYTHONPATH": "__UNSET__",
            "MESH_SOCKET": "/tmp/sock",
        }
        cleaned_unset = build_external_env(fake_env_unset)
        self.assertNotIn("PYTHONPATH", cleaned_unset)
        self.assertNotIn("MESH_SOCKET", cleaned_unset)

    def test_no_network_code(self) -> None:
        """ACC: C2 - Assert no AF_INET, AF_INET6, http.server, socketserver, or .bind(( in mesh/."""
        repo_root = get_install_root()
        mesh_dir = os.path.join(repo_root, "mesh")
        forbidden = ["AF_INET", "AF_INET6", "http.server", "socketserver", ".bind(("]
        hits = []

        for root, _, files in os.walk(mesh_dir):
            for file in files:
                if file.endswith(".py"):
                    full_path = os.path.join(root, file)
                    with open(full_path, "r", encoding="utf-8") as f:
                        for line_num, line in enumerate(f, 1):
                            # Ignore comments
                            stripped = line.strip()
                            if stripped.startswith("#"):
                                continue
                            for term in forbidden:
                                if term in line:
                                    hits.append(f"{full_path}:{line_num}: {line.strip()}")

        self.assertEqual(hits, [], f"Forbidden networking constructs found in mesh/:\n" + "\n".join(hits))

    def test_stdlib_only(self) -> None:
        """ACC: A2 - Every import in mesh/ resolves to sys.stdlib_module_names or mesh."""
        repo_root = get_install_root()
        mesh_dir = os.path.join(repo_root, "mesh")
        stdlib_names = set(sys.stdlib_module_names)
        stdlib_names.add("mesh")

        import ast
        disallowed = []
        for root, _, files in os.walk(mesh_dir):
            for file in files:
                if file.endswith(".py"):
                    full_path = os.path.join(root, file)
                    with open(full_path, "r", encoding="utf-8") as f:
                        tree = ast.parse(f.read(), filename=full_path)
                    for node in ast.walk(tree):
                        if isinstance(node, ast.Import):
                            for alias in node.names:
                                top_pkg = alias.name.split(".")[0]
                                if top_pkg not in stdlib_names:
                                    disallowed.append((full_path, node.lineno, top_pkg))
                        elif isinstance(node, ast.ImportFrom):
                            if node.level == 0 and node.module:
                                top_pkg = node.module.split(".")[0]
                                if top_pkg not in stdlib_names:
                                    disallowed.append((full_path, node.lineno, top_pkg))

        self.assertEqual(disallowed, [], f"Non-stdlib imports found: {disallowed}")


if __name__ == "__main__":
    unittest.main()
