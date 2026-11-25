"""Platform abstraction layer for Agent Mesh (Linux/POSIX and native Windows)."""

from __future__ import annotations

from mesh.platform import fs, ipc, locks, paths, perms, pipe_pump, proc

__all__ = ["fs", "ipc", "locks", "paths", "perms", "pipe_pump", "proc"]
