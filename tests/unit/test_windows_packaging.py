"""Unit tests for Windows packaging: Inno Setup .iss, launcher.c/rc, icon, and MinGit pinning."""

from __future__ import annotations

import os
import re
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts import build_windows_installer as inst_build
from scripts import build_windows_portable as port_build

REPO_ROOT = Path(__file__).resolve().parents[2]
PACK_WIN = REPO_ROOT / "packaging" / "windows"


class TestWindowsPackagingArtifacts(unittest.TestCase):
    def test_inno_setup_script_enforces_per_user_and_data_preservation_invariants(self) -> None:
        iss = (PACK_WIN / "agent-mesh.iss").read_text(encoding="utf-8")
        # Per-user install, no admin, isolated from ~/.agent-mesh
        self.assertIn("PrivilegesRequired=lowest", iss)
        self.assertIn(r"DefaultDirName={localappdata}\Programs\Agent Mesh", iss)
        self.assertNotIn("PrivilegesRequired=admin", iss)
        # Start menu shortcut
        self.assertIn(r'Name: "{group}\Agent Mesh"', iss)
        self.assertIn(r'Filename: "{app}\Agent Mesh.exe"', iss)
        # WebView2 registry check
        self.assertIn("{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}", iss)
        # User PATH touches HKCU only (never HKLM)
        self.assertIn("HKCU, UserEnvKey, 'Path'", iss)
        self.assertNotIn("HKLM, 'SYSTEM\\CurrentControlSet", iss)
        # Uninstaller keeps ~/.agent-mesh unless user explicitly opts in (default No = MB_DEFBUTTON2)
        self.assertIn(r"{%USERPROFILE}\.agent-mesh", iss)
        self.assertIn("MB_YESNO or MB_DEFBUTTON2", iss)
        self.assertIn("/PURGEDATA", iss)
        # Uninstaller stops only Agent Mesh executables (UTF-16 hex encoded path) and cleans runtime __pycache__
        self.assertIn("EncodeUtf16Hex", iss)
        self.assertIn("StopAppProcesses", iss)
        self.assertIn(r"python\pythonw.exe", iss)
        self.assertIn("DeletePycacheTree", iss)
        self.assertIn("FILE_ATTRIBUTE_REPARSE_POINT", iss)
        # No code signing
        self.assertNotIn("SignTool=", iss)

    def test_launcher_c_and_rc_use_isolated_python_flags_and_icon(self) -> None:
        src = (PACK_WIN / "launcher.c").read_text(encoding="utf-8")
        self.assertIn(r'L"-P -s -m mesh"', src)
        self.assertIn(r'L"-P -s -m mesh.desktop.app"', src)
        self.assertIn("_MESH_ORIG_PYTHONPATH", src)
        self.assertIn('SetEnvironmentVariableW(L"PYTHONPATH", NULL)', src)
        self.assertIn("PYTHONUTF8", src)
        self.assertIn("WriteFile(", src)

        rc = (PACK_WIN / "launcher.rc").read_text(encoding="utf-8")
        self.assertIn('ICON "icon.ico"', rc)
        self.assertIn("VS_VERSION_INFO", rc)
        self.assertIn("Agent Mesh.exe", rc)
        self.assertIn("mesh.exe", rc)

    def test_app_icons_are_valid_multi_size_ico_files(self) -> None:
        for ico_path in (
            PACK_WIN / "icon.ico",
            REPO_ROOT / "mesh" / "desktop" / "assets" / "agent-mesh.ico",
        ):
            self.assertTrue(ico_path.is_file(), f"Missing icon: {ico_path}")
            data = ico_path.read_bytes()
            self.assertGreater(len(data), 100)
            reserved, ico_type, count = struct.unpack_from("<HHH", data, 0)
            self.assertEqual(reserved, 0)
            self.assertEqual(ico_type, 1)
            self.assertGreaterEqual(count, 4)

    def test_mingit_url_and_sha256_are_pinned_in_portable_builder(self) -> None:
        self.assertTrue(port_build.MINGIT_URL.startswith("https://github.com/git-for-windows/git/releases/download/"))
        self.assertIn("MinGit-", port_build.MINGIT_URL)
        self.assertIn("64-bit.zip", port_build.MINGIT_URL)
        self.assertRegex(port_build.MINGIT_SHA256, re.compile(r"^[0-9a-f]{64}$"))
        self.assertRegex(port_build.PY_EMBED_SHA256, re.compile(r"^[0-9a-f]{64}$"))

    def test_installer_builder_prepare_only_writes_windows_and_wine_scripts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-inst-prep-") as tmp:
            # Include space and apostrophe in paths to verify PowerShell 5.1 single-quoting
            stage = Path(tmp) / "O'Brien stage"
            stage.mkdir()
            (stage / "Agent Mesh.exe").write_bytes(b"MZ")
            out = Path(tmp) / "out dir"
            rc = inst_build.main(["--out", str(out), "--stage-dir", str(stage), "--prepare-only"])
            self.assertEqual(rc, 0)
            ps1 = out / "build-installer.ps1"
            sh = out / "build-installer-wine.sh"
            self.assertTrue(ps1.is_file())
            self.assertTrue(sh.is_file())
            ps1_text = ps1.read_text(encoding="utf-8")
            self.assertIn("agent-mesh.iss", ps1_text)
            # Round 3 P3-1: must be valid PowerShell 5.1 (no PS 7-only `?.` operator or backtick-wrapped args)
            self.assertNotIn("?.", ps1_text)
            self.assertNotIn("`/D", ps1_text)
            self.assertIn("$IsccCmd = Get-Command iscc -ErrorAction SilentlyContinue", ps1_text)
            self.assertIn("$Iscc = if ($IsccCmd) { $IsccCmd.Source } else { $null }", ps1_text)
            self.assertIn("'/DAppVersion=", ps1_text)
            self.assertIn("'/DStageDir=", ps1_text)
            self.assertIn("O''Brien stage", ps1_text)
            self.assertIn("wine", sh.read_text(encoding="utf-8"))

    def test_mingit_precedence_docs_match_resolve_git_binary(self) -> None:
        """Round 3 P3-6: portable builder docs and README match docs/desktop.md (bundled MinGit preferred over PATH)."""
        portable_src = (REPO_ROOT / "scripts" / "build_windows_portable.py").read_text(encoding="utf-8")
        readme_src = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("system Git is always preferred", portable_src)
        self.assertNotIn("only when system git is missing", portable_src)
        self.assertNotIn("only if system Git is missing", portable_src)
        self.assertIn("preferred automatically", portable_src)
        self.assertNotIn("bundled MinGit\nfallback", readme_src)
        self.assertNotIn("bundled MinGit fallback", readme_src)


if __name__ == "__main__":
    unittest.main()
