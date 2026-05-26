#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
#  OMNI-ICT Auto Trader — Setup
#
#  Normal client install (credentials collected via Telegram):
#    bash <(curl -fsSL https://omni-full-algo-trading-bot-production.up.railway.app/install/YOUR_KEY)
#
#  Manual install:
#    bash setup.sh
# ═══════════════════════════════════════════════════════════════

set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

say()  { echo -e "${CYAN}${BOLD}► $*${RESET}"; }
ok()   { echo -e "${GREEN}✓ $*${RESET}"; }
warn() { echo -e "${YELLOW}⚠ $*${RESET}"; }
err()  { echo -e "${RED}✗ $*${RESET}"; exit 1; }
ask()  { echo -e "${BOLD}$1${RESET}"; read -r REPLY; echo "$REPLY"; }

LICENSE_SERVER="${OMNI_LICENSE_SERVER:-https://omni-full-algo-trading-bot-production.up.railway.app}"
LICENSE_MODE="${OMNI_LICENSE_MODE:-local}"
LICENSE_KEY=""

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}║     OMNI-ICT Auto Trader  —  Setup       ║${RESET}"
echo -e "${BOLD}╚══════════════════════════════════════════╝${RESET}"
echo ""

# ── Install deps ────────────────────────────────────────────────
say "Checking dependencies..."
if ! command -v python3 &>/dev/null; then
    warn "Python3 not found. Installing..."
    if command -v apt-get &>/dev/null; then
        apt-get update -qq && apt-get install -y python3 python3-pip python3-venv git curl
    elif command -v brew &>/dev/null; then
        brew install python3 git
    else
        err "Please install Python 3.11+ and git, then re-run."
    fi
fi
ok "Python3: $(python3 --version)"

# ── Authorization mode ───────────────────────────────────────────
echo ""
say "Authorization Mode"
echo ""
if [[ "$LICENSE_MODE" == "remote" ]]; then
    ok "Remote SaaS license validation enabled"

    # Check if already in .env
    if [[ -f .env ]]; then
        EXISTING_KEY=$(grep -E "^OMNI_LICENSE_KEY=" .env 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'")
    fi

    if [[ -n "${OMNI_LICENSE_KEY:-}" ]]; then
        LICENSE_KEY="$OMNI_LICENSE_KEY"
        ok "License key from environment: ${LICENSE_KEY:0:12}****"
    elif [[ -n "${EXISTING_KEY:-}" && "$EXISTING_KEY" != "CHANGE_ME" ]]; then
        LICENSE_KEY="$EXISTING_KEY"
        ok "License key from .env: ${LICENSE_KEY:0:12}****"
    else
        echo "Remote mode requires a license key."
        echo "Switch to local mode by running: OMNI_LICENSE_MODE=local bash setup.sh"
        echo ""
        LICENSE_KEY=$(ask "Paste your license key:")
        [[ -z "$LICENSE_KEY" ]] && err "License key is required in remote mode."
    fi

    LICENSE_KEY=$(echo "$LICENSE_KEY" | tr '[:lower:]' '[:upper:]' | tr -d ' ')

    # Validate remote licenses only when remote mode is explicitly selected.
    say "Validating license key..."
    VALIDATE=$(curl -sf "${LICENSE_SERVER}/validate?key=${LICENSE_KEY}" 2>/dev/null || echo '{"valid":false}')
    VALID=$(echo "$VALIDATE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('valid','false'))" 2>/dev/null || echo "false")
    if [[ "$VALID" != "True" && "$VALID" != "true" ]]; then
        warn "Could not validate key online (server may be unreachable)"
        warn "Will validate on first bot start"
    else
        PLAN=$(echo "$VALIDATE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('plan','?'))" 2>/dev/null)
        ok "License valid — ${PLAN} plan"
    fi
else
    LICENSE_MODE="local"
    ok "Local authorization mode enabled; no external license server will be contacted"
fi

# ── MT5 credentials ─────────────────────────────────────────────
echo ""
say "MT5 Account"

if [[ -n "${OMNI_MT5_LOGIN:-}" ]]; then
    MT5_LOGIN="$OMNI_MT5_LOGIN"
    MT5_PASS="${OMNI_MT5_PASSWORD:-}"
    MT5_SERVER="${OMNI_MT5_SERVER:-}"
    ok "MT5 credentials from environment"
else
    echo "Open your MT5 account at: ${BOLD}${GREEN}https://www.midasfx.com/?ib=1128101${RESET}"
    echo ""
    MT5_LOGIN=$(ask "MT5 account number (login ID):")
    MT5_PASS=$(ask "MT5 password:")
    MT5_SERVER=$(ask "MT5 server (e.g. ICMarkets-Live, XM.COM-Real 3):")
fi

# ── Risk mode ────────────────────────────────────────────────────
echo ""
say "Risk Mode"

if [[ -n "${OMNI_RISK_MODE:-}" ]]; then
    RISK_MODE="$OMNI_RISK_MODE"
    ok "Risk mode: $RISK_MODE"
else
    echo "  1) CONSERVATIVE — 0.5% risk/trade (safest)"
    echo "  2) MODERATE     — 1.0% risk/trade (recommended)"
    echo "  3) AGGRESSIVE   — 2.0% risk/trade"
    echo ""
    RISK_CHOICE=$(ask "Choose [1/2/3] (default: 2):")
    case "$RISK_CHOICE" in
        1) RISK_MODE="CONSERVATIVE" ;;
        3) RISK_MODE="AGGRESSIVE" ;;
        *) RISK_MODE="MODERATE" ;;
    esac
