#!/usr/bin/env bash
# =============================================================================
# LibreCrawl MCP — 1-click installer
# Self-hosted SEO crawler — MCP server for any AI agent
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/adityaarsharma/librecrawl-technical-seo-audit-mcp/main/install.sh | bash
#
# Or with a custom install dir:
#   INSTALL_DIR=/opt/librecrawl-technical-seo-audit-mcp bash install.sh
#
# What this installs:
#   1. LibreCrawl (Docker container) — SEO crawler on port 5080
#   2. LibreCrawl MCP server (Python + PM2) — MCP endpoint on port 5081
#   3. Applies session persistence bugfix to LibreCrawl
#   4. (Optional) Nginx reverse proxy config
# =============================================================================

set -euo pipefail

# ── Config ───────────────────────────────────────────────────────────────────
LIBRECRAWL_PORT="${LIBRECRAWL_PORT:-5080}"
MCP_PORT="${MCP_PORT:-5081}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/librecrawl-technical-seo-audit-mcp}"
PM2_NAME="librecrawl-technical-seo-audit-mcp"
MCP_USERNAME="${MCP_USERNAME:-mcp-user}"

# ── Colors ───────────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'
  BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
else
  GREEN=''; YELLOW=''; RED=''; BLUE=''; BOLD=''; NC=''
fi

log()  { echo -e "${GREEN}✓${NC} $*"; }
info() { echo -e "${BLUE}→${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC}  $*"; }
err()  { echo -e "${RED}✗ ERROR:${NC} $*" >&2; exit 1; }
hr()   { echo -e "${BLUE}$(printf '─%.0s' {1..60})${NC}"; }

hr
echo -e "${BOLD}LibreCrawl MCP — Installer${NC}"
echo -e "  Install dir : ${INSTALL_DIR}"
echo -e "  LibreCrawl  : http://127.0.0.1:${LIBRECRAWL_PORT}"
echo -e "  MCP server  : http://127.0.0.1:${MCP_PORT}/mcp"
hr

# ── Step 0: Check dependencies ───────────────────────────────────────────────
info "Checking dependencies..."

command -v docker &>/dev/null   || err "Docker not found. Install: https://docs.docker.com/get-docker/"
command -v python3 &>/dev/null  || err "Python 3.9+ not found. Install: sudo apt install python3 python3-venv"
command -v git &>/dev/null      || err "Git not found. Install: sudo apt install git"

PYTHON_VERSION=$(python3 -c 'import sys; print(sys.version_info.minor)')
[[ "$PYTHON_VERSION" -ge 9 ]] || err "Python 3.9+ required (found 3.${PYTHON_VERSION})"

# Check docker compose (v2 plugin or standalone)
if docker compose version &>/dev/null 2>&1; then
  DOCKER_COMPOSE="docker compose"
elif command -v docker-compose &>/dev/null; then
  DOCKER_COMPOSE="docker-compose"
else
  err "Docker Compose not found. Install: https://docs.docker.com/compose/install/"
fi

# PM2 — install if missing
if ! command -v pm2 &>/dev/null; then
  warn "PM2 not found. Attempting install via npm..."
  command -v npm &>/dev/null || err "npm not found. Install Node.js: https://nodejs.org"
  npm install -g pm2 --quiet || err "PM2 install failed. Try: sudo npm install -g pm2"
fi

log "All dependencies satisfied"

# ── Step 1: Clone LibreCrawl ─────────────────────────────────────────────────
hr
info "Step 1/5 — Setting up LibreCrawl..."

mkdir -p "${INSTALL_DIR}"
LIBRECRAWL_DIR="${INSTALL_DIR}/librecrawl"

if [[ -d "${LIBRECRAWL_DIR}/.git" ]]; then
  info "LibreCrawl repo exists — pulling latest..."
  git -C "${LIBRECRAWL_DIR}" pull origin main --quiet
