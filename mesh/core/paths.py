"""Path security, install-path validation, and filesystem checks for Agent Mesh."""

import os
import stat
from typing import List, Optional
import mesh.constants as c
from mesh.errors import MeshError


def get_install_root() -> str:
    """Return the absolute real path of the Mesh install root.

    The install root contains bin/mesh and the mesh/ package directory.
    """
    # mesh/core/paths.py -> mesh/core -> mesh -> install_root
    core_dir = os.path.dirname(os.path.abspath(__file__))
    mesh_pkg_dir = os.path.dirname(core_dir)
    install_root = os.path.dirname(mesh_pkg_dir)
    return os.path.realpath(install_root)


def resolve_path(path: str) -> str:
    """Absolute against the cwd once, then realpath'd, symlinks before "..".

    Note: os.path.abspath() must NOT run first, since it folds "a/link/.." to "a"
    lexically and realpath would then never see the link. A plain join with the cwd
    keeps the components for realpath. Used for the home (via resolve_home) and for
    every SQLite file path (mesh.core.db.sqlite_uri).
    """
    return os.path.realpath(os.path.join(os.getcwd(), os.fspath(path)))


def resolve_home(home: str) -> str:
    """The one home resolution for every entry point (m3-test-kdf round 3).

    Resolved with resolve_path (symlinks followed before ".."), so `a/link/..` is the
    link target's parent (what the kernel opens), not `a`. init, meshd, doctor and the
    CLI --home / MESH_HOME all use this.

    Round 4: an empty or whitespace-only home (`--home ""`) is refused instead of
    silently meaning the cwd, and so is "/" or any home that resolves to "/".
    Both raise INVALID_ARGUMENT (rc 2).
    """
    raw = os.fspath(home) if home is not None else ""
    if isinstance(raw, bytes):
        raw = os.fsdecode(raw)
    if not raw.strip():
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="The Mesh home (--home / MESH_HOME) must not be empty.",
            details={"param": "home", "home": raw},
        )
    resolved = resolve_path(raw)
    if resolved == os.sep:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"The Mesh home must not be the filesystem root '/' (given {raw!r}).",
            details={"param": "home", "home": raw, "resolved": resolved},
        )
    return resolved


def default_home_path() -> str:
    """The resolved default home (~/.agent-mesh), as meshd resolves it."""
    return os.path.realpath(os.path.expanduser("~/.agent-mesh"))


def _dir_identity_key(path: str) -> tuple:
    """Filesystem identity of a (resolved) home path, for alias detection.

    Existing directory: ("dir", st_dev, st_ino). Not existing yet: the identity of
    its parent plus its basename, ("name", parent st_dev, parent st_ino, basename).
    If even the parent cannot be stat'ed: ("path", realpath).
    """
    real = os.path.realpath(path)
    try:
        st = os.stat(real)
        return ("dir", st.st_dev, st.st_ino)
    except OSError:
        pass
    parent, name = os.path.split(real.rstrip(os.sep) or os.sep)
    try:
        pst = os.stat(parent or os.sep)
        return ("name", pst.st_dev, pst.st_ino, name)
    except OSError:
        return ("path", real)


def is_default_home(home: str) -> bool:
    """True if ``home`` is the default home ~/.agent-mesh, under any alias we can see.

    m3-test-kdf fix round (guard a): the realpath string compare catches symlinks;
    the filesystem identity compare (st_dev, st_ino) also catches a bind mount or
    any second path to the same directory. When the home does not exist yet, the
    parent's identity plus the basename is compared (so a bind-mounted alias of
    the default home's parent is caught too). Out of scope: a user with mount
    rights who mounts something over the default home after this check, or swaps
    directories between the check and init (same-UID / mount-capable attacker).
    """
    default = default_home_path()
    real = os.path.realpath(home)
    if real == default:
        return True
    return _dir_identity_key(real) == _dir_identity_key(default)


