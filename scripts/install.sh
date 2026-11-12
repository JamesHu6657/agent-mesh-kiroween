#!/bin/sh
# Agent Mesh installer for a clone of the repository (Linux only, no dependencies).
# Links bin/mesh into a directory on your PATH (default ~/.local/bin). Re-run to repair;
# `scripts/install.sh --uninstall` removes the link (your ~/.agent-mesh data stays).
set -eu
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)"
DEST="${MESH_BIN_DIR:-$HOME/.local/bin}"

if [ "${1:-}" = "--uninstall" ]; then
  if [ -L "$DEST/mesh" ] && [ "$(readlink "$DEST/mesh")" = "$ROOT/bin/mesh" ]; then
    rm -f "$DEST/mesh"; echo "removed $DEST/mesh"
  else
    echo "nothing to remove at $DEST/mesh (not a link to this clone)"
  fi
  echo "data is kept: stop the daemon first (mesh daemon stop --user), then delete ~/.agent-mesh and ~/.config/agent-mesh yourself if you want"
  exit 0
fi

fail() { echo "install: $*" >&2; exit 1; }
[ "$(uname -s)" = "Linux" ] || fail "Agent Mesh supports Linux only (found $(uname -s))"
command -v python3 >/dev/null 2>&1 || fail "python3 not found; install Python 3.11 or newer"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || fail "python3 is $(python3 -c 'import platform; print(platform.python_version())'); 3.11 or newer is required"
command -v git >/dev/null 2>&1 || echo "install: warning: git not found; repo projects need it" >&2

mkdir -p "$DEST"
if [ -e "$DEST/mesh" ] && ! [ -L "$DEST/mesh" ]; then
  fail "$DEST/mesh exists and is not a link; remove it or set MESH_BIN_DIR"
fi
ln -sfn "$ROOT/bin/mesh" "$DEST/mesh"
echo "linked $DEST/mesh -> $ROOT/bin/mesh"
case ":$PATH:" in *":$DEST:"*) ;; *) echo "note: add $DEST to your PATH, e.g. echo 'export PATH=\"$DEST:\$PATH\"' >> ~/.profile" ;; esac
echo "next: mesh setup"
