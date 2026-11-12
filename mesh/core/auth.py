"""Authentication, cryptographic verification, tokens, and run-session classification.

Per plan §7.1, §15.1, §15.3:
- User passphrase: KDF scrypt with n=2**15, r=8, p=1, dklen=64, maxmem=64*1024*1024.
  Testing homes (m3-test-kdf) may use n=2**10; only a testing-mode daemon unlocks them.
- Agent tokens: mesh_at_<lookup10>_<secret43>.
- Run tokens: mesh_rt_<lookup10>_<secret43>.
- SO_PEERCRED check: verify peer uid matches daemon uid.
- Run-session classifier: inspect peer pid session id, ancestry, and MESH_RUN_ID in environ.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import socket
import struct
from typing import Any, Dict, Optional, Set, Tuple

import mesh.constants as c
from mesh.errors import MeshError
from mesh.core.proc import get_proc_ancestry, get_proc_environ, get_proc_stat

# Scrypt parameters per plan §7.1
SCRYPT_N = 2**15  # 32768
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 64
SCRYPT_MAXMEM = 64 * 1024 * 1024  # 64 MiB

# m3-test-kdf: cheap scrypt cost for homes created by `mesh init` in testing mode
# (MESH_TESTING=1) on a non-default home. Such a home carries "testing": true in
# its kdf_params and outside testing mode it can never be unlocked (see
# parse_kdf_params), so the cheap cost cannot reach a production home.
SCRYPT_N_TESTING = 2**10  # 1024
# The floor below which a non-testing daemon refuses to unlock (production cost).
SCRYPT_N_MIN = SCRYPT_N
SCRYPT_R_MIN = SCRYPT_R
# Sane upper bounds (m3-test-kdf fix round): anything above is refused before scrypt
# runs, so a tampered row cannot make an unlock burn unbounded CPU or memory.
SCRYPT_N_MAX = 2**20
SCRYPT_R_MAX = 32
SCRYPT_P_MAX = 16
SCRYPT_MAXMEM_MAX = 256 * 1024 * 1024  # 256 MiB
# The only keys init ever writes (plus the optional testing marker).
_KDF_KEYS = frozenset({"n", "r", "p", "dklen", "maxmem"})
_KDF_OPTIONAL_KEYS = frozenset({"testing"})


def derive_user_keys(passphrase: str, salt: Optional[bytes] = None,
                     testing_kdf: bool = False) -> Tuple[bytes, bytes, bytes, Dict[str, Any]]:
    """Derive user verifier and MAC key from passphrase.

    ``testing_kdf=True`` (only `mesh init` in testing mode on a non-default home,
    see mesh.core.init) uses n=SCRYPT_N_TESTING and marks the params
    ``"testing": true``; the default is the production cost.

    Returns:
        (verifier, K_user, salt, kdf_params)
    """
    if len(passphrase) < 12:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Passphrase must be at least 12 characters long",
            details={"min_length": 12, "actual_length": len(passphrase)},
        )

    if salt is None:
        salt = os.urandom(16)

    n = SCRYPT_N_TESTING if testing_kdf else SCRYPT_N
    dk = hashlib.scrypt(
        passphrase.encode("utf-8"),
        salt=salt,
        n=n,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
        maxmem=SCRYPT_MAXMEM,
    )

    verifier = dk[:32]
    k_user = dk[32:]
    kdf_params: Dict[str, Any] = {
        "n": n,
        "r": SCRYPT_R,
        "p": SCRYPT_P,
        "dklen": SCRYPT_DKLEN,
        "maxmem": SCRYPT_MAXMEM,
    }
    if testing_kdf:
        kdf_params["testing"] = True
    return verifier, k_user, salt, kdf_params


class _KdfInvalid(Exception):
    pass


def _reject_json_constant(name: str) -> Any:
    raise _KdfInvalid(f"non-finite number {name}")


def _strict_int(params: Dict[str, Any], key: str) -> int:
    val = params[key]
    if isinstance(val, bool) or not isinstance(val, int):
        raise _KdfInvalid(f"{key} is not an integer")
    return val


def _kdf_invalid_error(reason: str) -> MeshError:
    return MeshError(
        code=c.KDF_PARAMS_INVALID,
        exit_code=c.EXIT_UNAUTHENTICATED,
        message=(
            f"Refusing to unlock: this home's stored passphrase KDF parameters are invalid ({reason}). "
            "The credential row is damaged or was edited; Mesh never runs scrypt on parameters it did "
            "not write. Restore mesh.db from a backup or create a new home with 'mesh init'."
        ),
        details={"reason": reason},
    )


def parse_kdf_params(raw: Any, testing_mode: bool) -> Dict[str, Any]:
    """Parse and validate a home's stored user-passphrase KDF params (fail closed).

    The single helper used by meshd's unlock and by `mesh doctor` (m3-test-kdf fix
    round), so both refuse the same homes with the same code and rc 3:

    - ``KDF_PARAMS_INVALID``: the stored value is not exactly what `mesh init`
      writes: SQL NULL, empty, garbled JSON, JSON null or any non-object, NaN or
      Infinity, a missing or unknown key, a non-integer value, n not a power of two
      or outside [2**10, 2**20], r outside [8, 32], p outside [1, 16], dklen other
      than 64, maxmem outside [needed memory, 256 MiB], or a testing marker that is
      not ``true``. Refused in every mode, testing mode included.
    - ``KDF_TOO_WEAK``: valid params below the production cost (n < 2**15) or
      carrying the testing marker, when ``testing_mode`` is False.

    ``testing_mode`` is required and is never read from the environment here: the
    caller (meshd at startup, doctor at start) decides it once. ``raw`` is the
    stored text (str or bytes); an already-decoded dict is accepted for callers
    that hold one and is validated the same way. Nothing here runs scrypt.
    """
    if not isinstance(testing_mode, bool):
        raise TypeError("testing_mode must be a bool decided by the caller")
    try:
        if isinstance(raw, dict):
            params: Any = raw
        else:
            if raw is None:
                raise _KdfInvalid("missing (NULL)")
            if isinstance(raw, (bytes, bytearray, memoryview)):
                try:
                    raw = bytes(raw).decode("utf-8")
                except UnicodeDecodeError:
                    raise _KdfInvalid("not UTF-8 text")
            if not isinstance(raw, str):
                raise _KdfInvalid(f"stored as {type(raw).__name__}, not text")
            if not raw.strip():
                raise _KdfInvalid("empty")
            try:
                params = json.loads(raw, parse_constant=_reject_json_constant)
            except _KdfInvalid:
                raise
            except (ValueError, RecursionError):
                raise _KdfInvalid("not valid JSON")
        if not isinstance(params, dict):
            raise _KdfInvalid("not a JSON object" if params is not None else "JSON null")
        keys = set(params)
        missing = sorted(_KDF_KEYS - keys)
        if missing:
            raise _KdfInvalid("missing " + ", ".join(missing))
        unknown = sorted(str(k) for k in keys - _KDF_KEYS - _KDF_OPTIONAL_KEYS)
        if unknown:
            raise _KdfInvalid("unknown key " + ", ".join(unknown))
        n = _strict_int(params, "n")
        r = _strict_int(params, "r")
        p = _strict_int(params, "p")
        dklen = _strict_int(params, "dklen")
        maxmem = _strict_int(params, "maxmem")
        if n < SCRYPT_N_TESTING or n > SCRYPT_N_MAX or n & (n - 1):
            raise _KdfInvalid(f"n={n} is not a power of two in [2**10, 2**20]")
        if r < SCRYPT_R_MIN or r > SCRYPT_R_MAX:
            raise _KdfInvalid(f"r={r} outside [{SCRYPT_R_MIN}, {SCRYPT_R_MAX}]")
        if p < 1 or p > SCRYPT_P_MAX:
            raise _KdfInvalid(f"p={p} outside [1, {SCRYPT_P_MAX}]")
        if dklen != SCRYPT_DKLEN:
            raise _KdfInvalid(f"dklen={dklen}, expected {SCRYPT_DKLEN}")
        # scrypt needs about 128*r*(n+p+2) bytes; maxmem below that would make
        # hashlib raise, above SCRYPT_MAXMEM_MAX is not something init writes.
        needed = 128 * r * (n + p + 2)
        if maxmem < needed or maxmem > SCRYPT_MAXMEM_MAX:
            raise _KdfInvalid(f"maxmem={maxmem} outside [{needed}, {SCRYPT_MAXMEM_MAX}]")
        marker = params.get("testing", False)
        if not isinstance(marker, bool) or ("testing" in params and marker is not True):
            raise _KdfInvalid("testing marker is not true")
    except _KdfInvalid as exc:
        raise _kdf_invalid_error(str(exc)) from None

    weak = n < SCRYPT_N_MIN or marker
    if weak and not testing_mode:
        raise MeshError(
            code=c.KDF_TOO_WEAK,
            exit_code=c.EXIT_UNAUTHENTICATED,
            message=(
                f"Refusing to unlock: this home's passphrase KDF (scrypt n={n}) is below the production "
                f"cost n={SCRYPT_N_MIN} or marked as a testing home; it was created in testing mode "
                "(MESH_TESTING=1) and can only be used by a testing-mode daemon. Create a new home with "
                "'mesh init' outside testing mode."
            ),
            details={"kdf_n": n, "min_n": SCRYPT_N_MIN, "testing_marker": bool(marker)},
        )
    return {"n": n, "r": r, "p": p, "dklen": dklen, "maxmem": maxmem, **({"testing": True} if marker else {})}


def kdf_params_weak(kdf_params: Any) -> bool:
    """True if the params would be refused outside testing mode (weak or invalid)."""
    try:
        parse_kdf_params(kdf_params, testing_mode=False)
    except MeshError:
        return True
    return False


def verify_user_passphrase(
    passphrase: str,
    salt: bytes,
    kdf_params: Any,
    stored_verifier: bytes,
    *,
    testing_mode: bool,
) -> Tuple[bool, Optional[bytes]]:
    """Verify passphrase against stored verifier.

    ``kdf_params`` is the stored value (text or dict); it goes through
    parse_kdf_params first, so KDF_PARAMS_INVALID / KDF_TOO_WEAK are raised before
    any scrypt work. ``testing_mode`` is required (the daemon's own mode).

    Returns:
        (is_valid, K_user or None)
    """
    params = parse_kdf_params(kdf_params, testing_mode)
    dk = hashlib.scrypt(
        passphrase.encode("utf-8"),
        salt=salt,
        n=params["n"],
        r=params["r"],
        p=params["p"],
        dklen=params["dklen"],
        maxmem=params["maxmem"],
    )

    verifier = dk[:32]
    k_user = dk[32:]
    if hmac.compare_digest(verifier, stored_verifier):
        return True, k_user
    return False, None


ALPHANUM = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def _generate_token_parts(prefix: str) -> Tuple[str, str, bytes]:
    # 10 chars lookup (alphanumeric so it never contains '_')
    lookup = "".join(secrets.choice(ALPHANUM) for _ in range(10))
    # 32 random bytes urlsafe base64 without padding is 43 chars
    raw_secret = secrets.token_bytes(32)
    secret = base64.urlsafe_b64encode(raw_secret).decode("ascii").rstrip("=")[:43]
    token = f"{prefix}_{lookup}_{secret}"
    secret_hash = hashlib.sha256(token.encode("utf-8")).digest()
    return token, lookup, secret_hash


def generate_agent_token() -> Tuple[str, str, bytes]:
    """Generate agent token: mesh_at_<lookup10>_<secret43>.

    Returns:
        (token, lookup, secret_hash)
    """
    return _generate_token_parts("mesh_at")


def generate_run_token() -> Tuple[str, str, bytes]:
    """Generate run token: mesh_rt_<lookup10>_<secret43>.

    Returns:
        (token, lookup, secret_hash)
    """
    return _generate_token_parts("mesh_rt")


def parse_token_lookup(token: str) -> Optional[str]:
    """Extract lookup ID from token string."""
    if token.startswith("mesh_at_") or token.startswith("mesh_rt_"):
        if len(token) >= 19 and token[18] == "_":
            return token[8:18]
    return None


def verify_token_hash(token: str, stored_hash: bytes) -> bool:
    """Compare sha256(token) against stored hash constant-time."""
    token_hash = hashlib.sha256(token.encode("utf-8")).digest()
    return hmac.compare_digest(token_hash, stored_hash)


def get_peer_credentials(sock: socket.socket) -> Tuple[int, int, int]:
    """Get (pid, uid, gid) from Unix socket using SO_PEERCRED."""
    ucred = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, gid = struct.unpack("3i", ucred)
    return pid, uid, gid


def classify_peer_run_session(
    peer_pid: int,
    active_runs: Dict[str, int],
    known_run_ids: Set[str],
    meshd_pid: int,
) -> Optional[str]:
    """Classify whether peer_pid is inside a run session per plan §7.1.

    A peer is inside run R if any of:
    (a) its session id equals the sid of an active run R (the wrapper pid)
    (b) walking PPid upward reaches R's wrapper pid before pid 1 or meshd
    (c) /proc/<pid>/environ contains MESH_RUN_ID=<R> for any known run R (active or ended)

    If /proc cannot be read: raise MeshError(CREDENTIAL_NOT_ALLOWED_IN_RUN).
    Returns run_id if inside run R, else None.
    """
    # Invert active_runs mapping: wrapper_pid -> run_id
    wrapper_to_run = {wpid: rid for rid, wpid in active_runs.items() if wpid}

    try:
        stat_info = get_proc_stat(peer_pid)
    except OSError as exc:
        raise MeshError(
            code=c.CREDENTIAL_NOT_ALLOWED_IN_RUN,
            exit_code=c.EXIT_FORBIDDEN,
            message=f"Unable to read /proc/{peer_pid}/stat for run-session classification: {exc}",
            details={"peer_pid": peer_pid},
        ) from exc
    if stat_info is None:
        raise MeshError(
            code=c.CREDENTIAL_NOT_ALLOWED_IN_RUN,
            exit_code=c.EXIT_FORBIDDEN,
            message=f"Unable to read /proc/{peer_pid}/stat for run-session classification",
            details={"peer_pid": peer_pid},
        )

    # (a) Check session id
    sid = stat_info["session"]
    if sid in wrapper_to_run:
        return wrapper_to_run[sid]

    # (b) Walk PPid upward
    try:
        ancestry = get_proc_ancestry(peer_pid, stop_at_pids={1, meshd_pid})
    except OSError as exc:
        raise MeshError(
            code=c.CREDENTIAL_NOT_ALLOWED_IN_RUN,
            exit_code=c.EXIT_FORBIDDEN,
            message=f"Unable to read /proc ancestry for peer {peer_pid}: {exc}",
            details={"peer_pid": peer_pid},
        ) from exc
    for ppid in ancestry:
        if ppid in wrapper_to_run:
            return wrapper_to_run[ppid]

    # (c) Check MESH_RUN_ID in environ
    try:
        env = get_proc_environ(peer_pid)
    except OSError as exc:
        raise MeshError(
            code=c.CREDENTIAL_NOT_ALLOWED_IN_RUN,
            exit_code=c.EXIT_FORBIDDEN,
            message=f"Unable to read /proc/{peer_pid}/environ for run-session classification: {exc}",
            details={"peer_pid": peer_pid},
        ) from exc
    if env is None:
        raise MeshError(
            code=c.CREDENTIAL_NOT_ALLOWED_IN_RUN,
            exit_code=c.EXIT_FORBIDDEN,
            message=f"Unable to read /proc/{peer_pid}/environ for run-session classification",
            details={"peer_pid": peer_pid},
        )

    run_id_in_env = env.get("MESH_RUN_ID")
    if run_id_in_env and run_id_in_env in known_run_ids:
        return run_id_in_env

    return None
