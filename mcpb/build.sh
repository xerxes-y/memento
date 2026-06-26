#!/usr/bin/env bash
# Build the devin-memento MCP Bundle (.mcpb) for one-click install in
# .mcpb-capable MCP clients. Output: dist/devin-memento.mcpb
#
# Requires Node/npx (uses @anthropic-ai/mcpb). The bundle ships the stdlib-only
# server files and runs them with the user's python3.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
STAGE="$(mktemp -d)"

cp "$HERE/manifest.json" "$HERE/icon.png" "$STAGE"/
# memento_auth is imported by mcp_server on every memory call (the team gate is
# a no-op when MEMENTO_AUTH is unset), so it must ship even in the solo bundle.
# memento_memory_pg is loaded lazily only for a Postgres DSN; bundled for parity.
cp "$ROOT"/mcp_server.py "$ROOT"/harvest_devin.py \
   "$ROOT"/judge.py "$ROOT"/memento_memory.py \
   "$ROOT"/memento_memory_pg.py "$ROOT"/memento_auth.py "$STAGE"/

npx -y @anthropic-ai/mcpb@latest validate "$STAGE/manifest.json"
mkdir -p "$ROOT/dist"
npx -y @anthropic-ai/mcpb@latest pack "$STAGE" "$ROOT/dist/devin-memento.mcpb"

rm -rf "$STAGE"
echo "→ $ROOT/dist/devin-memento.mcpb"
