#!/usr/bin/env bash
# sandbox.sh — install Hobson, run the setup wizard and uninstall it as a new
# user would, without touching your own install.
#
#   scripts/sandbox.sh new [--warm] [-- ARGS]  a fresh sandbox from this checkout's working tree,
#                                              then the remote installer (the wizard opens);
#                                              ARGS go to the installer (-- --yes: no wizard)
#   scripts/sandbox.sh shell                   a shell as the sandbox user: hobson setup,
#                                              hobson uninstall, hobson status, ...
#   scripts/sandbox.sh run CMD [ARGS]          one command as the sandbox user
#   scripts/sandbox.sh sync                    snapshot the working tree again (then hobson update)
#   scripts/sandbox.sh check                   is your own install as `new` found it?
#   scripts/sandbox.sh destroy                 stop what it started, reset its camera grant, delete it
#
# Everything Hobson keeps is under $HOME (~/.claude, ~/.local/bin,
# ~/.local/share/hobson) or in its checkout, so a scratch HOME and a scratch
# checkout hold all of it. The sandbox runs the real curl-pipe installer
# (install-remote.sh) against a snapshot of the working tree, uncommitted and
# untracked files included, in an empty environment: no CLAUDE_CONFIG_DIR, no
# virtualenv, and none of your ~/.local/bin, where `hobson` is the CLI of the
# checkout your hooks run.
#
# What is not under HOME is shared with the real you. Guarded:
#
#   tccutil  a camera grant belongs to the macOS user, not to a HOME: an
#            uninstall run in a scratch HOME once revoked the real one. The
#            sandbox builds its sensor as local.hobson.presence.sandbox, with
#            its own grant (and LaunchServices never hands its `open` to your
#            running sensor), and a tccutil shim passes a reset of that id only.
#   brew     one install for the whole Mac: a shim passes read-only verbs only.
#
# Shared, and said when `new` runs:
#
#   ports    the TTS daemons listen on fixed ports (19849, 19850). While yours
#            runs, the sandbox's voice comes out of `say`: yours refuses a
#            request without its token.
#   Ollama   one server, one model store: a pull from the wizard lands in
#            yours. A model you already have pulls nothing.
#
# Claude Code is not on the sandbox's PATH, so the installer warns it is
# missing: its login lives in the macOS keychain, per user like a camera
# grant, and a login from the sandbox could replace yours.
#
# Nothing of it outlives `destroy`: as on a new Mac, the wizard installs uv
# into the sandbox, and an engine downloads everything (Pocket TTS about
# 1 GB, deleted with it). --warm borrows your uv and its cache and the
# Hugging Face cache instead: an engine then installs from what you already
# have, but what it did not find stays in those caches (a Pocket TTS install
# left 866 MB there).
#
# It lives in $HOBSON_SANDBOX (default ~/.hobson-sandbox); `new` replaces it.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SB="${HOBSON_SANDBOX:-$HOME/.hobson-sandbox}"
SB_HOME="$SB/home"
CHECKOUT="$SB_HOME/.local/share/hobson"   # where install-remote.sh clones to
MARKER="$SB/.hobson-sandbox"
BRANCH="sandbox-snapshot"
BUNDLE_ID="local.hobson.presence"
SB_BUNDLE_ID="$BUNDLE_ID.sandbox"
LSREGISTER=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister

info() { echo -e "\033[0;34m[sandbox]\033[0m $1"; }
warn() { echo -e "\033[1;33m[sandbox]\033[0m $1"; }
die()  { echo -e "\033[0;31m[sandbox]\033[0m $1" >&2; exit 1; }

usage() {
    sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
}

