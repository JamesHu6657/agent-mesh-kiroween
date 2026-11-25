"""JS-free smoke tests for static desktop UI assets and packaging metadata."""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

from mesh.desktop.app import REQUIRED_ASSET_FILES, verify_assets
from mesh.desktop.bridge import asset_dir

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestDesktopStaticAssetsSmoke(unittest.TestCase):
    def test_assets_exist_and_load(self) -> None:
        info = verify_assets()
        root = Path(asset_dir())
        self.assertTrue(root.is_dir())
        for name in REQUIRED_ASSET_FILES:
            path = root / name
            self.assertTrue(path.is_file(), f"Missing asset {path}")
            self.assertGreater(path.stat().st_size, 100)
            self.assertIn(name, info["files"])
        self.assertTrue(info["capabilities"]["project_conversation"])
        self.assertTrue(info["capabilities"]["confirm_plan"])
        self.assertTrue(info["capabilities"]["approval_policy_inline"])

    def test_desktop_entry_check_mode_succeeds_without_pywebview(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-desk-smoke-") as tmp:
            proc = subprocess.run(
                [sys.executable, "-m", "mesh.desktop.app", "--home", tmp, "--check"],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=30,
            )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn('"ok": true', proc.stdout)

    def test_html_has_no_cdn_and_exposes_required_hooks(self) -> None:
        html = (Path(asset_dir()) / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        self.assertIn('http-equiv="Content-Security-Policy"', html)
        self.assertIn("default-src 'none'", html)
        self.assertIn("script-src 'self'", html)
        self.assertIn("style-src 'self' 'unsafe-inline'", html)
        self.assertIn("img-src 'self' data:", html)
        self.assertIn("base-uri 'none'", html)
        self.assertIn("form-action 'none'", html)
        # Ensure zero inline <script> bodies or inline on*= event handlers
        self.assertNotRegex(html, re.compile(r"<script(?![^>]*\bsrc=)[^>]*>", re.IGNORECASE))
        self.assertNotRegex(html, re.compile(r"<[^>]*\son[a-z]+\s*=", re.IGNORECASE))
        self.assertIn('data-theme="dark"', html)
        self.assertIn('href="styles.css"', html)
        self.assertIn('src="app.js"', html)

        required_ids = (
            "customTitlebar",
            "titlebarDrag",
            "btnWinMin",
            "btnWinMax",
            "btnWinClose",
            "leftSidebar",
            "btnCollapseSidebar",
            "btnNewChat",
            "sidebarProjectsContainer",
            "daemonDisconnectedBar",
            "btnInlineViewLog",
            "daemonDisconnectedDetail",
            "authUnlockBar",
            "authInlineForm",
            "chatThread",
            "projectPermissionModeSelect",
            "composerApprovalStrip",
            "composerQueueBadge",
            "composerRepoDrawer",
            "btnPickRepoFolder",
            "btnInspectRepoFolder",
            "recentFoldersBar",
            "repoInspectStatus",
            "repoBranchDatalist",
            "repoBranchChips",
            "repoTestCmdChips",
            "authInlineRememberWrap",
            "authInlineRemember",
            "authInlineRememberWarning",
            "composerInput",
            "btnLeadPill",
            "btnSteerLeadTurn",
            "btnStopLeadTurn",
            "btnSendComposer",
            "rightInspector",
            "inspectorWorkerTabStrip",
            "viewCli",
            "viewSettings",
            "wizardOverlay",
            "wizardStep1Form",
            "wizardStep2Pane",
            "wizardPassInput",
            "wizardPassConfirm",
            "wizardCliList",
            "btnWizardInit",
            "btnWizardFinish",
            "modalBackdrop",
            "confirmModalBackdrop",
        )
        for dom_id in required_ids:
            self.assertIn(f'id="{dom_id}"', html, f"Missing DOM id {dom_id!r}")
        composer_tag = re.search(r'<textarea[^>]*id="composerInput"[^>]*>', html)
        self.assertIsNotNone(composer_tag)
        self.assertIn('aria-label=', composer_tag.group(0))
        search_tag = re.search(r'<input[^>]*id="projectSearchInput"[^>]*>', html)
        self.assertIsNotNone(search_tag)
        self.assertIn('aria-label=', search_tag.group(0))
        self.assertIn("pywebview-drag-region", html)
        for edge in ("n", "s", "e", "w", "ne", "nw", "se", "sw"):
            self.assertIn(f'data-resize-edge="{edge}"', html, f"Missing resize handle {edge!r}")

    def test_css_uses_only_opaque_surfaces_and_dual_themes(self) -> None:
        css = (Path(asset_dir()) / "styles.css").read_text(encoding="utf-8")
        self.assertIn(':root[data-theme="dark"]', css)
        self.assertIn(':root[data-theme="light"]', css)
        self.assertNotIn("http://", css)
        self.assertNotIn("https://", css)
        # Strict Owner Rule: 100% OPAQUE high-contrast surfaces, zero transparency/blur/alpha
        self.assertNotRegex(css, re.compile(r"\brgba\s*\(", re.IGNORECASE))
        self.assertNotRegex(css, re.compile(r"\bhsla\s*\(", re.IGNORECASE))
        self.assertNotRegex(css, re.compile(r"\bcolor-mix\s*\(", re.IGNORECASE))
        self.assertNotRegex(css, re.compile(r"\bbackdrop-filter\b", re.IGNORECASE))
        self.assertNotRegex(css, re.compile(r"\bblur\s*\(", re.IGNORECASE))
        self.assertNotRegex(css, re.compile(r"(?:^|[^-])\bopacity\s*:", re.IGNORECASE | re.MULTILINE))
        self.assertNotRegex(css, re.compile(r"#[0-9a-fA-F]{8}\b"))
        # P3-8: disallow transparent keywords and `background: none`
        self.assertNotRegex(css, re.compile(r"\bbackground\s*:\s*none\b", re.IGNORECASE))
        self.assertNotRegex(css, re.compile(r":\s*transparent\b", re.IGNORECASE))

        # P3-8: verify WCAG AA >= 4.5:1 contrast for primary/secondary/muted text on all surfaces in both themes
        def _rel_lum(hex_color: str) -> float:
            h = hex_color.lstrip("#")
            ch = [int(h[i : i + 2], 16) / 255.0 for i in (0, 2, 4)]
            lin = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in ch]
            return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

        def _contrast(a: str, b: str) -> float:
            l1, l2 = sorted((_rel_lum(a), _rel_lum(b)), reverse=True)
            return (l1 + 0.05) / (l2 + 0.05)

        for theme in ("dark", "light"):
            m_theme = re.search(rf':root\[data-theme="{theme}"\]\s*\{{([^}}]+)\}}', css)
            self.assertIsNotNone(m_theme, f"Missing theme block {theme}")
            tokens = dict(re.findall(r"--([a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{6})", m_theme.group(1)))
            for fg in ("text-primary", "text-secondary", "text-muted"):
                for bg in (
                    "bg-canvas",
                    "bg-sidebar",
                    "bg-surface",
                    "bg-surface-hover",
                    "bg-surface-active",
                    "bg-card",
                    "bg-card-muted",
                    "bg-input",
                ):
                    ratio = _contrast(tokens[fg], tokens[bg])
                    self.assertGreaterEqual(
                        ratio,
                        4.5,
                        f"{theme} contrast too low: --{fg} ({tokens[fg]}) on --{bg} ({tokens[bg]}) = {ratio:.2f}",
                    )
        self.assertIn(".resize-handle", css)
        resize_block_match = re.search(r"\.resize-handle\s*\{([^}]*)\}", css)
        self.assertIsNotNone(resize_block_match)
        self.assertNotIn("background", resize_block_match.group(1).lower())
        self.assertNotIn("transparent", resize_block_match.group(1).lower())
        max_hide_match = re.search(r"body\.window-maximized\s+\.resize-handle\s*\{([^}]*)\}", css)
        self.assertIsNotNone(max_hide_match)
        self.assertIn("display: none", max_hide_match.group(1).lower())
        composer_match = re.search(r"\.composer-textarea\s*\{([^}]*)\}", css)
        self.assertIsNotNone(composer_match)
        min_w_match = re.search(r"min-width\s*:\s*(\d+)px", composer_match.group(1))
        self.assertIsNotNone(min_w_match, "Expected positive min-width on .composer-textarea")
        self.assertGreaterEqual(int(min_w_match.group(1)), 160)
        self.assertIn("@media (max-width: 1240px)", css)
        # Grok-Bot-style 88px collapsed icon rail + 690px centered transcript + 700px composer dock
        self.assertIn("--sidebar-rail-width: 88px", css)
        self.assertIn("max-width: 690px", css)
        self.assertIn("max-width: 700px", css)

    def test_js_wires_real_rpcs_and_never_calls_unimplemented_or_lead_only_rpcs(self) -> None:
        js = (Path(asset_dir()) / "app.js").read_text(encoding="utf-8")
        for needle in (
            "pywebviewready",
            "window.__meshOnEvents",
            "poll_events",
            "window.pywebview.api.call",
            "home_status",
            "initialize_home",
            "detect_local_clis",
            "pick_folder",
            "inspect_repo_folder",
            "desktop_prefs",
            "set_desktop_prefs",
            "quit_app",
            "window_minimize",
            "window_toggle_maximize",
            "window_restore_for_drag",
            "window_drag_move",
            "window_drag_end",
            "window_resize_start",
            "window_resize",
            "window_resize_end",
            "window_close",
            "_desktop.window_state",
            "window-maximized",
            "HOME_NOT_INITIALIZED",
            "desktop_log_path",
            "desktop.log",
            "LEAD_WAIT_NOTICE_TIMEOUT_S",
            "renderLeadTurnAndWaitStatusHtml",
            "data-lead-delivery=\"inbox\"",
            "data-lead-turn-status",
            "data-wait-start",
            "data-open-wizard-step2",
            "chat.send",
            "chat.list",
            "chat.queue_cancel",
            "chat.queue_edit",
            "lead.interrupt",
            "plan.list",
            "plan.cancel",
            "intake.submit",
            "project.list",
            "project.diff",
            "project.set_lead",
            "project.set_permission_mode",
            "task.list",
            "task.show",
            "task.diff",
            "task.create",
            "task.log",
            "task.accept",
            "task.reject",
            "task.merge",
            "approval.decide",
            "grant.list",
            "grant.revoke",
            "adapter.list",
            "agent.list",
            "config.list",
            "workspace.show",
            "run.log",
            "run.activity",
            "audit.verify",
        ):
            self.assertIn(needle, js, f"Missing bridge/RPC reference {needle!r}")

        for forbidden_call in (
            'apiCall("project.plan.confirm"',
            'apiCall("project.conversation"',
            'apiCall("chat.reply"',
            'apiCall("plan.propose"',
            'apiCall("plan.dispatch"',
        ):
            self.assertNotIn(forbidden_call, js)

    def test_pyproject_desktop_extra_and_core_dependency_free(self) -> None:
        pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        proj = pyproject["project"]
        self.assertEqual(proj["dependencies"], [])
        self.assertEqual(proj["requires-python"], ">=3.11")
        self.assertIn("classifiers", proj)
        self.assertIsInstance(proj["classifiers"], list)
        self.assertTrue(all(isinstance(item, str) and " :: " in item for item in proj["classifiers"]))
        self.assertIn("optional-dependencies", proj)
        self.assertNotIn("classifiers", proj["optional-dependencies"])
        self.assertIn("desktop", proj["optional-dependencies"])
        desktop_deps = proj["optional-dependencies"]["desktop"]
        self.assertIn("pywebview>=6.0,<7", desktop_deps)
        self.assertEqual(proj["scripts"]["mesh"], "mesh.cli.entry:main")
        self.assertNotIn("mesh-desktop", proj["scripts"])
        self.assertEqual(proj["gui-scripts"]["mesh-desktop"], "mesh.desktop.app:main")
        pkg_data = pyproject["tool"]["setuptools"]["package-data"]
        self.assertIn("assets/*", pkg_data["mesh.desktop"])
        license_files = pyproject["tool"]["setuptools"].get("license-files")
        self.assertEqual(license_files, ["LICENSE"])
        self.assertFalse((REPO_ROOT / "NOTICE").exists(), "Repo root NOTICE must not exist")
        portable_script = (REPO_ROOT / "scripts" / "build_windows_portable.py").read_text(encoding="utf-8")
        self.assertNotIn('"NOTICE"', portable_script)
        self.assertNotIn('"NOTICE.txt"', portable_script)

    def test_init_cleans_up_stale_crashed_init_tmp_dbs(self) -> None:
        from mesh.core.init import _cleanup_stale_init_tmp_dbs, init_mesh_home

        with tempfile.TemporaryDirectory(prefix="mh-init-stale-") as tmp:
            home = Path(tmp) / "home"
            home.mkdir(mode=0o700)
            # Dead PID leftover temp db files from a crashed previous init
            dead_pid = 99999999
            stale_files = [
                home / f".mesh.db.init.{dead_pid}.tmp",
                home / f".mesh.db.init.{dead_pid}.tmp-wal",
                home / f".mesh.db.init.{dead_pid}.tmp-shm",
            ]
            for f in stale_files:
                f.write_bytes(b"stale-verifier-salt")
            # Unrelated file should never be touched
            keep_file = home / "unrelated.txt"
            keep_file.write_text("keep", encoding="utf-8")

            removed = _cleanup_stale_init_tmp_dbs(str(home), db_exists=False)
            self.assertEqual(removed, 3)
            for f in stale_files:
                self.assertFalse(f.exists(), f"{f} should have been removed")
            self.assertTrue(keep_file.exists())

            # Full init_mesh_home also cleans stale dead-pid tmp files before initializing
            for f in stale_files:
                f.write_bytes(b"stale-again")
            import os

            old_testing = os.environ.get("MESH_TESTING")
            os.environ["MESH_TESTING"] = "1"
            try:
                res = init_mesh_home(str(home), "correct-horse-battery-staple")
                self.assertTrue(res["initialized"])
            finally:
                if old_testing is None:
                    os.environ.pop("MESH_TESTING", None)
                else:
                    os.environ["MESH_TESTING"] = old_testing
            for f in stale_files:
                self.assertFalse(f.exists(), f"{f} should be cleaned during init_mesh_home")
            self.assertTrue((home / "mesh.db").is_file())

    def test_desktop_docs_no_private_paths_and_documents_daemon_lifecycle(self) -> None:
        doc = (REPO_ROOT / "docs" / "desktop.md").read_text(encoding="utf-8")
        self.assertNotIn("ui-desk", doc)
        self.assertIn("continues running in the background", doc)
        self.assertIn("pywebview>=6.0,<7", doc)
        self.assertIn("chat.send", doc)
        self.assertIn("initialize_home", doc)
        self.assertIn("desktop.log", doc)
        self.assertIn("meshd.log", doc)

    def test_desktop_window_background_matches_dark_canvas(self) -> None:
        app_src = (REPO_ROOT / "mesh" / "desktop" / "app.py").read_text(encoding="utf-8")
        self.assertIn('background_color="#0b0b0c"', app_src)
        self.assertNotIn('background_color="#101216"', app_src)
        self.assertIn("frameless=True", app_src)
        self.assertIn("easy_drag=False", app_src)
        self.assertIn("DesktopJsApi(bridge)", app_src)


if __name__ == "__main__":
    unittest.main()
