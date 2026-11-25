#!/usr/bin/env python3
"""Build or stage the per-user Windows Inno Setup installer for Agent Mesh.

Runs on Windows (native Inno Setup 6 `ISCC.exe`) or Linux (via Wine + Inno Setup 6,
or in `--prepare-only` mode when Inno Setup is not installed on the Linux host).

Outputs in --out (default dist/):
  - When ISCC is available:
      AgentMesh-<ver>-win-x64-setup.exe
      SHA256SUMS.txt (updated with setup.exe checksum)
  - Always (or with --prepare-only):
      installer-stage/          staged app payload matching the portable bundle
      build-installer.ps1       one-line Windows build script for Inno Setup 6
      build-installer-wine.sh   one-line Linux+Wine build script for Inno Setup 6

Per-user install only (`PrivilegesRequired=lowest`, no admin, no paid code signing).
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts import build_windows_portable as portable  # noqa: E402

ISS_PATH = os.path.join(ROOT, "packaging", "windows", "agent-mesh.iss")


def _find_iscc(explicit_iscc: Optional[str] = None) -> Optional[List[str]]:
    """Return argv prefix to invoke Inno Setup's ISCC.exe natively or via Wine, or None."""
    if explicit_iscc:
        if os.name != "nt" and explicit_iscc.lower().endswith(".exe"):
            wine = shutil.which("wine")
            return [wine, explicit_iscc] if wine else None
        return [explicit_iscc]

    if os.name == "nt":
        found = shutil.which("iscc") or shutil.which("ISCC.exe")
        if found:
            return [found]
        for base in (
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("LOCALAPPDATA", ""),
        ):
            if not base:
                continue
            for sub in ("Inno Setup 6\\ISCC.exe", "Programs\\Inno Setup 6\\ISCC.exe"):
                cand = os.path.join(base, sub)
                if os.path.isfile(cand):
                    return [cand]
        return None

    # Linux/POSIX: native wrapper (e.g. `iscc`) or `wine` + Inno Setup 6 in WINEPREFIX
    native = shutil.which("iscc")
    if native:
        return [native]
    wine = shutil.which("wine")
    if wine:
        wineprefix = os.environ.get("WINEPREFIX") or os.path.expanduser("~/.wine")
        for rel in (
            "drive_c/Program Files (x86)/Inno Setup 6/ISCC.exe",
            "drive_c/Program Files/Inno Setup 6/ISCC.exe",
        ):
            cand = os.path.join(wineprefix, rel)
            if os.path.isfile(cand):
                return [wine, cand]
    return None


def _to_wine_path(path: str) -> str:
    abs_p = os.path.abspath(path)
    return "Z:" + abs_p.replace("/", "\\")


def _ps_single_quote(val: str) -> str:
    return "'" + str(val).replace("'", "''") + "'"


def _write_helper_scripts(out_dir: str, stage_dir: str, ver: str) -> None:
    ps1 = os.path.join(out_dir, "build-installer.ps1")
    sh = os.path.join(out_dir, "build-installer-wine.sh")
    out_base = f"AgentMesh-{ver}-win-x64-setup"

    ps1_body = (
        "# Run on Windows with Inno Setup 6 installed (winget install JRSoftware.InnoSetup)\r\n"
        "$ErrorActionPreference = 'Stop'\r\n"
        "$IsccCmd = Get-Command iscc -ErrorAction SilentlyContinue\r\n"
        "$Iscc = if ($IsccCmd) { $IsccCmd.Source } else { $null }\r\n"
        "if (-not $Iscc) { $Iscc = Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\\ISCC.exe' }\r\n"
        "if (-not (Test-Path -LiteralPath $Iscc)) { throw 'Inno Setup 6 ISCC.exe not found.' }\r\n"
        f"& $Iscc {_ps_single_quote(f'/DAppVersion={ver}')} "
        f"{_ps_single_quote(f'/DStageDir={stage_dir}')} "
        f"{_ps_single_quote(f'/DOutputDir={out_dir}')} "
        f"{_ps_single_quote(f'/DOutputBaseFilename={out_base}')} "
        f"{_ps_single_quote(ISS_PATH)}\r\n"
    )
    with open(ps1, "w", encoding="utf-8", newline="") as fp:
        fp.write(ps1_body)

    sh_body = (
        "#!/usr/bin/env bash\n"
        "# Run on Linux with Wine + Inno Setup 6 installed in $WINEPREFIX (default ~/.wine):\n"
        "#   wine innosetup-6.x.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART\n"
        "set -euo pipefail\n"
        'WINEPREFIX="${WINEPREFIX:-$HOME/.wine}"\n'
        'ISCC="${ISCC:-$WINEPREFIX/drive_c/Program Files (x86)/Inno Setup 6/ISCC.exe}"\n'
        'if [ ! -f "$ISCC" ]; then\n'
        '  echo "Inno Setup 6 ISCC.exe not found at: $ISCC" >&2\n'
        '  exit 1\n'
        "fi\n"
        f'wine "$ISCC" '
        f'"/DAppVersion={ver}" '
        f'"/DStageDir={_to_wine_path(stage_dir)}" '
        f'"/DOutputDir={_to_wine_path(out_dir)}" '
        f'"/DOutputBaseFilename={out_base}" '
        f'"{_to_wine_path(ISS_PATH)}"\n'
    )
    with open(sh, "w", encoding="utf-8", newline="\n") as fp:
        fp.write(sh_body)
    try:
        os.chmod(sh, 0o755)
    except OSError:
        pass


