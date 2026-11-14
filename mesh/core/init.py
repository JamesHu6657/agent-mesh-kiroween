"""Mesh initialization: storage root, directories, database schema, credentials, and settings."""

import json
import os
import sqlite3
from typing import Any, Dict, Optional

import mesh.constants as c
from mesh.errors import MeshError
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

    # Create directories with mode 0700
    subdirs = ["run", "runs", "workspaces", "logs", "merges"]
    os.makedirs(home, mode=0o700, exist_ok=True)
    os.chmod(home, 0o700)

    for sub in subdirs:
        subpath = os.path.join(home, sub)
        os.makedirs(subpath, mode=0o700, exist_ok=True)
        os.chmod(subpath, 0o700)

    # Derive user keys
    verifier, k_user, salt, kdf_params = derive_user_keys(passphrase, testing_kdf=testing)

    # Initialize SQLite database
    db_path = os.path.join(home, "mesh.db")
    conn = init_db_file(db_path)
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
    finally:
        conn.close()

    # Ensure mesh.db permissions are 0600
    if os.path.exists(db_path):
        os.chmod(db_path, 0o600)

    return {
        "home": home,
        "user_id": user_id,
        "initialized": True,
    }
