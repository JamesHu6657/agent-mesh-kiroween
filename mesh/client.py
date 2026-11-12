"""Unix-socket client for Agent Mesh (thin client)."""

import getpass
import json
import os
import socket
import sys
from typing import Any, Dict, Optional, Tuple

import mesh.constants as c
from mesh.errors import MeshError
from mesh.core.paths import check_token_file_secure


class MeshClient:
    """Client connecting to meshd over the local Unix socket."""

    def __init__(self, home: str, timeout: float = 30.0):
        self.home = os.path.realpath(home)
        # A launched child gets MESH_SOCKET instead of MESH_HOME (plan §7.1, §11.1).
        # It applies only when neither --home nor MESH_HOME chose a home.
        default_home = os.path.realpath(os.path.expanduser("~/.agent-mesh"))
        mesh_socket = os.environ.get("MESH_SOCKET")
        if mesh_socket and not os.environ.get("MESH_HOME") and self.home == default_home:
            self.socket_path = mesh_socket
        else:
            self.socket_path = os.path.join(self.home, "run", "mesh.sock")
        self.timeout = timeout

    def send_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Send a JSON request and read a JSON response from meshd."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            sock.connect(self.socket_path)
        except (FileNotFoundError, ConnectionRefusedError, socket.timeout) as exc:
            sock.close()
            raise MeshError(
                code=c.DAEMON_UNAVAILABLE,
                exit_code=c.EXIT_DAEMON_UNAVAILABLE,
                message=f"Daemon unavailable at {self.socket_path}: {exc}. Hint: mesh daemon start",
            ) from exc

        try:
            req_data = (json.dumps(payload) + "\n").encode("utf-8")
            sock.sendall(req_data)

            # Read response line
            f = sock.makefile("r", encoding="utf-8")
            line = f.readline()
            if not line:
                raise MeshError(
                    code=c.DAEMON_UNAVAILABLE,
                    exit_code=c.EXIT_DAEMON_UNAVAILABLE,
                    message="Connection closed by daemon without response",
                )
            return json.loads(line)
        except socket.timeout as exc:
            raise MeshError(
                code=c.DAEMON_UNAVAILABLE,
                exit_code=c.EXIT_DAEMON_UNAVAILABLE,
                message=f"Daemon response timeout at {self.socket_path}: {exc}",
            ) from exc
        finally:
            sock.close()

    def resolve_auth(self, args: Any) -> Dict[str, Any]:
        """Gather auth credential from CLI arguments or environment per plan §7.1."""
        if hasattr(args, "_resolved_auth"):
            return getattr(args, "_resolved_auth")
        if getattr(self, "_cached_auth", None) is not None:
            return self._cached_auth

        auth = self._resolve_auth_uncached(args)
        if hasattr(args, "__dict__"):
            setattr(args, "_resolved_auth", auth)
        self._cached_auth = auth
        return auth

    def _resolve_auth_uncached(self, args: Any) -> Dict[str, Any]:
        user = getattr(args, "user", False)
        token_file = getattr(args, "token_file", None)
        if user and token_file:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message="Cannot specify both --user and --token-file",
                details={"param": "user"},
            )

        if user:
            # Passphrase read from fd or tty
            passphrase_fd = getattr(args, "passphrase_fd", None)
            if passphrase_fd is not None:
                try:
                    # Rewind if seekable
                    try:
                        os.lseek(passphrase_fd, 0, os.SEEK_SET)
                    except OSError:
                        pass
                    # Duplicate fd to read without closing original
                    dup_fd = os.dup(passphrase_fd)
                    with os.fdopen(dup_fd, "r", encoding="utf-8") as f:
                        passphrase = f.read().rstrip("\r\n")
                except Exception as exc:
                    raise MeshError(
                        code=c.INVALID_ARGUMENT,
                        exit_code=c.EXIT_USAGE,
                        message=f"Failed to read passphrase from fd {passphrase_fd}: {exc}",
                    )
            else:
                # Disallow reading passphrase from env (plan §7.1, checklist G17: MESH_PASSPHRASE ignored)
                # Try reading from /dev/tty
                if os.isatty(0):
                    try:
                        passphrase = getpass.getpass("Enter user passphrase: ")
                    except Exception:
                        raise MeshError(
                            code=c.NO_TTY,
                            exit_code=c.EXIT_UNAUTHENTICATED,
                            message="No TTY available for passphrase prompt and --passphrase-fd not provided",
                        )
                else:
                    # Check if /dev/tty can be opened directly
                    try:
                        with open("/dev/tty", "r") as tty_in:
                            passphrase = getpass.getpass("Enter user passphrase: ", stream=sys.stderr)
                    except Exception:
                        raise MeshError(
                            code=c.NO_TTY,
                            exit_code=c.EXIT_UNAUTHENTICATED,
                            message="No TTY available for passphrase prompt and --passphrase-fd not provided",
                        )

            if not passphrase:
                raise MeshError(
                    code=c.PASSPHRASE_INVALID,
                    exit_code=c.EXIT_UNAUTHENTICATED,
                    message="Passphrase cannot be empty",
                )

            return {"type": "user", "passphrase": passphrase}

        if token_file:
            check_token_file_secure(token_file, home=self.home)
            try:
                with open(token_file, "r", encoding="utf-8") as f:
                    token = f.read().strip()
                return {"type": "token", "token": token}
            except Exception as exc:
                raise MeshError(
                    code=c.TOKEN_INVALID,
                    exit_code=c.EXIT_UNAUTHENTICATED,
                    message=f"Failed to read token file '{token_file}': {exc}",
                )

        if "MESH_TOKEN" in os.environ:
            return {"type": "token", "token": os.environ["MESH_TOKEN"].strip()}

        return {"type": "none"}

    def execute_cmd(
        self,
        args: Any,
        cmd: str,
        cmd_args: Optional[Dict[str, Any]] = None,
        auth_override: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute command via daemon and return response envelope dict."""
        if cmd_args is None:
            cmd_args = {}

        auth = auth_override if auth_override is not None else self.resolve_auth(args)
        req_id = getattr(args, "request_id", None)

        payload = {
            "v": 1,
            "request_id": req_id,
            "cmd": cmd,
            "auth": auth,
            "args": cmd_args,
            "idempotency_key": getattr(args, "idempotency_key", None),
            "dry_run": bool(getattr(args, "dry_run", False)),
        }
        if getattr(args, "approval", None):
            payload["approval"] = getattr(args, "approval")
        if getattr(args, "relayed", False):
            payload["relayed"] = True
            payload["user_statement"] = getattr(args, "user_statement", None)
            payload["evidence_ref"] = getattr(args, "evidence_ref", None)
        if getattr(args, "if_version", None) is not None:
            payload["if_version"] = getattr(args, "if_version")

        timeout_val = cmd_args.get("timeout") if isinstance(cmd_args, dict) else None
        if not timeout_val and hasattr(args, "timeout"):
            timeout_val = getattr(args, "timeout", None)
        if timeout_val:
            try:
                from mesh.core.clock import parse_duration
                dur = parse_duration(timeout_val)
                if cmd.endswith(".wait"):
                    self.timeout = float(dur) + 15.0
                else:
                    self.timeout = max(self.timeout or 0.0, float(dur) + 15.0)
            except Exception:
                pass
        elif cmd == "event.tail":
            self.timeout = None

        resp = self.send_request(payload)
        if not resp.get("ok", False):
            err = resp.get("error", {})
            raise MeshError(
                code=err.get("code", c.INTERNAL_ERROR),
                exit_code=resp.get("exit_code", c.EXIT_USAGE),
                message=err.get("message", "Unknown error"),
                details=err.get("details", {}),
                data=resp.get("data"),
            )

        if resp.get("idempotent_replay"):
            setattr(args, "_idempotent_replay", True)
        if resp.get("warnings"):
            setattr(args, "_warnings", resp.get("warnings"))

        return resp

    def stream_cmd(
        self,
        args: Any,
        cmd: str,
        cmd_args: Optional[Dict[str, Any]] = None,
        auth_override: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Stream NDJSON response lines from meshd."""
        if cmd_args is None:
            cmd_args = {}

        auth = auth_override if auth_override is not None else self.resolve_auth(args)
        req_id = getattr(args, "request_id", None)

        payload = {
            "v": 1,
            "request_id": req_id,
            "cmd": cmd,
            "auth": auth,
            "args": cmd_args,
            "idempotency_key": getattr(args, "idempotency_key", None),
            "dry_run": bool(getattr(args, "dry_run", False)),
        }
        if getattr(args, "approval", None):
            payload["approval"] = getattr(args, "approval")
        if getattr(args, "relayed", False):
            payload["relayed"] = True
            payload["user_statement"] = getattr(args, "user_statement", None)
            payload["evidence_ref"] = getattr(args, "evidence_ref", None)

        timeout_val = cmd_args.get("timeout") or getattr(args, "timeout", None)
        sock_timeout = None
        if timeout_val:
            try:
                from mesh.core.clock import parse_duration
                dur = parse_duration(timeout_val)
                sock_timeout = float(dur) + 15.0
            except Exception:
                pass

        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(sock_timeout)
            sock.connect(self.socket_path)
        except (FileNotFoundError, ConnectionRefusedError, socket.timeout) as exc:
            raise MeshError(
                code=c.DAEMON_UNAVAILABLE,
                exit_code=c.EXIT_DAEMON_UNAVAILABLE,
                message=f"Daemon unavailable at {self.socket_path}: {exc}. Hint: mesh daemon start",
            ) from exc

        try:
            req_data = (json.dumps(payload) + "\n").encode("utf-8")
            sock.sendall(req_data)

            f = sock.makefile("r", encoding="utf-8")
            items = []
            for line in f:
                line_str = line.strip()
                if not line_str:
                    continue
                try:
                    data = json.loads(line_str)
                except Exception:
                    continue

                if isinstance(data, dict) and data.get("ok") is False and "error" in data:
                    err = data.get("error", {})
                    raise MeshError(
                        code=err.get("code", c.INTERNAL_ERROR),
                        exit_code=data.get("exit_code", c.EXIT_USAGE),
                        message=err.get("message", "Unknown error"),
                        details=err.get("details", {}),
                    )

                items.append(data)
                sys.stdout.write(line_str + "\n")
                sys.stdout.flush()

            return {"items": items}
        except socket.timeout as exc:
            raise MeshError(
                code=c.DAEMON_UNAVAILABLE,
                exit_code=c.EXIT_DAEMON_UNAVAILABLE,
                message=f"Daemon response timeout at {self.socket_path}: {exc}",
            ) from exc
        finally:
            sock.close()
