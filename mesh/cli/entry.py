"""Process isolation trampoline for CLI entrypoints per plan §5.1 and §7.6 mitigation 8.

Only built-in modules (``sys``, ``posix``) are imported at module level so an untrusted
``PYTHONPATH`` or user site-packages cannot shadow stdlib modules before ``-P -s`` re-exec.
"""

import posix
import sys


def _stdlib_site_module():
    """Return already-loaded stdlib ``site`` module without triggering an import."""
    if getattr(sys.flags, "no_site", 0):
        return None
    site_mod = sys.modules.get("site")
    site_file = getattr(site_mod, "__file__", None) if site_mod is not None else None
    if not site_file or not hasattr(site_mod, "getsitepackages"):
        return None
    ver = f"python{sys.version_info[0]}.{sys.version_info[1]}"
    abi_thread = "t" if "t" in getattr(sys, "abiflags", "") else ""
    plat = getattr(sys, "platlibdir", "lib") or "lib"
    libdirs = [plat] if plat == "lib" else [plat, "lib"]
    expected = set()
    stdlib_dir = getattr(sys, "_stdlib_dir", "")
    if stdlib_dir:
        expected.add(_resolve_realpath(f"{stdlib_dir.rstrip('/')}/site.py"))
    for raw_base in (
        getattr(sys, "base_prefix", sys.prefix),
        getattr(sys, "base_exec_prefix", sys.exec_prefix),
        getattr(sys, "real_prefix", ""),
    ):
        if not raw_base:
            continue
        b = _resolve_realpath(raw_base).rstrip("/")
        for lib in libdirs:
            expected.add(_resolve_realpath(f"{b}/{lib}/{ver}/site.py"))
            if abi_thread:
                expected.add(_resolve_realpath(f"{b}/{lib}/{ver}{abi_thread}/site.py"))
        expected.add(_resolve_realpath(f"{b}/Lib/site.py"))
    return site_mod if _resolve_realpath(site_file) in expected else None


def _global_site_packages_dirs() -> set:
    """Return realpaths of active global site-packages dirs without importing site.

    Matches ``mesh.core.paths.is_site_packages_dir()`` by querying the interpreter's
    already-loaded stdlib ``site.getsitepackages()`` (verified under stdlib prefix).
    When ``site`` is not loaded (e.g., ``python -S``) or untrusted, returns ``set()``
    so the first hop preserves ``PYTHONPATH=install_root`` and re-execs ``-P -s``
    where stdlib ``site`` is loaded and re-evaluated before proceeding.
    """
    site_mod = _stdlib_site_module()
    if site_mod is None:
        return set()
    return {_resolve_realpath(sp) for sp in site_mod.getsitepackages() if sp}


def _is_site_packages_dir(path: str) -> bool:
    """Return True only if *path* resolves to an active global site-packages directory."""
    if not path:
        return False
    real = _resolve_realpath(path)
    return bool(real) and real in _global_site_packages_dirs()


def _resolve_realpath(path: str) -> str:
    """Resolve symlinks on a path using only built-in ``posix``."""
    base = path if path.startswith("/") else posix.getcwd() + "/" + path
    parts = [p for p in base.split("/") if p and p != "."]
    cur = "/"
    seen = 0
    while parts:
        part = parts.pop(0)
        if part == "..":
            cur = cur.rpartition("/")[0] or "/"
            continue
        nxt = ("/" + part) if cur == "/" else (cur + "/" + part)
        try:
            target = posix.readlink(nxt)
        except OSError:
            cur = nxt
            continue
        seen += 1
        if seen > 40:
            return path
        if target.startswith("/"):
            cur = "/"
            parts = [p for p in target.split("/") if p and p != "."] + parts
        else:
            parts = [p for p in target.split("/") if p and p != "."] + parts
    return cur


def _mesh_install_root() -> str:
    pkg = sys.modules.get("mesh")
    pkg_paths = getattr(pkg, "__path__", None) if pkg is not None else None
    if pkg_paths:
        pkg_dir = _resolve_realpath(pkg_paths[0])
        return pkg_dir.rpartition("/")[0] or "/"
    return ""


def ensure_isolated_process() -> None:
    """Re-exec with ``-P -s -m mesh`` and sanitized ``PYTHONPATH`` if not isolated."""
    posix.umask(0o077)
    env = dict(posix.environ)
    site_mod = _stdlib_site_module()
    install_root = _mesh_install_root()
    enc = sys.getfilesystemencoding() or "utf-8"
    if not install_root or _is_site_packages_dir(install_root):
        expected_pp = None
    else:
        expected_pp = install_root.encode(enc, "surrogateescape")
    if (
        sys.flags.safe_path
        and sys.flags.no_user_site
        and site_mod is not None
        and b"_MESH_ORIG_PYTHONPATH" in env
        and env.get(b"PYTHONPATH") == expected_pp
    ):
        return
    if b"_MESH_ORIG_PYTHONPATH" not in env:
        orig = env.get(b"PYTHONPATH")
        env[b"_MESH_ORIG_PYTHONPATH"] = orig if orig else b"__UNSET__"
    if expected_pp is None:
        env.pop(b"PYTHONPATH", None)
    else:
        env[b"PYTHONPATH"] = expected_pp
    argv = [sys.executable, "-P", "-s", "-m", "mesh", *sys.argv[1:]]
    posix.execve(sys.executable, argv, env)


def main() -> int:
    ensure_isolated_process()
    from mesh.cli.main import main as _cli_main

    return _cli_main()
