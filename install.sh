#!/usr/bin/env bash
set -euo pipefail

# ── Crow Agent — one-line installer ──────────────────────────────────
# curl -fsSL https://raw.githubusercontent.com/USER/crow-agent/main/install.sh | bash
#
# What it does:
#   1. Checks Python 3.12+
#   2. Creates venv
#   3. Clones repo (or uses existing dir)
#   4. pip install
#   5. Prints next steps
# ──────────────────────────────────────────────────────────────────────

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
say()  { printf "${GREEN}→${NC} %s\n" "$1"; }
warn() { printf "${CYAN}→${NC} %s\n" "$1"; }
die()  { printf "${RED}✗ %s${NC}\n" "$1"; exit 1; }

INSTALL_DIR="${CROW_INSTALL_DIR:-$HOME/crow-agent}"
REPO_URL="${CROW_REPO:-https://github.com/USER/crow-agent.git}"
BRANCH="${CROW_BRANCH:-main}"

echo ""
echo "  🐦‍⬛  Crow Agent Installer"
echo "  ========================="
echo ""

# ── 1. Check Python ──
PYTHON=""
for cmd in python3.12 python3.13 python3 python; do
    if command -v "$cmd" &>/dev/null; then
        v=$("$cmd" -c 'import sys; print(".".join(map(str, sys.version_info[:2])))' 2>/dev/null || echo "0")
        major=$(echo "$v" | cut -d. -f1)
        minor=$(echo "$v" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 12 ] 2>/dev/null; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    die "Python 3.12+ required. Install it first:
  macOS:   brew install python@3.12
  Ubuntu:  sudo apt install python3.12 python3.12-venv
  Windows: winget install Python.Python.3.12"
fi
say "Found $PYTHON ($($PYTHON --version))"

# ── 2. Clone or update ──
if [ -d "$INSTALL_DIR/.git" ]; then
    say "Updating existing install at $INSTALL_DIR..."
    git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH" 2>/dev/null || warn "Could not pull — using existing code"
else
    if [ -d "$INSTALL_DIR" ]; then
        warn "$INSTALL_DIR exists but is not a git repo. Remove it first or set CROW_INSTALL_DIR."
        exit 1
    fi
    say "Cloning Crow Agent..."
    git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# ── 3. Setup venv ──
if [ ! -d ".venv" ]; then
    say "Creating virtual environment..."
    "$PYTHON" -m venv .venv
fi

# ── 4. Install ──
say "Installing Crow Agent..."
.venv/bin/pip install -e . -q 2>&1 | tail -1 || die "pip install failed. Check Python version (need 3.12+)."

# ── 5. First-run config ──
if [ ! -f ".env" ]; then
    cp .env.example .env
    say "Created .env — add your OpenRouter API key:"
    echo ""
    printf "  ${CYAN}%s${NC}\n" "1. Get a free key: https://openrouter.ai/keys"
    printf "  ${CYAN}%s${NC}\n" "2. Edit .env:   nano $INSTALL_DIR/.env"
    printf "  ${CYAN}%s${NC}\n" "3. Or just run crow and paste your key in the setup page"
    echo ""
fi

# ── 6. Done ──
say "Crow Agent installed!"
echo ""
echo "  Start the web UI:"
printf "  ${GREEN}%s${NC}\n" "$INSTALL_DIR/.venv/bin/crow"
echo ""
echo "  Or add to PATH:"
printf "  ${CYAN}%s${NC}\n" "  echo 'export PATH=\"$INSTALL_DIR/.venv/bin:\$PATH\"' >> ~/.bashrc"
echo ""

# Try launching
if [ -t 0 ]; then
    printf "  Launch now? [Y/n] "
    read -r answer
    if [ "$answer" != "n" ] && [ "$answer" != "N" ]; then
        echo ""
        exec "$INSTALL_DIR/.venv/bin/crow"
    fi
fi
