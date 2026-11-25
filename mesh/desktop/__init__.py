"""Agent Mesh Desktop UI package (optional GUI frontend over Mesh IPC)."""

from mesh.desktop.bridge import (
    ALLOWED_RPC_METHODS,
    CAPABILITIES,
    UNAUTH_RPC_METHODS,
    DesktopBridge,
    DesktopJsApi,
    asset_dir,
    index_html_path,
)

__all__ = [
    "ALLOWED_RPC_METHODS",
    "CAPABILITIES",
    "UNAUTH_RPC_METHODS",
    "DesktopBridge",
    "DesktopJsApi",
    "asset_dir",
    "index_html_path",
]
