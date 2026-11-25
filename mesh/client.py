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
from mesh.platform import ipc
from mesh.platform.paths import is_fs_root, norm_cmp


#: Set by the CLI when --home was given on the command line (blind review P2): then
#: MESH_SOCKET never redirects the request, even when --home names the default path.
EXPLICIT_HOME = False


class MeshClient:
    """Client connecting to meshd over the local Unix socket or Windows named pipe."""

    def __init__(self, home: str, timeout: float = 30.0):
        self.home = os.path.realpath(home)
        # A launched child gets MESH_SOCKET instead of MESH_HOME (plan §7.1, §11.1).
        # It applies only when neither --home nor MESH_HOME chose a home.
        default_home = os.path.realpath(os.path.expanduser("~/.agent-mesh"))
        mesh_socket = os.environ.get("MESH_SOCKET")
        self._pipe_home_pending = False
        self._socket_explicit = False
        if (mesh_socket and not os.environ.get("MESH_HOME") and not EXPLICIT_HOME
                and norm_cmp(self.home) == norm_cmp(default_home)):
            self.socket_path = mesh_socket
            self._socket_explicit = True
            if ipc.is_win_pipe_path(mesh_socket):
                self._pipe_home_pending = True
            else:
                sock_real = os.path.realpath(mesh_socket)
                sock_dir = os.path.dirname(sock_real)
                if os.path.basename(sock_real) == "mesh.sock" and os.path.basename(sock_dir) == "run":
                    derived_home = os.path.dirname(sock_dir)
                    if derived_home and not is_fs_root(derived_home):
                        self.home = derived_home
        else:
            self.socket_path = ipc.daemon_endpoint_path(self.home)
        self.timeout = timeout

    def _resolve_win_pipe_home(self) -> None:
        if not getattr(self, "_pipe_home_pending", False):
            return
        self._pipe_home_pending = False
        resp = self.send_request({"v": 1, "cmd": "daemon.status", "auth": {"type": "none"}})
        if not isinstance(resp, dict) or not resp.get("ok"):
            return
        reported = (resp.get("data") or {}).get("home")
        if not isinstance(reported, str) or not reported.strip():
            return
        if norm_cmp(ipc.daemon_endpoint_path_win(reported)) != norm_cmp(self.socket_path):
            raise MeshError(
                code=c.TOKEN_FILE_INSECURE,
                exit_code=c.EXIT_UNAUTHENTICATED,
                message="Refusing token file: daemon reported home does not match MESH_SOCKET pipe",
                details={"socket": self.socket_path, "reported_home": reported},
            )
        self.home = os.path.realpath(reported)

    def _connect_socket(self, timeout: Optional[float]) -> Any:
        if sys.platform != "win32":
            from mesh.core.paths import check_socket_path_length

            check_socket_path_length(self.socket_path)
        else:
            if not getattr(self, "_socket_explicit", False):
                try:
                    self.socket_path = ipc.daemon_endpoint_path(self.home)
                except OSError as exc:
                    raise MeshError(
                        code=c.DAEMON_UNAVAILABLE,
                        exit_code=c.EXIT_DAEMON_UNAVAILABLE,
                        message=f"Daemon unavailable at {self.socket_path}: {exc}. Hint: mesh daemon start",
                    ) from exc
            if not ipc.is_win_pipe_path(self.socket_path):
                raise MeshError(
                    code=c.DAEMON_UNAVAILABLE,
                    exit_code=c.EXIT_DAEMON_UNAVAILABLE,
                    message=(
                        f"Daemon unavailable at {self.socket_path}: "
                        "invalid Windows named pipe endpoint. Hint: mesh daemon start"
                    ),
                )
        try:
            return ipc.connect_endpoint(self.socket_path, timeout=timeout)
        except OSError as exc:
            raise MeshError(
                code=c.DAEMON_UNAVAILABLE,
                exit_code=c.EXIT_DAEMON_UNAVAILABLE,
                message=f"Daemon unavailable at {self.socket_path}: {exc}. Hint: mesh daemon start",
            ) from exc

    def send_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Send a JSON request and read a JSON response from meshd."""
        sock = self._connect_socket(self.timeout)
        f = None
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
        except OSError as exc:
            raise MeshError(
                code=c.DAEMON_UNAVAILABLE,
                exit_code=c.EXIT_DAEMON_UNAVAILABLE,
                message=f"Daemon communication error at {self.socket_path}: {exc}",
            ) from exc
        except (ValueError, UnicodeDecodeError) as exc:
            raise MeshError(
                code=c.DAEMON_UNAVAILABLE,
                exit_code=c.EXIT_DAEMON_UNAVAILABLE,
                message=f"Invalid response from daemon at {self.socket_path}: {exc}",
            ) from exc
        finally:
            if f is not None:
                try:
                    f.close()
                except OSError:
                    pass
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
            # Passphrase: fd, file, env opt-in, then tty (mesh/core/passphrase.py)
            from mesh.core.passphrase import read_passphrase
            passphrase = read_passphrase(args)

            if not passphrase:
                raise MeshError(
                    code=c.PASSPHRASE_INVALID,
                    exit_code=c.EXIT_UNAUTHENTICATED,
                    message="Passphrase cannot be empty",
                )

            return {"type": "user", "passphrase": passphrase}

        if token_file:
            if getattr(self, "_pipe_home_pending", False):
                self._resolve_win_pipe_home()
            # Linear round 4 P2: also refuse tokens inside the configured workspaces.root
            # and registered repositories, not only the default locations.
            from mesh.core.paths import read_token_file_secure
            from mesh.core.token_roots import configured_roots

            ws_root, repo_paths = configured_roots(self.home)
            token = read_token_file_secure(
                token_file, home=self.home, workspaces_root=ws_root, repo_paths=repo_paths
            )
            return {"type": "token", "token": token}

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
        if getattr(args, "_replay_only", False):
            payload["replay_only"] = True

        has_cmd_timeout = isinstance(cmd_args, dict) and cmd_args.get("timeout") is not None
        if cmd == "task.create":
            timeout_val = None
        else:
            timeout_val = cmd_args["timeout"] if has_cmd_timeout else getattr(args, "timeout", None)
        if timeout_val is not None:
            try:
                from mesh.core.clock import parse_duration
                dur = parse_duration(timeout_val)
            except (ValueError, TypeError) as exc:
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message=f"Invalid --timeout value '{timeout_val}': {exc}",
                    details={"param": "timeout", "value": timeout_val},
                ) from exc
            if cmd.endswith(".wait") or cmd == "event.tail":
                self.timeout = float(dur) + 15.0
            else:
                if dur <= 0:
                    raise MeshError(
                        code=c.INVALID_ARGUMENT,
                        exit_code=c.EXIT_USAGE,
                        message=f"--timeout must be positive for client requests, got '{timeout_val}'",
                        details={"param": "timeout", "value": timeout_val},
                    )
                self.timeout = float(dur)
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

        has_cmd_timeout = isinstance(cmd_args, dict) and cmd_args.get("timeout") is not None
        timeout_val = cmd_args["timeout"] if has_cmd_timeout else getattr(args, "timeout", None)
        sock_timeout = None if cmd == "event.tail" else self.timeout
        if timeout_val is not None:
            try:
                from mesh.core.clock import parse_duration
                dur = parse_duration(timeout_val)
            except (ValueError, TypeError) as exc:
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message=f"Invalid --timeout value '{timeout_val}': {exc}",
                    details={"param": "timeout", "value": timeout_val},
                ) from exc
            if cmd.endswith(".wait") or cmd == "event.tail":
                sock_timeout = float(dur) + 15.0
            else:
                if dur <= 0:
                    raise MeshError(
                        code=c.INVALID_ARGUMENT,
                        exit_code=c.EXIT_USAGE,
                        message=f"--timeout must be positive for client requests, got '{timeout_val}'",
                        details={"param": "timeout", "value": timeout_val},
                    )
                sock_timeout = float(dur)

        sock = self._connect_socket(sock_timeout)
        f = None
        try:
            req_data = (json.dumps(payload) + "\n").encode("utf-8")
            sock.sendall(req_data)

            f = sock.makefile("r", encoding="utf-8")
            # Blind review P3: a live tail can run for days; count events, never keep them.
            count = 0
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

                count += 1
                sys.stdout.write(line_str + "\n")
                sys.stdout.flush()

            return {"count": count}
        except socket.timeout as exc:
            raise MeshError(
                code=c.DAEMON_UNAVAILABLE,
                exit_code=c.EXIT_DAEMON_UNAVAILABLE,
                message=f"Daemon response timeout at {self.socket_path}: {exc}",
            ) from exc
        except (OSError, UnicodeDecodeError) as exc:
            raise MeshError(
                code=c.DAEMON_UNAVAILABLE,
                exit_code=c.EXIT_DAEMON_UNAVAILABLE,
                message=f"Daemon communication error at {self.socket_path}: {exc}",
            ) from exc
        finally:
            if f is not None:
                try:
                    f.close()
                except OSError:
                    pass
            sock.close()
