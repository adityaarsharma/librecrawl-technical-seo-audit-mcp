# Getting Started

This guide takes you from nothing to a finished SEO audit zip. Pick an install path, wire up your MCP client, run your first audit.

- [Install](#install)
  - [Option A — one-liner installer](#option-a--one-liner-installer)
  - [Option B — Docker (full stack)](#option-b--docker-full-stack)
  - [Option C — manual](#option-c--manual)
- [Connect your MCP client](#connect-your-mcp-client)
- [Your first audit](#your-first-audit)
- [Where the report goes](#where-the-report-goes)

---

## Install

The server is a thin MCP wrapper (`server.py`) that drives a separate **LibreCrawl** engine. However you install, you end up with two things running: the engine on port `5080` and the MCP on port `5081`.

### Option A — one-liner installer

The fastest path. Handles Python, the Dockerized LibreCrawl backend, and your client config.

```bash
curl -fsSL https://raw.githubusercontent.com/adityaarsharma/librecrawl-technical-seo-audit-mcp/main/install.sh | bash
```

It asks three questions — target client, optional Google PageSpeed key, optional GSC integration — then writes a ready-to-use MCP entry into your Claude / Cursor / Codex / Windsurf config.

### Option B — Docker (full stack)

Best for self-hosting or a VPS. Brings up the LibreCrawl engine **and** the MCP together, health-gated, in one command.

```bash
git clone https://github.com/adityaarsharma/librecrawl-technical-seo-audit-mcp.git
cd librecrawl-technical-seo-audit-mcp
docker compose up --build
```

- MCP endpoint: `http://127.0.0.1:5081/mcp`
- The `mcp` service waits until LibreCrawl reports healthy before starting.
- First build takes a few minutes (LibreCrawl pulls Chromium for JS rendering).
- Audit zips land in `./reports`.

Stop with `docker compose down` (add `-v` to also drop the shared data volume).

### Option C — manual

Python 3.10+, plus a LibreCrawl backend you run yourself (the Docker path above is the easiest way to get one).

```bash
git clone https://github.com/adityaarsharma/librecrawl-technical-seo-audit-mcp.git
cd librecrawl-technical-seo-audit-mcp
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
LIBRECRAWL_URL=http://127.0.0.1:5080 python server.py
```

By default the MCP serves HTTP on `127.0.0.1:5081`. Set `MCP_TRANSPORT=stdio` to run it over stdio instead (see [Connect your MCP client](#connect-your-mcp-client)).

---

## Connect your MCP client

There are two transports. **HTTP** (default) is best when the MCP runs as a long-lived service (Docker, PM2, a VPS). **stdio** is best when your client launches the process itself.

### Claude Code / Claude Desktop — HTTP

```json
{
  "mcpServers": {
    "librecrawl": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://127.0.0.1:5081/mcp"]
    }
  }
}
```

- Claude Desktop config: `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS)
- Claude Code: add via your project/user MCP config, then restart the session.

### Cursor / Windsurf / Codex / Continue.dev — stdio

Run the server in stdio mode by launching it directly, with `MCP_TRANSPORT=stdio`:

```json
{
  "mcpServers": {
    "librecrawl": {
      "command": "python",
      "args": ["/absolute/path/to/librecrawl-technical-seo-audit-mcp/server.py"],
      "env": {
        "MCP_TRANSPORT": "stdio",
        "LIBRECRAWL_URL": "http://127.0.0.1:5080"
      }
    }
  }
}
```

Use the venv's Python (`/path/to/venv/bin/python`) if you installed manually. The LibreCrawl backend still needs to be running separately.

After editing config, **fully restart the client** so it reconnects.

---

## Your first audit

Just ask your assistant in plain language. Under the hood it calls the tools:

```text
You:   Audit https://example.com — full site, no caps

Agent: → librecrawl_start_chunked_audit(url="https://example.com", total_max_pages=10000)
         returns a session_id in < 2s
       → polls librecrawl_audit_status(session_id) every ~25s
         crawling · pages_done: 47 · current_delay_ms: 250
         crawling · pages_done: 312 · p95: 480ms · err_rate: 0%
         done · pages_done: 534 · artifacts_ready: true
       → librecrawl_audit_zip(session_id)
         saves example.com-<id>.zip locally
       → librecrawl_audit_confirm_saved(session_id, sha256)
         hash matches, server wipes its copy

You:   Show me broken pages + broken external links + hreflang errors
Agent: → unzips, filters per-page.csv and external-links.csv, prints the tables
```

You never call tools by hand — the agent picks them. See **[TOOLS.md](TOOLS.md)** for the full list.

---

## Where the report goes

Each audit produces a single zip (8 files): `SUMMARY.txt`, a branded PDF, its Markdown source, and 5 CSVs (`per-page`, `sitemap-recon`, `external-links`, `content-audit`, `extended-checks`).

- **Docker:** written to `./reports` in the repo.
- **Manual / installer:** written to `REPORTS_DIR` (default `~/librecrawl-reports`).

The audit is **ephemeral**: once the agent saves the zip and confirms its sha256, the server deletes the session, the artifact files, the zip and the upstream crawl record. Your local zip is the only copy.

Stuck? See **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)**.
