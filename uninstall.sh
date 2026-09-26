#!/usr/bin/env bash
set -euo pipefail

# hobson uninstaller
#
#   ./uninstall.sh          Remove hooks, config and state; ask about caches,
#                           venvs, the log, the API key file and the checkout
#   ./uninstall.sh --yes    Remove all of it, no prompts
#
# Other entries in settings.json are never touched, and settings.json is
# backed up before it is changed. A checkout is deleted only when it is the
# one the remote installer manages (~/.local/share/hobson, or HOBSON_DIR),
# never a clone you made yourself.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_DIR="$HOME/.claude"
CLI_SYMLINK="$HOME/.local/bin/hobson"
MANAGED_DIR="${HOBSON_DIR:-$HOME/.local/share/hobson}"
# A checkout `claudio update` kept updating in place still lives at the old path.
[[ -z "${HOBSON_DIR:-}" && "$SCRIPT_DIR" == "$HOME/.local/share/claudio" ]] && MANAGED_DIR="$SCRIPT_DIR"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "\033[0;34m[info]\033[0m $1"; }
ok()    { echo -e "${GREEN}[ok]${NC} $1"; }
warn()  { echo -e "${YELLOW}[warn]${NC} $1"; }

ASSUME_YES=false
case "${1:-}" in
    -y|--yes) ASSUME_YES=true ;;
    "")       ;;
    *)        echo "Usage: $0 [--yes]"; exit 1 ;;
esac

# ask "Question" -- --yes answers yes; with no terminal to ask, the answer is
# no, so an unattended run removes only what the default run removes.
ask() {
    local answer
    [[ "$ASSUME_YES" == true ]] && return 0
    [[ -t 0 ]] || return 1
    read -rp "$1 (y/N): " answer
    [[ "$answer" == "y" || "$answer" == "Y" ]]
}

PYTHON=python3
command -v python3 &>/dev/null || PYTHON=/usr/bin/python3

echo ""
echo "Hobson uninstaller"
echo "=================="
echo ""

# ── Stop daemons and detached nudges ─────────────────────────────────

for spec in "kokoro:19849" "pocket-tts:19850"; do
    name="${spec%%:*}" port="${spec##*:}"
    pid_file="$CLAUDE_DIR/$name-daemon.pid"
    [[ -f "$pid_file" ]] || continue
    pid=$(cat "$pid_file" 2>/dev/null || true)
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        info "Stopping $name daemon (pid $pid)..."
        curl -s -X POST "http://127.0.0.1:$port/shutdown" >/dev/null 2>&1 || true
        sleep 0.5
        kill -0 "$pid" 2>/dev/null && kill "$pid" 2>/dev/null || true
        ok "$name daemon stopped"
    fi
    rm -f "$pid_file"
done

# A nudge or watchdog outlives the hook that started it; without this, one
# already counting down would still speak after hobson is gone.
if pkill -f "$SCRIPT_DIR/scripts/nudge.py" 2>/dev/null; then
    ok "Stopped pending nudges"
fi

# ── Remove hooks from settings.json ──────────────────────────────────

info "Removing hooks from settings.json..."
"$PYTHON" "$SCRIPT_DIR/scripts/settings-merge.py" --remove
ok "Hooks removed"

# ── Remove config and state ──────────────────────────────────────────

