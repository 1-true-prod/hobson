#!/usr/bin/env bash
set -euo pipefail

# hobson installer
#
# Usage:
#   ./install.sh            Install, then open the setup wizard (a window) for every choice
#   ./install.sh --yes      No prompts, no window. A new install gets the defaults (macOS `say`,
#                           hobson, stop/permission/notification) and installs nothing
#                           heavy; an existing install keeps its settings
#   ./install.sh --update   Keep the current settings, refresh hooks (used by `hobson update`)
#
# Remote one-liner (clones into ~/.local/share/hobson, then runs this):
#   curl -fsSL https://raw.githubusercontent.com/1-true-prod/hobson/main/install-remote.sh | bash

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_DIR="$HOME/.claude"   # hobson's own state lives here, whatever CLAUDE_CONFIG_DIR says
CONFIG_FILE="$CLAUDE_DIR/hobson.json"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
DIM='\033[2m'
NC='\033[0m' # No Color

info()  { echo -e "${BLUE}[info]${NC} $1"; }
ok()    { echo -e "${GREEN}[ok]${NC} $1"; }
warn()  { echo -e "${YELLOW}[warn]${NC} $1"; }
error() { echo -e "${RED}[error]${NC} $1"; }

usage() {
    sed -n '4,12p' "$0" | sed 's/^# \{0,1\}//'
}

# ── Arguments ────────────────────────────────────────────────────────

ASSUME_YES=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        -y|--yes|--non-interactive) ASSUME_YES=true ;;
        # --update is --yes by another name: with a config present, --yes keeps it.
        --update)                   ASSUME_YES=true ;;
        # Accepted for older install-remote.sh copies; the install dir is
        # always the directory this script lives in.
        --install-dir)              shift ;;
        --install-dir=*)            ;;
        -h|--help)                  usage; exit 0 ;;
        *) error "Unknown option: $1"; usage; exit 1 ;;
    esac
    shift
done
[[ "${HOBSON_YES:-}" == "1" ]] && ASSUME_YES=true

# With no terminal (CI, a pipe) there is nobody to answer: take the defaults.
if [[ "$ASSUME_YES" != true && ! -t 0 ]]; then
    warn "No terminal to answer prompts; installing with defaults (as --yes)."
    ASSUME_YES=true
fi

# confirm "Question" Y|N -- the default is what Enter picks. Unattended runs
# answer no.
confirm() {
    local prompt="$1" default="$2" answer
    if [[ "$ASSUME_YES" == true ]]; then
        return 1
    fi
    if [[ "$default" == "Y" ]]; then
        read -rp "$prompt (Y/n): " answer
        [[ "$answer" != "n" && "$answer" != "N" ]]
    else
        read -rp "$prompt (y/N): " answer
        [[ "$answer" == "y" || "$answer" == "Y" ]]
    fi
}

# A desktop to put the wizard's window on: not over SSH, and a GUI login.
_gui_session() {
    [[ -z "${SSH_CONNECTION:-}" && -z "${SSH_TTY:-}" ]] || return 1
    [[ "$(launchctl managername 2>/dev/null || true)" == "Aqua" ]]
}

# ── Pre-flight checks ────────────────────────────────────────────────

if [[ "$(uname)" != "Darwin" ]]; then
    error "Hobson requires macOS (uses afplay and say)."
    exit 1
fi

# The interpreter the hooks will run, pinned by absolute path (see
# settings-merge.py). A project virtualenv that happens to be active is the
# wrong choice: it disappears with the project.
_python_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; }

