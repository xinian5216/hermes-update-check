#!/bin/sh
# One-click installer for hermes-update-check (Linux, macOS, WSL, git-bash).
#
#   curl -fsSL https://raw.githubusercontent.com/xinian5216/hermes-update-check/main/install.sh | bash
#   curl -fsSL https://raw.githubusercontent.com/xinian5216/hermes-update-check/main/install.sh | bash -s -- --dir /opt/hermes-update-check
#
# What it does: clones the repository, creates an isolated virtual environment,
# installs the package into it, links the `hermes-update-check` command, enables
# the pre-commit privacy hook and runs a first check.
#
# What it never does: use sudo, modify HERMES_HOME, run `hermes update`, or touch
# anything outside the target directory plus the bin symlink.
set -eu

REPO_URL="https://github.com/xinian5216/hermes-update-check.git"
BRANCH="main"
TARGET_DIR="${HOME}/projects/hermes-update-check"
BIN_DIR="${HOME}/.local/bin"
INSTALL_HOOK="auto"

log() { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<EOF
hermes-update-check installer

usage: install.sh [options]

  --dir PATH      where to install            (default: \$HOME/projects/hermes-update-check)
  --repo URL      git remote or local path    (default: ${REPO_URL})
  --branch NAME   branch to check out         (default: ${BRANCH})
  --bin-dir PATH  where to link the command   (default: \$HOME/.local/bin)
  --no-hook       do not enable .githooks/pre-commit
  -h, --help      show this help

The installer is read-only towards Hermes itself: it never calls \`hermes update\`.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --dir) TARGET_DIR="$2"; shift 2 ;;
        --repo) REPO_URL="$2"; shift 2 ;;
        --branch) BRANCH="$2"; shift 2 ;;
        --bin-dir) BIN_DIR="$2"; shift 2 ;;
        --no-hook) INSTALL_HOOK="no"; shift ;;
        -h | --help) usage; exit 0 ;;
        *) die "unknown option: $1 (try --help)" ;;
    esac
done

# ---------------------------------------------------------------- 1. python ---
# Probe by *executing* the candidate: Windows ships python3.exe stubs that exist
# on PATH but cannot run, and `command -v` alone would accept them.
find_python() {
    for candidate in python3 python py; do
        if command -v "$candidate" >/dev/null 2>&1 &&
            "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

PY=$(find_python) || die "python >= 3.9 not found. Install it first (Debian/Ubuntu: apt install python3 python3-venv)."
log "python:   $PY ($("$PY" -c 'import platform; print(platform.python_version())'))"

# --------------------------------------------------------------- 2. checkout ---
if [ -d "$TARGET_DIR/.git" ]; then
    log "updating: $TARGET_DIR"
    git -C "$TARGET_DIR" fetch --quiet origin "$BRANCH" || warn "fetch failed, using the existing checkout"
    git -C "$TARGET_DIR" checkout --quiet "$BRANCH" 2>/dev/null || true
    git -C "$TARGET_DIR" pull --ff-only --quiet || warn "could not fast-forward, keeping the current revision"
elif command -v git >/dev/null 2>&1; then
    log "cloning:  $REPO_URL -> $TARGET_DIR"
    mkdir -p "$(dirname "$TARGET_DIR")"
    git clone --quiet --branch "$BRANCH" --depth 1 "$REPO_URL" "$TARGET_DIR"
else
    [ -d "$TARGET_DIR" ] || mkdir -p "$TARGET_DIR"
    case "$REPO_URL" in
        https://github.com/*/*)
            slug=$(printf '%s' "$REPO_URL" | sed -E 's#^https://github.com/##; s#\.git$##')
            url="https://github.com/${slug}/archive/refs/heads/${BRANCH}.tar.gz"
            log "no git found; downloading $url"
            command -v curl >/dev/null 2>&1 || die "need git or curl to download the source"
            curl -fsSL "$url" | tar -xz --strip-components=1 -C "$TARGET_DIR"
            ;;
        *) die "git is required for $REPO_URL" ;;
    esac
fi
[ -f "$TARGET_DIR/pyproject.toml" ] || die "$TARGET_DIR does not look like hermes-update-check"

# ------------------------------------------------------------------ 3. venv ---
VENV="$TARGET_DIR/.venv"
if [ -x "$VENV/Scripts/python.exe" ]; then
    VENV_PY="$VENV/Scripts/python.exe" # Windows layout (git-bash)
elif [ -x "$VENV/bin/python" ]; then
    VENV_PY="$VENV/bin/python"
else
    log "creating virtual environment in $VENV"
    if command -v uv >/dev/null 2>&1; then
        uv venv "$VENV" --quiet
    else
        "$PY" -m venv "$VENV" || die "python3-venv is missing (Debian/Ubuntu: apt install python3-venv)"
    fi
    if [ -x "$VENV/Scripts/python.exe" ]; then VENV_PY="$VENV/Scripts/python.exe"; else VENV_PY="$VENV/bin/python"; fi
fi

log "installing package (this pulls PyYAML and rich)"
if command -v uv >/dev/null 2>&1; then
    uv pip install --quiet --python "$VENV_PY" -e "$TARGET_DIR" || die "uv pip install failed"
else
    "$VENV_PY" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
    "$VENV_PY" -m pip install --quiet -e "$TARGET_DIR" || die "pip install failed"
fi

# -------------------------------------------------------------- 4. launcher ---
if [ -x "$VENV/bin/hermes-update-check" ]; then
    ENTRY="$VENV/bin/hermes-update-check"
else
    ENTRY="$VENV/Scripts/hermes-update-check.exe"
fi
[ -e "$ENTRY" ] || ENTRY="$VENV_PY -m hermes_update_check"

if mkdir -p "$BIN_DIR" 2>/dev/null && [ -w "$BIN_DIR" ]; then
    ln -sf "$ENTRY" "$BIN_DIR/hermes-update-check" 2>/dev/null || true
    case ":${PATH}:" in
        *":${BIN_DIR}:"*) log "command:  hermes-update-check" ;;
        *) warn "$BIN_DIR is not on your PATH; add it or call $ENTRY directly" ;;
    esac
fi

# ---------------------------------------------------------------- 5. verify ---
log "verifying installation"
"$VENV_PY" -m hermes_update_check version >/dev/null 2>&1 || die "the tool did not start; see the message above"

# ------------------------------------------------------------------ 6. hook ---
if [ "$INSTALL_HOOK" = "auto" ] || [ "$INSTALL_HOOK" = "yes" ]; then
    if [ -d "$TARGET_DIR/.git" ]; then
        git -C "$TARGET_DIR" config core.hooksPath .githooks || true
        log "privacy:  pre-commit secret scan enabled (.githooks)"
    fi
fi

# ------------------------------------------------------------- 7. next steps ---
cat <<EOF

Installed hermes-update-check in ${TARGET_DIR}

Next:
  1. see what it thinks about the current release:
       ${ENTRY} check
       ${ENTRY} report            # full factor-by-factor detail
  2. optional Telegram/webhook alerts: put the token in the environment, e.g.
       echo 'HERMES_UPDATE_CHECK_TELEGRAM_TOKEN=...' >> ~/.hermes/.env
     then enable the channel in the config file (hermes-update-check config path).
  3. optional daily watch (nothing is updated automatically):
       crontab -e
       0 9 * * * ${ENTRY} watch >> ~/.hermes-update-check/watch.log 2>&1

Exit codes: 0 = can update / already current, 10 = wait, 11 = not enough data,
12 = health check failed, 13 = cancelled, 14 = preflight failed.
This tool only ever *advises*; \`hermes-update-check update\` asks for confirmation.
EOF
