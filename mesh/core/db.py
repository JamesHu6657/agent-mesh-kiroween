"""Database connection management, PRAGMAs, transactions, and migrations per plan §5.2 and §5.3."""

from contextlib import contextmanager
import os
import pathlib
import sqlite3
from typing import Generator, List, Optional

import mesh.constants as c
from mesh.errors import DbIntegrityError, SchemaTooNewError
from mesh.core.paths import resolve_path
from mesh.core.schema import CURRENT_SCHEMA_VERSION, MIGRATIONS


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
    except Exception:
        conn.execute("ROLLBACK;")
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


def migrate_db(conn: sqlite3.Connection) -> int:
    """Apply pending migrations or verify schema version compatibility.

    Raises:
        SchemaTooNewError (exit 1, SCHEMA_TOO_NEW) if DB version > CURRENT_SCHEMA_VERSION.
    """
    current_ver = get_schema_version(conn)
    if current_ver > CURRENT_SCHEMA_VERSION:
        raise SchemaTooNewError(current_version=current_ver, supported_version=CURRENT_SCHEMA_VERSION)

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
        finally:
            os.umask(old_umask)

    old_umask = os.umask(0o077)
    try:
        conn = connect(path, read_only=False)
        migrate_db(conn)
        check_quick(conn)
        # Ensure file mode is 0600
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return conn
    finally:
        os.umask(old_umask)