else
  info "Cloning LibreCrawl (github.com/PhialsBasement/LibreCrawl)..."
  git clone --quiet https://github.com/PhialsBasement/LibreCrawl.git "${LIBRECRAWL_DIR}"
fi

# ── Step 2: Apply session persistence patch ──────────────────────────────────
info "Step 2/5 — Applying session persistence patch..."

MAIN_PY="${LIBRECRAWL_DIR}/main.py"

# Patch: move session_id read to AFTER get_or_create_crawler() which creates it.
# Without this patch, crawl_id is always null and results are never saved to DB.
python3 - "${MAIN_PY}" << 'PATCHEOF'
import sys

path = sys.argv[1]
content = open(path).read()

old = """    user_id = session.get('user_id')
    session_id = session.get('session_id')
    tier = session.get('tier', 'guest')"""

new = """    user_id = session.get('user_id')
    tier = session.get('tier', 'guest')"""

old2 = """    # Get or create crawler for this session
    crawler = get_or_create_crawler()"""

new2 = """    # Get or create crawler for this session (also initialises session_id)
    crawler = get_or_create_crawler()
    session_id = session.get('session_id')  # Must read AFTER get_or_create_crawler sets it"""

if old not in content:
    print("Patch 1 already applied or not needed — skipping")
else:
    content = content.replace(old, new, 1)
    content = content.replace(old2, new2, 1)
    open(path, 'w').write(content)
    print("Session persistence patch applied")
PATCHEOF

# ── Step 3: Write Docker config + start LibreCrawl ───────────────────────────
info "Step 3/5 — Building and starting LibreCrawl Docker container..."
info "  (First build takes 5–8 min — Playwright + Chromium install)"

# Patch base docker-compose.yml to use LIBRECRAWL_PORT (avoids port merge conflicts)
python3 - "${LIBRECRAWL_DIR}/docker-compose.yml" "${LIBRECRAWL_PORT}" << 'COMPOSEPATCHEOF'
import sys
path, port = sys.argv[1], sys.argv[2]
content = open(path).read()
import re
patched = re.sub(
    r'"\$\{HOST_BINDING[^}]*\}:\d+:\d+"',
    f'"127.0.0.1:{port}:5000"',
    content
)
if patched != content:
    open(path, "w").write(patched)
    print(f"docker-compose.yml port patched to 127.0.0.1:{port}:5000")
else:
    print("docker-compose.yml port already patched or pattern changed — skipping")
COMPOSEPATCHEOF

cat > "${LIBRECRAWL_DIR}/.env" << ENVEOF
LOCAL_MODE=true
REGISTRATION_DISABLED=true
DEMO_MODE=false
DANGEROUSLY_SKIP_AUTH=true
ENVEOF

cat > "${LIBRECRAWL_DIR}/docker-compose.override.yml" << OVERRIDEEOF
services:
  librecrawl:
    restart: always
    shm_size: '2gb'
    healthcheck:
      test: ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:5000/')"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 90s
    logging:
      driver: "json-file"
      options:
        max-size: "50m"
        max-file: "3"
    environment:
      - FLASK_APP=main.py
      - PYTHONUNBUFFERED=1
      - LOCAL_MODE=true
      - REGISTRATION_DISABLED=true
      - DEMO_MODE=false
      - DANGEROUSLY_SKIP_AUTH=true
OVERRIDEEOF

cd "${LIBRECRAWL_DIR}"
$DOCKER_COMPOSE build --quiet
$DOCKER_COMPOSE up -d
log "LibreCrawl container started on port ${LIBRECRAWL_PORT}"

# Wait for healthcheck
info "Waiting for LibreCrawl to be healthy (up to 90s)..."
for i in $(seq 1 18); do
  sleep 5
  CONTAINER_ID=$(${DOCKER_COMPOSE} ps -q librecrawl 2>/dev/null | head -1)
  STATUS=$([ -n "$CONTAINER_ID" ] && docker inspect --format='{{.State.Health.Status}}' "$CONTAINER_ID" 2>/dev/null || echo "starting")
  if [[ "$STATUS" == "healthy" ]]; then
    log "LibreCrawl is healthy"
    break
  fi
  [[ $i -eq 18 ]] && warn "Health check timed out — container may still be starting"
