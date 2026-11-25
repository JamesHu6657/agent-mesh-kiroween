"""Opt-in probe tier (m3-r1; ACC: N10, N21, N22). Coordinator only (C1).

Skipped unless ``MESH_PROBE=1``. Never part of ``scripts/test.sh``. It runs the REAL
vendor binaries read-only (``--version``, help and model-list commands):

    MESH_PROBE=1 [MESH_PROBE_ADAPTERS=droid,agy,kiro,vibe] [MESH_PROBE_CAPTURE=/tmp/mh-probe-…] \
        python3 -m unittest discover -s tests/probe -t .

Per adapter: resolve the binary on PATH with ``tests/shims`` removed; ``m adapter probe <id>
--binary <abs>`` against a shim-free, non-testing harness daemon on ``/tmp/mh-*`` (records
the ``adapter_probes`` row); capture live help; compare its sha256 with the fixture at
``planned_version``; print the flag diff; assert every flag ``build()`` emits is in the live
help; ``list_models()`` parses. With ``MESH_PROBE_CAPTURE`` set, the live outputs whose ``<version>/<file>`` does not exist yet are written with sidecars to
``<capture>/<adapter>/<version>/`` (promoted later with ``python3 -m tests.tierlib promote``).
A changed sha at an existing version is reported as ``same_version_drift`` and never captured.

:func:`probe_report` and :func:`check_report` take an explicit binary, version and capture
dir; the offline tests (tests/unit/test_probe_offline.py) drive them with stubs.
"""

import hashlib
import json
import os
import shutil
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from mesh.adapters import helpdiff
from mesh.adapters.base import run_readonly_cmd
from mesh.adapters.registry import ADAPTER_CLASSES
from tests import tierlib

# (fixture file name, argv after the binary, is a flag source). Sidecar commands use the
# binary *name* (Q16), never its path.
HELP_COMMANDS: Dict[str, List[Tuple[str, List[str], bool]]] = {
    "droid": [("help.txt", ["--help"], True), ("exec-help.txt", ["exec", "--help"], True)],
    "kiro": [("help.txt", ["--help"], True), ("chat-help.txt", ["chat", "--help"], True),
             ("list-models.json", ["chat", "--list-models", "-f", "json"], False)],
    "agy": [("help.txt", ["--help"], True), ("models.txt", ["models"], False)],
    "vibe": [("help.txt", ["--help"], True)],
}
MODELS_EXPECTED = {"droid": True, "agy": True, "kiro": True, "vibe": False}  # False: list_models() is None
DEFAULT_ADAPTERS = "droid,agy,kiro,vibe"
CMD_TIMEOUT_S = 30.0


