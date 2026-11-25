"""Unit tests for S3: adapter supported_profiles and safe default_profile selection."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import mesh.constants as c
from mesh.adapters.base import (
    ALL_PROFILES,
    Adapter,
    AdapterNotInstalled,
    ProfileUnsupported,
    SAFE_DEFAULT_PROFILE_PREFERENCE,
)
from mesh.adapters.evidence import LAUNCH_HOLDS
from mesh.adapters.registry import get_adapter, list_adapters
from mesh.cli.commands import agent as agent_cli
from mesh.core.db import connect_db, transaction
from mesh.core.init import init_mesh_home
from mesh.daemon.handlers.adapter import handle_adapter_list, handle_adapter_probe, handle_adapter_show
from mesh.daemon.handlers.agent import handle_agent_create
from mesh.daemon.handlers.base import HandlerContext
from mesh.errors import MeshError


def _make_user_ctx(conn, home: str, cmd: str, args: dict, *, testing_mode: bool = True) -> HandlerContext:
    row = conn.execute("SELECT id, name FROM principals WHERE kind = 'user' LIMIT 1;").fetchone()
    return HandlerContext(
        conn=conn,
        home=home,
        cmd=cmd,
        args=args,
        principal={"id": row["id"], "kind": "user", "name": row["name"]},
        actor_kind="user",
        via="direct",
        request_id="req_s3_test",
        k_user=b"k" * 32,
        testing_mode=testing_mode,
    )


class TestS3AdapterSupportedProfiles(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mh-s3-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}):
            init_mesh_home(self.home, "test-passphrase-1234")
        self.conn = connect_db(os.path.join(self.home, "mesh.db"))
        self.addCleanup(self.conn.close)

    def test_every_adapter_supported_profiles_matches_profile_flags(self) -> None:
        for adapter in list_adapters(testing_mode=True):
            actual_supported = []
            for prof in ALL_PROFILES:
                try:
                    adapter.profile_flags(prof)
                    actual_supported.append(prof)
                except (ProfileUnsupported, AdapterNotInstalled):
                    pass
            self.assertEqual(
                list(adapter.supported_profiles),
                actual_supported,
                f"Adapter '{adapter.id}' supported_profiles drifted from profile_flags()",
            )

    def test_default_safe_profile_preferences_and_safety_invariants(self) -> None:
        self.assertNotIn("elevated", SAFE_DEFAULT_PROFILE_PREFERENCE)
        self.assertNotIn("unrestricted", SAFE_DEFAULT_PROFILE_PREFERENCE)

        expected = {
            "droid": "edit",
            "agy": "edit",
            "kiro": "edit",
            "vibe": "edit",
            "claude": "edit",
            "codex": "dev",
            "none": "edit",
            "fake": "edit",
            "devin": None,      # only supports unrestricted -> must NOT default to unrestricted
            "hermes": None,     # stub
            "openclaw": None,   # stub
        }
        for aid, want in expected.items():
            ad = get_adapter(aid, testing_mode=True)
            self.assertEqual(ad.default_safe_profile(), want, f"Unexpected safe default for {aid}")

        # Synthetic adapter supporting only elevated/unrestricted must return None
        class _HighOnly(Adapter):
            id = "high_only"
            supported_profiles = ("elevated", "unrestricted")

        self.assertIsNone(_HighOnly().default_safe_profile())

        # Synthetic adapter supporting only readonly/unrestricted picks readonly
        class _ReadonlyOrUnrestricted(Adapter):
            id = "ro_or_unres"
            supported_profiles = ("readonly", "unrestricted")

        self.assertEqual(_ReadonlyOrUnrestricted().default_safe_profile(), "readonly")

    def test_adapter_list_show_and_probe_expose_supported_profiles(self) -> None:
        # Mock resolve_binary so no external CLI binaries are invoked
        with mock.patch.object(Adapter, "resolve_binary", return_value=None):
            with transaction(self.conn):
                list_res = handle_adapter_list(_make_user_ctx(self.conn, self.home, "adapter.list", {}), testing_mode=True)
            items = list_res.data["items"]
            by_id = {item["id"]: item for item in items}
            self.assertIn("codex", by_id)
            self.assertEqual(by_id["codex"]["supported_profiles"], ["readonly", "dev", "unrestricted"])
            self.assertEqual(by_id["claude"]["supported_profiles"], ["readonly", "edit", "unrestricted"])
            self.assertEqual(by_id["devin"]["supported_profiles"], ["unrestricted"])
            self.assertEqual(by_id["hermes"]["supported_profiles"], [])
            self.assertEqual(by_id["droid"]["supported_profiles"], ["readonly", "edit", "dev", "elevated", "unrestricted"])

            # F2 & F4: model_presets and profile_flags exposed in adapter.list
            self.assertEqual(by_id["claude"]["model_presets"]["chips"], ["sonnet", "opus", "haiku"])
            self.assertEqual(
                by_id["codex"]["model_presets"]["chips"],
                ["gpt-6.2-sol", "gpt-6.1-sol", "gpt-6-luna", "gpt-5.6-luna"],
            )
            self.assertEqual(by_id["codex"]["model_presets"]["default_allowed"], [])
            self.assertEqual(by_id["vibe"]["model_presets"]["chips"], ["mistral-medium-3.5"])
            self.assertEqual(by_id["vibe"]["model_presets"]["default_allowed"], ["mistral-medium-3.5"])
            self.assertIn("~/.vibe/config.toml", by_id["vibe"]["model_presets"]["note"])
            self.assertEqual(by_id["devin"]["model_presets"]["default_allowed"], [])
            self.assertIsNone(by_id["droid"]["model_presets"])
            self.assertEqual(
                by_id["codex"]["profile_flags"]["unrestricted"],
                ["--dangerously-bypass-approvals-and-sandbox"],
            )

            for aid in ("codex", "claude", "vibe", "devin", "droid", "hermes"):
                ad = get_adapter(aid, testing_mode=True)
                with transaction(self.conn):
                    show_res = handle_adapter_show(
                        _make_user_ctx(self.conn, self.home, "adapter.show", {"adapter_id": aid}),
                        testing_mode=True,
                    )
                self.assertEqual(show_res.data["supported_profiles"], list(ad.supported_profiles))
                self.assertEqual(show_res.data["model_presets"], ad.model_presets)
                for prof in ad.supported_profiles:
                    self.assertEqual(show_res.data["profile_flags"][prof], ad.profile_flags(prof))

                with transaction(self.conn):
                    probe_res = handle_adapter_probe(
                        _make_user_ctx(self.conn, self.home, "adapter.probe", {"adapter_id": aid}),
                        testing_mode=True,
                    )
                self.assertEqual(probe_res.data["supported_profiles"], list(ad.supported_profiles))
                self.assertEqual(probe_res.data["model_presets"], ad.model_presets)

    def test_model_presets_valid_and_include_smoke_evidence_models(self) -> None:
        import json
        from pathlib import Path
        from mesh.adapters.base import is_safe_cli_token

        results_dir = Path(__file__).resolve().parents[2] / "tests" / "smoke" / "results"
        # Known non-vendor proxy/custom aliases used in 2026-11-19 smoke runs (not first-party model IDs):
        # - claude: ds-flash (DeepSeek via Anthropic-compatible proxy; stderr logs unrecognized_model)
        # - vibe: ds-flash (custom ~/.vibe/config.toml alias; vibe 3.1.0 only ships mistral-medium-3.5)
        non_vendor_proxy_models = {("claude", "ds-flash"), ("vibe", "ds-flash")}

        for ad in list_adapters(testing_mode=True):
            presets = ad.model_presets
            if presets is None:
                continue
            chips = presets.get("chips")
            default_allowed = presets.get("default_allowed")
            note = presets.get("note")
            self.assertIsInstance(chips, list, f"{ad.id}: chips must be a list")
            self.assertTrue(chips, f"{ad.id}: chips must be non-empty")
            self.assertEqual(len(chips), len(set(chips)), f"{ad.id}: duplicate model chips")
            for m in chips:
                self.assertTrue(is_safe_cli_token(m), f"{ad.id}: invalid model token {m!r}")
            self.assertIsInstance(default_allowed, list, f"{ad.id}: default_allowed must be a list")
            for m in default_allowed:
                self.assertIn(m, chips, f"{ad.id}: default_allowed item {m!r} not in chips")
            self.assertIsInstance(note, str)
            self.assertTrue(note.strip(), f"{ad.id}: note must be non-empty")

            # Verify latest committed smoke-evidence model (if any first-party vendor model) is in chips
            matches = sorted(
                p for p in results_dir.glob(f"{ad.id}-*.json") if not p.name.endswith(".meta.json")
            )
            if matches:
                doc = json.loads(matches[-1].read_text(encoding="utf-8"))
                smoke_model = doc.get("model")
                if smoke_model and (ad.id, smoke_model) not in non_vendor_proxy_models:
                    self.assertIn(
                        smoke_model,
                        chips,
                        f"{ad.id}: committed smoke-evidence model {smoke_model!r} (from {matches[-1].name}) "
                        f"must be included in model_presets['chips']={chips}",
                    )

    def test_agent_create_omitted_default_profile_picks_safe_default_per_adapter(self) -> None:
        with mock.patch.object(Adapter, "is_installed", return_value=True):
            # 1. codex with omitted default_profile -> dev (not unsupported edit!)
            with transaction(self.conn):
                res_codex = handle_agent_create(
                    _make_user_ctx(
                        self.conn,
                        self.home,
                        "agent.create",
                        {
                            "name": "worker-codex",
                            "adapter": "codex",
                            "delivery": "launch",
                            "allowed_models": ["gpt-5.4"],
                        },
                    )
                )
            self.assertEqual(res_codex.data["default_profile"], "dev")
            self.assertEqual(res_codex.data["max_unapproved_profile"], "dev")

            # 2. claude with omitted default_profile -> edit
            with transaction(self.conn):
                res_claude = handle_agent_create(
                    _make_user_ctx(
                        self.conn,
                        self.home,
                        "agent.create",
                        {
                            "name": "worker-claude",
                            "adapter": "claude",
                            "delivery": "launch",
                            "allowed_models": ["sonnet"],
                        },
                    )
                )
            self.assertEqual(res_claude.data["default_profile"], "edit")

            # 3. devin (only supports unrestricted) with omitted default_profile -> keeps "edit" (NEVER unrestricted!)
            self.assertEqual(LAUNCH_HOLDS.get("devin"), "M5")
            with transaction(self.conn):
                res_devin = handle_agent_create(
                    _make_user_ctx(
                        self.conn,
                        self.home,
                        "agent.create",
                        {
                            "name": "worker-devin",
                            "adapter": "devin",
                            "delivery": "launch",
                            "allowed_models": ["swe-2-flash"],
                        },
                    )
                )
            self.assertEqual(res_devin.data["default_profile"], "edit")
            self.assertEqual(res_devin.data["max_unapproved_profile"], "dev")

            # 4. Explicit default_profile="edit" for codex is NOT silently broadened to dev
            with transaction(self.conn):
                res_codex_explicit = handle_agent_create(
                    _make_user_ctx(
                        self.conn,
                        self.home,
                        "agent.create",
                        {
                            "name": "worker-codex-edit",
                            "adapter": "codex",
                            "delivery": "launch",
                            "allowed_models": ["gpt-5.4"],
                            "default_profile": "edit",
                        },
                    )
                )
            self.assertEqual(res_codex_explicit.data["default_profile"], "edit")

            # 5. Explicit default_profile=None or "" is still rejected
            for bad in (None, ""):
                with self.assertRaises(MeshError) as cm:
                    handle_agent_create(
                        _make_user_ctx(
                            self.conn,
                            self.home,
                            "agent.create",
                            {
                                "name": f"bad-codex-{bad!r}",
                                "adapter": "codex",
                                "delivery": "launch",
                                "allowed_models": ["gpt-5.4"],
                                "default_profile": bad,
                            },
                        )
                    )
                self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT)

    def test_cli_agent_create_and_onboard_delegate_default_profile_to_daemon(self) -> None:
        sent = []

        class _CapClient:
            def __init__(self, home=None):
                pass

            def execute_cmd(self, args, cmd, payload):
                sent.append((cmd, dict(payload)))
                if cmd == "agent.token.issue":
                    return {"data": {"credential_id": "cred_1", "token": "msh_agt_abc_secret"}}
                return {"data": {"id": "agt_1", "name": payload.get("name")}}

        def _ns(adapter: str, **overrides):
            d = dict(
                home=self.home,
                name=f"ag-{adapter}",
                adapter=adapter,
                delivery="launch" if adapter != "none" else "inbox",
                default_model=None,
                allowed_models=["m1"] if adapter != "none" else None,
                allowed_model=[],
                default_effort=None,
                allowed_efforts=None,
                allowed_effort=[],
                max_concurrency=None,
                can_lead=False,
                capabilities=None,
                capability=None,
                default_profile=None,
                max_unapproved_profile=None,
                binary=None,
                options=None,
                option=[],
                strict_models=False,
                no_intake=False,
                ack_timeout=None,
                _from_json=set(),
            )
            d.update(overrides)
            return SimpleNamespace(**d)

        with mock.patch.object(agent_cli, "MeshClient", _CapClient):
            for aid in ("codex", "claude", "devin", "none"):
                agent_cli.handle_agent_create(_ns(aid))
                self.assertEqual(sent[-1][0], "agent.create")
                self.assertNotIn(
                    "default_profile",
                    sent[-1][1],
                    f"CLI must omit default_profile when unset for {aid} so meshd is single source of truth",
                )

            # Explicit override on codex is preserved
            agent_cli.handle_agent_create(_ns("codex", default_profile="readonly"))
            self.assertEqual(sent[-1][1]["default_profile"], "readonly")

            # Explicit null from --from-json is still sent (so meshd refuses it)
            agent_cli.handle_agent_create(_ns("codex", default_profile=None, _from_json={"default_profile"}))
            self.assertIn("default_profile", sent[-1][1])
            self.assertIsNone(sent[-1][1]["default_profile"])

            # handle_agent_onboard also omits hardcoded default_profile
            tok_dir = os.path.join(self.tmp, "tokens")
            os.makedirs(tok_dir, mode=0o700, exist_ok=True)
            out_path = os.path.join(tok_dir, "w1.token")
            onboard_ns = SimpleNamespace(
                home=self.home,
                name="w1",
                out=out_path,
                force=False,
                adapter="none",
                max_concurrency="1",
                ack_timeout=None,
                capability=None,
                no_intake=False,
                expires_in=None,
                dry_run=True,
                idempotency_key=None,
                _from_json=set(),
            )
            agent_cli.handle_agent_onboard(onboard_ns)
            self.assertEqual(sent[-1][0], "agent.create")
            self.assertNotIn("default_profile", sent[-1][1])


if __name__ == "__main__":
    unittest.main()
