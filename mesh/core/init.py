"""Mesh initialization: storage root, directories, database schema, credentials, and settings."""

import json
import os
import re
import sqlite3
import stat
import sys
from typing import Any, Dict, Optional

import mesh.constants as c
from mesh.errors import MeshError
from mesh.platform import perms
from mesh.platform.paths import is_reparse_stat
from mesh.platform.proc import is_process_alive
from mesh.core.auth import derive_user_keys
from mesh.core.clock import now_iso_utc
from mesh.core.db import connect_db, init_db as init_db_file
from mesh.core.events import insert_event
from mesh.core.ids import new_id
from mesh.core.paths import (
    check_home_permissions,
    check_install_path_secure,
    get_install_root,
    refuse_testing_on_default_home,
    resolve_home,
)
from mesh.core.settings import get_default_settings

_INIT_TMP_DB_RE = re.compile(r"^\.mesh\.db\.init\.(\d+)\.tmp(?:-wal|-shm)?$")


def _cleanup_stale_init_tmp_dbs(home: str, *, db_exists: bool = False) -> int:
    """Remove leftover ``.mesh.db.init.<pid>.tmp*`` files from crashed init attempts."""
    removed = 0
    try:
        entries = os.listdir(home)
    except OSError:
        return 0
    my_pid = os.getpid()
    alive_cache: Dict[int, bool] = {}
    for name in entries:
        m = _INIT_TMP_DB_RE.match(name)
        if not m:
            continue
        try:
            pid = int(m.group(1))
        except ValueError:
            pid = -1
        should_remove = db_exists or pid <= 0 or pid == my_pid
        if not should_remove:
            alive = alive_cache.get(pid)
            if alive is None:
                try:
                    alive = is_process_alive(pid)
                except Exception:
                    alive = False
                alive_cache[pid] = alive
            should_remove = not alive
        if should_remove:
            try:
                os.unlink(os.path.join(home, name))
                removed += 1
            except OSError:
                pass
    return removed