def refuse_testing_on_default_home(home: str, testing_mode: bool) -> None:
    """The one default-home guard shared by `mesh init`, meshd and `mesh doctor`.

    Testing mode (MESH_TESTING=1: fake adapter, crash hooks, cheap KDF accepted) is
    refused on the default home ~/.agent-mesh under any alias is_default_home sees.
    Raises INVALID_ARGUMENT (rc 2).
    """
    if testing_mode and is_default_home(home):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Testing mode (MESH_TESTING=1) is refused on the default home ~/.agent-mesh; "
                    "set MESH_HOME to a separate directory.",
            details={"param": "MESH_TESTING", "home": os.path.realpath(home)},
        )


def is_path_equal_or_parent(parent: str, child: str) -> bool:
    """Return True if child is equal to parent or located inside parent directory."""
    parent_real = os.path.realpath(parent)
    child_real = os.path.realpath(child)
    if parent_real == child_real:
        return True
    try:
        common = os.path.commonpath([parent_real, child_real])
        return common == parent_real
    except ValueError:
        return False


def check_install_path_secure(
    install_root: Optional[str] = None,
    home: Optional[str] = None,
    workspaces_root: Optional[str] = None,
    repo_paths: Optional[List[str]] = None,
) -> None:
    """Verify bidirectionally that the install root does not overlap with workspaces, runs, or repos.

    Per plan §7.6 mitigation 7 and review-r3 minor findings 2 & 4:
    1. The install root MUST NOT be equal to or inside $MESH_HOME (round 6),
       workspaces.root, $MESH_HOME/runs, or any registered project repo root.
    2. Conversely, workspaces.root, $MESH_HOME, $MESH_HOME/runs, and any registered project
       repo root MUST NOT be inside the install root.

    Any overlap in either direction triggers exit 2 INSTALL_PATH_INSECURE with
    details: {'install_root': ..., 'conflict': ...}.
    """
    if install_root is None:
        install_root = get_install_root()
    else:
        install_root = os.path.realpath(install_root)

    candidates: List[tuple[str, str]] = []

    if home is not None:
        home_real = os.path.realpath(home)
        runs_real = os.path.realpath(os.path.join(home, "runs"))
        candidates.append(("home", home_real))
        candidates.append(("runs", runs_real))
        if workspaces_root is None:
            workspaces_root = os.path.join(home, "workspaces")

    if workspaces_root is not None:
        candidates.append(("workspaces.root", os.path.realpath(workspaces_root)))

    if repo_paths:
        for rp in repo_paths:
            if rp:
                candidates.append(("repo", os.path.realpath(rp)))

    for label, target_path in candidates:
        # Check direction 1: install root is equal to or inside target_path (the home
        # itself is checked after the loop, so a more specific conflict is named first).
        if label != "home" and is_path_equal_or_parent(target_path, install_root):
            raise MeshError(
                code=c.INSTALL_PATH_INSECURE,
                exit_code=c.EXIT_USAGE,
                message=f"Install root '{install_root}' is inside or equal to {label} '{target_path}'",
                details={"install_root": install_root, "conflict": target_path},
            )

        # Check direction 2: target_path is equal to or inside install_root
        if is_path_equal_or_parent(install_root, target_path):
            raise MeshError(
                code=c.INSTALL_PATH_INSECURE,
                exit_code=c.EXIT_USAGE,
                message=f"{label} '{target_path}' is inside or equal to install root '{install_root}'",
                details={"install_root": install_root, "conflict": target_path},
            )

    # m3-test-kdf round 6: direction 1 for the home itself too. An install root nested
    # anywhere in $MESH_HOME (e.g. $MESH_HOME/logs/install) is refused, so the home and
    # the install root never overlap in either direction.
    if home is not None and is_path_equal_or_parent(home_real, install_root):
        raise MeshError(
            code=c.INSTALL_PATH_INSECURE,
            exit_code=c.EXIT_USAGE,
            message=f"Install root '{install_root}' is inside or equal to home '{home_real}'",
            details={"install_root": install_root, "conflict": home_real},
        )