done

# ── Step 4: MCP server ───────────────────────────────────────────────────────
info "Step 4/5 — Installing LibreCrawl MCP server (v2.3.0 — 38 tools)..."

MCP_DIR="${INSTALL_DIR}/mcp-server"
mkdir -p "${MCP_DIR}"

# Download all 10 Python modules that make up the v2.0.3 MCP wrapper.
# server.py is the FastMCP entrypoint; the others are imported by it.
# Server-side instructions + ephemeral mode + 38 tools all need these files.
info "Downloading MCP server modules from GitHub (10 files)..."
BASE_URL="https://raw.githubusercontent.com/adityaarsharma/librecrawl-technical-seo-audit-mcp/main"
for f in server.py state.py libreclient.py runner.py external_links.py \
         content_audit.py extended_checks.py schema_validator.py \
         sitemap_fill.py pdf_report.py; do
  curl -fsSL "${BASE_URL}/${f}" -o "${MCP_DIR}/${f}" \
       || err "Failed to download ${f} from GitHub"
done
log "10 Python modules downloaded"

# Optional: drop the Claude Code skill into ~/.claude/skills/ for clients
# that pick it up (Claude Code does — server-side `instructions` covers
# the rest, so this is a developer-experience convenience, not required
# for correctness).
SKILL_DIR="${HOME}/.claude/skills/librecrawl-audit"
mkdir -p "${SKILL_DIR}"
curl -fsSL "${BASE_URL}/.claude/skills/librecrawl-audit/SKILL.md" \
     -o "${SKILL_DIR}/SKILL.md" 2>/dev/null \
     && log "Claude Code skill installed at ${SKILL_DIR}/" \
     || info "(Optional skill download skipped — server-side instructions still apply.)"

# Create venv and install deps
info "Creating Python venv and installing dependencies..."
python3 -m venv "${MCP_DIR}/venv"
"${MCP_DIR}/venv/bin/pip" install --quiet --upgrade pip
# Core MCP + HTTP + report stack:
#   mcp + httpx + uvicorn — FastMCP server runtime
#   weasyprint + markdown — PDF rendering pipeline (.pdf sidecar)
"${MCP_DIR}/venv/bin/pip" install --quiet \
    "mcp>=1.0.0" httpx uvicorn weasyprint markdown

# WeasyPrint needs Pango/Cairo system libraries to render PDFs. We install
# them via apt non-interactively. If the user is not on Debian/Ubuntu OR
# doesn't have passwordless sudo, the install still completes but
# WeasyPrint will throw at first PDF render — the surrounding try/except
# in runner.py logs the failure without failing the whole audit.
if command -v apt-get &>/dev/null && sudo -n true 2>/dev/null; then
  info "Installing WeasyPrint system deps (libpango/libcairo/libharfbuzz)..."
  sudo apt-get install -y -q \
    libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b \
    libcairo2 libcairo-gobject2 2>/dev/null \
    && log "System deps installed" \
    || info "(System deps install skipped — PDF rendering may not work; install manually if needed.)"
fi

log "Python dependencies installed"

# ── Step 5: Configure + Register ─────────────────────────────────────────────
info "Step 5/5 — 3 quick questions then you're done..."

# Helper: safely merge an MCP server entry into a JSON config file
_write_mcp_config() {
  local CONFIG_PATH="$1" SERVER_NAME="$2" SERVER_JSON="$3"
  WCFG_PATH="$CONFIG_PATH" WCFG_NAME="$SERVER_NAME" WCFG_JSON="$SERVER_JSON" \
  python3 - << 'PYEOF'
import json, os
from pathlib import Path
config_path = os.environ["WCFG_PATH"]
server_name = os.environ["WCFG_NAME"]
server_json_str = os.environ["WCFG_JSON"]
path = Path(config_path).expanduser()
path.parent.mkdir(parents=True, exist_ok=True)
data = {}
if path.exists():
    try: data = json.loads(path.read_text())
    except: data = {}
if "mcpServers" not in data:
    data["mcpServers"] = {}
data["mcpServers"][server_name] = json.loads(server_json_str)
path.write_text(json.dumps(data, indent=2))
print(f"    ✓ Added '{server_name}' to {config_path}")
PYEOF
}

