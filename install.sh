#!/usr/bin/env bash
set -euo pipefail

# hobson installer
#
# Usage:
#   ./install.sh            Interactive: pick an engine, personality and events
#   ./install.sh --yes      No prompts. A new install gets the defaults (macOS `say`,
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

# The pickers read single keys from stdin. With no terminal there (CI, a
# pipe) the first read hits EOF and `set -e` ends the install before any hook
# is written -- so fall back to the defaults instead.
if [[ "$ASSUME_YES" != true && ! -t 0 ]]; then
    warn "No terminal to answer prompts; installing with defaults (as --yes)."
    ASSUME_YES=true
fi

# confirm "Question" Y|N -- the default is what Enter picks. Unattended runs
# answer no: nothing heavy (Homebrew formulae, 2 GB models) is installed
# without someone having said yes to it.
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

_spinner() {
    local pid=$1 msg="${2:-Working...}"
    local frames=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏')
    local i=0
    printf "  ${DIM}%s %s${NC}" "${frames[0]}" "$msg"
    while kill -0 "$pid" 2>/dev/null; do
        printf "\r  ${DIM}%s %s${NC}" "${frames[$i]}" "$msg"
        i=$(( (i + 1) % ${#frames[@]} ))
        sleep 0.1
    done
    local rc=0
    wait "$pid" 2>/dev/null || rc=$?
    printf "\r\033[K"
    return $rc
}

# ── Arrow-key picker ───────────────────────────────────────────────
# Set _PICK_OPTIONS[@] and _PICK_DESCS[@] before calling.
# After return, PICK_RESULT holds the selected option string.
_pick_menu() {
    local sel="${1:-0}"
    local max_len=0
    for opt in "${_PICK_OPTIONS[@]}"; do
        (( ${#opt} > max_len )) && max_len=${#opt}
    done
    local col=$((max_len + 2))

    tput civis 2>/dev/null || true  # TERM=dumb: tput fails, set -e would end the menu
    trap 'tput cnorm 2>/dev/null || true' EXIT

    _pick_draw() {
        [[ "${1:-}" == "r" ]] && printf '\033[%dA' "${#_PICK_OPTIONS[@]}"
        for i in "${!_PICK_OPTIONS[@]}"; do
            if [[ $i -eq $sel ]]; then
                printf "  ${GREEN}▸ %-${col}s${NC} ${DIM}%s${NC}\n" "${_PICK_OPTIONS[$i]}" "${_PICK_DESCS[$i]:-}"
            else
                printf "    %-${col}s ${DIM}%s${NC}\n" "${_PICK_OPTIONS[$i]}" "${_PICK_DESCS[$i]:-}"
            fi
        done
    }
    _pick_draw
    while true; do
        IFS= read -rsn1 key
        case "$key" in
            $'\x1b')
                read -rsn2 rest
                case "$rest" in
                    '[A') ((sel > 0)) && sel=$((sel - 1)) ;;
                    '[B') ((sel < ${#_PICK_OPTIONS[@]}-1)) && sel=$((sel + 1)) ;;
                esac
                _pick_draw r
                ;;
            '')
                break
                ;;
        esac
    done
    tput cnorm 2>/dev/null || true
    PICK_RESULT="${_PICK_OPTIONS[$sel]}"
}

# ── Multi-select picker ────────────────────────────────────────────
# Set _CHECK_OPTIONS[@], _CHECK_DESCS[@], _CHECK_STATE[@] before calling.
# After return, CHECK_RESULT holds space-separated selected options.
_check_menu() {
    local sel="${1:-0}"
    local max_len=0
    for opt in "${_CHECK_OPTIONS[@]}"; do
        (( ${#opt} > max_len )) && max_len=${#opt}
    done
    local col=$((max_len + 2))

    tput civis 2>/dev/null || true  # TERM=dumb: tput fails, set -e would end the menu
    trap 'tput cnorm 2>/dev/null || true' EXIT

    _check_draw() {
        [[ "${1:-}" == "r" ]] && printf '\033[%dA' "${#_CHECK_OPTIONS[@]}"
        for i in "${!_CHECK_OPTIONS[@]}"; do
            local box="[ ]"
            [[ ${_CHECK_STATE[$i]} -eq 1 ]] && box="${GREEN}[x]${NC}"
            if [[ $i -eq $sel ]]; then
                printf "  ${GREEN}▸${NC} %b %-${col}s ${DIM}%s${NC}\n" "$box" "${_CHECK_OPTIONS[$i]}" "${_CHECK_DESCS[$i]:-}"
            else
                printf "    %b %-${col}s ${DIM}%s${NC}\n" "$box" "${_CHECK_OPTIONS[$i]}" "${_CHECK_DESCS[$i]:-}"
            fi
        done
    }
    _check_draw
    while true; do
        IFS= read -rsn1 key
        case "$key" in
            $'\x1b')
                read -rsn2 rest
                case "$rest" in
                    '[A') ((sel > 0)) && sel=$((sel - 1)) ;;
                    '[B') ((sel < ${#_CHECK_OPTIONS[@]}-1)) && sel=$((sel + 1)) ;;
                esac
                _check_draw r
                ;;
            ' ')
                if [[ ${_CHECK_STATE[$sel]} -eq 1 ]]; then
                    _CHECK_STATE[$sel]=0
                else
                    _CHECK_STATE[$sel]=1
                fi
                _check_draw r
                ;;
            '')
                break
                ;;
        esac
    done
    tput cnorm 2>/dev/null || true
    CHECK_RESULT=""
    for i in "${!_CHECK_OPTIONS[@]}"; do
        [[ ${_CHECK_STATE[$i]} -eq 1 ]] && CHECK_RESULT="${CHECK_RESULT} ${_CHECK_OPTIONS[$i]}"
    done
    CHECK_RESULT="${CHECK_RESULT# }"
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

_ensure_uv() {
    command -v uv &>/dev/null && return 0
    [[ -x "$HOME/.local/bin/uv" ]] && export PATH="$HOME/.local/bin:$PATH" && return 0
    warn "uv not found. It's needed to set up the Python venv for this engine."
    if confirm "Install uv now (https://astral.sh/uv)?" Y; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
        export PATH="$HOME/.local/bin:$PATH"
        ok "uv installed"
        return 0
    fi
    return 1
}

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
echo -e "  ${DIM}A well-mannered butler for Claude Code${NC}"
echo ""

# ── Existing installation ────────────────────────────────────────────
#
# Re-running the installer is how hobson updates, so an existing config is
# kept by default and only the hooks are refreshed.

# Hobson was claudio until 0.3.0: take over its config, API key, log and
# session history before looking for an existing install.
"$PYTHON" - "$SCRIPT_DIR/scripts" <<'PY' || warn "Could not move claudio's settings over; see ~/.claude"
import sys
sys.path.insert(0, sys.argv[1])
from home import migrate_legacy_state
for moved in migrate_legacy_state():
    print(f"  Moved ~/.claude/{moved} (claudio is now Hobson)")
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

if [[ "$KEEP_CONFIG" == true ]]; then
    ENGINE=$(_config_value engine say)
    PERSONALITY=$(_config_value personality hobson)
    EVENTS=$(_config_value events "stop permission notification")
elif [[ "$ASSUME_YES" == true ]]; then
    ENGINE="say"
    PERSONALITY="hobson"
    EVENTS="stop permission notification"
else
    # ── Engine selection ─────────────────────────────────────────────

    info "Choose a TTS engine:"
    echo ""

    _PICK_OPTIONS=("say" "kokoro-realtime" "chatterbox")
    _PICK_DESCS=(
        "instant & zero deps — robotic macOS voice, good enough to start"
        "natural neural voice + AI phrases — needs Ollama, ~120MB models"
        "clones any voice from a sample — cached templates only, HEAVY, GPU recommended (~2GB)"
    )
    _pick_menu 0
    ENGINE="$PICK_RESULT"

    # ── Personality selection ────────────────────────────────────────

    echo ""
    info "Choose a voice personality:"
    echo ""

    _PICK_OPTIONS=()
    _PICK_DESCS=()
    for pdir in "$SCRIPT_DIR/scripts/personalities"/*/; do
        pjson="$pdir/personality.json"
        [[ -f "$pjson" ]] || continue
        pname=$("$PYTHON" -c "import json,sys; print(json.load(open(sys.argv[1]))['name'])" "$pjson" 2>/dev/null) || continue
        pdesc=$("$PYTHON" -c "import json,sys; d=json.load(open(sys.argv[1])); print(d.get('display_name','') + ' — ' + d.get('description',''))" "$pjson" 2>/dev/null) || continue
        _PICK_OPTIONS+=("$pname")
        _PICK_DESCS+=("$pdesc")
    done

    # Default to hobson
    sel=0
    for i in "${!_PICK_OPTIONS[@]}"; do
        [[ "${_PICK_OPTIONS[$i]}" == "hobson" ]] && sel=$i
    done
    _pick_menu "$sel"
    PERSONALITY="$PICK_RESULT"

    # ── Event selection ──────────────────────────────────────────────

    echo ""
    info "Choose which events trigger voice (space to toggle, enter to confirm):"
    echo ""

    _CHECK_OPTIONS=("stop" "permission" "notification" "commentary")
    _CHECK_DESCS=(
        "Speak when Claude finishes a task"
        "Speak when Claude requests tool permission"
        "Speak when Claude sends a notification"
        "Running commentary on tool use (needs Ollama)"
    )
    _CHECK_STATE=(1 1 1 0)  # defaults: stop, permission, notification on
    _check_menu 0
    EVENTS="$CHECK_RESULT"
    [[ -z "$EVENTS" ]] && EVENTS="stop"  # fallback
fi

# ── Engine-specific setup ────────────────────────────────────────────

setup_kokoro() {
    echo ""
    info "Setting up Kokoro..."

    if ! _ensure_uv; then
        warn "Skipping Kokoro setup; run 'hobson setup kokoro' once uv is installed."
        return 0
    fi

    VENV_DIR="$SCRIPT_DIR/venvs/kokoro"
    if [[ ! -d "$VENV_DIR" ]]; then
        uv venv --python 3.13 "$VENV_DIR" >/dev/null 2>&1
    fi

    uv pip install --python "$VENV_DIR/bin/python3" kokoro-onnx soundfile huggingface_hub >/dev/null 2>&1 &
    if _spinner $! "Installing dependencies..."; then
        ok "Dependencies installed"
    else
        warn "Failed to install Kokoro dependencies. Check your Python/uv setup."
    fi

    # Download models if needed — check both locations
    MODEL_DIR="$SCRIPT_DIR/models"
    mkdir -p "$MODEL_DIR"

    _has_kokoro_model=false
    for _d in "$MODEL_DIR" "$HOME/.claude/models"; do
        [[ -f "$_d/kokoro-v1.0.int8.onnx" ]] && _has_kokoro_model=true && break
    done
    if [[ "$_has_kokoro_model" != "true" ]]; then
        HF_BASE="https://huggingface.co/hexgrad/Kokoro-82M-v1.0-ONNX/resolve/main"
        (
            "$VENV_DIR/bin/python3" -c "
from huggingface_hub import hf_hub_download
hf_hub_download('hexgrad/Kokoro-82M-v1.0-ONNX', 'kokoro-v1.0.int8.onnx', local_dir='$MODEL_DIR')
hf_hub_download('hexgrad/Kokoro-82M-v1.0-ONNX', 'voices-v1.0.bin', local_dir='$MODEL_DIR')
" 2>/dev/null || {
                curl -fsSL -o "$MODEL_DIR/kokoro-v1.0.int8.onnx" "$HF_BASE/kokoro-v1.0.int8.onnx" && \
                curl -fsSL -o "$MODEL_DIR/voices-v1.0.bin" "$HF_BASE/voices-v1.0.bin"
            }
        ) &
        _spinner $! "Downloading models (~92MB)..." || true
        if [[ -f "$MODEL_DIR/kokoro-v1.0.int8.onnx" ]]; then
            ok "Models downloaded"
        else
            warn "Auto-download failed. Download manually from:"
            echo "  https://huggingface.co/hexgrad/Kokoro-82M-v1.0-ONNX"
            echo "  Place kokoro-v1.0.int8.onnx and voices-v1.0.bin in $MODEL_DIR/"
        fi
    fi

    ok "Kokoro ready"
}

setup_chatterbox() {
    echo ""
    info "Setting up Chatterbox..."

    if ! _ensure_uv; then
        warn "Skipping Chatterbox setup; run 'hobson setup chatterbox' once uv is installed."
        return 0
    fi

    VENV_DIR="$SCRIPT_DIR/venvs/chatterbox"
    if [[ ! -d "$VENV_DIR" ]]; then
        uv venv --python 3.13 "$VENV_DIR" >/dev/null 2>&1
    fi

    uv pip install --python "$VENV_DIR/bin/python3" "numpy>=2.0" chatterbox-tts torch torchaudio >/dev/null 2>&1 &
    if _spinner $! "Installing dependencies (this may take a few minutes)..."; then
        ok "Dependencies installed"
    else
        warn "Failed to install Chatterbox dependencies. Check your Python/uv setup."
    fi

    # Reference audio
    MODEL_DIR="$SCRIPT_DIR/models"
    mkdir -p "$MODEL_DIR"

    has_ref=false
    for ext in wav mp3 flac ogg m4a; do
        if [[ -f "$MODEL_DIR/hobson-reference.$ext" || -f "$MODEL_DIR/alfred-reference.$ext" ]]; then
            has_ref=true
            break
        fi
    done

    if [[ "$has_ref" != "true" ]]; then
        echo ""
        warn "Chatterbox needs a reference audio file for voice cloning."
        echo "  Place a 5-10 second audio clip at:"
        echo "  $MODEL_DIR/hobson-reference.wav"
        echo ""
        read -rp "Path to your reference audio file (or press Enter to skip): " ref_path
        if [[ -n "$ref_path" && -f "$ref_path" ]]; then
            ext="${ref_path##*.}"
            cp "$ref_path" "$MODEL_DIR/hobson-reference.$ext"
            ok "Reference audio copied"
        else
            warn "No reference audio provided. You'll need to add one before generating cache."
        fi
    fi

    ok "Chatterbox ready"
}

if [[ "$KEEP_CONFIG" != true ]]; then
    case "$ENGINE" in
        kokoro-realtime) setup_kokoro ;;
        chatterbox)      setup_chatterbox ;;
        say)             ;;
    esac
fi

# ── Ollama (optional) ────────────────────────────────────────────────
#
# Without it hobson still speaks, from templates. With it, Stops are
# classified (done / broken / waiting on you) and phrases fit the moment.
# An Ollama the user already runs is never upgraded or restarted unasked, and
# an update (existing settings kept) only reports -- it does not re-ask what
# was already declined once.

OLLAMA_MODEL="llama3.2:3b"
OLLAMA_STATUS=""
_offer() { [[ "$KEEP_CONFIG" != true ]] && confirm "$@"; }
echo ""

_ollama_up() { curl -s --max-time 2 http://localhost:11434/api/tags >/dev/null 2>&1; }

_ollama_brew_install() {
    if ! command -v brew &>/dev/null; then
        warn "Homebrew not found. Install Ollama from https://ollama.com/download"
        return 1
    fi
    brew install ollama >/dev/null 2>&1 &
    _spinner $! "Installing Ollama..." || { warn "brew install ollama failed"; return 1; }
    brew services start ollama >/dev/null 2>&1 &
    _spinner $! "Starting the Ollama service..." || true
    for _ in $(seq 1 20); do _ollama_up && break; sleep 0.5; done
    return 0
}

_ollama_pull() {
    ollama pull "$OLLAMA_MODEL" >/dev/null 2>&1 &
    _spinner $! "Pulling $OLLAMA_MODEL (~2 GB)..."
}

if ! command -v ollama &>/dev/null; then
    info "Ollama is optional: it makes Hobson's phrases fit what just happened."
    if _offer "Install Ollama with Homebrew now?" Y; then
        _ollama_brew_install && ok "Ollama installed" || true
    fi
fi

if command -v ollama &>/dev/null; then
    if ! _ollama_up; then
        OLLAMA_STATUS="installed but not running — start the Ollama app, or: ollama serve"
    # Not `ollama list | grep -q`: under pipefail, grep's early exit SIGPIPEs
    # ollama and the check reports the model missing when it is there.
    elif grep -q "llama3\.2.*3b" <<<"$(ollama list 2>/dev/null || true)"; then
        OLLAMA_STATUS="ready ($OLLAMA_MODEL)"
    elif _offer "Ollama model $OLLAMA_MODEL not found. Pull it now? (~2 GB)" Y; then
        if _ollama_pull; then
            ok "Model ready"
            OLLAMA_STATUS="ready ($OLLAMA_MODEL)"
        elif brew list ollama &>/dev/null \
                && _offer "Pull failed; an older Ollama is the usual cause. Upgrade it with Homebrew and retry?" N; then
            brew upgrade ollama >/dev/null 2>&1 &
            _spinner $! "Upgrading Ollama..." || true
            brew services restart ollama >/dev/null 2>&1 || true
            sleep 2
            if _ollama_pull; then
                ok "Model ready"
                OLLAMA_STATUS="ready ($OLLAMA_MODEL)"
            fi
        fi
        [[ -z "$OLLAMA_STATUS" ]] && OLLAMA_STATUS="model missing — try: ollama pull $OLLAMA_MODEL"
    else
        OLLAMA_STATUS="model missing — for smarter phrases: ollama pull $OLLAMA_MODEL"
    fi
else
    OLLAMA_STATUS="not installed — optional; see https://ollama.com (then: ollama pull $OLLAMA_MODEL)"
fi

# ── Write config ─────────────────────────────────────────────────────
#
# Only the choices made here are written. Everything else comes from
# DEFAULT_CONFIG in home.py at load time, so an update that improves
# a default reaches every install instead of being frozen by the installer.

if [[ "$KEEP_CONFIG" != true ]]; then
    # shellcheck disable=SC2086  # EVENTS is a space-separated list on purpose
    "$PYTHON" - "$CONFIG_FILE" "$ENGINE" "$PERSONALITY" $EVENTS <<'PY'
import json, shutil, sys
path, engine, personality, *events = sys.argv[1:]
try:
    with open(path) as f:
        config = json.load(f)
except FileNotFoundError:
    config = {}
except json.JSONDecodeError:
    shutil.copy2(path, path + ".invalid")
    print(f"  Previous config was not valid JSON; kept a copy at {path}.invalid")
    config = {}
config.update(engine=engine, personality=personality, events=events or ["stop"])
# Deprecated kokoro.realtime_events: superseded by the top-level events just set.
if isinstance(config.get("kokoro"), dict):
    config["kokoro"].pop("realtime_events", None)
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
# The command was `claudio` until 0.3.0. Remove that link only when it points
# into a checkout of ours: another tool also ships a `claudio` command.
if [[ -L "$BIN_DIR/claudio" && -f "$(dirname "$(readlink "$BIN_DIR/claudio")")/scripts/settings-merge.py" ]]; then
    rm -f "$BIN_DIR/claudio"
    ok "The command is now 'hobson' (removed the old 'claudio' link)"
fi

# ── Presence sensor ──────────────────────────────────────────────────
#
# Knows whether anyone is listening (scripts/presence.py), so what Hobson
# would say to an empty room or over a call is held until you are back.
# A one-file Swift helper, built with the Command Line Tools already on any
# Mac that has git. The camera is asked for only here, never mid-session.

if command -v swiftc >/dev/null 2>&1; then
    build_out=$("$SCRIPT_DIR/scripts/build-presence.sh" 2>&1) && build_rc=0 || build_rc=$?
    if [[ $build_rc -eq 0 ]]; then
        ok "Presence sensor ready"
        # A sensor still running the old build keeps it until it exits.
        if [[ "$build_out" == *Built* ]]; then
            pkill -f "$SCRIPT_DIR/build/HobsonPresence.app/Contents/MacOS" 2>/dev/null || true
        fi
        camera=$("$PYTHON" "$SCRIPT_DIR/scripts/presence.py" --camera-status 2>/dev/null || true)
        if [[ "$ASSUME_YES" != true && "$camera" == "not-determined" ]]; then
            echo ""
            echo "Hobson can glance through the camera, only when he is about to speak"
            echo "and you have been idle, to hold his news until you are back."
            echo "Frames stay in memory on this Mac; nothing is saved or sent."
            if confirm "Allow the camera? (macOS will ask)" Y; then
                "$PYTHON" "$SCRIPT_DIR/scripts/presence.py" --look --request-permission >/dev/null 2>&1 || true
            else
                "$PYTHON" "$SCRIPT_DIR/scripts/presence.py" --set mode signals
                info "Presence will use the keyboard, screen lock and calls only (hobson presence mode)"
            fi
        fi
    else
        warn "Could not build the presence sensor; 'hobson presence setup' shows why"
    fi
else
    info "Presence sensor skipped: no swiftc (xcode-select --install, then hobson presence setup)"
fi

# ── Optional: generate voice cache ───────────────────────────────────

if [[ "$ENGINE" == "chatterbox" && "$ASSUME_YES" != true ]]; then
    CACHE_DIR="$CLAUDE_DIR/voice-cache-chatterbox"
    if [[ -d "$CACHE_DIR" ]] && [[ -n "$(ls -A "$CACHE_DIR" 2>/dev/null)" ]]; then
        cache_files=$(find "$CACHE_DIR" -type f -name '*.wav' | wc -l | tr -d ' ')
        cache_size=$(du -sh "$CACHE_DIR" 2>/dev/null | cut -f1 | tr -d ' ')
        ok "Existing voice cache found at $CACHE_DIR ($cache_files files, $cache_size)"
        prompt="Regenerate cache?"
    else
        echo ""
        echo "Generate voice cache now? This pre-generates ~507 phrases."
        echo "  Estimated time: ~2 hours on MPS, ~45 min on CUDA"
        echo ""
        prompt="Generate now?"
    fi
    if confirm "$prompt" N; then
        "$SCRIPT_DIR/venvs/chatterbox/bin/python3" "$SCRIPT_DIR/scripts/cache-gen/chatterbox_gen.py"
    fi
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

if [[ "$KEEP_CONFIG" != true && "$INSTALL_OK" == true ]]; then
    SAY_VOICE=$("$PYTHON" -c "import json,sys; print(json.load(open(sys.argv[1])).get('say_voice', 'Daniel'))" \
        "$SCRIPT_DIR/scripts/personalities/$PERSONALITY/personality.json" 2>/dev/null || echo "Daniel")
    HELLO_PHRASE="Good day. Hobson, at your service."
    _hello_spoken=false

    # For kokoro-realtime, start daemon and use it for the hello bark
    if [[ "$ENGINE" == "kokoro-realtime" ]]; then
        DAEMON_VENV="$SCRIPT_DIR/venvs/kokoro/bin/python3"
        DAEMON_SCRIPT="$SCRIPT_DIR/scripts/kokoro-daemon.py"
        DAEMON_PORT=19849
        DAEMON_PID_FILE="$HOME/.claude/kokoro-daemon.pid"

        # Start daemon if not already running
        _daemon_running=false
        if [[ -f "$DAEMON_PID_FILE" ]] && kill -0 "$(cat "$DAEMON_PID_FILE" 2>/dev/null)" 2>/dev/null; then
            _daemon_running=true
        elif [[ -f "$DAEMON_VENV" ]]; then
            "$DAEMON_VENV" "$DAEMON_SCRIPT" &
            disown
        fi

        # Wait for daemon to be healthy (up to 5s)
        for _ in $(seq 1 20); do
            if curl -s --max-time 1 "http://127.0.0.1:$DAEMON_PORT/health" >/dev/null 2>&1; then
                _daemon_running=true
                break
            fi
            sleep 0.25
        done

        if [[ "$_daemon_running" == true ]]; then
            KOKORO_VOICE=$("$PYTHON" -c "import json,sys; print(json.load(open(sys.argv[1])).get('kokoro', {}).get('voice', 'am_puck'))" \
                "$CONFIG_FILE" 2>/dev/null || echo "am_puck")
            # A directory, not mktemp …XXXXXX.wav: BSD mktemp does not fill in
            # the X's when a suffix follows, so that name was a fixed literal.
            HELLO_DIR=$(mktemp -d "${TMPDIR:-/tmp}/hobson-hello.XXXXXX")
            HELLO_WAV="$HELLO_DIR/hello.wav"
            if curl -s --max-time 10 -X POST "http://127.0.0.1:$DAEMON_PORT/generate" \
                -H "Content-Type: application/json" \
                -d "{\"text\": \"$HELLO_PHRASE\", \"voice\": \"$KOKORO_VOICE\", \"speed\": 0.8}" \
                -o "$HELLO_WAV" 2>/dev/null && [[ -s "$HELLO_WAV" ]]; then
                afplay "$HELLO_WAV" 2>/dev/null && _hello_spoken=true
            fi
            rm -rf "$HELLO_DIR"
        fi
    fi

    # Fallback to macOS say; a voice this Mac lacks must not fail the install.
    if [[ "$_hello_spoken" == false ]]; then
        say -v "$SAY_VOICE" "$HELLO_PHRASE" 2>/dev/null || say "$HELLO_PHRASE" 2>/dev/null || true
    fi
fi

# ── Done ─────────────────────────────────────────────────────────────

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
echo -e "  ${DIM}hobson status · hobson doctor · hobson update · hobson uninstall${NC}"
echo ""
[[ "$INSTALL_OK" == true ]]