PYTHON="${HOBSON_PYTHON:-$(command -v python3 || true)}"
if [[ -n "$PYTHON" && -n "${VIRTUAL_ENV:-}" && "$PYTHON" == "$VIRTUAL_ENV"/* ]]; then
    for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
        if [[ -x "$candidate" ]] && _python_ok "$candidate"; then
            warn "Ignoring the active virtualenv's python; hooks will use $candidate"
            PYTHON="$candidate"
            break
        fi
    done
fi
# A version-manager shim (pyenv, asdf, mise) picks its Python per directory,
# from whatever .python-version the project a hook runs in has -- pinning
# the shim pins nothing. Pin what it resolves to here instead.
if [[ "$PYTHON" == */shims/* ]]; then
    resolved=$("$PYTHON" -c 'import sys; print(sys.executable)' 2>/dev/null || true)
    if [[ -n "$resolved" && -x "$resolved" ]]; then
        PYTHON="$resolved"
    fi
fi
if [[ -z "$PYTHON" ]] || ! "$PYTHON" -c 'pass' 2>/dev/null; then
    error "Python 3 is required and did not run."
    echo "  Install the Xcode Command Line Tools (xcode-select --install),"
    echo "  or Python from python.org or Homebrew (brew install python)."
    exit 1
fi
if ! _python_ok "$PYTHON"; then
    error "Python 3.9+ required (found $("$PYTHON" -V 2>&1) at $PYTHON)."
    exit 1
fi

# Claude Code reads settings.json from $CLAUDE_CONFIG_DIR when set.
SETTINGS_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
if ! command -v claude &>/dev/null && [[ ! -d "$SETTINGS_DIR" ]]; then
    warn "Claude Code not found. Hobson's hooks will take effect once it is installed."
fi
mkdir -p "$CLAUDE_DIR"

echo ""
echo -e "${GREEN}"
cat << 'LOGO'
 _           _
| |__   ___ | |__  ___  ___  _ __
| '_ \ / _ \| '_ \/ __|/ _ \| '_ \
| | | | (_) | |_) \__ \ (_) | | | |
|_| |_|\___/|_.__/|___/\___/|_| |_|
LOGO
echo -e "${NC}"
echo -e "  ${DIM}A synthetic butler for Claude Code${NC}"
echo ""

# ── Existing installation ────────────────────────────────────────────
#
# Re-running the installer is how hobson updates, so an existing config is
# kept by default and only the hooks are refreshed.

# Hobson was claude-bark once: take over its config before looking for an
# existing install.
"$PYTHON" - "$SCRIPT_DIR/scripts" <<'PY' || warn "Could not move the old settings over; see ~/.claude"
import sys
sys.path.insert(0, sys.argv[1])
from home import migrate_legacy_state
for moved in migrate_legacy_state():
    print(f"  Moved ~/.claude/{moved}")
PY

KEEP_CONFIG=false
if [[ -f "$CONFIG_FILE" ]]; then
    if [[ "$ASSUME_YES" == true ]]; then
        KEEP_CONFIG=true
        info "Keeping your settings in $CONFIG_FILE"
    else
        info "Existing settings found at $CONFIG_FILE"
        if confirm "Keep them and just update Hobson?" Y; then
            KEEP_CONFIG=true
        fi
    fi
fi

_config_value() {
    "$PYTHON" - "$CONFIG_FILE" "$1" "$2" <<'PY' 2>/dev/null || echo "$2"
import json, sys
path, key, default = sys.argv[1:]
try:
    with open(path) as f:
        value = json.load(f).get(key, default)
except Exception:
    value = default
print(" ".join(value) if isinstance(value, list) else value)
PY
}

# The setup sections this install has not been shown (setup_wizard.unseen),
# space-separated; empty when there are none.
_unseen_sections() {
    "$PYTHON" - "$SCRIPT_DIR/scripts" <<'PY' 2>/dev/null || true
import os, sys
sys.path.insert(0, sys.argv[1])
import home, setup_wizard
print(" ".join(setup_wizard.unseen(setup_wizard.load_setup_state(), os.path.isfile(home.config_file()))))
PY
}

# ── The setup wizard, or the defaults ────────────────────────────────
#
# Every choice (voice, events, brain, decider, presence, phone) is made in
# the wizard's window, and anything heavy is installed from it, with its size
# shown first. Without a desktop to show it on, or unattended, a new install
# gets the defaults and nothing heavy; `hobson setup` opens the wizard later.

WIZARD=false
UNSEEN=""
if [[ "$KEEP_CONFIG" == true ]]; then
    UNSEEN="$(_unseen_sections)"
fi
if [[ "$ASSUME_YES" != true ]]; then
    if ! _gui_session; then
        info "No desktop here (SSH?): installing with defaults. Run 'hobson setup' at the Mac to choose."
    elif [[ "$KEEP_CONFIG" != true ]]; then
        WIZARD=true
    elif [[ -n "$UNSEEN" ]] && confirm "Setup has new sections for you ($UNSEEN). Open the setup wizard?" Y; then
        WIZARD=true
    fi
fi

if [[ "$KEEP_CONFIG" != true && "$WIZARD" != true ]]; then
    # Only what the installer chooses is written; every other key comes from
    # DEFAULT_CONFIG in home.py at load time, so an improved default still
    # reaches this install.
    "$PYTHON" - "$CONFIG_FILE" <<'PY'
import json, shutil, sys
path = sys.argv[1]
try:
    with open(path) as f:
        config = json.load(f)
except FileNotFoundError:
    config = {}
except json.JSONDecodeError:
    shutil.copy2(path, path + ".invalid")
    print(f"  Previous config was not valid JSON; kept a copy at {path}.invalid")
    config = {}
config.update(engine="say", personality="hobson", events=["stop", "permission", "notification"])
with open(path, "w") as f:
    json.dump(config, f, indent=2)
    f.write("\n")
PY
    ok "Settings saved to $CONFIG_FILE"
fi

# ── Merge settings.json hooks ────────────────────────────────────────

"$PYTHON" "$SCRIPT_DIR/scripts/settings-merge.py" --install-dir "$SCRIPT_DIR" --python "$PYTHON"

# ── Symlink CLI to PATH ────────────────────────────────────────────

CLI_SOURCE="$SCRIPT_DIR/hobson"
BIN_DIR="$HOME/.local/bin"
CLI_TARGET="$BIN_DIR/hobson"
mkdir -p "$BIN_DIR"
if [[ -e "$CLI_TARGET" && ! -L "$CLI_TARGET" ]]; then
    warn "$CLI_TARGET exists and is not a symlink; left it alone. The CLI is at $CLI_SOURCE"
else
    ln -sf "$CLI_SOURCE" "$CLI_TARGET"
fi
# Clean up old symlink from previous installs
[[ -L "$BIN_DIR/claude-bark" ]] && rm -f "$BIN_DIR/claude-bark"

# ── Presence sensor ──────────────────────────────────────────────────
#
# Knows whether anyone is listening (scripts/presence.py), so what Hobson
# would say to an empty room or over a call is held until you are back.
# A one-file Swift helper, built with the Command Line Tools already on any
# Mac that has git. The camera is asked for only in the wizard (or `hobson
# presence setup`), never mid-session.

if command -v swiftc >/dev/null 2>&1; then
    build_out=$("$SCRIPT_DIR/scripts/build-presence.sh" 2>&1) && build_rc=0 || build_rc=$?
    if [[ $build_rc -eq 0 ]]; then
        ok "Presence sensor ready"
        # A sensor still running the old build keeps it until it exits.
        if [[ "$build_out" == *Built* ]]; then
            pkill -f "$SCRIPT_DIR/build/HobsonPresence.app/Contents/MacOS" 2>/dev/null || true
        fi
    else
        warn "Could not build the presence sensor; 'hobson presence setup' shows why"
    fi
else
    info "Presence sensor skipped: no swiftc (xcode-select --install, then hobson presence setup)"
fi

# ── Wizard ───────────────────────────────────────────────────────────

WIZARD_RC=""
if [[ "$WIZARD" == true ]]; then
    echo ""
    info "Opening the setup wizard. Close its window when you're done."
    WIZARD_RC=0
    "$PYTHON" "$SCRIPT_DIR/scripts/setup_wizard.py" --python "$PYTHON" || WIZARD_RC=$?
    case "$WIZARD_RC" in
        0)  ok "Setup applied" ;;
        10) info "The wizard closed before COMMIT: the defaults stand (hobson setup to choose later)" ;;
        3)  ;;  # no desktop after all: it said why
        *)  warn "A setup task failed: 'hobson doctor' shows what's left, 'hobson setup' retries" ;;
    esac
fi

# ── Verification ─────────────────────────────────────────────────────

INSTALL_OK=true
if [[ -f "$CONFIG_FILE" ]] && ! "$PYTHON" -c "import json,sys; json.load(open(sys.argv[1]))" "$CONFIG_FILE" 2>/dev/null; then
    error "Config file $CONFIG_FILE is not valid JSON"
    INSTALL_OK=false
fi
if ! "$PYTHON" "$SCRIPT_DIR/scripts/settings-merge.py" --check >/dev/null 2>&1; then
    error "Hooks not detected in settings.json. Run: hobson doctor"
    INSTALL_OK=false
fi
# A hook that cannot even start fails silently inside Claude Code; catch it here.
if ! echo '{"hook_event_name":"UserPromptSubmit"}' | HOME="$(mktemp -d)" "$PYTHON" "$SCRIPT_DIR/scripts/hobson.py" 2>/dev/null; then
    error "The hook script failed to run with $PYTHON"
    INSTALL_OK=false
fi

# ── Hello bark ───────────────────────────────────────────────────────
#
# The wizard says its own hello, through the voice chosen there; a new
# install without it gets one from macOS `say`.

if [[ "$KEEP_CONFIG" != true && "$WIZARD" != true && "$INSTALL_OK" == true ]]; then
    SAY_VOICE=$("$PYTHON" -c "import json,sys; print(json.load(open(sys.argv[1])).get('say_voice', 'Daniel'))" \
        "$SCRIPT_DIR/scripts/personalities/hobson/personality.json" 2>/dev/null || echo "Daniel")
    # A voice this Mac lacks must not fail the install.
    say -v "$SAY_VOICE" "Good day. Hobson, at your service." 2>/dev/null \
        || say "Good day. Hobson, at your service." 2>/dev/null || true
fi

# ── Ollama (a report) ────────────────────────────────────────────────
#
# Without it hobson still speaks, from templates. The wizard installs it and
# pulls a model when asked; here it is only reported on.

OLLAMA_MODEL="$("$PYTHON" - "$SCRIPT_DIR/scripts" <<'PY' 2>/dev/null || echo llama3.2:3b
import sys
sys.path.insert(0, sys.argv[1])
import home
print(home.load_config()["ollama"]["model"])
PY
)"
OLLAMA_STATUS=""
if command -v ollama &>/dev/null; then
    if ! curl -s --max-time 2 http://localhost:11434/api/tags >/dev/null 2>&1; then
        OLLAMA_STATUS="installed but not running — start the Ollama app, or: ollama serve"
    # Not `ollama list | grep -q`: under pipefail, grep's early exit SIGPIPEs
    # ollama and the check reports the model missing when it is there.
    elif grep -qF "$OLLAMA_MODEL" <<<"$(ollama list 2>/dev/null || true)"; then
        OLLAMA_STATUS="ready ($OLLAMA_MODEL)"
    else
        OLLAMA_STATUS="model missing — hobson setup, or: ollama pull $OLLAMA_MODEL"
    fi
else
    OLLAMA_STATUS="not installed — optional; hobson setup installs it"
fi

# ── Done ─────────────────────────────────────────────────────────────

ENGINE=$(_config_value engine say)
PERSONALITY=$(_config_value personality hobson)
EVENTS=$(_config_value events "stop permission notification")

echo ""
if [[ "$INSTALL_OK" == true ]]; then
    if [[ "$KEEP_CONFIG" == true ]]; then
        echo -e "${GREEN}  Hobson is up to date.${NC}"
    else
        echo -e "${GREEN}  Installation complete!${NC}"
    fi
else
    echo -e "${YELLOW}  Installed with problems — see the errors above, or run: hobson doctor${NC}"
fi
echo ""
echo -e "  Engine:      ${GREEN}$ENGINE${NC}"
echo -e "  Personality: ${GREEN}$PERSONALITY${NC}"
echo -e "  Events:      ${GREEN}$EVENTS${NC}"
[[ -n "$OLLAMA_STATUS" ]] && echo -e "  Ollama:      ${DIM}$OLLAMA_STATUS${NC}"
echo ""
if [[ -n "$UNSEEN" && "$WIZARD" != true ]]; then
    echo -e "  ${YELLOW}New in setup: $UNSEEN.${NC} Run ${GREEN}hobson setup${NC} at the Mac to see them."
    echo ""
fi
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    case "${SHELL:-}" in
        */zsh)  rc="$HOME/.zshrc" ;;
        */bash) rc="$HOME/.bash_profile" ;;
        *)      rc="your shell profile" ;;
    esac
    echo -e "  ${YELLOW}To use the hobson command, add this to $rc:${NC}"
    echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
    echo ""
fi
echo -e "  ${DIM}Start a new Claude Code session to hear it (running sessions keep their old hooks).${NC}"
echo -e "  ${DIM}hobson setup · hobson status · hobson doctor · hobson update · hobson uninstall${NC}"
echo ""
[[ "$INSTALL_OK" == true ]]
