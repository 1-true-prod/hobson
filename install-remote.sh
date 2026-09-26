#!/usr/bin/env bash
# claudio remote installer
#
#   curl -fsSL https://raw.githubusercontent.com/1-true-prod/claudio/main/install-remote.sh | bash
#
# No prompts (defaults: macOS `say` voice, nothing heavy installed):
#   curl -fsSL https://raw.githubusercontent.com/1-true-prod/claudio/main/install-remote.sh | bash -s -- --yes
#
# Re-running it updates an existing install and keeps its settings.
#
# Environment:
#   CLAUDIO_REF   branch or tag to install (default: main)
#   CLAUDIO_DIR   where to keep the checkout (default: ~/.local/share/claudio)
#   CLAUDIO_REPO  git URL to clone (default: the GitHub repo)
#
# Everything is inside main(), called on the last line, so a download cut off
# halfway runs nothing at all.

set -euo pipefail

main() {
    local repo="${CLAUDIO_REPO:-https://github.com/1-true-prod/claudio.git}"
    local ref="${CLAUDIO_REF:-main}"
    local dir="${CLAUDIO_DIR:-$HOME/.local/share/claudio}"

    local red='\033[0;31m' green='\033[0;32m' dim='\033[2m' nc='\033[0m'
    die() { echo -e "${red}Error:${nc} $1" >&2; exit 1; }

    echo ""
    echo -e "  ${green}claudio${nc} installer"
    echo -e "  ${dim}Context-aware voice notifications for Claude Code${nc}"
    echo ""

    [[ "$(uname)" == "Darwin" ]] || die "claudio requires macOS."

    # /usr/bin/git is only a stub until the Command Line Tools are installed.
    git --version &>/dev/null \
        || die "git is required. Install the Xcode Command Line Tools: xcode-select --install"

    # Updating fetches, switches branch and runs whatever install.sh is there,
    # so it must be claudio's checkout and not some other repo CLAUDIO_DIR
    # happens to name.
    if [[ -e "$dir" ]] && ! [[ -d "$dir/.git" && -f "$dir/scripts/settings-merge.py" && -f "$dir/install.sh" ]]; then
        die "$dir exists but is not a claudio checkout. Move it aside, or set CLAUDIO_DIR."
    fi

    if [[ -d "$dir/.git" ]]; then
        echo -e "${dim}Updating $dir to $ref...${nc}"
        # Only tracked files count: venvs/ and models/ are ignored, and survive.
        if [[ -n "$(git -C "$dir" status --porcelain --untracked-files=no)" ]]; then
            die "$dir has local changes. Commit or stash them, then re-run."
        fi
        git -C "$dir" fetch --quiet --tags origin \
            || die "Could not fetch from $(git -C "$dir" remote get-url origin)."
        if git -C "$dir" show-ref --verify --quiet "refs/remotes/origin/$ref"; then
            # checkout -B resets the branch; commits made on it and never
            # pushed would be left for the reflog to remember. Refuse instead.
            if git -C "$dir" show-ref --verify --quiet "refs/heads/$ref" \
                    && [[ "$(git -C "$dir" rev-list --count "origin/$ref..$ref")" != 0 ]]; then
                die "$dir has commits on $ref that are not on origin. Push or move them, then re-run."
            fi
            git -C "$dir" checkout --quiet -B "$ref" "origin/$ref"
        else
            git -C "$dir" -c advice.detachedHead=false checkout --quiet "$ref" 2>/dev/null \
                || die "No branch or tag named '$ref' in $repo."
        fi
    else
        echo -e "${dim}Cloning claudio ($ref) into $dir...${nc}"
        mkdir -p "$(dirname "$dir")"
        # Not `clone --branch "$ref"`: given a release tag, that prints
        # "is not a commit!" and git's detached-HEAD lecture, which reads
        # like a failure to someone who only asked for a version.
        git clone --quiet --no-checkout "$repo" "$dir" \
            || die "Could not clone $repo."
        git -C "$dir" -c advice.detachedHead=false checkout --quiet "$ref" 2>/dev/null \
            || { rm -rf "$dir"; die "No branch or tag named '$ref' in $repo."; }
    fi

    # curl | bash leaves stdin as the pipe that delivered this script, so the
    # installer's prompts would read EOF. Give them the terminal instead, and
    # when there is none (CI, ssh without a tty) install with the defaults.
    if [[ -t 0 ]]; then
        exec "$dir/install.sh" "$@"
    elif (exec </dev/tty) 2>/dev/null; then
        exec "$dir/install.sh" "$@" </dev/tty
    else
        exec "$dir/install.sh" --yes "$@"
    fi
}

main "$@"