# ── Q1: PageSpeed Insights API key ───────────────────────────────────────────
echo ""
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BOLD}  Setup — 3 questions${NC}"
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

if [[ -z "${PAGESPEED_API_KEY:-}" ]]; then
  echo ""
  echo -e "${YELLOW}[1/3] Google PageSpeed Insights API key${NC} (optional)"
  echo -e "  Enables Core Web Vitals — LCP, CLS, INP, FCP, Lighthouse scores."
  echo -e "  Free — 25,000 requests/day. Get one:"
  echo -e "  console.cloud.google.com → APIs & Services → PageSpeed Insights API"
  echo -e "  Press Enter to skip:"
  read -rp "  Key: " PAGESPEED_API_KEY </dev/tty || PAGESPEED_API_KEY=""
fi

# ── Q2: Which AI client to auto-configure ────────────────────────────────────
echo ""
echo -e "${YELLOW}[2/3] Auto-add LibreCrawl MCP to your AI client?${NC}"
echo -e "  1) Claude Desktop  (GUI app — ~/Library/Application Support/Claude/...)"
echo -e "  2) Claude Code     (CLI — ~/.claude/settings.json)"
echo -e "  3) Both Claude Desktop + Code"
echo -e "  4) Cursor          (~/.cursor/mcp.json — stdio mode)"
echo -e "  5) Windsurf        (~/.codeium/windsurf/mcp_config.json — stdio mode)"
echo -e "  6) Skip — I'll add it manually"
read -rp "  Choice [1-6, default 3]: " CLIENT_CHOICE </dev/tty || CLIENT_CHOICE="3"
CLIENT_CHOICE="${CLIENT_CHOICE:-3}"

# JSON entries for HTTP mode (Claude) and stdio mode (Cursor/Windsurf)
MCP_HTTP_JSON="{\"type\":\"http\",\"url\":\"http://127.0.0.1:${MCP_PORT}/mcp\"}"
MCP_STDIO_JSON="{\"command\":\"python3\",\"args\":[\"${MCP_DIR}/server.py\"],\"env\":{\"MCP_TRANSPORT\":\"stdio\",\"LIBRECRAWL_PORT\":\"${LIBRECRAWL_PORT}\",\"PAGESPEED_API_KEY\":\"${PAGESPEED_API_KEY:-}\"}}"

if [[ "$(uname)" == "Darwin" ]]; then
  CLAUDE_DESKTOP_CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
else
  CLAUDE_DESKTOP_CONFIG="$HOME/.config/Claude/claude_desktop_config.json"
fi
CLAUDE_CODE_CONFIG="$HOME/.claude/settings.json"
CURSOR_CONFIG="$HOME/.cursor/mcp.json"
WINDSURF_CONFIG="$HOME/.codeium/windsurf/mcp_config.json"