def check_home_permissions(home: str) -> None:
    """Verify that MESH_HOME is owned by current uid and permissions are 0700 (mode & 0o077 == 0).

    Per plan §5.1, §15.3, C3, C4:
    If permissions are insecure, raise MeshError(INSECURE_PERMISSIONS, exit_code 2).
    """
    if not os.path.exists(home):
        return

    st = os.stat(home)
    # Check ownership
    if st.st_uid != os.getuid():
        raise MeshError(
            code=c.INSECURE_PERMISSIONS,
            exit_code=c.EXIT_USAGE,
            message=f"Storage root '{home}' is not owned by current uid {os.getuid()}",
            details={"home": home, "owner_uid": st.st_uid, "current_uid": os.getuid()},
        )

    # Check mode & 0o077 == 0 (no group or other read/write/execute permissions)
    if (st.st_mode & 0o077) != 0:
        mode_octal = oct(stat.S_IMODE(st.st_mode))
        raise MeshError(
            code=c.INSECURE_PERMISSIONS,
            exit_code=c.EXIT_USAGE,
            message=f"Storage root '{home}' has insecure permissions {mode_octal} (must be mode 0700)",
            details={"home": home, "mode": mode_octal},
        )


def check_socket_path_length(socket_path: str) -> None:
    """Verify Unix domain socket path length does not exceed 107 bytes per plan §5.1."""
    encoded_len = len(socket_path.encode("utf-8"))
    if encoded_len > 107:
        raise MeshError(
            code=c.SOCKET_PATH_TOO_LONG,
            exit_code=c.EXIT_USAGE,
            message=f"Unix socket path length ({encoded_len} bytes) exceeds maximum limit of 107 bytes: '{socket_path}'",
            details={"socket_path": socket_path, "bytes": encoded_len, "limit": 107},
        )


def check_token_file_secure(
    token_file: str,
    home: str,
    workspaces_root: Optional[str] = None,
    repo_paths: Optional[List[str]] = None,
) -> None:
    """Verify agent token file permissions and path security per plan §7.1, §7.6 mitigation 2, C10, C12.

    1. File must exist and be mode 0600 (st_mode & 0o077 == 0).
    2. Real path must not be under workspaces.root, $MESH_HOME/runs, or any registered repo.
    """
    if not os.path.exists(token_file):
        raise MeshError(
            code=c.TOKEN_FILE_INSECURE,
            exit_code=c.EXIT_UNAUTHENTICATED,
            message=f"Token file '{token_file}' does not exist",
            details={"token_file": token_file},
        )

    st = os.stat(token_file)
    if (st.st_mode & 0o077) != 0:
        mode_octal = oct(stat.S_IMODE(st.st_mode))
        raise MeshError(
            code=c.TOKEN_FILE_INSECURE,
            exit_code=c.EXIT_UNAUTHENTICATED,
            message=f"Token file '{token_file}' has insecure permissions {mode_octal} (must be mode 0600)",
            details={"token_file": token_file, "mode": mode_octal},
        )

    # Check location
    token_real = os.path.realpath(token_file)
    runs_dir = os.path.realpath(os.path.join(home, "runs"))
    if is_path_equal_or_parent(runs_dir, token_real):
        raise MeshError(
            code=c.TOKEN_FILE_INSECURE,
            exit_code=c.EXIT_UNAUTHENTICATED,
            message=f"Token file '{token_file}' cannot be located inside runs directory '{runs_dir}'",
            details={"token_file": token_file, "conflict": runs_dir},
        )

    if workspaces_root is None:
        workspaces_root = os.path.join(home, "workspaces")
    ws_real = os.path.realpath(workspaces_root)
    if is_path_equal_or_parent(ws_real, token_real):
        raise MeshError(
            code=c.TOKEN_FILE_INSECURE,
            exit_code=c.EXIT_UNAUTHENTICATED,
            message=f"Token file '{token_file}' cannot be located inside workspaces root '{ws_real}'",
            details={"token_file": token_file, "conflict": ws_real},
        )

    if repo_paths:
        for rp in repo_paths:
            if rp and is_path_equal_or_parent(os.path.realpath(rp), token_real):
                raise MeshError(
                    code=c.TOKEN_FILE_INSECURE,
                    exit_code=c.EXIT_UNAUTHENTICATED,
                    message=f"Token file '{token_file}' cannot be located inside repository '{rp}'",
                    details={"token_file": token_file, "conflict": rp},
                )
