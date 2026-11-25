"""Process isolation trampoline for CLI entrypoints per plan §5.1 and §7.6 mitigation 8.

Only built-in modules (``sys``, ``posix`` on POSIX, ``nt`` and ``_winapi`` on Windows) are
imported at module level so an untrusted ``PYTHONPATH`` or user site-packages cannot
shadow stdlib modules before ``-P -s`` re-exec.
"""

import sys

if sys.platform == "win32":
    import _winapi
    import nt
else:
    import posix


def _strip_win_prefix(path: str) -> str:
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def _win_abspath(path: str) -> str:
    p = path.replace("/", "\\")
    getfull = getattr(nt, "_getfullpathname", None)
    if getfull is not None:
        try:
            return _strip_win_prefix(getfull(p))
        except OSError:
            pass
    if len(p) >= 2 and p[0].isascii() and p[0].isalpha() and p[1] == ":":
        if len(p) >= 3 and p[2] == "\\":
            return p
        # Drive-relative path (e.g. "C:foo" or "C:"): resolve against same-drive cwd
        # or return unchanged instead of producing "<cwd>\\C:foo".
        cwd = nt.getcwd()
        cwd_clean = cwd if (len(cwd) == 3 and cwd[1:3] == ":\\") else cwd.rstrip("\\")
        if len(cwd_clean) >= 2 and cwd_clean[1] == ":" and cwd_clean[0].lower() == p[0].lower():
            tail = p[2:]
            if not tail:
                return cwd_clean
            return (cwd_clean + tail) if cwd_clean.endswith("\\") else (cwd_clean + "\\" + tail)
        return p
    if p.startswith("\\\\"):
        return p
    cwd = nt.getcwd()
    cwd_clean = cwd if (len(cwd) == 3 and cwd[1:3] == ":\\") else cwd.rstrip("\\")
    if p.startswith("\\"):
        if len(cwd_clean) >= 2 and cwd_clean[1] == ":":
            return cwd_clean[:2] + p
        if cwd_clean.startswith("\\\\"):
            parts = cwd_clean[2:].split("\\", 2)
            if len(parts) >= 2:
                return "\\\\" + parts[0] + "\\" + parts[1] + p
        return p
    if not p:
        return cwd_clean
    return (cwd_clean + p) if cwd_clean.endswith("\\") else (cwd_clean + "\\" + p)


def _resolve_realpath_win(path: str) -> str:
    """Resolve symlinks/junctions and 8.3 names on Windows using only built-in ``nt``."""
    if not path:
        return path
    p = path.replace("/", "\\")
    getfinal = getattr(nt, "_getfinalpathname", None)
    if getfinal is None:
        return _win_abspath(p)
    try:
        return _strip_win_prefix(getfinal(p))
    except OSError:
        pass
    cur = _win_abspath(p)
    tail = []
    while cur:
        try:
            resolved = _strip_win_prefix(getfinal(cur))
            if tail:
                return resolved.rstrip("\\") + "\\" + "\\".join(reversed(tail))
            return resolved
        except OSError:
            pass
        if len(cur) == 3 and cur[1:3] == ":\\":
            break
        if cur.startswith("\\\\"):
            stripped = cur[2:]
            if "\\" not in stripped or stripped.count("\\") <= 1:
                break
        head, sep, part = cur.rpartition("\\")
        if not sep:
            break
        if part:
            tail.append(part)
        if len(head) == 2 and head[1] == ":":
            cur = head + "\\"
        elif not head:
            cur = "\\"
        else:
            cur = head
    return _win_abspath(p)


def _resolve_realpath_posix(path: str) -> str:
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


def _resolve_realpath(path: str) -> str:
    if sys.platform == "win32":
        return _resolve_realpath_win(path)
    return _resolve_realpath_posix(path)


def _path_key(path: str) -> str:
    real = _resolve_realpath(path)
    return real.lower() if sys.platform == "win32" else real


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
    if sys.platform == "win32":
        if stdlib_dir:
            stdlib_clean = stdlib_dir.rstrip("/\\")
            expected.add(_path_key(f"{stdlib_clean}\\site.py"))
        for raw_base in (
            getattr(sys, "base_prefix", sys.prefix),
            getattr(sys, "base_exec_prefix", sys.exec_prefix),
            getattr(sys, "real_prefix", ""),
        ):
            if not raw_base:
                continue
            b = _resolve_realpath(raw_base).rstrip("\\/")
            for lib in libdirs:
                expected.add(_path_key(f"{b}\\{lib}\\{ver}\\site.py"))
                if abi_thread:
                    expected.add(_path_key(f"{b}\\{lib}\\{ver}{abi_thread}\\site.py"))
            expected.add(_path_key(f"{b}\\Lib\\site.py"))
        return site_mod if _path_key(site_file) in expected else None

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
    if not real:
        return False
    dirs = _global_site_packages_dirs()
    if sys.platform == "win32":
        return real.lower() in {d.lower() for d in dirs}
    return real in dirs


