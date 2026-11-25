"""Database connection management, PRAGMAs, transactions, and migrations per plan §5.2 and §5.3."""

from contextlib import contextmanager
import os
import pathlib
import sqlite3
import sys
from typing import Generator, List, Optional

import mesh.constants as c
from mesh.errors import DbIntegrityError, SchemaTooNewError
from mesh.core.paths import resolve_path
from mesh.core.schema import CURRENT_SCHEMA_VERSION, MIGRATIONS
from mesh.platform import perms


def sqlite_uri(path: str, mode: str) -> str:
    """SQLite URI for ``path`` with every reserved character escaped.

    m3-test-kdf round 3: the one way mesh opens SQLite (read and write paths). The
    path is resolved (realpath, symlinks before "..") and percent-encoded by Path.as_uri, so
    a relative or hostile name ("file:rel", "q?mode=memory", "#", "%", spaces)
    opens exactly that file, never a URI SQLite parses out of the name. ``mode`` is
    the SQLite open mode query parameter ("ro" or "rwc"). Resolution is the same
    as mesh.core.paths.resolve_path (no lexical ".." folding before symlinks).
    """
    resolved = resolve_path(path)
    return pathlib.Path(resolved).as_uri() + "?mode=" + mode


def sqlite_ro_uri(path: str) -> str:
    """Read-only SQLite URI for ``path`` with every reserved character escaped."""
    return sqlite_uri(path, "ro")