def init_mesh_home(home: str, passphrase: str) -> Dict[str, Any]:
    """Initialize a MESH_HOME directory and storage per plan §4, §5.1, §7.1.

    Creates:
    - Directories: home, run/, runs/, workspaces/, logs/, merges/ (all 0700)
    - Database: mesh.db (+ wal, shm) with authoritative schema
    - Principals: user ('user') and system ('mesh')
    - Credentials: user passphrase scrypt verifier and salt
    - Settings: default settings populated in settings table
    - Initial event: system.init
    """
    if len(passphrase) < 12:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Passphrase must be at least 12 characters long",
            details={"min_length": 12, "actual_length": len(passphrase)},
        )

    # m3-test-kdf guard a: testing mode is refused on the default home (as meshd
    # already does), and only a testing-mode init on another home gets the cheap
    # KDF. The home records it ("testing": true in kdf_params), and a non-testing
    # daemon refuses to unlock it (mesh.core.auth.parse_kdf_params). "Default home"
    # means any alias of it: realpath and filesystem identity (mesh.core.paths.is_default_home).
    # Round 3: resolve the home once (relative to the cwd, symlinks before "..").
    home = resolve_home(home)
    testing = os.environ.get("MESH_TESTING") == "1"
    refuse_testing_on_default_home(home, testing)

    # Check bidirectional install-path security
    install_root = get_install_root()
    check_install_path_secure(install_root=install_root, home=home)

    # Create directories with mode 0700 and verify ownership/permissions (fails closed
    # with INSECURE_PERMISSIONS exit 2 on foreign-owned or unwritable dirs).
    subdirs = ["run", "runs", "workspaces", "logs", "merges"]
    old_umask = os.umask(0o077)
    try:
        try:
            if os.path.lexists(home):
                st_home = os.lstat(home)
                if is_reparse_stat(st_home) or not stat.S_ISDIR(st_home.st_mode):
                    raise MeshError(
                        code=c.INSECURE_PERMISSIONS,
                        exit_code=c.EXIT_USAGE,
                        message=f"Storage root '{home}' must be a real directory, not a symlink or file",
                        details={"home": home},
                    )
                if sys.platform == "win32" and not perms.is_owned_by_current_user(home):
                    raise MeshError(
                        code=c.INSECURE_PERMISSIONS,
                        exit_code=c.EXIT_USAGE,
                        message=f"Storage root '{home}' is not owned by the current Windows user",
                        details={"home": home},
                    )
            os.makedirs(home, mode=0o700, exist_ok=True)
            if sys.platform == "win32":
                perms.set_private_dir_acl(home, 0o700)
            else:
                os.chmod(home, 0o700)
            check_home_permissions(home)

            for sub in subdirs:
                subpath = os.path.join(home, sub)
                if os.path.lexists(subpath):
                    st_sub = os.lstat(subpath)
                    if is_reparse_stat(st_sub) or not stat.S_ISDIR(st_sub.st_mode):
                        raise MeshError(
                            code=c.INSECURE_PERMISSIONS,
                            exit_code=c.EXIT_USAGE,
                            message=f"Storage subdirectory '{subpath}' must be a real directory, not a symlink or file",
                            details={"home": home, "path": subpath},
                        )
                    if sys.platform == "win32":
                        if not perms.is_owned_by_current_user(subpath):
                            raise MeshError(
                                code=c.INSECURE_PERMISSIONS,
                                exit_code=c.EXIT_USAGE,
                                message=f"Storage subdirectory '{subpath}' is not owned by the current Windows user",
                                details={"home": home, "path": subpath},
                            )
                    elif st_sub.st_uid != os.getuid():
                        raise MeshError(
                            code=c.INSECURE_PERMISSIONS,
                            exit_code=c.EXIT_USAGE,
                            message=f"Storage subdirectory '{subpath}' is not owned by current uid {os.getuid()}",
                            details={"home": home, "path": subpath, "owner_uid": st_sub.st_uid, "current_uid": os.getuid()},
                        )
                else:
                    os.makedirs(subpath, mode=0o700, exist_ok=True)
                if sys.platform == "win32":
                    perms.set_private_dir_acl(subpath, 0o700)
                else:
                    os.chmod(subpath, 0o700)
        except (FileExistsError, NotADirectoryError) as exc:
            raise MeshError(
                code=c.INSECURE_PERMISSIONS,
                exit_code=c.EXIT_USAGE,
                message=f"Storage root '{home}' must be a real directory, not a symlink or file",
                details={"home": home},
            ) from exc
        except PermissionError as exc:
            check_home_permissions(home)
            raise MeshError(
                code=c.INSECURE_PERMISSIONS,
                exit_code=c.EXIT_USAGE,
                message=f"Permission denied initializing storage root '{home}': {exc.strerror or exc}",
                details={"home": home},
            ) from exc
        except OSError as exc:
            if sys.platform == "win32":
                raise MeshError(
                    code=c.INSECURE_PERMISSIONS,
                    exit_code=c.EXIT_USAGE,
                    message=f"Storage root '{home}' cannot be initialized safely: {exc.strerror or exc}",
                    details={"home": home},
                ) from exc
            raise
    finally:
        os.umask(old_umask)

    # Derive user keys
    verifier, k_user, salt, kdf_params = derive_user_keys(passphrase, testing_kdf=testing)

    # Initialize SQLite database atomically when creating fresh mesh.db so a crash
    # mid-init never leaves an empty schema without a user principal behind.
    db_path = os.path.join(home, "mesh.db")
    creating_fresh = not os.path.exists(db_path)
    _cleanup_stale_init_tmp_dbs(home, db_exists=not creating_fresh)
    target_db = os.path.join(home, f".mesh.db.init.{os.getpid()}.tmp") if creating_fresh else db_path
    if creating_fresh:
        for suf in ("", "-wal", "-shm"):
            try:
                os.unlink(target_db + suf)
            except FileNotFoundError:
                pass

    conn = init_db_file(target_db)
    try:
        now = now_iso_utc()
        user_id = new_id("usr")
        system_id = "principal_system"  # Or generated system ID

        with conn:
            conn.execute("BEGIN IMMEDIATE;")
            # Check if user already exists
            cur = conn.execute("SELECT id FROM principals WHERE kind = 'user' LIMIT 1;")
            existing_user = cur.fetchone()
            if existing_user:
                # Already initialized
                conn.execute("ROLLBACK;")
                return {
                    "home": home,
                    "user_id": existing_user["id"],
                    "initialized": False,
                    "message": "Storage already initialized",
                }

            # Insert system principal
            conn.execute(
                "INSERT INTO principals (id, kind, name, created_at) VALUES (?, 'system', 'mesh', ?);",
                (system_id, now),
            )

            # Insert user principal
            conn.execute(
                "INSERT INTO principals (id, kind, name, created_at) VALUES (?, 'user', 'user', ?);",
                (user_id, now),
            )

            # Insert user passphrase credential
            cred_id = new_id("cred")
            conn.execute(
                """INSERT INTO credentials (
                    id, principal_id, kind, lookup, secret_hash, salt, kdf_params, scope_json, created_at
                ) VALUES (?, ?, 'passphrase', NULL, ?, ?, ?, '{}', ?);""",
                (cred_id, user_id, verifier, salt, json.dumps(kdf_params), now),
            )

            # Populate default settings
            default_settings = get_default_settings(home)
            for k, v in default_settings.items():
                conn.execute(
                    "INSERT INTO settings (key, value_json, updated_at, updated_by) VALUES (?, ?, ?, ?);",
                    (k, json.dumps(v), now, user_id),
                )

            # Write initial genesis event
            req_id = new_id("req")
            insert_event(
                conn=conn,
                event_type="system.init",
                actor_id=user_id,
                actor_kind="user",
                via="direct",
                request_id=req_id,
                payload={"home": home},
                k_user=k_user,
            )

            conn.execute("COMMIT;")
        if creating_fresh:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            conn.execute("PRAGMA journal_mode=DELETE;")
    except BaseException:
        conn.close()
        if creating_fresh:
            for suf in ("", "-wal", "-shm"):
                try:
                    os.unlink(target_db + suf)
                except OSError:
                    pass
        raise
    else:
        conn.close()

    if creating_fresh:
        perms.set_private_file_acl(target_db, 0o600)
        os.replace(target_db, db_path)
        for suf in ("-wal", "-shm"):
            try:
                os.unlink(target_db + suf)
            except OSError:
                pass
        # Re-open briefly to restore WAL mode on the final db_path
        wal_conn = connect_db(db_path)
        wal_conn.close()

    # Ensure mesh.db permissions are 0600
    if os.path.exists(db_path):
        perms.set_private_file_acl(db_path, 0o600)

    return {
        "home": home,
        "user_id": user_id,
        "initialized": True,
    }