shopt -s nullglob
state=(
    "$CLAUDE_DIR/hobson.json"
    "$CLAUDE_DIR/hobson.muted"
    "$CLAUDE_DIR/hobson.lock"
    "$CLAUDE_DIR/hobson-commentary.lock"
    "$CLAUDE_DIR"/hobson-nudge-*.lock
    "$CLAUDE_DIR"/hobson-watchdog-*.lock
    "$CLAUDE_DIR"/hobson-activity-*
    "$CLAUDE_DIR"/hobson-alive-*
    "$CLAUDE_DIR/kokoro-daemon.log"
    "$CLAUDE_DIR/pocket-tts-daemon.log"
    "$CLAUDE_DIR/kokoro-playback.wav"
    "$CLAUDE_DIR/pocket-tts-playback.wav"
    # Names from before 0.3.0, when Hobson was claudio
    "$CLAUDE_DIR/claudio.json"
    "$CLAUDE_DIR/claudio.muted"
    "$CLAUDE_DIR/claudio.lock"
    "$CLAUDE_DIR/claudio-commentary.lock"
    "$CLAUDE_DIR"/claudio-nudge-*.lock
    "$CLAUDE_DIR"/claudio-watchdog-*.lock
    "$CLAUDE_DIR"/claudio-activity-*
    "$CLAUDE_DIR"/claudio-alive-*
    # Legacy names from claude-bark installs
    "$CLAUDE_DIR/claude-bark.json"
    "$CLAUDE_DIR/voice-bark.muted"
    "$CLAUDE_DIR/voice-bark.log"
    "$CLAUDE_DIR/voice-bark.lock"
    "$CLAUDE_DIR/voice-bark-commentary.lock"
)
removed=0
for f in "${state[@]}"; do
    if [[ -f "$f" ]]; then
        rm -f "$f"
        removed=$((removed + 1))
    fi
done
for dir in "$CLAUDE_DIR/hobson-sessions" "$CLAUDE_DIR/claudio-sessions"; do
    if [[ -d "$dir" ]]; then
        rm -rf "$dir"
        removed=$((removed + 1))
    fi
done
ok "Removed config and state ($removed items)"

# ── Optional: caches, venvs, log, key ────────────────────────────────

caches=("$CLAUDE_DIR"/voice-cache-*/)
if [[ ${#caches[@]} -gt 0 ]]; then
    for dir in "${caches[@]}"; do
        count=$(find "$dir" -type f | wc -l | tr -d ' ')
        size=$(du -sh "$dir" 2>/dev/null | cut -f1)
        echo "  $(basename "$dir")/  [$count files, $size]"
    done
    if ask "Remove voice caches?"; then
        rm -rf "${caches[@]}"
        ok "Voice caches removed"
    fi
fi

if [[ -d "$SCRIPT_DIR/venvs" && "$SCRIPT_DIR" != "$MANAGED_DIR" ]]; then
    size=$(du -sh "$SCRIPT_DIR/venvs" 2>/dev/null | cut -f1)
    if ask "Remove Python venvs in $SCRIPT_DIR/venvs ($size)?"; then
        rm -rf "$SCRIPT_DIR/venvs"
        ok "Venvs removed"
    fi
fi

if [[ -f "$CLAUDE_DIR/hobson.log" ]] && ask "Remove the log ($CLAUDE_DIR/hobson.log)?"; then
    rm -f "$CLAUDE_DIR/hobson.log"
    ok "Log removed"
fi

if [[ -f "$CLAUDE_DIR/hobson.env" ]] && ask "Remove $CLAUDE_DIR/hobson.env (your OpenRouter key for Jev)?"; then
    rm -f "$CLAUDE_DIR/hobson.env"
    ok "Key file removed"
fi

# ── CLI symlinks ─────────────────────────────────────────────────────

for link in "$CLI_SYMLINK" "$HOME/.local/bin/claudio" "$HOME/.local/bin/claude-bark"; do
    # Only links into a checkout of ours: another tool ships a `claudio` too.
    if [[ -L "$link" && -f "$(dirname "$(readlink "$link")")/scripts/settings-merge.py" ]]; then
        rm -f "$link"
        ok "Removed $link"
    fi
done

# ── The checkout itself ──────────────────────────────────────────────

echo ""
if [[ "$SCRIPT_DIR" == "$MANAGED_DIR" ]]; then
    if ask "Delete the Hobson checkout at $SCRIPT_DIR (including venvs and models)?"; then
        # Last step: this script is being read from inside that directory.
        rm -rf "$SCRIPT_DIR"
        ok "Removed $SCRIPT_DIR"
    else
        echo "  Left $SCRIPT_DIR in place. Delete it any time with: rm -rf $SCRIPT_DIR"
    fi
else
    echo "  The checkout at $SCRIPT_DIR was left in place (not installer-managed)."
fi

ok "Hobson uninstalled. Running Claude Code sessions keep their hooks until restarted."
echo ""
