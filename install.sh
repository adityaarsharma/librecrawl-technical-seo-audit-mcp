#!/usr/bin/env bash
# =============================================================================
# LibreCrawl MCP — 1-click installer
# Self-hosted SEO crawler exposed as a Claude MCP server
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/adityaarsharma/librecrawl-mcp/main/install.sh | bash
#
# Or with a custom install dir:
#   INSTALL_DIR=/opt/librecrawl-mcp bash install.sh
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
INSTALL_DIR="${INSTALL_DIR:-$HOME/librecrawl-mcp}"
PM2_NAME="librecrawl-mcp"
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
info "Step 4/5 — Installing LibreCrawl MCP server..."

MCP_DIR="${INSTALL_DIR}/mcp-server"
mkdir -p "${MCP_DIR}"

# Download the full MCP server (19 tools) from the repo
info "Downloading MCP server (server.py)..."
curl -fsSL "https://raw.githubusercontent.com/adityaarsharma/librecrawl-mcp/main/server.py" \
     -o "${MCP_DIR}/server.py" || err "Failed to download server.py from GitHub"
log "MCP server downloaded"

# Create venv and install deps
info "Creating Python venv and installing dependencies..."
python3 -m venv "${MCP_DIR}/venv"
"${MCP_DIR}/venv/bin/pip" install --quiet --upgrade pip
"${MCP_DIR}/venv/bin/pip" install --quiet "mcp>=1.0.0" httpx uvicorn

log "Python dependencies installed"

# ── Step 5: Register with PM2 ────────────────────────────────────────────────
info "Step 5/5 — Registering with PM2..."

# Optional: PageSpeed Insights API key
if [[ -z "${PAGESPEED_API_KEY:-}" ]]; then
  echo ""
  echo -e "${YELLOW}Optional: Google PageSpeed Insights API key${NC}"
  echo -e "  Enables Core Web Vitals (LCP, CLS, INP, FCP) and Lighthouse scores."
  echo -e "  Get one free (25k req/day): https://console.cloud.google.com → APIs → PageSpeed Insights API"
  echo -e "  Press Enter to skip for now."
  read -rp "  PAGESPEED_API_KEY: " PAGESPEED_API_KEY
fi

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

# ── Nginx config hint ─────────────────────────────────────────────────────────
hr
echo ""
echo -e "${BOLD}Optional: Nginx reverse proxy${NC}"
echo "Add this location block to expose MCP over HTTPS:"
echo ""
cat << NGINX
location /librecrawl/ {
    proxy_pass          http://127.0.0.1:${MCP_PORT}/;
    proxy_http_version  1.1;
    proxy_set_header    Host \$host;
    proxy_read_timeout  600s;
    proxy_buffering     off;
    proxy_cache         off;
    chunked_transfer_encoding on;
}
NGINX

# ── Claude config ─────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}Add to Claude claude_desktop_config.json or settings.json:${NC}"
echo ""
cat << JSON
{
  "mcpServers": {
    "librecrawl": {
      "type": "http",
      "url": "http://127.0.0.1:${MCP_PORT}/mcp"
    }
  }
}
JSON
echo ""
echo -e "Or via mcp-remote for remote access:"
echo '  "url": "https://your-domain.com/librecrawl/mcp"'

# ── Done ──────────────────────────────────────────────────────────────────────
hr
echo ""
echo -e "${BOLD}${GREEN}Install complete!${NC}"
echo ""
echo -e "  LibreCrawl UI : http://127.0.0.1:${LIBRECRAWL_PORT}"
echo -e "  MCP endpoint  : http://127.0.0.1:${MCP_PORT}/mcp"
echo ""
echo -e "  ${BOLD}19 tools available:${NC}"
echo -e "    Crawl lifecycle  : librecrawl_start_crawl, librecrawl_get_status,"
echo -e "                       librecrawl_export_results, librecrawl_list_crawls,"
echo -e "                       librecrawl_stop_crawl, librecrawl_pause_crawl,"
echo -e "                       librecrawl_resume_crawl"
echo -e "    Site analysis    : librecrawl_audit, librecrawl_site_check,"
echo -e "                       librecrawl_internal_links_analysis,"
echo -e "                       librecrawl_filter_issues"
echo -e "    Technical SEO    : librecrawl_pagespeed, librecrawl_pagespeed_audit,"
echo -e "                       librecrawl_schema_check, librecrawl_schema_audit,"
echo -e "                       librecrawl_get_settings"
echo -e "    Reporting        : librecrawl_append_gsc_section,"
echo -e "                       librecrawl_visualization_data,"
echo -e "                       librecrawl_generate_report"
echo ""
echo -e "  ${BOLD}Test it:${NC}"
echo -e "  pm2 status ${PM2_NAME}"
echo -e "  docker ps | grep librecrawl"
echo ""