def _mesh_install_root() -> str:
    pkg = sys.modules.get("mesh")
    pkg_paths = getattr(pkg, "__path__", None) if pkg is not None else None
    if pkg_paths:
        pkg_dir = _resolve_realpath(pkg_paths[0])
        if sys.platform == "win32":
            trimmed = pkg_dir.rstrip("\\/")
            parent, sep, _name = trimmed.rpartition("\\")
            if not sep:
                return pkg_dir
            if len(parent) == 2 and parent[1] == ":":
                return parent + "\\"
            return parent or "\\"
        return pkg_dir.rpartition("/")[0] or "/"
    return ""


def _win_quote_cmdline(argv: list) -> str:
    """Quote an argv list using MS C runtime rules without importing subprocess."""
    result = []
    for arg in argv:
        bs_buf = []
        if result:
            result.append(" ")
        needquote = (" " in arg) or ("\t" in arg) or not arg
        if needquote:
            result.append('"')
        for ch in arg:
            if ch == "\\":
                bs_buf.append(ch)
            elif ch == '"':
                result.append("\\" * (len(bs_buf) * 2))
                bs_buf = []
                result.append('\\"')
            else:
                if bs_buf:
                    result.extend(bs_buf)
                    bs_buf = []
                result.append(ch)
        if bs_buf:
            result.extend(bs_buf)
        if needquote:
            result.extend(bs_buf)
            result.append('"')
    return "".join(result)


def _win_env_pp_matches(actual: object, expected: object) -> bool:
    if expected is None:
        return actual is None
    if not isinstance(actual, str) or not isinstance(expected, str):
        return False
    return actual.replace("/", "\\").lower() == expected.replace("/", "\\").lower()


def _ensure_isolated_win() -> None:
    raw_env = dict(nt.environ)
    has_orig_marker = False
    orig_marker_val = None
    pp_val = None
    new_env = {}
    for k, v in raw_env.items():
        ku = k.upper()
        if ku == "_MESH_ORIG_PYTHONPATH":
            has_orig_marker = True
            orig_marker_val = v
        elif ku == "PYTHONPATH":
            pp_val = v
        else:
            new_env[k] = v

    site_mod = _stdlib_site_module()
    install_root = _mesh_install_root()
    if not install_root or _is_site_packages_dir(install_root):
        expected_pp = None
    else:
        expected_pp = install_root

    if (
        sys.flags.safe_path
        and sys.flags.no_user_site
        and site_mod is not None
        and has_orig_marker
        and _win_env_pp_matches(pp_val, expected_pp)
    ):
        return

    if has_orig_marker:
        new_env["_MESH_ORIG_PYTHONPATH"] = orig_marker_val if orig_marker_val is not None else "__UNSET__"
    else:
        new_env["_MESH_ORIG_PYTHONPATH"] = pp_val if pp_val else "__UNSET__"

    if expected_pp is not None:
        new_env["PYTHONPATH"] = expected_pp

    argv = [sys.executable, "-P", "-s", "-m", "mesh", *sys.argv[1:]]
    cmdline = _win_quote_cmdline(argv)
    hp, ht, _pid, _tid = _winapi.CreateProcess(
        sys.executable,
        cmdline,
        None,
        None,
        1,
        0,
        new_env,
        None,
        None,
    )
    try:
        _winapi.CloseHandle(ht)
        ht = 0
        _winapi.WaitForSingleObject(hp, _winapi.INFINITE)
        rc = _winapi.GetExitCodeProcess(hp)
    finally:
        if ht:
            _winapi.CloseHandle(ht)
        _winapi.CloseHandle(hp)
    sys.exit(rc)


def _ensure_isolated_posix() -> None:
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


def ensure_isolated_process() -> None:
    """Re-exec with ``-P -s -m mesh`` and sanitized ``PYTHONPATH`` if not isolated."""
    if sys.platform == "win32":
        _ensure_isolated_win()
        return
    _ensure_isolated_posix()


def main() -> int:
    ensure_isolated_process()
    from mesh.cli.main import main as _cli_main

    return _cli_main()
