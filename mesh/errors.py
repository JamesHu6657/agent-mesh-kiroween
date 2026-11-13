"""Error hierarchy for Agent Mesh."""

from typing import Any, Dict, Optional
import mesh.constants as c


class MeshError(Exception):
    """Base error class for all Mesh errors per plan §3 and §13.2."""

    def __init__(
        self,
        code: str,
        exit_code: int = c.EXIT_USAGE,
        message: str = "",
        details: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code
        self.message = message
        self.details = details if details is not None else {}
        self.data = data

    def to_dict(self) -> Dict[str, Any]:
        from mesh.core.redact import redact, redact_value

        return {
            "code": self.code,
            "message": redact(self.message),
            "details": redact_value(self.details),
        }

    def to_envelope(self, request_id: Optional[str] = None) -> Dict[str, Any]:
        env = {
            "ok": False,
            "error": self.to_dict(),
            "exit_code": self.exit_code,
            "request_id": request_id,
        }
        if self.data is not None:
            env["data"] = self.data
        return env


class NotImplementedInM1Error(MeshError):
    """Raised for commands/verbs not yet implemented in M1 or the current gate."""

    def __init__(self, verb: str, milestone: Optional[str] = None):
        details = {}
        if milestone is not None:
            details["milestone"] = milestone
        super().__init__(
            code=c.NOT_IMPLEMENTED_IN_M1,
            exit_code=c.EXIT_USAGE,
            message=f"'{verb}' is not available in this version of Agent Mesh",
            details=details,
        )


class SchemaTooNewError(MeshError):
    """Raised when the database schema version is newer than supported."""

    def __init__(self, current_version: int, supported_version: int):
        super().__init__(
            code=c.SCHEMA_TOO_NEW,
            exit_code=c.EXIT_INTERNAL,
            message=f"Database schema version {current_version} is newer than supported {supported_version}",
            details={"current_version": current_version, "supported_version": supported_version},
        )


class DbIntegrityError(MeshError):
    """Raised when SQLite quick_check or integrity_check fails."""

    def __init__(self, lines: list):
        super().__init__(
            code=c.DB_INTEGRITY_FAILED,
            exit_code=c.EXIT_INTERNAL,
            message="Database integrity verification failed",
            details={"quick_check": lines[:20]},
        )