def _extract_portable_zip_to_stage(zip_path: str, stage_dir: str) -> None:
    if os.path.exists(stage_dir):
        shutil.rmtree(stage_dir)
    os.makedirs(stage_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mesh-iscc-unzip-") as tmp:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(tmp)
        entries = os.listdir(tmp)
        src = os.path.join(tmp, entries[0]) if len(entries) == 1 and os.path.isdir(os.path.join(tmp, entries[0])) else tmp
        for name in os.listdir(src):
            shutil.move(os.path.join(src, name), os.path.join(stage_dir, name))


def _append_sha256(out_dir: str, artifact_path: str) -> None:
    sums_path = os.path.join(out_dir, "SHA256SUMS.txt")
    basename = os.path.basename(artifact_path)
    digest = portable.sha256(artifact_path)
    lines: List[str] = []
    if os.path.isfile(sums_path):
        with open(sums_path, "r", encoding="utf-8") as fp:
            lines = [ln.rstrip("\n") for ln in fp if ln.strip() and not ln.strip().endswith(f"  {basename}")]
    lines.append(f"{digest}  {basename}")
    with open(sums_path, "w", encoding="utf-8", newline="\n") as fp:
        fp.write("\n".join(lines) + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"), help="output directory")
    ap.add_argument("--portable-zip", help="reuse an existing AgentMesh-*-win-x64-portable.zip instead of rebuilding")
    ap.add_argument("--stage-dir", help="reuse an existing staged directory instead of rebuilding")
    ap.add_argument("--embed-zip", help="local copy of CPython embeddable zip")
    ap.add_argument("--mingit-zip", help="local copy of MinGit zip")
    ap.add_argument("--no-launchers", action="store_true")
    ap.add_argument("--no-mingit", action="store_true")
    ap.add_argument("--iscc", help="explicit path to Inno Setup ISCC.exe")
    ap.add_argument("--prepare-only", action="store_true", help="stage files and helper scripts without invoking ISCC")
    args = ap.parse_args(argv)

    if not os.path.isfile(ISS_PATH):
        raise SystemExit(f"Inno Setup script not found: {ISS_PATH}")

    ver = portable.mesh_version()
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    stage_dir = os.path.abspath(args.stage_dir) if args.stage_dir else os.path.join(out_dir, "installer-stage")

    if args.stage_dir:
        if not os.path.isdir(stage_dir):
            raise SystemExit(f"--stage-dir does not exist: {stage_dir}")
    elif args.portable_zip:
        _extract_portable_zip_to_stage(args.portable_zip, stage_dir)
    else:
        if os.path.exists(stage_dir):
            shutil.rmtree(stage_dir)
        tmp = tempfile.mkdtemp(prefix="mesh-instbuild-")
        try:
            portable.stage_portable_bundle(
                stage=stage_dir,
                wheel_out_dir=out_dir,
                tmp=tmp,
                embed_zip=args.embed_zip,
                mingit_zip=args.mingit_zip,
                no_launchers=args.no_launchers,
                no_mingit=args.no_mingit,
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    _write_helper_scripts(out_dir, stage_dir, ver)
    out_base = f"AgentMesh-{ver}-win-x64-setup"

    if args.prepare_only:
        print(f"prepared Inno Setup staging in {stage_dir}")
        print(f"  Windows script: {os.path.join(out_dir, 'build-installer.ps1')}")
        print(f"  Linux/Wine script: {os.path.join(out_dir, 'build-installer-wine.sh')}")
        return 0

    iscc_cmd = _find_iscc(args.iscc)
    if iscc_cmd is None:
        print(
            "Inno Setup 6 (ISCC.exe) is not installed on this host.\n"
            f"Staged installer payload and scripts in: {out_dir}\n"
            f"  - On Windows: run {os.path.join(out_dir, 'build-installer.ps1')}\n"
            f"  - On Linux with Wine + Inno Setup 6: run {os.path.join(out_dir, 'build-installer-wine.sh')}\n"
            "  - Or distribute the portable zip built by scripts/build_windows_portable.py",
            file=sys.stderr,
        )
        return 0

    uses_wine = len(iscc_cmd) >= 2 and os.path.basename(iscc_cmd[0]).startswith("wine")
    d_stage = _to_wine_path(stage_dir) if uses_wine else stage_dir
    d_out = _to_wine_path(out_dir) if uses_wine else out_dir
    iss_arg = _to_wine_path(ISS_PATH) if uses_wine else ISS_PATH

    cmd = iscc_cmd + [
        f"/DAppVersion={ver}",
        f"/DStageDir={d_stage}",
        f"/DOutputDir={d_out}",
        f"/DOutputBaseFilename={out_base}",
        iss_arg,
    ]
    portable.run(cmd)
    setup_exe = os.path.join(out_dir, f"{out_base}.exe")
    if os.path.isfile(setup_exe):
        _append_sha256(out_dir, setup_exe)
        print("built installer:", setup_exe)
    return 0


if __name__ == "__main__":
    sys.exit(main())