def connect(path: str, read_only: bool = False, timeout_ms: int = 5000) -> sqlite3.Connection:
    """Connect to SQLite database with required pragmas per plan §5.2.

    PRAGMAs executed on every connection:
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=FULL;
        PRAGMA foreign_keys=ON;
        PRAGMA busy_timeout=5000;
        PRAGMA trusted_schema=OFF;
    """
    if read_only:
        # Open in read-only URI mode. The path is percent-encoded (Path.as_uri), so a
        # home containing "?", "#", "%" or spaces opens that exact file instead of
        # being cut at the first "?" / "#" (m3-test-kdf round 2).
        uri = sqlite_ro_uri(path)
        conn = sqlite3.connect(uri, uri=True, isolation_level=None)
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute(f"PRAGMA busy_timeout={timeout_ms};")
        conn.execute("PRAGMA trusted_schema=OFF;")
    else:
        # Round 3: the write path is an escaped URI too (mode=rwc), never a raw path.
        conn = sqlite3.connect(sqlite_uri(path, "rwc"), uri=True, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=FULL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute(f"PRAGMA busy_timeout={timeout_ms};")
        conn.execute("PRAGMA trusted_schema=OFF;")

    conn.row_factory = sqlite3.Row
    return conn


connect_db = connect


@contextmanager
def transaction(conn: sqlite3.Connection, mode: str = "IMMEDIATE") -> Generator[sqlite3.Connection, None, None]:
    """Execute within an explicit SQLite transaction block.

    Default mode is IMMEDIATE (BEGIN IMMEDIATE per plan §5.2).
    Automatically commits on normal completion or rolls back on exception.
    """
    conn.execute(f"BEGIN {mode};")
    try:
        yield conn
        conn.execute("COMMIT;")
    except BaseException:
        # Full review: SQLite may have rolled back already (SQLITE_FULL/IOERR); never
        # mask the real error, and roll back on KeyboardInterrupt/SystemExit too.
        if conn.in_transaction:
            try:
                conn.execute("ROLLBACK;")
            except sqlite3.Error:
                pass
        raise


def check_quick(conn: sqlite3.Connection) -> None:
    """Run PRAGMA quick_check at startup per plan §5.2.

    Raises:
        DbIntegrityError (exit 1, DB_INTEGRITY_FAILED) if quick_check != ok.
    """
    cursor = conn.execute("PRAGMA quick_check;")
    rows = cursor.fetchall()
    lines = [row[0] for row in rows]
    if lines != ["ok"]:
        raise DbIntegrityError(lines)


def parse_schema_version(raw: object) -> Optional[int]:
    """Return integer schema version if *raw* is a strict unsigned int, else None."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    if isinstance(raw, str) and raw.isascii() and raw.isdigit():
        return int(raw)
    return None


def get_schema_version(conn: sqlite3.Connection) -> int:
    """Retrieve current schema version from meta table, or 0 if meta does not exist."""
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='meta';")
    if not cursor.fetchone():
        return 0

    cursor = conn.execute("SELECT value FROM meta WHERE key='schema_version';")
    row = cursor.fetchone()
    if not row:
        return 0
    try:
        return int(row["value"])
    except (ValueError, TypeError):
        return 0


def backup_db(conn: sqlite3.Connection, backup_path: str) -> str:
    """Write a consistent, private (0600) SQLite backup before schema migration (P2-2).

    Downgrade procedure: stop meshd, replace `mesh.db` with `mesh.db.v<N>.bak`,
    remove any stale `mesh.db-wal` / `mesh.db-shm` sidecars, and restart the
    older meshd binary.
    """
    backup_path = resolve_path(backup_path)
    parent = os.path.dirname(backup_path)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    tmp_path = f"{backup_path}.tmp.{os.getpid()}"
    old_umask = os.umask(0o077)
    dst_conn: Optional[sqlite3.Connection] = None
    try:
        # Ensure WAL checkpoint so backup captures all committed pages cleanly.
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except sqlite3.Error:
            pass
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        dst_conn = sqlite3.connect(sqlite_uri(tmp_path, "rwc"), uri=True, isolation_level=None)
        conn.backup(dst_conn)
        dst_conn.close()
        dst_conn = None
        try:
            perms.set_private_file_acl(tmp_path, 0o600)
        except OSError:
            pass
        try:
            fd = os.open(tmp_path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass
        os.replace(tmp_path, backup_path)
        try:
            perms.set_private_file_acl(backup_path, 0o600)
        except OSError:
            pass
        return backup_path
    except BaseException:
        if dst_conn is not None:
            try:
                dst_conn.close()
            except Exception:
                pass
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise
    finally:
        os.umask(old_umask)


def migrate_db(conn: sqlite3.Connection) -> int:
    """Apply pending migrations or verify schema version compatibility.

    Callers running on an existing live home MUST hold the daemon single-instance
    lock and save a backup (`backup_db`) before calling `migrate_db`.
    Downgrade is performed by stopping meshd and restoring `mesh.db.v<N>.bak`.

    Raises:
        SchemaTooNewError (exit 1, SCHEMA_TOO_NEW) if DB version > CURRENT_SCHEMA_VERSION.
        DbIntegrityError (exit 1, DB_INTEGRITY_FAILED) if meta exists with invalid/zero schema_version.
    """
    has_meta = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='meta';").fetchone()
    if has_meta:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version';").fetchone()
        if not row:
            raise DbIntegrityError(["schema_version missing in meta table"])
        raw = row["value"]
        current_ver = parse_schema_version(raw)
        if current_ver is None:
            raise DbIntegrityError([f"schema_version is not an integer: {raw!r}"])
        if current_ver > CURRENT_SCHEMA_VERSION:
            raise SchemaTooNewError(current_version=current_ver, supported_version=CURRENT_SCHEMA_VERSION)
        if current_ver < 1:
            raise DbIntegrityError([f"schema_version out of range: {current_ver}"])
    else:
        current_ver = 0

    for migration in MIGRATIONS:
        if migration.version > current_ver:
            with transaction(conn, mode="IMMEDIATE"):
                for stmt in migration.statements:
                    conn.execute(stmt)
                current_ver = migration.version

    return current_ver


def init_db(path: str) -> sqlite3.Connection:
    """Initialize a new database file at path with umask 077 and apply all migrations."""
    path = resolve_path(path)  # round 3: same resolution as the home and the URI
    db_dir = os.path.dirname(path)
    if not os.path.exists(db_dir):
        old_umask = os.umask(0o077)
        try:
            os.makedirs(db_dir, mode=0o700, exist_ok=True)
            if sys.platform == "win32":
                perms.set_private_dir_acl(db_dir, 0o700)
        finally:
            os.umask(old_umask)

    old_umask = os.umask(0o077)
    try:
        conn = connect(path, read_only=False)
        migrate_db(conn)
        check_quick(conn)
        # Ensure file mode is 0600
        try:
            perms.set_private_file_acl(path, 0o600)
        except OSError:
            pass
        return conn
    finally:
        os.umask(old_umask)
