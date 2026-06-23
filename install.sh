#!/usr/bin/env bash
# install.sh — set up SkillOpt-Sleep for Devin
#
# What this does:
#   1. Clones microsoft/SkillOpt (provides the sleep engine, ~20 MB)
#   2. Installs it (editable) into the current Python environment
#   3. Creates the runtime data dir (~/.skillopt-sleep-devin)
#   4. Copies the seed SKILL.md into every detected Devin workspace
#   5. Registers the MCP server with Devin CLI (devin mcp add)
#
# Usage:
#   bash install.sh [--skillopt-dir PATH] [--data-dir PATH] [--dry-run]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR" && pwd)"

# ── defaults ──────────────────────────────────────────────────────────────────
SKILLOPT_DIR="${SKILLOPT_DIR:-$PROJECT_DIR/../SkillOpt}"
DATA_DIR="${SKILLOPT_DEVIN_CLAUDE_HOME:-$HOME/.skillopt-sleep-devin}"
DRY_RUN=0

# ── argument parsing ──────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skillopt-dir) SKILLOPT_DIR="$2"; shift 2 ;;
    --data-dir)     DATA_DIR="$2"; shift 2 ;;
    --dry-run)      DRY_RUN=1; shift ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

log() { echo "[install] $*"; }
run() {
  if [[ $DRY_RUN -eq 1 ]]; then echo "[dry-run] $*"; else "$@"; fi
}

# ── 1. Python check ───────────────────────────────────────────────────────────
PYTHON="${PYTHON:-python3}"
PY_VER=$("$PYTHON" -c "import sys; print(sys.version_info[:2])" 2>/dev/null || echo "(,)")
if [[ "$PY_VER" < "(3, 10)" ]]; then
  echo "Error: Python >= 3.10 required (found $PY_VER). Set PYTHON=python3.10 and retry."
  exit 1
fi
log "Python: $("$PYTHON" --version)"

# ── 2. Clone / update SkillOpt ────────────────────────────────────────────────
if [[ -d "$SKILLOPT_DIR/.git" ]]; then
  log "Updating SkillOpt at $SKILLOPT_DIR"
  run git -C "$SKILLOPT_DIR" pull --ff-only --quiet
else
  log "Cloning SkillOpt → $SKILLOPT_DIR"
  run git clone --depth=1 https://github.com/microsoft/SkillOpt.git "$SKILLOPT_DIR"
fi

# ── 3. Install skillopt_sleep ─────────────────────────────────────────────────
log "Installing skillopt (editable)"
run "$PYTHON" -m pip install --quiet -e "$SKILLOPT_DIR" --break-system-packages 2>/dev/null \
  || run "$PYTHON" -m pip install --quiet -e "$SKILLOPT_DIR"

# ── 4. Create data dir ────────────────────────────────────────────────────────
log "Creating data dir: $DATA_DIR"
run mkdir -p "$DATA_DIR/projects"

# ── 5. Seed skill into Devin workspaces ──────────────────────────────────────
MANAGED_SKILL="${SKILLOPT_MANAGED_SKILL:-skillopt-sleep-learned}"
SEED="$SCRIPT_DIR/seed_skill/SKILL.md"

_seed_skill_in_folder() {
  local folder="$1"
  if [[ -d "$folder/.devin" ]]; then
    local skill_dir="$folder/.devin/skills/$MANAGED_SKILL"
    if [[ ! -f "$skill_dir/SKILL.md" ]]; then
      log "Seeding skill → $skill_dir/SKILL.md"
      run mkdir -p "$skill_dir"
      run cp "$SEED" "$skill_dir/SKILL.md"
    else
      log "Skill already present: $skill_dir/SKILL.md (skipped)"
    fi
  fi
}

if [[ -d "$HOME/.config/Devin/User/workspaceStorage" ]]; then
  while IFS= read -r ws_json; do
    folder=$(python3 -c "
import json; d=json.load(open('$ws_json')); f=d.get('folder','')
print(f[7:] if f.startswith('file://') else f)
" 2>/dev/null)
    if [[ -n "$folder" && -d "$folder" ]]; then
      _seed_skill_in_folder "$folder"
    fi
  done < <(find "$HOME/.config/Devin/User/workspaceStorage" -name "workspace.json" 2>/dev/null)
fi

# ── 6. Register with Devin CLI MCP ────────────────────────────────────────────
DEVIN_BIN=""
for candidate in \
  "$HOME/.local/share/devin/cli/$(ls "$HOME/.local/share/devin/cli/_versions/" 2>/dev/null | sort -V | tail -1)/bin/devin" \
  "$HOME/.local/bin/devin" \
  "$(command -v devin 2>/dev/null)"; do
  if [[ -x "$candidate" ]]; then
    DEVIN_BIN="$candidate"
    break
  fi
done

if [[ -n "$DEVIN_BIN" ]]; then
  log "Registering with Devin CLI MCP: $DEVIN_BIN"
  if [[ $DRY_RUN -eq 0 ]]; then
    # Remove stale entry silently, then re-add
    "$DEVIN_BIN" mcp remove skillopt-sleep 2>/dev/null || true
    "$DEVIN_BIN" mcp add skillopt-sleep \
      --env "SKILLOPT_SLEEP_REPO=$SKILLOPT_DIR" \
      --env "SKILLOPT_DEVIN_CLAUDE_HOME=$DATA_DIR" \
      -- python3 "$SCRIPT_DIR/mcp_server.py"
    log "Devin MCP registered: skillopt-sleep"
  else
    echo "[dry-run] Would run: devin mcp add skillopt-sleep -- python3 $SCRIPT_DIR/mcp_server.py"
  fi
else
  log "Devin CLI not found — skipping Devin MCP registration"
  log "(Install Devin CLI, then run: devin mcp add skillopt-sleep -- python3 $SCRIPT_DIR/mcp_server.py)"
fi

# ── done ──────────────────────────────────────────────────────────────────────
echo ""
echo "✓ Installation complete."
echo ""
echo "  Devin next steps:"
echo "  1. MCP registration was handled automatically (if Devin CLI was found)"
echo "  2. (Optional) copy devin-rules.snippet.md to .devin/rules/skillopt-sleep.md"
echo "  3. Ask Devin: 'run the sleep cycle'"
echo ""
echo "  Default backend is 'mock' (free). For real optimization:"
echo "    ANTHROPIC_API_KEY=... → backend: claude"
echo "    OPENAI_API_KEY=...    → backend: codex"
