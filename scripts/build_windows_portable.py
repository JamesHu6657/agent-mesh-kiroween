#!/usr/bin/env python3
"""Build the portable Windows bundle of Agent Mesh (desktop client + CLI + optional MinGit).

Runs on Linux or Windows. Output (in --out, default dist/):

  agent_mesh-<ver>-py3-none-any.whl      the plain wheel (pip install "...whl[desktop]")
  AgentMesh-<ver>-win-x64-portable.zip   unzip anywhere, double-click "Agent Mesh.exe"
  SHA256SUMS.txt

The zip holds:
- The official CPython 3.12 embeddable distribution + the wheel and pywebview/pythonnet
  in python/Lib/site-packages
- Two tiny native launchers compiled from packaging/windows/launcher.c with icon/version
  resources from packaging/windows/launcher.rc ("Agent Mesh.exe" = GUI, "mesh.exe" = CLI)
  that invoke CPython with `-P -s` and sanitized PYTHONPATH directly (no double-spawn hop)
- Official pinned Git for Windows MinGit in mingit/ (downloaded and SHA256-verified at
  build time, never at runtime; preferred by meshd over PATH git when bundled, with
  fallback to system git if omitted)

Launchers need `zig` (pip install ziglang) or x86_64-w64-mingw32-gcc; with --no-launchers
they are skipped and only the .cmd fallbacks are written. Pass --no-mingit to omit MinGit.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from typing import Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY_VERSION = "3.12.10"
PY_EMBED_URL = f"https://www.python.org/ftp/python/{PY_VERSION}/python-{PY_VERSION}-embed-amd64.zip"
PY_EMBED_SHA256 = "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"

# Official Git for Windows MinGit (build-time download only; never downloaded at runtime).
MINGIT_VERSION = "2.56.1"
MINGIT_TAG = "v2.56.1.windows.1"
MINGIT_URL = (
    f"https://github.com/git-for-windows/git/releases/download/"
    f"{MINGIT_TAG}/MinGit-{MINGIT_VERSION}-64-bit.zip"
)
MINGIT_SHA256 = "4a92850b6e917da4fd010669b614b55dccf5b0ec333a534cc4be4700dfefdafd"

# pywebview deps published only as sdists; they are pure Python, so a locally built wheel is fine.
PURE_SDIST_DEPS = ["proxy_tools"]
# pip evaluates environment markers against the *build* host, so pywebview's
# `pythonnet; sys_platform == "win32"` must be requested explicitly when building on Linux.
WIN32_MARKER_DEPS = ["pythonnet>=3.0.3"]

README = """Agent Mesh {ver} - Windows portable