case "$CLIENT_CHOICE" in
  1)
    _write_mcp_config "$CLAUDE_DESKTOP_CONFIG" "librecrawl" "$MCP_HTTP_JSON"
    log "Claude Desktop configured"
    echo -e "  ${YELLOW}→ Restart Claude Desktop to activate.${NC}"
    ;;
  2)
    _write_mcp_config "$CLAUDE_CODE_CONFIG" "librecrawl" "$MCP_HTTP_JSON"
    log "Claude Code configured"
    echo -e "  ${YELLOW}→ Start a new Claude Code session to activate (or run: claude mcp list).${NC}"
    ;;
  3)
    _write_mcp_config "$CLAUDE_DESKTOP_CONFIG" "librecrawl" "$MCP_HTTP_JSON"
    _write_mcp_config "$CLAUDE_CODE_CONFIG"    "librecrawl" "$MCP_HTTP_JSON"
    log "Claude Desktop + Code configured"
    echo -e "  ${YELLOW}→ Restart Claude Desktop to activate.${NC}"
    echo -e "  ${YELLOW}→ Start a new Claude Code session to activate (or run: claude mcp list).${NC}"
    ;;
  4)
    _write_mcp_config "$CURSOR_CONFIG" "librecrawl" "$MCP_STDIO_JSON"
    log "Cursor configured (stdio mode)"
    echo -e "  ${YELLOW}→ Restart Cursor to activate.${NC}"
    ;;
  5)
    _write_mcp_config "$WINDSURF_CONFIG" "librecrawl" "$MCP_STDIO_JSON"
    log "Windsurf configured (stdio mode)"
    echo -e "  ${YELLOW}→ Restart Windsurf to activate.${NC}"
    ;;
  6)
    echo "  Skipped — manual config shown at the end."
    ;;
esac

# ── Q3: Google Search Console MCP ────────────────────────────────────────────
echo ""
echo -e "${YELLOW}[3/3] Install Google Search Console MCP?${NC} (recommended)"
echo -e "  Lets your AI pull real GSC indexing errors into your audit reports."
echo -e "  1) Yes — install via uvx  (recommended, no extra deps)"
echo -e "  2) Yes — install via pip"
echo -e "  3) Skip"
read -rp "  Choice [1-3, default 1]: " GSC_CHOICE </dev/tty || GSC_CHOICE="1"
GSC_CHOICE="${GSC_CHOICE:-1}"

