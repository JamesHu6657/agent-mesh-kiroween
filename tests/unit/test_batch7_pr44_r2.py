"""Regression tests for batch7 PR44 round 2 findings."""

import argparse
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv
from types import SimpleNamespace
from unittest import mock

from mesh.cli import entry
from mesh.core.paths import get_mesh_bin_dir, is_site_packages_dir


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _copy_mesh_pkg(target_dir: str) -> None:
    shutil.copytree(
        os.path.join(REPO_ROOT, "mesh"),
        os.path.join(target_dir, "mesh"),
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )


def _create_venv(vdir: str, *, system_site_packages: bool = False) -> str:
    venv.create(vdir, system_site_packages=system_site_packages, with_pip=False, symlinks=True)
    return os.path.join(vdir, "bin", "python3")


class TestBatch7Pr44R2SitePackagesConsistency(unittest.TestCase):
    """P2: entry._global_site_packages_dirs must match site.getsitepackages() and paths.is_site_packages_dir."""

    def test_entry_never_imports_site_or_non_builtin_in_pre_exec_path(self) -> None:
        entry_path = os.path.join(REPO_ROOT, "mesh", "cli", "entry.py")
        with open(entry_path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=entry_path)

        top_level_imports = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top_level_imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_level_imports.add(node.module)
        self.assertEqual(top_level_imports, {"posix", "sys"})

        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name != "main":
                for sub in ast.walk(node):
                    self.assertNotIsInstance(
                        sub,
                        (ast.Import, ast.ImportFrom),
                        f"pre-exec helper {node.name} must not perform imports",
                    )

        # Importing mesh.cli.entry under -S must not import site into sys.modules.
        probe = subprocess.run(
            [
                sys.executable,
                "-S",
                "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); import mesh.cli.entry as e; "
                "print('site' in sys.modules, len(e._global_site_packages_dirs()))",
                REPO_ROOT,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        self.assertEqual(probe.stdout.strip(), "False 0")

    def test_entry_matches_site_getsitepackages_across_interpreter_and_venvs(self) -> None:
        ver = f"python{sys.version_info[0]}.{sys.version_info[1]}"
        probe_code = (
            "import json, os, site, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import mesh.cli.entry as e\n"
            "from mesh.core.paths import is_site_packages_dir\n"
            "expected = sorted({os.path.realpath(sp) for sp in site.getsitepackages() if sp})\n"
            "actual = sorted(e._global_site_packages_dirs())\n"
            "user_sp = site.getusersitepackages() or ''\n"
            "extra_candidates = [\n"
            f"    os.path.join(sys.prefix, 'local', 'lib', '{ver}', 'site-packages'),\n"
            "    os.path.join(sys.prefix, 'local', 'lib', 'python3', 'dist-packages'),\n"
            f"    '/usr/local/lib/{ver}/site-packages',\n"
            f"    '/usr/lib/{ver}/site-packages',\n"
            "    sys.argv[1],\n"
            "    '/tmp/custom-target/site-packages',\n"
            "]\n"
            "if user_sp:\n"
            "    extra_candidates.append(user_sp)\n"
            "pairs = [(p, is_site_packages_dir(p), e._is_site_packages_dir(p)) for p in expected + extra_candidates]\n"
            "print(json.dumps({'expected': expected, 'actual': actual, 'pairs': pairs}))\n"
        )

        with tempfile.TemporaryDirectory(prefix="mh-pr44r2-cmp-") as d:
            v_false = os.path.join(d, "venv_false")
            vpy_false = _create_venv(v_false, system_site_packages=False)

            v_true = os.path.join(d, "venv_true")
            vpy_true = _create_venv(v_true, system_site_packages=True)

            # pyvenv.cfg omitting include-system-site-packages defaults to true per CPython site.venv().
            v_omit = os.path.join(d, "venv_omit")
            vpy_omit = _create_venv(v_omit, system_site_packages=False)
            cfg_omit = os.path.join(v_omit, "pyvenv.cfg")
            with open(cfg_omit, "r", encoding="utf-8") as fh:
                omit_lines = fh.readlines()
            with open(cfg_omit, "w", encoding="utf-8") as fh:
                fh.writelines(ln for ln in omit_lines if "include-system-site-packages" not in ln.lower())

            # Duplicate lines in pyvenv.cfg: last line wins per CPython site.venv().
            v_dup_true = os.path.join(d, "venv_dup_true")
            vpy_dup_true = _create_venv(v_dup_true, system_site_packages=False)
            with open(os.path.join(v_dup_true, "pyvenv.cfg"), "a", encoding="utf-8") as fh:
                fh.write("include-system-site-packages = true\n")

            v_dup_false = os.path.join(d, "venv_dup_false")
            vpy_dup_false = _create_venv(v_dup_false, system_site_packages=True)
            with open(os.path.join(v_dup_false, "pyvenv.cfg"), "a", encoding="utf-8") as fh:
                fh.write("include-system-site-packages = false\n")

            # Symlinked venv path.
            v_link = os.path.join(d, "venv_link")
            os.symlink(v_true, v_link)
            vpy_link = os.path.join(v_link, "bin", "python3")

            interpreters = [
                ("host", sys.executable, None),
                ("venv_false", vpy_false, False),
                ("venv_true", vpy_true, True),
                ("venv_omit", vpy_omit, True),
                ("venv_dup_true", vpy_dup_true, True),
                ("venv_dup_false", vpy_dup_false, False),
                ("venv_link", vpy_link, True),
            ]
            host_expected = None
            base_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
            for label, py, expects_system in interpreters:
                with self.subTest(interpreter=label):
                    proc = subprocess.run(
                        [py, "-P", "-s", "-c", probe_code, REPO_ROOT],
                        cwd=d,
                        env=base_env,
                        capture_output=True,
                        text=True,
                        timeout=15,
                        check=True,
                    )
                    data = json.loads(proc.stdout)
                    self.assertEqual(data["actual"], data["expected"])
                    if label == "host":
                        host_expected = set(data["expected"])
                    elif expects_system is True:
                        self.assertTrue(host_expected.issubset(set(data["expected"])))
                    else:
                        self.assertTrue(host_expected.isdisjoint(set(data["expected"])))
                    for path, p_res, e_res in data["pairs"]:
                        self.assertEqual(
                            e_res,
                            p_res,
                            f"{label} mismatch on {path!r}: paths={p_res}, entry={e_res}",
                        )
                    # Active global dirs must all be True; REPO_ROOT and custom target must be False.
                    self.assertTrue(all(p_res and e_res for _, p_res, e_res in data["pairs"][: len(data["expected"])]))
                    self.assertFalse(data["pairs"][-2][1] or data["pairs"][-2][2])
                    self.assertFalse(data["pairs"][-1][1] or data["pairs"][-1][2])

    def test_empty_path_returns_false_even_if_cwd_is_site_packages(self) -> None:
        sp = next((p for p in sys.modules["site"].getsitepackages() if os.path.isdir(p)), None)
        if sp is None:
            self.skipTest("no existing global site-packages directory")
        old_cwd = os.getcwd()
        try:
            os.chdir(sp)
            self.assertFalse(is_site_packages_dir(""))
            self.assertFalse(entry._is_site_packages_dir(""))
        finally:
            os.chdir(old_cwd)

    def test_overmatch_local_site_packages_runs_via_shim_and_console_script(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-pr44r2-over-") as d:
            vdir = os.path.join(d, "venv")
            vpy = _create_venv(vdir, system_site_packages=False)
            ver = f"python{sys.version_info[0]}.{sys.version_info[1]}"
            local_sp = os.path.join(vdir, "local", "lib", ver, "site-packages")
            os.makedirs(local_sp)
            _copy_mesh_pkg(local_sp)

            cache_dir = os.path.join(d, "cache")
            base_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "XDG_CACHE_HOME": cache_dir}
            shim_dir = subprocess.run(
                [vpy, "-P", "-s", "-c", "from mesh.core.paths import get_mesh_bin_dir; print(get_mesh_bin_dir())"],
                env={**base_env, "PYTHONPATH": local_sp},
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            ).stdout.strip()
            shim_path = os.path.join(shim_dir, "mesh")
            with open(shim_path, "r", encoding="utf-8") as fh:
                self.assertIn("export PYTHONPATH=", fh.read())

            p_shim = subprocess.run(
                [shim_path, "--version"],
                cwd=d,
                env=base_env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(p_shim.returncode, 0, p_shim.stderr + p_shim.stdout)
            self.assertTrue(json.loads(p_shim.stdout)["ok"])

            # Console script invocation without -P -s must also preserve PYTHONPATH=local_sp.
            console_script = os.path.join(d, "mesh-local")
            with open(console_script, "w", encoding="utf-8") as fh:
                fh.write("import sys\nfrom mesh.cli.entry import main\nsys.exit(main())\n")
            p_console = subprocess.run(
                [vpy, console_script, "--version"],
                cwd=d,
                env={**base_env, "PYTHONPATH": local_sp},
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(p_console.returncode, 0, p_console.stderr + p_console.stdout)
            self.assertTrue(json.loads(p_console.stdout)["ok"])

    def test_undermatch_omitted_pyvenv_cfg_does_not_reinject_pythonpath(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-pr44r2-under-") as d:
            vdir = os.path.join(d, "venv")
            vpy = _create_venv(vdir, system_site_packages=False)
            cfg = os.path.join(vdir, "pyvenv.cfg")
            with open(cfg, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
            with open(cfg, "w", encoding="utf-8") as fh:
                fh.writelines(ln for ln in lines if "include-system-site-packages" not in ln.lower())

            # Simulate an active system-level global dir under this omitted-config venv
            # (using site.getsitepackages()[-1], which is from base_prefix).
            sys_sp = subprocess.run(
                [vpy, "-P", "-s", "-c", "import os, site; print(os.path.realpath(site.getsitepackages()[-1]))"],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            ).stdout.strip()

            # Install mesh inside the venv's active global site-packages and ensure
            # neither shim nor console script re-injects PYTHONPATH.
            venv_sp = subprocess.run(
                [vpy, "-P", "-s", "-c", "import os, site; print(os.path.realpath(site.getsitepackages()[0]))"],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            ).stdout.strip()
            _copy_mesh_pkg(venv_sp)

            # Verify sys_sp (system site dir active due to omitted pyvenv.cfg) matches in both.
            cmp_proc = subprocess.run(
                [
                    vpy,
                    "-P",
                    "-s",
                    "-c",
                    "import sys, mesh.cli.entry as e; from mesh.core.paths import is_site_packages_dir; "
                    "print(is_site_packages_dir(sys.argv[1]), e._is_site_packages_dir(sys.argv[1]))",
                    sys_sp,
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            self.assertEqual(cmp_proc.stdout.strip(), "True True")

            # Patch mesh/__init__.py check via shim: pre-isolated shim must not re-exec
            # and must keep PYTHONPATH unset inside the running process.
            probe = subprocess.run(
                [
                    vpy,
                    "-P",
                    "-s",
                    "-c",
                    "import os, posix, sys, mesh.cli.entry as e; "
                    "orig_execve = posix.execve; "
                    "posix.execve = lambda *a: (_ for _ in ()).throw(AssertionError(f'unexpected execve: {a}')); "
                    "e.ensure_isolated_process(); "
                    "print('PYTHONPATH' in os.environ, os.environ.get('_MESH_ORIG_PYTHONPATH'))",
                ],
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "_MESH_ORIG_PYTHONPATH": "__UNSET__"},
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            self.assertEqual(probe.stdout.strip(), "False __UNSET__")

            # Verify get_mesh_bin_dir() generates `unset PYTHONPATH` for venv_sp and runs cleanly.
            cache_dir = os.path.join(d, "cache")
            shim_dir = subprocess.run(
                [vpy, "-P", "-s", "-c", "from mesh.core.paths import get_mesh_bin_dir; print(get_mesh_bin_dir())"],
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "XDG_CACHE_HOME": cache_dir},
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            ).stdout.strip()
            shim_path = os.path.join(shim_dir, "mesh")
            with open(shim_path, "r", encoding="utf-8") as fh:
                self.assertIn("unset PYTHONPATH", fh.read())

            # Also test console-script entry with dirty PYTHONPATH=<evil>:<venv_sp>:
            # ensure_isolated_process strips PYTHONPATH completely and never re-injects venv_sp.
            evil_dir = os.path.join(d, "evil")
            os.makedirs(evil_dir)
            with open(os.path.join(evil_dir, "json.py"), "w", encoding="utf-8") as fh:
                fh.write("raise SystemExit(99)\n")
            script = os.path.join(d, "probe_entry.py")
            with open(script, "w", encoding="utf-8") as fh:
                fh.write("import sys\nfrom mesh.cli.entry import main\nsys.exit(main())\n")
            # Run entry.main() with --version from dirty env; must succeed with -P -s and strip evil_dir.
            res = subprocess.run(
                [vpy, script, "--version"],
                cwd=evil_dir,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": f"{evil_dir}:{venv_sp}"},
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
            self.assertTrue(json.loads(res.stdout)["ok"])

            # Verify directly via real check: after re-exec from global site-packages (via console
            # script or via shim), PYTHONPATH is NOT present in child env and -P -s are active.
            with open(os.path.join(venv_sp, "mesh", "__main__.py"), "w", encoding="utf-8") as fh:
                fh.write(
                    "import os, sys\n"
                    "from mesh.cli.entry import ensure_isolated_process, main\n"
                    "if sys.argv[1:] == ['--probe-isolated-env']:\n"
                    "    ensure_isolated_process()\n"
                    "    print('PP=' + repr(os.environ.get('PYTHONPATH')))\n"
                    "    print('SAFE=' + repr(bool(sys.flags.safe_path)))\n"
                    "    print('NOSITE=' + repr(bool(sys.flags.no_user_site)))\n"
                    "    print('ORIG=' + repr(os.environ.get('_MESH_ORIG_PYTHONPATH')))\n"
                    "else:\n"
                    "    sys.exit(main())\n"
                )
            dirty_pp = f"{evil_dir}:{venv_sp}"
            probe_reexec = subprocess.run(
                [vpy, script, "--probe-isolated-env"],
                cwd=evil_dir,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": dirty_pp},
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
            self.assertIn("PP=None", probe_reexec.stdout)
            self.assertIn("SAFE=True", probe_reexec.stdout)
            self.assertIn("NOSITE=True", probe_reexec.stdout)
            self.assertIn(f"ORIG={dirty_pp!r}", probe_reexec.stdout)

            probe_shim = subprocess.run(
                [shim_path, "--probe-isolated-env"],
                cwd=evil_dir,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": dirty_pp},
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
            self.assertIn("PP=None", probe_shim.stdout)
            self.assertIn("SAFE=True", probe_shim.stdout)
            self.assertIn("NOSITE=True", probe_shim.stdout)
            self.assertIn(f"ORIG={dirty_pp!r}", probe_shim.stdout)

    def test_stdlib_site_validation_rejects_untrusted_and_no_site(self) -> None:
        self.assertIsNotNone(entry._stdlib_site_module())

        with mock.patch.object(entry.sys, "flags", argparse.Namespace(no_site=1)):
            self.assertIsNone(entry._stdlib_site_module())
            self.assertEqual(entry._global_site_packages_dirs(), set())

        fake_site = SimpleNamespace(__file__="/tmp/evil/site.py", getsitepackages=lambda: ["/tmp/evil"])
        with mock.patch.dict(entry.sys.modules, {"site": fake_site}):
            self.assertIsNone(entry._stdlib_site_module())
            self.assertEqual(entry._global_site_packages_dirs(), set())

        with mock.patch.dict(entry.sys.modules, {}, clear=False):
            orig = entry.sys.modules.pop("site", None)
            try:
                self.assertIsNone(entry._stdlib_site_module())
                self.assertEqual(entry._global_site_packages_dirs(), set())
            finally:
                if orig is not None:
                    entry.sys.modules["site"] = orig

    def test_poisoned_site_and_dash_S_reexec_cleanly(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-pr44r2-sec-") as d:
            evil_dir = os.path.join(d, "evil")
            os.makedirs(evil_dir)
            with open(os.path.join(evil_dir, "site.py"), "w", encoding="utf-8") as fh:
                fh.write("def getsitepackages():\n    return [%r]\n" % REPO_ROOT)
            with open(os.path.join(evil_dir, "json.py"), "w", encoding="utf-8") as fh:
                fh.write("raise SystemExit(99)\n")

            # -X frozen_modules=off allows PYTHONPATH/evil/site.py to shadow stdlib site on hop 1;
            # entry must reject untrusted site.py, strip evil_dir, and re-exec -P -s cleanly.
            p_poison = subprocess.run(
                [sys.executable, "-X", "frozen_modules=off", "-m", "mesh", "--version"],
                cwd=d,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": f"{evil_dir}:{REPO_ROOT}"},
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(p_poison.returncode, 0, p_poison.stderr + p_poison.stdout)
            self.assertTrue(json.loads(p_poison.stdout)["ok"])

            # -S (or -P -s -S with pre-set marker) in global venv site-packages must re-exec without -S
            # and strip PYTHONPATH on hop 2.
            vdir = os.path.join(d, "venv")
            vpy = _create_venv(vdir, system_site_packages=False)
            vsp = subprocess.run(
                [vpy, "-P", "-s", "-c", "import os, site; print(os.path.realpath(site.getsitepackages()[0]))"],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            ).stdout.strip()
            _copy_mesh_pkg(vsp)
            with open(os.path.join(vsp, "mesh", "__main__.py"), "w", encoding="utf-8") as fh:
                fh.write(
                    "import os, sys\n"
                    "from mesh.cli.entry import ensure_isolated_process, main\n"
                    "if sys.argv[1:] == ['--probe-s']:\n"
                    "    ensure_isolated_process()\n"
                    "    print('PP=' + repr(os.environ.get('PYTHONPATH')))\n"
                    "    print('NO_SITE=' + repr(sys.flags.no_site))\n"
                    "else:\n"
                    "    sys.exit(main())\n"
                )
            for args, extra_env in (
                ([vpy, "-P", "-s", "-S", "-m", "mesh", "--probe-s"], {"_MESH_ORIG_PYTHONPATH": "__UNSET__"}),
                ([vpy, "-S", "-m", "mesh", "--probe-s"], {}),
            ):
                p_dash_s = subprocess.run(
                    args,
                    cwd=evil_dir,
                    env={
                        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                        "PYTHONPATH": vsp,
                        **extra_env,
                    },
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=True,
                )
                self.assertIn("PP=None", p_dash_s.stdout)
                self.assertIn("NO_SITE=0", p_dash_s.stdout)


if __name__ == "__main__":
    unittest.main()