def _sha_file(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def probe_report(adapter_id: str, binary: str, version: str, capture_dir: Optional[str] = None,
                 fixture_root: Optional[Path] = None) -> Dict[str, Any]:
    """Run the adapter's help/model commands read-only on ``binary`` (absolute) and compare
    them with the committed fixtures. Captures when asked and needed. Pure data result."""
    if not os.path.isabs(binary):
        raise ValueError(f"binary must be absolute: {binary!r}")
    if not version:
        raise ValueError("version is required")
    adapter = ADAPTER_CLASSES[adapter_id]()
    root = Path(fixture_root) if fixture_root is not None else helpdiff.fixture_root()
    planned_dir = root / adapter_id / (adapter.planned_version or "")
    version_dir = root / adapter_id / version
    name = adapter.binary_names[0]
    files: Dict[str, Dict[str, Any]] = {}
    texts: Dict[str, str] = {}
    live_tokens = set()
    diffs: Dict[str, Dict[str, List[str]]] = {}
    for fname, args, is_help in HELP_COMMANDS[adapter_id]:
        res = run_readonly_cmd([binary] + args, timeout_s=CMD_TIMEOUT_S)
        text = res.help_text
        texts[fname] = text
        planned_sha = _sha_file(planned_dir / fname)
        files[fname] = {"command": [name] + args, "returncode": res.returncode, "sha256": res.help_sha256,
                        "planned_sha256": planned_sha, "matches_planned": res.help_sha256 == planned_sha,
                        "version_sha256": _sha_file(version_dir / fname)}
        if is_help and res.returncode == 0:
            live_tokens |= helpdiff.flag_tokens(text)
            old = (planned_dir / fname).read_text(encoding="utf-8") if planned_sha else ""
            diffs[fname] = helpdiff.diff(old, text)
    emitted = helpdiff.emitted_flags(adapter)
    help_ok = all(files[f]["returncode"] == 0 for f, _, is_help in HELP_COMMANDS[adapter_id] if is_help)
    # Only files whose destination does not exist yet are captured: promote never
    # overwrites, so a changed sha at an existing version is reported, not captured.
    new_files = [f for f, _, _ in HELP_COMMANDS[adapter_id]
                 if files[f]["returncode"] == 0 and files[f]["version_sha256"] is None]
    drift = sorted(f for f, v in files.items() if v["returncode"] == 0 and v["version_sha256"] is not None
                   and v["version_sha256"] != v["sha256"])
    needs_capture = bool(new_files)
    captured: List[str] = []
    if capture_dir and needs_capture:
        for fname, args, _ in HELP_COMMANDS[adapter_id]:
            if fname not in new_files:
                continue
            captured.append(tierlib.write_capture(capture_dir, f"{adapter_id}/{version}/{fname}",
                                                  texts[fname].encode("utf-8"), files[fname]["command"]))
    return {"adapter": adapter_id, "binary": binary, "version": version,
            "planned_version": adapter.planned_version, "version_dir_exists": version_dir.is_dir(),
            "files": files, "diff_vs_planned": diffs, "help_ok": help_ok,
            "emitted_flags": sorted(emitted), "missing_emitted_flags": sorted(emitted - live_tokens),
            "needs_capture": needs_capture, "same_version_drift": drift, "captured": captured}


def check_report(report: Dict[str, Any]) -> None:
    """Raise AssertionError unless every help command ran and every emitted flag is in the
    live help. Added flags are reported, never a failure."""
    if not report["help_ok"]:
        raise AssertionError(f"{report['adapter']}: a help command failed: "
                             f"{ {k: v['returncode'] for k, v in report['files'].items()} }")
    if report["missing_emitted_flags"]:
        raise AssertionError(f"{report['adapter']} {report['version']}: emitted flags missing from live help: "
                             f"{report['missing_emitted_flags']}")


def _path_without_shims() -> str:
    repo = Path(__file__).resolve().parents[2]
    shims = os.path.realpath(repo / "tests" / "shims")
    keep = [p for p in os.environ.get("PATH", "").split(os.pathsep)
            if p and os.path.realpath(p) != shims]
    return os.pathsep.join(keep)


def daemon_probe(tc: unittest.TestCase, adapter_id: str, binary: str, extra_path: Optional[str] = None) -> str:
    """``mesh adapter probe <id> --binary <abs>`` on a shim-free, non-testing harness daemon;
    asserts rc 0, a version, and that the newest ``adapter_probes`` row is (version, binary).
    sqlite3.Row never equals a tuple, so the row is converted (C1 failure 2026-11-09).
    Shared by the tier and the offline test. Returns the version."""
    from tests.helpers import MeshHarness

    kwargs = {"extra_path": extra_path} if extra_path else {}
    harness = MeshHarness(init_db=True, start_daemon=True, use_shims=False, **kwargs)
    try:
        rc, resp = harness.cli("adapter", "probe", adapter_id, "--binary", binary, user=True, timeout=120.0)
        tc.assertEqual(rc, 0, resp)
        version = resp["data"].get("version")
        tc.assertTrue(version, resp)
        conn = harness.db()
        try:
            row = conn.execute("SELECT version, binary_path FROM adapter_probes WHERE adapter = ? "
                               "ORDER BY probed_at DESC LIMIT 1;", (adapter_id,)).fetchone()
        finally:
            conn.close()
        tc.assertIsNotNone(row, f"{adapter_id}: no adapter_probes row")
        tc.assertEqual(tuple(row), (version, binary))
        return version
    finally:
        harness.teardown()


@unittest.skipUnless(os.environ.get("MESH_PROBE") == "1", "probe tier: set MESH_PROBE=1 (coordinator only)")
class ProbeTier(unittest.TestCase):
    def test_probe_adapters(self) -> None:
        """ACC: N10, N22, N21 - real read-only probe per adapter; adapter_probes row; help sha vs fixture; flag diff printed; emitted flags in live help; models parse; capture with sidecars."""
        from tests.helpers import MeshHarness

        wanted = [a.strip() for a in os.environ.get("MESH_PROBE_ADAPTERS", DEFAULT_ADAPTERS).split(",") if a.strip()]
        capture = os.environ.get("MESH_PROBE_CAPTURE") or None
        if capture:
            self.assertTrue(capture.startswith("/tmp/mh-"), capture)
        path = _path_without_shims()
        for adapter_id in wanted:
            with self.subTest(adapter=adapter_id):
                self.assertIn(adapter_id, HELP_COMMANDS)
                adapter = ADAPTER_CLASSES[adapter_id]()
                binary = shutil.which(adapter.binary_names[0], path=path)
                if not binary:
                    print(json.dumps({"adapter": adapter_id, "status": "not_installed"}))
                    continue
                binary = os.path.abspath(binary)
                version = daemon_probe(self, adapter_id, binary)
                report = probe_report(adapter_id, binary, version, capture_dir=capture)
                print(json.dumps(report, indent=2, sort_keys=True))
                check_report(report)
                models = adapter.list_models(binary)
                if MODELS_EXPECTED[adapter_id]:
                    self.assertTrue(models, f"{adapter_id}: list_models() empty")
                else:
                    self.assertIsNone(models)


if __name__ == "__main__":
    unittest.main()