case "$GSC_CHOICE" in
  1|2)
    if [[ "$GSC_CHOICE" == "1" ]]; then
      if ! command -v uvx &>/dev/null; then
        info "Installing uv (needed for uvx)..."
        curl -LsSf https://astral.sh/uv/install.sh | sh 2>/dev/null
        export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
      fi
      GSC_CMD="uvx"
      GSC_ARGS='["mcp-search-console"]'
      log "GSC MCP will use uvx"
    else
      "${MCP_DIR}/venv/bin/pip" install --quiet mcp-search-console
      GSC_CMD="${MCP_DIR}/venv/bin/python3"
      GSC_ARGS='["-m","mcp_search_console"]'
      log "GSC MCP installed via pip"
    fi

    # ── Google credentials setup ──────────────────────────────────────────────
    echo ""
    echo -e "${BOLD}  GSC credentials setup${NC}"
    echo -e "  The GSC MCP needs a Google OAuth credentials file. 3 steps:"
    echo ""
    echo -e "  ${BOLD}Step 1${NC} — Create a Google Cloud project (skip if you have one):"
    echo -e "  → https://console.cloud.google.com/projectcreate"
    echo ""
    echo -e "  ${BOLD}Step 2${NC} — Enable the Search Console API:"
    echo -e "  → https://console.cloud.google.com/apis/library/searchconsole.googleapis.com"
    echo -e "     Click Enable."
    echo ""
    echo -e "  ${BOLD}Step 3${NC} — Create OAuth credentials:"
    echo -e "  → https://console.cloud.google.com/apis/credentials"
    echo -e "     Create Credentials → OAuth client ID → Desktop app"
    echo -e "     Download the JSON file → save it somewhere (e.g. ~/.gsc-credentials.json)"
    echo ""
    echo -e "  ${YELLOW}Enter the full path to your credentials.json now, or press Enter to skip${NC}"
    echo -e "  (You can add it later by editing your MCP config's GOOGLE_CREDENTIALS_FILE env)"
    read -rp "  Path: " GSC_CREDS_PATH </dev/tty || GSC_CREDS_PATH=""

    # Build GSC server JSON — include credentials path if provided
    if [[ -n "${GSC_CREDS_PATH}" && -f "${GSC_CREDS_PATH}" ]]; then
      GSC_SERVER_JSON="{\"command\":\"${GSC_CMD}\",\"args\":${GSC_ARGS},\"env\":{\"GOOGLE_CREDENTIALS_FILE\":\"${GSC_CREDS_PATH}\"}}"
      log "GSC MCP configured with credentials: ${GSC_CREDS_PATH}"
    elif [[ -n "${GSC_CREDS_PATH}" ]]; then
      warn "File not found at '${GSC_CREDS_PATH}' — adding config without path. Edit it later."
      GSC_SERVER_JSON="{\"command\":\"${GSC_CMD}\",\"args\":${GSC_ARGS},\"env\":{\"GOOGLE_CREDENTIALS_FILE\":\"${GSC_CREDS_PATH}\"}}"
    else
      GSC_SERVER_JSON="{\"command\":\"${GSC_CMD}\",\"args\":${GSC_ARGS}}"
      warn "No credentials path entered. GSC MCP installed but won't connect until you add GOOGLE_CREDENTIALS_FILE."
    fi

    # Write GSC to whichever client config was chosen
    case "$CLIENT_CHOICE" in
      1) _write_mcp_config "$CLAUDE_DESKTOP_CONFIG" "gsc" "$GSC_SERVER_JSON" ;;
      2) _write_mcp_config "$CLAUDE_CODE_CONFIG"    "gsc" "$GSC_SERVER_JSON" ;;
      3) _write_mcp_config "$CLAUDE_DESKTOP_CONFIG" "gsc" "$GSC_SERVER_JSON"
         _write_mcp_config "$CLAUDE_CODE_CONFIG"    "gsc" "$GSC_SERVER_JSON" ;;
      4) _write_mcp_config "$CURSOR_CONFIG"         "gsc" "$GSC_SERVER_JSON" ;;
      5) _write_mcp_config "$WINDSURF_CONFIG"       "gsc" "$GSC_SERVER_JSON" ;;
    esac
    echo ""
    echo -e "  ${YELLOW}First use:${NC} Your AI agent will open a browser for Google OAuth."
    echo -e "  Select the Google account that has access to your Search Console properties."
    echo -e "  Token is cached after that — no re-auth needed."
    echo ""
    echo -e "  ${BOLD}⚠️  Property type gotcha${NC} — common reason GSC returns 'insufficient permission':"
    echo -e "  • If your site is a ${BOLD}Domain property${NC} in GSC → use ${YELLOW}sc-domain:yoursite.com${NC}"
    echo -e "  • If your site is a ${BOLD}URL-prefix property${NC} → use ${YELLOW}https://yoursite.com/${NC} (trailing slash)"
    echo -e "  Ask your AI: ${YELLOW}'list my GSC sites'${NC} to see how yours is registered."
    ;;
  3)
    echo "  Skipped."
    ;;
esac

# ── Register with PM2 ─────────────────────────────────────────────────────────
echo ""
pm2 stop "${PM2_NAME}"   2>/dev/null || true
pm2 delete "${PM2_NAME}" 2>/dev/null || true

pm2 start "${MCP_DIR}/server.py" \
  --name "${PM2_NAME}" \
  --interpreter "${MCP_DIR}/venv/bin/python3" \
  --restart-delay 3000 \
  --max-restarts 10 \
  --env LIBRECRAWL_PORT="${LIBRECRAWL_PORT}" \
  --env MCP_PORT="${MCP_PORT}" \
  --env PAGESPEED_API_KEY="${PAGESPEED_API_KEY:-}"

pm2 save
log "PM2 process registered and saved (survives reboots)"

# ── Health check ──────────────────────────────────────────────────────────────
info "Verifying MCP server is responding..."
MCP_OK=false
for i in $(seq 1 6); do
  sleep 3
  if curl -sf "http://127.0.0.1:${MCP_PORT}/mcp" -o /dev/null 2>/dev/null; then
    MCP_OK=true
    break
  fi