case "$SB" in
    /*) ;;
    *)  die "HOBSON_SANDBOX must be an absolute path" ;;
esac
[[ "$SB" != "/" && "${SB%/}" != "$HOME" ]] || die "HOBSON_SANDBOX must not be / or your home"
# From the sandbox's own shell, "your install" would be the sandbox's.
[[ "$HOME" != "$SB_HOME" ]] || die "Run this from your own shell, not the sandbox's (exit it first)"

need_sandbox() {
    [[ -f "$MARKER" ]] || die "No sandbox at $SB: scripts/sandbox.sh new"
}

# ── The sandbox user's environment ───────────────────────────────────

# Built from nothing (env -i), so no variable of yours leaks in.
sandbox_env() {
    local path="$SB/bin:$SB_HOME/.local/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    local adb
    adb="$(command -v adb 2>/dev/null || true)"
    if [[ -n "$adb" ]]; then
        path="$path:$(dirname "$adb")"
    fi
    SANDBOX_ENV=(HOME="$SB_HOME" USER="$USER" LOGNAME="${LOGNAME:-$USER}" SHELL=/bin/zsh
                 TERM="${TERM:-xterm-256color}" LANG="${LANG:-en_US.UTF-8}" TMPDIR="$SB/tmp"
                 PATH="$path" HOBSON_SANDBOX="$SB" HOBSON_SANDBOX_BUNDLE_ID="$SB_BUNDLE_ID"
                 # One adb server serves the whole account; with your keys, a
                 # paired phone stays paired whichever HOME started it.
                 ANDROID_USER_HOME="${ANDROID_USER_HOME:-$HOME/.android}")
    if [[ -f "$SB/warm" ]]; then
        SANDBOX_ENV+=(UV_CACHE_DIR="${UV_CACHE_DIR:-$HOME/.cache/uv}"
                      HF_HOME="${HF_HOME:-$HOME/.cache/huggingface}")
    fi
}

in_sandbox() {
    sandbox_env
    (cd "$SB_HOME" && exec env -i "${SANDBOX_ENV[@]}" "$@")
}

write_guards() {
    cat > "$SB/bin/tccutil" <<'EOF'
#!/bin/bash
# Sandbox guard: a permission belongs to the macOS user, not to $HOME. Only a
# reset of the sandbox's own sensor goes through.
if [[ $# -eq 3 && "$1" == reset && "$3" == "$HOBSON_SANDBOX_BUNDLE_ID" ]]; then
    echo "$(date '+%F %T') passed  tccutil $*" >> "$HOBSON_SANDBOX/guard.log"
    exec /usr/bin/tccutil "$@"
fi
echo "$(date '+%F %T') BLOCKED tccutil $*" >> "$HOBSON_SANDBOX/guard.log"
echo "sandbox: blocked 'tccutil $*': it acts on your real account" >&2
exit 1
EOF
    chmod +x "$SB/bin/tccutil"

    # No Homebrew here: no shim either, so the wizard sees none, as it would.
    local brew
    brew="$(command -v brew 2>/dev/null || true)"
    [[ -n "$brew" ]] || return 0
    cat > "$SB/bin/brew" <<EOF
#!/bin/bash
# Sandbox guard: Homebrew is one install for the whole Mac. Only verbs that
# change nothing go through.
case "\${1:-}" in
    --version|--prefix|--cellar|--repository|config|list|ls|info|search|deps|leaves|outdated)
        exec "$brew" "\$@" ;;
esac
echo "\$(date '+%F %T') BLOCKED brew \$*" >> "\$HOBSON_SANDBOX/guard.log"
echo "sandbox: blocked 'brew \$*': Homebrew is shared with your real install" >&2
exit 1
EOF
    chmod +x "$SB/bin/brew"
}

borrow_uv() {
    local tool path
    for tool in uv uvx; do
        path="$(command -v "$tool" 2>/dev/null || true)"
        if [[ -n "$path" ]]; then
            ln -sf "$path" "$SB/bin/$tool"
        fi
    done
}

write_zshrc() {
    cat > "$SB_HOME/.zshrc" <<'EOF'
# The sandbox's shell (scripts/sandbox.sh shell). HOME is the sandbox's.
PROMPT='%F{yellow}(hobson sandbox)%f %~ %# '
EOF
}

# ── The snapshot the installer clones ────────────────────────────────

# $SB/src: the working tree as it is, committed on the snapshot branch. Each
# snapshot is a commit on top of the last, so `hobson update` in the sandbox
# fast-forwards to it.
snapshot() {
    local from="$1" src="$SB/src"
    if [[ ! -d "$src/.git" ]]; then
        snapshot_git clone --quiet --no-checkout "$from" "$src"
        snapshot_git -C "$src" checkout --quiet -B "$BRANCH" "$(git -C "$from" rev-parse HEAD)"
    fi
    find "$src" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
    (cd "$from" && git ls-files -z -co --exclude-standard | while IFS= read -r -d '' f; do
        if [[ -e "$f" || -L "$f" ]]; then printf '%s\0' "$f"; fi
    done | tar --null -T - -cf -) | tar -xpf - -C "$src"

    # The sensor's own identity: its own camera grant, its own name in
    # System Settings, and no handing off to your running sensor.
    { grep -rlIF --exclude-dir=.git "$BUNDLE_ID" "$src" || true; } | while IFS= read -r f; do
        python3 - "$f" "$BUNDLE_ID" "$SB_BUNDLE_ID" <<'PY'
import re, sys
path, old, new = sys.argv[1:]
with open(path, encoding="utf-8") as f:
    text = f.read()
text = re.sub(re.escape(old) + r"(?![\w.])", new, text)
text = text.replace("<string>Hobson Presence</string>", "<string>Hobson Presence (sandbox)</string>")
with open(path, "w", encoding="utf-8") as f:
    f.write(text)
PY
    done
    grep -qF "\"$SB_BUNDLE_ID\"" "$src/scripts/build-presence.sh" \
        || die "Could not give the sandbox's sensor its own bundle id, so nothing was installed"

    snapshot_git -C "$src" add -A
    snapshot_git -C "$src" commit --quiet --allow-empty -m "Sandbox snapshot of $(git -C "$from" rev-parse --short HEAD)"
}

snapshot_git() {
    git -c commit.gpgsign=false -c core.hooksPath=/dev/null \
        -c user.name="Hobson sandbox" -c user.email=sandbox@localhost "$@"
}

# ── Your install, as the sandbox must leave it ───────────────────────

settings_file() { echo "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json"; }

# The checkout your hooks run (the one whose CLI, venvs and sensor a
# misdirected uninstall would take).
hooks_checkout() {
    python3 - "$(settings_file)" <<'PY' 2>/dev/null || true
import json, os, shlex, sys
try:
    with open(sys.argv[1]) as f:
        hooks = json.load(f).get("hooks") or {}
except Exception:
    sys.exit()
for groups in hooks.values():
    for group in groups or []:
        for hook in group.get("hooks") or []:
            try:
                words = shlex.split(hook.get("command") or "")
            except ValueError:
                continue
            for word in words:
                if word.endswith("/scripts/hobson.py"):
                    print(os.path.dirname(os.path.dirname(word)))
                    sys.exit()
PY
}

ollama_models() {
    curl -s --max-time 2 http://localhost:11434/api/tags 2>/dev/null | python3 -c '
import json, sys
try:
    print(" ".join(sorted(m["name"] for m in json.load(sys.stdin).get("models", []))) or "none")
except Exception:
    print("(not running)")' || true
}

# hobson.json key by key: a change then names its key, and the presence
# menu bar's toggles (presence.paused, presence.preview) read as yours. It
# holds no secret (the key is in hobson.env, which is only hashed).
config_lines() {
    python3 - "$HOME/.claude/hobson.json" <<'PY' 2>/dev/null || echo "$HOME/.claude/hobson.json  unreadable"
import json, sys
path = sys.argv[1]
try:
    with open(path) as f:
        config = json.load(f)
except FileNotFoundError:
    print(f"{path}  absent")
    sys.exit()

def walk(prefix, value):
    if isinstance(value, dict) and value:
        for key in sorted(value):
            walk(f"{prefix}.{key}" if prefix else key, value[key])
    else:
        print(f"{path}  {prefix} = {json.dumps(value)}")

walk("", config)
PY
}

# One line per thing of yours the sandbox must not change. State Hobson
# rewrites on its own (the log, sessions, presence) is left out: it moves.
fingerprint() {
    local f real
    config_lines
    for f in "$(settings_file)" "$HOME/.claude/hobson.env" "$HOME/.claude/hobson-setup.json"; do
        if [[ -f "$f" ]]; then
            echo "$f  sha256 $(shasum -a 256 < "$f" | cut -c1-16)"
        else
            echo "$f  absent"
        fi
    done
    for f in "$HOME/.local/bin/hobson" "$HOME/.local/share/hobson"; do
        if [[ -L "$f" ]]; then
            echo "$f  -> $(readlink "$f")"
        elif [[ -e "$f" ]]; then
            echo "$f  present"
        else
            echo "$f  absent"
        fi
    done
    real="$(hooks_checkout)"
    if [[ -n "$real" ]]; then
        echo "hooks run  $real"
        echo "$real/venvs  $(ls "$real/venvs" 2>/dev/null | tr '\n' ' ')"
        echo "$real/build  $(ls "$real/build" 2>/dev/null | tr '\n' ' ')"
    else
        echo "hooks run  (no Hobson hooks in $(settings_file))"
    fi
    echo "ollama models  $(ollama_models)"
}

say_what_is_shared() {
    local spec name port pids pid found
    for spec in "kokoro:19849" "pocket-tts:19850"; do
        name="${spec%%:*}" port="${spec##*:}"
        pids="$(lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
        pid="${pids%%$'\n'*}"
        if [[ -n "$pid" ]]; then
            warn "Your $name daemon (pid $pid) holds port $port, so in the sandbox $name speaks through \`say\`."
            warn "  To hear the real voice there, stop yours first (kill $pid). Then the sandbox's holds the"
            warn "  port instead, and your own sessions speak through \`say\` until it idles out or you destroy it."
        fi
    done
    info "Ollama is shared: a model the wizard pulls lands in yours. You have: $(ollama_models)"
    found="$(cd "$SB/src" && grep -rnIE --exclude-dir=.git --exclude-dir=tests --exclude='*.md' --exclude=sandbox.sh \
        '(^|[^[:alnum:]_/.-])(sudo |/usr/bin/tccutil|defaults write|launchctl (load|bootstrap|bootout|unload))' . || true)"
    if [[ -n "$found" ]]; then
        warn "The snapshot runs commands no sandbox guard covers; they act on your real account:"
        echo "$found" | sed 's/^/    /'
    fi
}

# ── Commands ─────────────────────────────────────────────────────────

cmd_new() {
    local warm=false from="$ROOT" rc=0
    local installer_args=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --warm) warm=true ;;
            --)     shift; installer_args=("$@"); break ;;
            *)      die "Unknown option: $1 (installer options go after --)" ;;
        esac
        shift
    done
    if [[ -e "$SB" ]]; then
        [[ -f "$MARKER" ]] || die "$SB exists and is not a sandbox: set HOBSON_SANDBOX to another path"
        info "Replacing the sandbox at $SB"
        cmd_destroy
    fi

    mkdir -p "$SB_HOME" "$SB/bin" "$SB/tmp"
    touch "$MARKER"
    echo "$from" > "$SB/source"
    if [[ "$warm" == true ]]; then
        touch "$SB/warm"
        borrow_uv
    fi
    write_guards
    write_zshrc
    info "Snapshotting $from (working tree, uncommitted changes included)"
    snapshot "$from"
    fingerprint > "$SB/baseline"
    say_what_is_shared

    info "Installing into $SB_HOME as a new user would (curl … | bash)"
    in_sandbox HOBSON_REPO="$SB/src" HOBSON_REF="$BRANCH" \
        /bin/bash "$SB/src/install-remote.sh" ${installer_args[@]+"${installer_args[@]}"} || rc=$?
    echo ""
    [[ $rc -eq 0 ]] || warn "The installer exited $rc"
    info "Next: scripts/sandbox.sh shell    then hobson setup, hobson status, hobson uninstall, ..."
    info "      scripts/sandbox.sh check    is your own install untouched?"
    info "      scripts/sandbox.sh destroy  when you are done"
    return $rc
}

cmd_shell() {
    need_sandbox
    info "A shell as the sandbox user; exit to leave. Your own ~/.local/bin is not on its PATH."
    in_sandbox /bin/zsh -i
}

cmd_sync() {
    need_sandbox
    snapshot "$(cat "$SB/source")"
    info "Snapshot $(git -C "$SB/src" rev-parse --short HEAD) committed: hobson update in the sandbox moves to it"
}

cmd_check() {
    need_sandbox
    local now changes rc=0 running
    now="$(fingerprint)"
    if [[ "$now" == "$(cat "$SB/baseline")" ]]; then
        info "Your own install is as the sandbox found it:"
        sed 's/^/    /' "$SB/baseline"
    else
        warn "Changed since the sandbox was made (a change you made yourself shows here too):"
        changes="$({ diff "$SB/baseline" <(echo "$now") || true; } | sed -n -e 's/^< /    was  /p' -e 's/^> /    now  /p')"
        echo "$changes"
        if ! grep -qvE 'presence\.(paused|preview) = ' <<<"$changes"; then
            info "Only presence.paused or presence.preview: the presence menu bar's toggles write those."
        fi
        rc=1
    fi
    if [[ -s "$SB/guard.log" ]]; then
        info "What the guards saw:"
        sed 's/^/    /' "$SB/guard.log"
    else
        info "Nothing has called tccutil or brew in the sandbox."
    fi
    running="$(pgrep -fl "$SB/" 2>/dev/null || true)"
    if [[ -n "$running" ]]; then
        info "Still running from the sandbox:"
        echo "$running" | sed 's/^/    /'
    fi
    return $rc
}

cmd_destroy() {
    need_sandbox
    local pids app
    # Daemons, nudges, the sensor, a wizard window: all run from inside $SB.
    pids="$(pgrep -f "$SB/" 2>/dev/null || true)"
    if [[ -n "$pids" ]]; then
        # shellcheck disable=SC2086  # one pid per word
        kill $pids 2>/dev/null || true
        info "Stopped what was still running from the sandbox"
    fi
    /usr/bin/tccutil reset Camera "$SB_BUNDLE_ID" >/dev/null 2>&1 || true
    for app in "$CHECKOUT"/build/*.app; do
        if [[ -d "$app" ]]; then
            "$LSREGISTER" -u "$app" 2>/dev/null || true
        fi
    done
    rm -rf "$SB"
    info "Removed $SB and its camera grant"
}

case "${1:-}" in
    new)     shift; cmd_new "$@" ;;
    shell)   cmd_shell ;;
    run)     shift; need_sandbox; [[ $# -gt 0 ]] || die "Usage: scripts/sandbox.sh run CMD [ARGS]"; in_sandbox "$@" ;;
    sync)    cmd_sync ;;
    check)   cmd_check ;;
    destroy) cmd_destroy ;;
    ""|-h|--help|help) usage ;;
    *)       usage; exit 1 ;;
esac
