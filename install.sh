#!/usr/bin/env bash
#
# Agents-Core one-command installer (macOS and Linux)
#
#   curl -fsSL https://raw.githubusercontent.com/IEZhu/Agents/main/install.sh | bash
#
# Clones (or fast-forward updates) the repository into ~/.agents-core and runs
# scripts/init_repo.sh after a single confirmation. Extra arguments are passed to
# init_repo.sh: `... | bash -s -- --skip-index`. Windows: use init_repo.bat.
#
# Environment:
#   AGENTS_HOME       install directory (default: ~/.agents-core)
#   AGENTS_REPO_URL   repository URL (default: https://github.com/IEZhu/Agents.git)
#   AGENTS_BRANCH     branch to install (default: main)
#   AGENTS_ASSUME_YES=1  skip the confirmation (also the behavior without a TTY)
#   AGENTS_USER_SYNC_*   set up sync between machines without questions; init_repo.sh
#                        never asks about it under --yes (README, "Sync between machines")
#   AGENTS_GITHUB_TOKEN  for that setup on GitHub: passed to init_repo.sh only, never to git

# All logic lives in main(), called on the last line, so a truncated download
# never runs a partial script.
main() {
    set -euo pipefail
    # Inherited Git variables (hooks, wrappers) could redirect git to another repository.
    unset GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR GIT_INDEX_FILE \
          GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_PREFIX
    # A GitHub token for sync setup reaches init_repo.sh only, as a prefix assignment on its
    # exec, never git or any other child: keep it in a variable that is not exported.
    local sync_github_token="${AGENTS_GITHUB_TOKEN:-}"
    unset AGENTS_GITHUB_TOKEN

    local home_dir="${AGENTS_HOME:-$HOME/.agents-core}"
    local repo_url="${AGENTS_REPO_URL:-https://github.com/IEZhu/Agents.git}"
    local branch="${AGENTS_BRANCH:-main}"

    if ! command -v git >/dev/null 2>&1; then
        echo "install.sh: git is required but was not found in PATH." >&2
        return 1
    fi

    # Under `curl | bash` stdin is the pipe; prompts must read from the terminal.
    local have_tty=false
    if { : </dev/tty; } 2>/dev/null; then
        have_tty=true
    fi

    local assume_yes=false
    case "${AGENTS_ASSUME_YES:-}" in 1|true|yes) assume_yes=true ;; esac
    local arg
    for arg in "$@"; do
        case "$arg" in --yes|-y) assume_yes=true ;; esac
    done
    [ "$have_tty" = true ] || assume_yes=true

    local action="clone into"
    if [ -d "$home_dir/.git" ]; then
        if [ ! -f "$home_dir/scripts/init_repo.sh" ]; then
            echo "install.sh: $home_dir is a git repository but not an Agents-Core checkout." >&2
            return 1
        fi
        action="update (git pull --ff-only) in"
    elif [ -e "$home_dir" ] && [ -n "$(ls -A "$home_dir" 2>/dev/null)" ]; then
        echo "install.sh: $home_dir exists and is not an Agents-Core checkout." >&2
        return 1
    fi

    echo "Agents-Core installer"
    echo "  Repository : $repo_url ($branch)"
    echo "  Directory  : $action $home_dir"
    echo "  Then runs  : scripts/init_repo.sh --yes"
    echo "               (Python venv, the default embedding model,"
    echo "                MCP registration and routing instructions for detected clients)"

    if [ "$assume_yes" = false ]; then
        local reply=""
        if ! read -r -p "Proceed with these defaults? [Y/n]: " reply </dev/tty; then
            echo "install.sh: no answer received; aborting." >&2
            return 1
        fi
        case "$reply" in
            [Nn]*)
                echo "Aborted. For step-by-step prompts run: git clone $repo_url \"$home_dir\" && \"$home_dir/scripts/init_repo.sh\""
                return 0
                ;;
        esac
    fi

    if [ -d "$home_dir/.git" ]; then
        # Only tracked changes block an update; untracked files are left alone.
        if [ -n "$(git -C "$home_dir" status --porcelain --untracked-files=no)" ]; then
            echo "install.sh: $home_dir has local changes to tracked files; commit or stash them first." >&2
            return 1
        fi
        git -C "$home_dir" fetch --quiet origin "$branch"
        git -C "$home_dir" checkout --quiet "$branch"
        git -C "$home_dir" pull --ff-only --quiet origin "$branch" || {
            echo "install.sh: cannot fast-forward $home_dir; resolve it manually." >&2
            return 1
        }
    else
        git clone --quiet --branch "$branch" "$repo_url" "$home_dir"
    fi

    local init_args=("--yes")
    for arg in "$@"; do
        case "$arg" in --yes|-y) ;; *) init_args+=("$arg") ;; esac
    done
    if [ "$have_tty" = true ] && [ ! -t 0 ]; then
        AGENTS_GITHUB_TOKEN="$sync_github_token" exec "$home_dir/scripts/init_repo.sh" "${init_args[@]}" </dev/tty
    fi
    AGENTS_GITHUB_TOKEN="$sync_github_token" exec "$home_dir/scripts/init_repo.sh" "${init_args[@]}"
}

main "$@"
