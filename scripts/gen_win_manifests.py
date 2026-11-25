#!/usr/bin/env python3
"""Generate Windows Scoop and Winget (v1.6) manifests for Agent Mesh (Batch 3 B4).

Pure Python stdlib — runs identically on Linux and Windows. Does NOT publish
anything anywhere and does NOT require code signing.

Usage examples:
  python3 scripts/gen_win_manifests.py --version 1.0.0 --sha256sums dist/SHA256SUMS.txt
  python3 scripts/gen_win_manifests.py --out-dir packaging
  python3 scripts/gen_win_manifests.py --check --out-dir packaging
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_REPO = "JamesHu6657/agent-mesh-kiroween"
DEFAULT_PUBLISHER = "AgentMesh"
DEFAULT_PACKAGE_ID = "AgentMesh.AgentMesh"
DEFAULT_PACKAGE_NAME = "Agent Mesh"
WINGET_SCHEMA_VERSION = "1.6.0"
PLACEHOLDER_SHA256 = "0" * 64

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_SAFE_VER_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]*$")
_SAFE_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SAFE_PKG_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_URL_RE = re.compile(r"^https?://[^\s'\"\\<>#]+$")
_PLAIN_YAML_SCALAR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")


def read_mesh_version() -> str:
    init_py = ROOT / "mesh" / "__init__.py"
    ns: Dict[str, object] = {}
    for line in init_py.read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            exec(line, ns)
    return str(ns.get("__version__", "1.0.0.dev1"))


def parse_sha256sums(path: Path) -> Dict[str, str]:
    """Parse standard ``<hex>  <filename>`` lines from a ``SHA256SUMS.txt`` file."""
    out: Dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        digest = parts[0].strip()
        filename = parts[-1].lstrip("*").strip()
        if _SHA256_RE.match(digest) and filename:
            out[filename] = digest.lower()
    return out


def _validate_sha256(digest: str, label: str) -> str:
    cleaned = digest.strip().lower()
    if not _SHA256_RE.match(cleaned):
        raise ValueError(f"Invalid SHA-256 hex digest for {label}: {digest!r}")
    return cleaned


def _yaml_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _validate_version(version: str) -> str:
    if not isinstance(version, str) or not _SAFE_VER_RE.match(version):
        raise ValueError(f"Invalid version string: {version!r}")
    return version


def _validate_repo(repo: str) -> str:
    if not isinstance(repo, str) or not _SAFE_REPO_RE.match(repo):
        raise ValueError(f"Invalid GitHub owner/repo slug: {repo!r}")
    return repo


def _validate_package_id(package_id: str) -> str:
    if (
        not isinstance(package_id, str)
        or not _SAFE_PKG_ID_RE.match(package_id)
        or ".." in package_id
    ):
        raise ValueError(f"Invalid Winget package_id: {package_id!r}")
    return package_id


def _validate_release_base_url(url: str) -> str:
    cleaned = url.rstrip("/") if isinstance(url, str) else ""
    if not cleaned or not _SAFE_URL_RE.match(cleaned) or any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
        raise ValueError(f"Invalid release_base_url: {url!r}")
    return cleaned


def _yaml_safe_scalar(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Invalid {label}: must be a non-empty string")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError(f"Invalid {label}: control characters/newlines are disallowed")
    if _PLAIN_YAML_SCALAR_RE.match(value) and not value.endswith(" "):
        return value
    return _yaml_quote(value)


def build_scoop_manifest(
    *,
    version: str,
    repo: str,
    portable_sha256: str,
    release_base_url: Optional[str] = None,
) -> Dict[str, object]:
    """Build a Scoop manifest dictionary for ``AgentMesh-<ver>-win-x64-portable.zip``."""
    version = _validate_version(version)
    repo = _validate_repo(repo)
    base = _validate_release_base_url(release_base_url or f"https://github.com/{repo}/releases/download")
    zip_name = f"AgentMesh-{version}-win-x64-portable.zip"
    extract_dir = f"AgentMesh-{version}-win-x64-portable"
    return {
        "$schema": "https://raw.githubusercontent.com/ScoopInstaller/Scoop/master/schema.json",
        "version": version,
        "description": "Local-first multi-agent orchestrator with chat-first desktop UI, inline approval cards, and cryptographic audit chain.",
        "homepage": f"https://github.com/{repo}",
        "license": "MIT",
        "notes": [
            "Launch the desktop client from Start Menu ('Agent Mesh') or run 'mesh --help' in your terminal.",
            "Your data directory (%USERPROFILE%\\.agent-mesh) is preserved across upgrades and uninstalls.",
            "Requires Microsoft Edge WebView2 Runtime (preinstalled on Windows 11).",
        ],
        "architecture": {
            "64bit": {
                "url": f"{base}/v{version}/{zip_name}",
                "hash": _validate_sha256(portable_sha256, "portable_sha256"),
                "extract_dir": extract_dir,
            }
        },
        "bin": ["mesh.exe"],
        "shortcuts": [["Agent Mesh.exe", "Agent Mesh"]],
        "checkver": {
            "github": f"https://github.com/{repo}",
        },
        "autoupdate": {
            "architecture": {
                "64bit": {
                    "url": f"{base}/v$version/AgentMesh-$version-win-x64-portable.zip",
                    "extract_dir": "AgentMesh-$version-win-x64-portable",
                }
            },
            "hash": {
                "url": f"{base}/v$version/SHA256SUMS.txt",
            },
        },
    }


def build_winget_manifests(
    *,
    version: str,
    repo: str,
    portable_sha256: str,
    setup_sha256: str,
    package_id: str = DEFAULT_PACKAGE_ID,
    publisher: str = DEFAULT_PUBLISHER,
    package_name: str = DEFAULT_PACKAGE_NAME,
    release_base_url: Optional[str] = None,
) -> Dict[str, str]:
    """Return Winget v1.6.0 manifest YAML files mapped by filename."""
    version = _validate_version(version)
    repo = _validate_repo(repo)
    package_id = _validate_package_id(package_id)
    publisher_yaml = _yaml_safe_scalar(publisher, "publisher")
    package_name_yaml = _yaml_safe_scalar(package_name, "package_name")
    base = _validate_release_base_url(release_base_url or f"https://github.com/{repo}/releases/download")
    portable_hash = _validate_sha256(portable_sha256, "portable_sha256").upper()
    setup_hash = _validate_sha256(setup_sha256, "setup_sha256").upper()
    zip_name = f"AgentMesh-{version}-win-x64-portable.zip"
    zip_root = f"AgentMesh-{version}-win-x64-portable"
    setup_name = f"AgentMesh-{version}-win-x64-setup.exe"

    version_yaml = "\n".join(
        [
            f"# yaml-language-server: $schema=https://aka.ms/winget-manifest.version.{WINGET_SCHEMA_VERSION}.schema.json",
            f"PackageIdentifier: {package_id}",
            f'PackageVersion: "{version}"',
            "DefaultLocale: en-US",
            "ManifestType: version",
            f"ManifestVersion: {WINGET_SCHEMA_VERSION}",
            "",
        ]
    )

    installer_yaml = "\n".join(
        [
            f"# yaml-language-server: $schema=https://aka.ms/winget-manifest.installer.{WINGET_SCHEMA_VERSION}.schema.json",
            f"PackageIdentifier: {package_id}",
            f'PackageVersion: "{version}"',
            "Platform:",
            "  - Windows.Desktop",
            "MinimumOSVersion: 10.0.17763.0",
            "UpgradeBehavior: install",
            "Installers:",
            "  - Architecture: x64",
            "    Scope: user",
            "    InstallerType: inno",
            f"    InstallerUrl: {base}/v{version}/{setup_name}",
            f'    InstallerSha256: "{setup_hash}"',
            "    InstallerSwitches:",
            "      Silent: /VERYSILENT /SUPPRESSMSGBOXES /NORESTART",
            "      SilentWithProgress: /SILENT /SUPPRESSMSGBOXES /NORESTART",
            "  - Architecture: x64",
            "    Scope: user",
            "    InstallerType: zip",
            "    NestedInstallerType: portable",
            "    NestedInstallerFiles:",
            f"      - RelativeFilePath: {zip_root}\\mesh.exe",
            "        PortableCommandAlias: mesh",
            f"    InstallerUrl: {base}/v{version}/{zip_name}",
            f'    InstallerSha256: "{portable_hash}"',
            "ManifestType: installer",
            f"ManifestVersion: {WINGET_SCHEMA_VERSION}",
            "",
        ]
    )

    locale_yaml = "\n".join(
        [
            f"# yaml-language-server: $schema=https://aka.ms/winget-manifest.defaultLocale.{WINGET_SCHEMA_VERSION}.schema.json",
            f"PackageIdentifier: {package_id}",
            f'PackageVersion: "{version}"',
            "PackageLocale: en-US",
            f"Publisher: {publisher_yaml}",
            f"PublisherUrl: https://github.com/{repo}",
            f"PublisherSupportUrl: https://github.com/{repo}/issues",
            f"PackageName: {package_name_yaml}",
            f"PackageUrl: https://github.com/{repo}",
            "License: MIT",
            f"LicenseUrl: https://github.com/{repo}/blob/main/LICENSE",
            "ShortDescription: Local-first multi-agent orchestrator with chat-first desktop UI and inline approval cards.",
            "Description: >-",
            "  Agent Mesh coordinates local AI coding CLIs (Claude Code, Codex, Antigravity, Droid, Kiro, Vibe)",
            "  with a single Lead planner, per-task Git worktrees, inline card-button approvals, standing grants,",
            "  and a hash-chained cryptographic audit log. Runs strictly locally with no network listener.",
            "Moniker: agent-mesh",
            "Tags:",
            "  - ai",
            "  - agent",
            "  - orchestrator",
            "  - git-worktree",
            "  - desktop",
            f"ReleaseNotesUrl: https://github.com/{repo}/releases/tag/v{version}",
            "ManifestType: defaultLocale",
            f"ManifestVersion: {WINGET_SCHEMA_VERSION}",
            "",
        ]
    )

    return {
        f"{package_id}.yaml": version_yaml,
        f"{package_id}.installer.yaml": installer_yaml,
        f"{package_id}.locale.en-US.yaml": locale_yaml,
    }


def generate_all_manifests(
    *,
    out_dir: Path,
    version: str,
    repo: str = DEFAULT_REPO,
    portable_sha256: str = PLACEHOLDER_SHA256,
    setup_sha256: str = PLACEHOLDER_SHA256,
    package_id: str = DEFAULT_PACKAGE_ID,
    publisher: str = DEFAULT_PUBLISHER,
    package_name: str = DEFAULT_PACKAGE_NAME,
    release_base_url: Optional[str] = None,
) -> List[Path]:
    """Write Scoop and Winget manifests under ``<out_dir>/scoop/`` and ``<out_dir>/winget/``."""
    version = _validate_version(version)
    repo = _validate_repo(repo)
    package_id = _validate_package_id(package_id)

    written: List[Path] = []
    scoop_dir = out_dir / "scoop"
    winget_dir = out_dir / "winget"
    scoop_dir.mkdir(parents=True, exist_ok=True)
    winget_dir.mkdir(parents=True, exist_ok=True)

    scoop_doc = build_scoop_manifest(
        version=version,
        repo=repo,
        portable_sha256=portable_sha256,
        release_base_url=release_base_url,
    )
    scoop_path = scoop_dir / "agent-mesh.json"
    scoop_path.write_text(json.dumps(scoop_doc, indent=2) + "\n", encoding="utf-8", newline="\n")
    written.append(scoop_path)

    winget_files = build_winget_manifests(
        version=version,
        repo=repo,
        portable_sha256=portable_sha256,
        setup_sha256=setup_sha256,
        package_id=package_id,
        publisher=publisher,
        package_name=package_name,
        release_base_url=release_base_url,
    )
    for fname, content in winget_files.items():
        target = winget_dir / fname
        target.write_text(content, encoding="utf-8", newline="\n")
        written.append(target)

    return written


def verify_manifests(
    out_dir: Path,
    package_id: str = DEFAULT_PACKAGE_ID,
    *,
    allow_placeholders: bool = False,
) -> Tuple[bool, List[str]]:
    """Validate Scoop & Winget manifests for structure, schema, and non-placeholder hashes.

    By default (``allow_placeholders=False``), all-zero placeholder SHA-256 digests are
    rejected so unfilled release manifests fail closed. Pass ``allow_placeholders=True``
    only when validating checked-in template manifests.
    """
    errors: List[str] = []
    scoop_path = out_dir / "scoop" / "agent-mesh.json"
    if not scoop_path.is_file():
        errors.append(f"Missing Scoop manifest: {scoop_path}")
    else:
        try:
            doc = json.loads(scoop_path.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"Invalid JSON in {scoop_path}: {exc}")
            doc = {}
        if isinstance(doc, dict):
            for key in ("version", "description", "homepage", "license", "architecture", "bin", "shortcuts"):
                if key not in doc:
                    errors.append(f"Scoop manifest missing key: {key}")
            arch64 = (doc.get("architecture") or {}).get("64bit") if isinstance(doc.get("architecture"), dict) else None
            if not isinstance(arch64, dict):
                errors.append("Scoop manifest missing architecture.64bit")
            else:
                hval = str(arch64.get("hash", "")).strip().lower()
                if not _SHA256_RE.match(hval):
                    errors.append(f"Scoop manifest has invalid 64bit hash: {arch64.get('hash')!r}")
                elif not allow_placeholders and hval == PLACEHOLDER_SHA256:
                    errors.append("Scoop manifest contains unfilled placeholder SHA-256 (000...0)")
                if not str(arch64.get("url", "")).endswith("-win-x64-portable.zip"):
                    errors.append(f"Scoop manifest url must target portable zip: {arch64.get('url')!r}")

    winget_dir = out_dir / "winget"
    expected_files = (
        f"{package_id}.yaml",
        f"{package_id}.installer.yaml",
        f"{package_id}.locale.en-US.yaml",
    )
    for fname in expected_files:
        fpath = winget_dir / fname
        if not fpath.is_file():
            errors.append(f"Missing Winget manifest: {fpath}")
            continue
        text = fpath.read_text(encoding="utf-8")
        if f"PackageIdentifier: {package_id}" not in text:
            errors.append(f"{fname} missing PackageIdentifier: {package_id}")
        ver_match = re.search(r'^PackageVersion:\s*(.+?)\s*$', text, re.M)
        if not ver_match:
            errors.append(f"{fname} missing PackageVersion")
        else:
            raw_ver = ver_match.group(1)
            if not (len(raw_ver) >= 2 and raw_ver[0] == '"' and raw_ver[-1] == '"'):
                errors.append(f"{fname} PackageVersion must be a quoted YAML string: {raw_ver!r}")
            elif not _SAFE_VER_RE.match(raw_ver[1:-1]):
                errors.append(f"{fname} has invalid PackageVersion: {raw_ver!r}")
        if f"ManifestVersion: {WINGET_SCHEMA_VERSION}" not in text:
            errors.append(f"{fname} missing ManifestVersion: {WINGET_SCHEMA_VERSION}")
        if fname.endswith(".installer.yaml"):
            for req in ("Scope: user", "InstallerType: inno", "InstallerType: zip", "NestedInstallerType: portable"):
                if req not in text:
                    errors.append(f"{fname} missing required field {req!r}")
            sha_matches = list(re.finditer(r"^\s*InstallerSha256:\s*(.+?)\s*$", text, re.M))
            if not sha_matches:
                errors.append(f"{fname} missing InstallerSha256 entries")
            for match in sha_matches:
                raw_digest = match.group(1).strip()
                if not (len(raw_digest) >= 2 and raw_digest[0] == '"' and raw_digest[-1] == '"'):
                    errors.append(f"{fname} InstallerSha256 must be a quoted YAML string: {raw_digest!r}")
                    continue
                digest = raw_digest[1:-1].strip().lower()
                if not _SHA256_RE.match(digest):
                    errors.append(f"{fname} has invalid InstallerSha256: {raw_digest!r}")
                elif not allow_placeholders and digest == PLACEHOLDER_SHA256:
                    errors.append(f"{fname} contains unfilled placeholder InstallerSha256 (000...0)")

    return (len(errors) == 0, errors)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", default=None, help="Release version (default: mesh.__version__)")
    ap.add_argument("--repo", default=DEFAULT_REPO, help=f"GitHub owner/repo (default: {DEFAULT_REPO})")
    ap.add_argument("--out-dir", default=str(ROOT / "packaging"), help="Output directory (default: packaging/)")
    ap.add_argument("--sha256sums", help="Path to SHA256SUMS.txt to read portable zip & setup.exe hashes")
    ap.add_argument("--portable-sha256", help="Explicit SHA-256 hex digest for AgentMesh-<ver>-win-x64-portable.zip")
    ap.add_argument("--setup-sha256", help="Explicit SHA-256 hex digest for AgentMesh-<ver>-win-x64-setup.exe")
    ap.add_argument("--release-base-url", help="Optional custom releases/download base URL")
    ap.add_argument("--check", action="store_true", help="Validate existing manifests in --out-dir without writing")
    ap.add_argument(
        "--allow-placeholders",
        action="store_true",
        help="Allow all-zero placeholder SHA-256 hashes (for validating checked-in templates only)",
    )
    args = ap.parse_args(argv)

    out_dir = Path(args.out_dir).resolve()
    if args.check:
        ok, errs = verify_manifests(out_dir, allow_placeholders=args.allow_placeholders)
        if not ok:
            for e in errs:
                print(f"ERROR: {e}", file=sys.stderr)
            return 1
        print(f"OK: verified Scoop & Winget manifests in {out_dir}")
        return 0

    version = args.version or read_mesh_version()
    portable_hash = args.portable_sha256 or PLACEHOLDER_SHA256
    setup_hash = args.setup_sha256 or PLACEHOLDER_SHA256

    if args.sha256sums:
        sums = parse_sha256sums(Path(args.sha256sums))
        zip_name = f"AgentMesh-{version}-win-x64-portable.zip"
        setup_name = f"AgentMesh-{version}-win-x64-setup.exe"
        if not args.portable_sha256 and zip_name in sums:
            portable_hash = sums[zip_name]
        if not args.setup_sha256 and setup_name in sums:
            setup_hash = sums[setup_name]

    written = generate_all_manifests(
        out_dir=out_dir,
        version=version,
        repo=args.repo,
        portable_sha256=portable_hash,
        setup_sha256=setup_hash,
        release_base_url=args.release_base_url,
    )
    ok, errs = verify_manifests(out_dir, allow_placeholders=args.allow_placeholders)
    if not ok:
        for e in errs:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1
    for p in written:
        print("wrote:", p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