done
if $MCP_OK; then
  log "MCP server is live at http://127.0.0.1:${MCP_PORT}/mcp"
else
  warn "MCP server did not respond yet — it may still be starting."
  warn "Check with: pm2 logs ${PM2_NAME}"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
hr
echo ""
echo -e "${BOLD}${GREEN}Install complete!${NC}"
echo ""
echo -e "  LibreCrawl UI : http://127.0.0.1:${LIBRECRAWL_PORT}"
echo -e "  MCP endpoint  : http://127.0.0.1:${MCP_PORT}/mcp"
echo ""

# Show manual config only if user skipped auto-config
if [[ "${CLIENT_CHOICE}" == "6" ]]; then
  echo -e "${BOLD}  Manual MCP config (claude_desktop_config.json, ~/.claude/settings.json, or your client's equivalent):${NC}"
  cat << MANUALJSON
  {
    "mcpServers": {
      "librecrawl": { "type": "http", "url": "http://127.0.0.1:${MCP_PORT}/mcp" }
    }
  }
MANUALJSON
  echo ""
  echo -e "  For Cursor/Codex/Windsurf (stdio mode), add to your client's MCP config:"
  cat << STUDIOJSON
  {
    "mcpServers": {
      "librecrawl": {
        "command": "python3",
        "args": ["${MCP_DIR}/server.py"],
        "env": { "MCP_TRANSPORT": "stdio", "LIBRECRAWL_PORT": "${LIBRECRAWL_PORT}" }
      }
    }
  }
STUDIOJSON
  echo ""
fi

echo -e "  ${BOLD}38 tools available (v2.3.0):${NC}"
echo -e "    Chunked audit    : librecrawl_start_chunked_audit (USE THIS),"
echo -e "                       librecrawl_audit_status, librecrawl_audit_zip,"
echo -e "                       librecrawl_audit_artifacts,"
echo -e "                       librecrawl_audit_pause, _resume, _cancel,"
echo -e "                       librecrawl_audit_force_advance"
echo -e "    Ephemeral mode   : librecrawl_audit_zip (auto_cleanup=True),"
echo -e "                       librecrawl_wipe_everything"
echo -e "    External links   : librecrawl_external_links_audit"
echo -e "    Schema           : librecrawl_schema_validate, librecrawl_schema_check,"
echo -e "                       librecrawl_schema_audit"
echo -e "    PageSpeed        : librecrawl_pagespeed, librecrawl_pagespeed_audit,"
echo -e "                       librecrawl_pagespeed_audit_all_crawl_pages"
echo -e "    GSC merge        : librecrawl_merge_gsc_data, librecrawl_append_gsc_section"
echo -e "    Reports          : librecrawl_audit_pdf, librecrawl_report_content,"
echo -e "                       librecrawl_generate_report"
echo -e "    Crawl lifecycle  : librecrawl_start_crawl, librecrawl_get_status,"
echo -e "                       librecrawl_export_results, librecrawl_list_crawls,"
echo -e "                       librecrawl_stop_crawl, librecrawl_pause_crawl,"
echo -e "                       librecrawl_resume_crawl, librecrawl_resume_from_crawl_id"
echo -e "    Site analysis    : librecrawl_audit (legacy), librecrawl_site_check,"
echo -e "                       librecrawl_full_audit_strict, librecrawl_internal_links_analysis,"
echo -e "                       librecrawl_filter_issues, librecrawl_visualization_data,"
echo -e "                       librecrawl_get_settings, librecrawl_brain_purge_audit"
echo ""
echo -e "  ${BOLD}First audit:${NC} Ask your AI agent: \"Audit https://example.com\""
echo -e "                 (Server instructions tell the LLM exactly how to drive the chunked audit + save the zip locally.)"
echo -e "  ${BOLD}Test:${NC}"
echo -e "  pm2 status ${PM2_NAME}"
echo -e "  docker ps | grep librecrawl"
echo ""