fi

# ── Anthropic API key ─────────────────────────────────────────────
echo ""
say "Anthropic API Key (for AI market analysis)"
echo "Get yours at: https://console.anthropic.com (free tier works)"
echo "Press Enter to skip:"
if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
    read -r ANTHROPIC_KEY
    ANTHROPIC_KEY="${ANTHROPIC_KEY:-}"
else
    ANTHROPIC_KEY="$ANTHROPIC_API_KEY"
    ok "Anthropic key from environment"
fi

# ── Write .env ────────────────────────────────────────────────────
echo ""
say "Writing configuration..."

cat > .env <<ENVEOF
# Generated by setup.sh on $(date)
OMNI_LICENSE_MODE=${LICENSE_MODE}
OMNI_LICENSE_KEY=${LICENSE_KEY}
OMNI_LICENSE_SERVER=${LICENSE_SERVER}
OMNI_RISK_MODE=${RISK_MODE}
OMNI_FREQ_MODE=NORMAL
OMNI_PAPER_MODE=true

OMNI_MT5_LOGIN=${MT5_LOGIN}
OMNI_MT5_PASSWORD=${MT5_PASS}
OMNI_MT5_SERVER=${MT5_SERVER}

ANTHROPIC_API_KEY=${ANTHROPIC_KEY}
ENVEOF

ok ".env written"

# ── Install Python deps ────────────────────────────────────────────
echo ""
say "Installing Python dependencies..."
if [[ ! -d venv ]]; then
    python3 -m venv venv
fi
venv/bin/pip install -q -r requirements.txt
ok "Dependencies installed"

# ── Start the bot ──────────────────────────────────────────────────
echo ""
say "Starting OMNI-ICT..."
mkdir -p logs mt5

nohup venv/bin/python python/watchdog.py > logs/watchdog.log 2>&1 &
echo $! > logs/watchdog.pid
sleep 2

if kill -0 "$(cat logs/watchdog.pid)" 2>/dev/null; then
    ok "OMNI-ICT started (PID $(cat logs/watchdog.pid))"
else
    warn "Watchdog may not have started. Check logs/watchdog.log"
fi

echo ""
echo -e "${GREEN}${BOLD}═══════════════════════════════════════════${RESET}"
echo -e "${GREEN}${BOLD}  Setup complete! Welcome to OMNI-ICT. 🚀  ${RESET}"
echo -e "${GREEN}${BOLD}═══════════════════════════════════════════${RESET}"
echo ""
echo -e "${BOLD}Next steps:${RESET}"
echo "  1. Attach the MT5 EA to your chart:"
echo "     MT5 → Navigator → Expert Advisors → OMNI_EA → drag onto chart"
echo "  2. Check your Telegram bot for status updates"
echo "  3. View logs: tail -f logs/watchdog.log"
echo ""
echo -e "${YELLOW}⚠  PAPER MODE ON — no real trades until you confirm you're ready${RESET}"
echo ""