Requirements
  - Windows 10/11 x64
  - Microsoft Edge WebView2 Runtime (preinstalled on Windows 11; otherwise
    https://go.microsoft.com/fwlink/p/?LinkId=2124703)
  - .NET Framework 4.7.2+ (preinstalled on Windows 10 1803+ / 11)
  - Git: {git_req}

Use
  1. Unzip the whole folder anywhere (keep the "python" folder{mingit_keep} next to the .exe files).
  2. Double-click "Agent Mesh.exe". The window starts meshd automatically.
     On first launch, follow the 2-step first-run wizard (set passphrase,
     pick your Lead CLI, and land directly in chat).
  3. Closing the window minimizes Agent Mesh to the Windows system tray by default
     (keeping your in-memory session and background meshd running). Use the tray
     icon to show/hide the window or quit (quitting stops nothing unless you
     explicitly choose "Quit and stop daemon").
  4. (Optional CLI setup) You can also run `mesh.exe setup` in a terminal if you prefer CLI setup.

  CLI: mesh.exe <command>   (same as the "mesh" command; mesh.exe --help)
  Data lives in %USERPROFILE%\\.agent-mesh unless you pass --home or set MESH_HOME.
  If the .exe launchers are blocked, use "Agent Mesh.cmd" / "mesh.cmd" instead.

Bundled: CPython {py} embeddable (PSF license), pywebview and its dependencies
(see python\\Lib\\site-packages\\*.dist-info for licenses){mingit_license}. Agent Mesh is MIT licensed.
"""


def run(cmd, **kw):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def mesh_version() -> str:
    ns: Dict[str, object] = {}
    with open(os.path.join(ROOT, "mesh", "__init__.py"), encoding="utf-8") as f:
        for line in f:
            if line.startswith("__version__"):
                exec(line, ns)
    return str(ns["__version__"])


def find_cc() -> Optional[List[str]]:
    """Return a cross C compiler argv prefix for x86_64 Windows, or None."""
    if os.name == "nt" and shutil.which("gcc"):
        return ["gcc"]
    if shutil.which("x86_64-w64-mingw32-gcc"):
        return ["x86_64-w64-mingw32-gcc"]
    if shutil.which("zig"):
        return ["zig", "cc", "-target", "x86_64-windows-gnu"]
    try:
        import ziglang  # noqa: F401
        return [sys.executable, "-m", "ziglang", "cc", "-target", "x86_64-windows-gnu"]
    except ImportError:
        return None


def find_rc() -> Optional[tuple[str, List[str]]]:
    """Return ``(kind, argv_prefix)`` for a Windows resource compiler, or None."""
    if shutil.which("zig"):
        return ("zig_rc", ["zig", "rc"])
    try:
        import ziglang  # noqa: F401
        return ("zig_rc", [sys.executable, "-m", "ziglang", "rc"])
    except ImportError:
        pass
    if shutil.which("x86_64-w64-mingw32-windres"):
        return ("windres", ["x86_64-w64-mingw32-windres"])
    if os.name == "nt" and shutil.which("windres"):
        return ("windres", ["windres"])
    return None


def _compile_rc(rc_info: Optional[tuple[str, List[str]]], rc_src: str, res_out: str, *, is_cli: bool) -> Optional[str]:
    if rc_info is None or not os.path.isfile(rc_src):
        return None
    kind, prefix = rc_info
    rc_dir = os.path.dirname(rc_src)
    if kind == "zig_rc":
        cmd = prefix + ["/i", rc_dir]
        if is_cli:
            cmd += ["/d", "MESH_CLI"]
        cmd += ["/fo", res_out, "--", rc_src]
        run(cmd)
        return res_out
    cmd = prefix + ["-I", rc_dir]
    if is_cli:
        cmd += ["-DMESH_CLI"]
    cmd += ["-O", "coff", rc_src, res_out]
    run(cmd)
    return res_out


def build_launchers(dest: str, tmp: str) -> None:
    cc = find_cc()
    if cc is None:
        raise SystemExit("no Windows C compiler: pip install ziglang, or pass --no-launchers")
    rc_info = find_rc()
    src = os.path.join(ROOT, "packaging", "windows", "launcher.c")
    rc_src = os.path.join(ROOT, "packaging", "windows", "launcher.rc")
    for name, is_cli, extra in (
        ("Agent Mesh.exe", False, ["-Wl,--subsystem,windows"]),
        ("mesh.exe", True, ["-DMESH_CLI", "-Wl,--subsystem,console"]),
    ):
        stem = name.replace(" ", "_").replace(".exe", "")
        out = os.path.join(tmp, f"{stem}.exe")
        res_inputs: List[str] = []
        if rc_info is not None and os.path.isfile(rc_src):
            res_ext = ".res" if rc_info[0] == "zig_rc" else ".o"
            res_file = os.path.join(tmp, f"{stem}{res_ext}")
            compiled = _compile_rc(rc_info, rc_src, res_file, is_cli=is_cli)
            if compiled:
                res_inputs.append(compiled)
        run(cc + ["-O2", "-s", "-municode", "-o", out, src, *res_inputs, *extra])
        shutil.copy2(out, os.path.join(dest, name))


def _download_with_sha256(url: str, dest: str, expected_sha256: str, *, local_path: Optional[str] = None, label: str) -> str:
    target = local_path or dest
    if not local_path:
        print(f"+ download {label}: {url}", flush=True)
        req = urllib.request.Request(url, headers={"User-Agent": "agent-mesh-build"})
        with urllib.request.urlopen(req, timeout=120) as resp, open(target, "wb") as fp:
            shutil.copyfileobj(resp, fp)
    got = sha256(target)
    if got != expected_sha256:
        raise SystemExit(f"{label} sha256 mismatch: expected {expected_sha256}, got {got}")
    return target


def stage_portable_bundle(
    *,
    stage: str,
    wheel_out_dir: str,
    tmp: str,
    embed_zip: Optional[str] = None,
    mingit_zip: Optional[str] = None,
    no_launchers: bool = False,
    no_mingit: bool = False,
) -> Dict[str, str]:
    """Build wheel and populate *stage* directory with embeddable Python, launchers, and MinGit."""
    ver = mesh_version()
    os.makedirs(wheel_out_dir, exist_ok=True)
    os.makedirs(stage, exist_ok=True)

    # 1. wheel
    wheel_dir = os.path.join(tmp, "wheel")
    os.makedirs(wheel_dir, exist_ok=True)
    run([sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", wheel_dir, ROOT])
    wheels = [f for f in os.listdir(wheel_dir) if f.endswith(".whl")]
    assert len(wheels) == 1, wheels
    wheel = os.path.join(wheel_out_dir, wheels[0])
    shutil.copy2(os.path.join(wheel_dir, wheels[0]), wheel)

    # 2. embeddable CPython
    embed = _download_with_sha256(
        PY_EMBED_URL,
        os.path.join(tmp, "embed.zip"),
        PY_EMBED_SHA256,
        local_path=embed_zip,
        label="embeddable CPython zip",
    )
    pydir = os.path.join(stage, "python")
    with zipfile.ZipFile(embed) as z:
        z.extractall(pydir)

    # 3. site-packages: the wheel + pywebview, Windows binaries only
    site = os.path.join(pydir, "Lib", "site-packages")
    major, minor = PY_VERSION.split(".")[:2]
    local = os.path.join(tmp, "local-wheels")
    os.makedirs(local, exist_ok=True)
    run([sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", local, *PURE_SDIST_DEPS])
    for f in os.listdir(local):
        if not f.endswith("-py3-none-any.whl") and not f.endswith("-py2.py3-none-any.whl"):
            raise SystemExit(f"expected a pure-Python wheel, got {f}")
    run([
        sys.executable, "-m", "pip", "install", "--no-compile", "--target", site, "--find-links", local,
        "--platform", "win_amd64", "--python-version", f"{major}.{minor}",
        "--implementation", "cp", "--abi", f"cp{major}{minor}", "--only-binary=:all:",
        f"{wheel}[desktop]", *WIN32_MARKER_DEPS,
    ])
    shutil.rmtree(os.path.join(site, "bin"), ignore_errors=True)  # Linux-style script stubs

    pth = os.path.join(pydir, f"python{major}{minor}._pth")
    with open(pth, encoding="utf-8") as f:
        lines = [ln.rstrip("\r\n") for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
    lines += ["Lib\\site-packages", "import site"]
    with open(pth, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\n".join(lines) + "\n")

    # 4. Bundled MinGit (build-time download + SHA256 check; never downloaded at runtime)
    mingit_included = not no_mingit
    if mingit_included:
        mg_zip = _download_with_sha256(
            MINGIT_URL,
            os.path.join(tmp, "mingit.zip"),
            MINGIT_SHA256,
            local_path=mingit_zip,
            label=f"MinGit {MINGIT_VERSION}",
        )
        mingit_dir = os.path.join(stage, "mingit")
        with zipfile.ZipFile(mg_zip) as z:
            z.extractall(mingit_dir)
        git_exe = os.path.join(mingit_dir, "cmd", "git.exe")
        if not os.path.isfile(git_exe):
            raise SystemExit(f"MinGit extraction missing expected cmd/git.exe at {git_exe}")

    # 5. launchers + .cmd fallbacks + docs
    if not no_launchers:
        build_launchers(stage, tmp)

    gui_cmd = (
        "@echo off\r\n"
        "setlocal EnableExtensions\r\n"
        "if not defined _MESH_ORIG_PYTHONPATH (\r\n"
        '  if defined PYTHONPATH (set "_MESH_ORIG_PYTHONPATH=%PYTHONPATH%") else (set "_MESH_ORIG_PYTHONPATH=__UNSET__")\r\n'
        ")\r\n"
        'set "PYTHONPATH="\r\n'
        'if not defined PYTHONUTF8 set "PYTHONUTF8=1"\r\n'
        'start "" "%~dp0python\\pythonw.exe" -P -s -m mesh.desktop.app %*\r\n'
    )
    cli_cmd = (
        "@echo off\r\n"
        "setlocal EnableExtensions\r\n"
        "if not defined _MESH_ORIG_PYTHONPATH (\r\n"
        '  if defined PYTHONPATH (set "_MESH_ORIG_PYTHONPATH=%PYTHONPATH%") else (set "_MESH_ORIG_PYTHONPATH=__UNSET__")\r\n'
        ")\r\n"
        'set "PYTHONPATH="\r\n'
        'if not defined PYTHONUTF8 set "PYTHONUTF8=1"\r\n'
        '"%~dp0python\\python.exe" -P -s -m mesh %*\r\n'
        "exit /b %ERRORLEVEL%\r\n"
    )
    with open(os.path.join(stage, "Agent Mesh.cmd"), "w", encoding="utf-8", newline="") as f:
        f.write(gui_cmd)
    with open(os.path.join(stage, "mesh.cmd"), "w", encoding="utf-8", newline="") as f:
        f.write(cli_cmd)

    readme_text = README.format(
        ver=ver,
        py=PY_VERSION,
        git_req=(
            f"Optional — MinGit {MINGIT_VERSION} is bundled in mingit\\ and preferred automatically "
            "by meshd over PATH Git (falls back to system Git if mingit\\ is omitted)."
            if mingit_included
            else "Git for Windows on PATH (https://git-scm.com/download/win)"
        ),
        mingit_keep=' and "mingit" folders' if mingit_included else "",
        mingit_license=f", and MinGit {MINGIT_VERSION} (see mingit\\LICENSE.txt)" if mingit_included else "",
    )
    with open(os.path.join(stage, "README.txt"), "w", encoding="utf-8", newline="\r\n") as f:
        f.write(readme_text)
    shutil.copy2(os.path.join(ROOT, "LICENSE"), os.path.join(stage, "LICENSE.txt"))

    return {"version": ver, "wheel": wheel, "stage": stage}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--embed-zip", help="local copy of the CPython embeddable zip (sha256 is still checked)")
    ap.add_argument("--mingit-zip", help="local copy of the MinGit zip (sha256 is still checked)")
    ap.add_argument("--no-launchers", action="store_true")
    ap.add_argument("--no-mingit", action="store_true", help="omit bundled MinGit from the portable bundle")
    ap.add_argument("--stage-out", help="optional directory to copy the staged folder into (for installer builds)")
    ap.add_argument("--inno", action="store_true", help="also stage/compile the per-user Inno Setup installer via build_windows_installer.py")
    ap.add_argument("--manifests", action="store_true", help="also generate Scoop & Winget manifests in --out/manifests from SHA256SUMS.txt")
    args = ap.parse_args(argv)

    ver = mesh_version()
    os.makedirs(args.out, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="mesh-winbuild-")
    try:
        name = f"AgentMesh-{ver}-win-x64-portable"
        stage = os.path.join(tmp, name)
        info = stage_portable_bundle(
            stage=stage,
            wheel_out_dir=args.out,
            tmp=tmp,
            embed_zip=args.embed_zip,
            mingit_zip=args.mingit_zip,
            no_launchers=args.no_launchers,
            no_mingit=args.no_mingit,
        )
        wheel = info["wheel"]

        if args.stage_out:
            if os.path.exists(args.stage_out):
                shutil.rmtree(args.stage_out)
            shutil.copytree(stage, args.stage_out)

        # zip (deterministic order)
        zpath = os.path.join(args.out, name + ".zip")
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for base, dirs, files in os.walk(stage):
                dirs.sort()
                for fn in sorted(files):
                    full = os.path.join(base, fn)
                    z.write(full, os.path.join(name, os.path.relpath(full, stage)))

        if args.inno:
            run([
                sys.executable,
                os.path.join(ROOT, "scripts", "build_windows_installer.py"),
                "--out",
                args.out,
                "--portable-zip",
                zpath,
            ])

        setup_exe = os.path.join(args.out, f"AgentMesh-{ver}-win-x64-setup.exe")
        sum_targets = [wheel, zpath]
        if os.path.isfile(setup_exe):
            sum_targets.append(setup_exe)
        sums_path = os.path.join(args.out, "SHA256SUMS.txt")
        with open(sums_path, "w", newline="\n") as f:
            for p in sum_targets:
                f.write(f"{sha256(p)}  {os.path.basename(p)}\n")
        print("built:", *sum_targets, sep="\n  ")

        if args.manifests:
            manifest_cmd = [
                sys.executable,
                os.path.join(ROOT, "scripts", "gen_win_manifests.py"),
                "--version",
                ver,
                "--sha256sums",
                sums_path,
                "--out-dir",
                os.path.join(args.out, "manifests"),
            ]
            if not os.path.isfile(setup_exe):
                print(
                    "WARNING: setup.exe was not built on this host; allowing placeholder setup hash in generated manifests.",
                    file=sys.stderr,
                )
                manifest_cmd.append("--allow-placeholders")
            run(manifest_cmd)
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
