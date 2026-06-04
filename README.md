# librecrawl-mcp — Self-Hosted SEO Audit MCP Server for Claude, Cursor & Codex

*Run a full technical SEO audit on any website from your AI assistant. 50+ checks, chunked-progressive crawling that never times out, ephemeral by default.*

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-orange)](https://modelcontextprotocol.io)
[![Works With](https://img.shields.io/badge/Works%20With-Claude%20%7C%20Cursor%20%7C%20Codex%20%7C%20Windsurf%20%7C%20Continue.dev-blue)](https://modelcontextprotocol.io)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-green)](https://python.org)
[![Powered by](https://img.shields.io/badge/Powered%20by-LibreCrawl-green)](https://github.com/PhialsBasement/LibreCrawl)

**librecrawl-mcp** is a self-hosted SEO crawler exposed as an MCP server. It wraps the open-source [LibreCrawl](https://github.com/PhialsBasement/LibreCrawl) engine and lets Claude Code, Claude Desktop, Cursor, OpenAI Codex, Windsurf, Continue.dev, or any MCP-compatible AI agent run a full technical SEO audit, schema validation, hreflang and canonical checks, sitemap reconciliation, content-quality scoring and broken-link sweeps on a live site — without copy-pasting CSVs and without hitting MCP client timeouts on large crawls.

---

## Why librecrawl-mcp

Three things this project does that no comparable open-source SEO MCP server does:

1. **Chunked-progressive crawling that never hits the MCP client timeout.** Every other SEO MCP server (SiteAudit MCP, AgentAEO, SE Ranking MCP) runs a synchronous audit and disconnects on sites over a few hundred pages. librecrawl-mcp runs the crawl in a background runner thread, persists progress to SQLite WAL, and returns immediately. Your agent polls a tiny status tool until the audit is done. Enterprise sites with 10,000+ pages work the same way as 50-page blogs.
2. **WAF / bot-block detection during the crawl.** Cloudflare, Akamai, DataDome, Imperva and PerimeterX challenge pages are fingerprinted in the response body and flagged as `bot_block_challenge_detected`. No other open-source SEO crawler does this, and audits without it silently misreport 403-blocked pages as "200 OK clean".
3. **Ephemeral by default + AIMD adaptive crawl-delay.** Once your client downloads the zip bundle, the server deletes the session row, all artifact files on disk, and the upstream crawl record in LibreCrawl's database. The local client is the only memory of any audited site. While crawling, an additive-increase / multiplicative-decrease controller tunes the per-request delay live from the target's p95 latency + 5xx rate — polite by construction, no rate-limit blow-ups.

---

## vs Screaming Frog, Sitebulb & Ahrefs Site Audit

| Capability | Screaming Frog Free | Screaming Frog Paid | Sitebulb | Ahrefs Site Audit | **librecrawl-mcp** |
|---|:---:|:---:|:---:|:---:|:---:|
| **Pages** | 500 limit | Unlimited (£199/yr) | Unlimited (£35/mo) | 50K (Ahrefs sub) | **Unlimited, self-hosted** |
| **Price** | Free (capped) | £199/yr | £35–£140/mo | $99–$999/mo | **Free + MIT** |
| **Runs in your AI assistant** | ❌ | ❌ | ❌ | ❌ | ✅ |
| **Chunked / background crawl** | ❌ | ❌ | ❌ | Cloud | ✅ |
| **Auto-adaptive crawl delay (AIMD)** | ❌ | ❌ | Manual | Hidden | ✅ |
| **Broken-link audit (4xx/5xx/timeout/DNS/SSL)** | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Redirect chains with destination** | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Title / meta / H1 audits + duplicates** | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Canonical chain depth + relative + → 3xx** | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Hreflang return-tag bidirectional graph** | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Hreflang full: self-ref · x-default · invalid codes · → noindex · → broken** | Partial | ✅ | ✅ | Partial | ✅ |
| **Sitemap cross-checks (noindex / canonicalised / 3xx / robots-disallow / 50k+ / 50MB+)** | Partial | ✅ | ✅ | Partial | ✅ |
| **Sitemap-orphan fill (URLs not internally linked)** | ❌ | ❌ | ❌ | ❌ | ✅ |
| **Schema.org validation (16 types, required fields + Google Rich Results)** | Partial | ✅ | ✅ | Partial | ✅ |
| **Soft-404 fingerprinting** | ❌ | ✅ | ✅ | ✅ | ✅ |
| **WAF / bot-block detection on 200-OK pages** | ❌ | ❌ | ❌ | ❌ | ✅ |
| **Mixed-content scanner (HTTPS → HTTP refs)** | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Security headers pack (HSTS · CSP · X-Frame · X-Content · Referrer-Policy · X-Robots-Tag)** | Partial | ✅ | ✅ | Partial | ✅ |
| **Image performance + CLS (lazy-load · srcset · width/height · WebP/AVIF)** | Partial | ✅ | ✅ | ✅ | ✅ |
| **Content quality (Flesch · AI-tell tokens · missing punct · boilerplate · lorem ipsum)** | ❌ | ❌ | Partial | ❌ | ✅ |
| **Crawl-budget traps (calendar future-year · session-id params · faceted explosion)** | Manual | ✅ | ✅ | ✅ | ✅ |
| **PDF report with brand footer** | ❌ | ❌ | ✅ | ❌ | ✅ |
| **Ephemeral by default (server forgets after download)** | N/A | N/A | N/A | N/A | ✅ |
| **GSC clicks/impressions merge with crawl data** | Paid add-on | Paid add-on | Paid add-on | Native | ✅ |
| **JS-render (DOM diff vs raw HTML)** | ✅ | ✅ | ✅ | Cloud | Out of scope |

---

## What it checks (50+ technical SEO checks)

Grouped by category. Every check has a stable name in the `.extended-checks.csv` sidecar and is documented in the checks manifest of each report.

### Security & headers
`missing_hsts_header` · `missing_csp_header` · `missing_x_frame_options` · `missing_x_content_type_options` · `missing_referrer_policy` · `x_robots_tag_vs_meta_mismatch` · `mixed_content` (HTTPS page → HTTP resources)

### Bot-block / WAF detection
`bot_block_challenge_detected` — fingerprints Cloudflare · Akamai · DataDome · Imperva · PerimeterX challenge pages served as 200-OK

### Sitemap & robots cross-checks
`sitemap_url_noindex` · `sitemap_url_3xx` · `sitemap_url_disallowed_in_robots` · `sitemap_contains_canonicalized` · `sitemap_over_50k_urls` · `sitemap_over_50mb`

### Hreflang full audit
`hreflang_missing_return_tag` · `hreflang_missing_self_reference` · `hreflang_missing_x_default` · `hreflang_invalid_codes` · `hreflang_to_noindex` · `hreflang_to_broken` · `hreflang_conflicts_lang_attr`

### Canonical health
`canonical_chain_depth` (>1 hop) · `canonical_to_relative` · `canonical_to_redirect` · `canonical_outside_head` · `bad_canonical` (→ broken)

### Redirects & soft redirects
`redirect_chains` (with full chain) · `meta_refresh_redirect` · `js_redirect` (window.location heuristic) · `http_refresh_redirect` (`Refresh:` header)

### Schema.org validation (16 types)
`schema_validation_errors` · `schema_rich_result_errors` · `schema_parse_errors` — validates Article · Product · Recipe · FAQPage · BreadcrumbList · Event · JobPosting · VideoObject · HowTo · Organization · LocalBusiness · Person · Review · AggregateRating · Course · NewsArticle against schema.org spec **and** Google Rich Results required fields. Handles `@graph` wrappers (Yoast / RankMath / WPRM)

### URL quality
`url_contains_space` · `url_multiple_slashes` · `url_non_ascii` · `url_underscores` · `url_repetitive_path` · `long_urls` (>115c) · `uppercase_urls` · `url_params_heavy`

### Anchor text
`non_descriptive_anchor_text` ("click here" / "read more" / etc.) · `empty_anchor_text` · `anchor_image_no_alt` · `broken_bookmarks` (`#fragment` → missing id)

### Internal linking patterns
`internal_nofollow_outlinks` · `nofollow_only_inbound` · `follow_and_nofollow_mixed` · `orphan_pages` (no inbound links)

### Image performance + CLS
`lazy_load_attr_missing` · `srcset_missing` · `image_dimensions_missing` (CLS risk) · `next_gen_image_format` (PNG/JPG → WebP/AVIF) · `image_oversized_kb` · `missing_alt_pages` · `broken_img_pages`

### HTML structure
`html_over_2mb` (Google parse limit) · `noscript_in_head` · `broken_or_invalid_html` · `dom_size_excessive` (>1500 nodes) · `lorem_ipsum_detected`

### Accessibility / metadata
`iframes_present` · `iframe_missing_title` · `missing_favicon` · `missing_html_lang` · `invalid_html_lang` · `missing_charset` · `missing_viewport`

### Crawl-budget killers
`spider_trap_calendar` (future-year params) · `url_session_id_high_entropy` (PHPSESSID / JSESSIONID) · `faceted_url_explosion` (parameter permutation cluster)

### Dev leaks
`outlinks_to_localhost` (`http://localhost` / `127.0.0.1` / RFC1918 hosts in production)

### Content quality
`low_readability` (Flesch reading ease) · `long_sentences` · `passive_voice_pct` · `missing_terminal_punctuation` · `double_space_count` · `smart_quote_mismatch_count` · `boilerplate_ratio` (5-word shingle overlap) · `ai_tell_tokens_found` (delve / unlock / seamlessly / leverage / em-dash density) · `has_lorem_ipsum`

### Site-level
`robots_txt_found` · `sitemap_found` · `https_redirect` · `www_redirect` · `noindex_pages` · `bad_canonical` → broken · `redirect_chains`

### External link audit
Every outbound URL is HEAD/GET-validated with a concurrent pool, classified into 17 status classes: `ok` · `ok_after_redirect` · `redirect` · `forbidden` · `not_found` · `gone` · `client_error_4xx` · `server_error_5xx` · `timeout` · `dns_error` · `connection_refused` · `network_unreachable` · `ssl_error` · `connection_failed` · `malformed_url` · `protocol_error` · `skipped`. Per-target output: final URL after redirects · redirect count · source pages · anchor text · response time · server header · last-modified.

---

## Install

### One-line installer (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/adityaarsharma/librecrawl-mcp/main/install.sh | bash
```

The installer asks three questions (target client, optional Google PageSpeed API key, optional GSC integration) and produces a ready-to-use MCP entry in your Claude / Cursor / Codex / Windsurf config.

### Compatible AI agents

Tested on Claude Code · Claude Desktop · Cursor · OpenAI Codex CLI · Windsurf · Continue.dev · any MCP-compatible client over stdio or streamable-HTTP transport.

### Manual install

Requires Python 3.10+, Docker (for the LibreCrawl backend), and one of: PM2, systemd, or a process manager of your choice.

```bash
git clone https://github.com/adityaarsharma/librecrawl-mcp.git
cd librecrawl-mcp
python3 -m venv venv && source venv/bin/activate
pip install httpx mcp weasyprint markdown fpdf2
# Start LibreCrawl backend on :5080 (see install.sh for Docker compose)
# Then start the MCP wrapper on :5081:
python server.py
```

Then add the MCP server to your client config. Example for Claude Desktop:

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

---

## Quick start — your first audit

Talk to your AI agent in plain English:

```
You: Audit https://example.com — full site, no caps, give me the zip.

Agent: [calls librecrawl_start_chunked_audit]
       [polls librecrawl_audit_status until done]
       [calls librecrawl_audit_zip with auto_cleanup=True]
       [saves the base64-decoded zip locally as example.com-<timestamp>.zip]

You:  Show me only the broken pages.
Agent: [unzips, reads per-page.csv, filters status_4xx OR status_5xx, prints table]
```

That's the workflow. The agent picks the right tools, the server forgets the site existed once the zip is downloaded, and the local zip is the only persistent record.

---

## Chunked-progressive crawling — how big sites work

For any audit on a site you don't already know is small (under 100 pages), use the **chunked** workflow. It returns a `session_id` in under 2 seconds and the crawl runs in a background worker thread.

```text
1. librecrawl_start_chunked_audit(url, total_max_pages=10000, chunk_target_pages=50, ...)
   → returns { session_id }  in < 2s

2. librecrawl_audit_status(session_id)
   → poll every 20-30s. Reads SQLite, doesn't hit upstream. Returns:
       status (queued / crawling / throttled / paused / done / cancelled / failed)
       pages_done · total_max_pages · current_delay_ms
       last 3 chunk metrics (p95 ms · err_rate)
       recent state-transition events
       ETA seconds
       artifacts_ready flag

3. librecrawl_audit_zip(session_id, auto_cleanup=True)
   → returns { content_base64, zip_path, sha256, files[], cleanup }
   → server wipes the session + on-disk artifacts + upstream crawl row
```

The AIMD controller tunes `crawlDelay` after each chunk: error rate > 10% → multiplicative decrease (halve size, double delay); p95 latency > 1.5× target → 1.5× delay increase; clean signals + p95 < 0.6× target → additive decrease. Respects `robots.txt` `Crawl-Delay` floor. PM2 restart mid-crawl re-queues the session and resumes from SQLite state.

---

## Reports — PDF + 7 sidecar CSVs

Every chunked audit produces a zip bundle with 8 files:

| File | Format | Purpose |
|---|---|---|
| `SUMMARY.txt` | Plain text | One-page orientation: site, session ID, pages, audit_complete flag, artifact list |
| `<domain>-<ts>.pdf` | PDF | Branded human-readable audit (WeasyPrint, A4, page numbers). Footer on every page: *Report Generated by LibreCrawl MCP — By Aditya Sharma · github.com/adityaarsharma/librecrawl-mcp* |
| `<domain>-<ts>.md` | Markdown | Source of truth for the PDF — grep-friendly |
| `<domain>-<ts>.per-page.csv` | CSV | One row per crawled URL × 30 columns (status, depth, word_count, title, meta, plus boolean column per check + `failed_checks_list`) |
| `<domain>-<ts>.sitemap-recon.csv` | CSV | Sitemap-vs-crawl diff: URLs in sitemap not crawled · URLs crawled not in sitemap · non-indexable sitemap entries |
| `<domain>-<ts>.external-links.csv` | CSV | Every outbound URL · HEAD-validated status code · redirect chain final URL · source pages · anchor text |
| `<domain>-<ts>.content-audit.csv` | CSV | Per-page readability + AI-tell + punctuation + boilerplate findings |
| `<domain>-<ts>.extended-checks.csv` | CSV | One row per (URL × check_name × severity × detail) finding across all 50+ check classes |

---

## GSC integration

Pull search-console data via your `gsc-posi` / `aditya-gsc` / native GSC MCP, then call:

```text
librecrawl_merge_gsc_data(crawl_id, gsc_data)
```

`gsc_data` is `{rows: [{url, clicks, impressions, ctr, position, top_query}, ...]}`. URLs are normalised (scheme + www + trailing-slash) before joining. Emits 4 extra CSVs:

- `.per-page-with-gsc.csv` — adds clicks/impressions/CTR/position/top_query to every per-page row
- `.gsc-winners.csv` — top 50 by clicks
- `.gsc-losers.csv` — high impressions + low CTR (<2%)
- `.gsc-quick-wins.csv` — position 11–20 + impressions ≥ 100

---

## Ephemeral mode & data handling

**Default behaviour:** the MCP wrapper keeps zero memory of audited sites once the client downloads the zip. After `librecrawl_audit_zip(session_id, auto_cleanup=True)` returns:

- Session row in `state.db` is deleted (every chunk + artifact + event row for the session)
- All artifact files on disk are unlinked
- The upstream LibreCrawl crawl record + its `crawled_urls` + `crawl_links` + `crawl_issues` rows are removed via direct SQLite delete (LibreCrawl's REST API doesn't expose DELETE)
- The zip file itself is unlinked from `REPORTS_DIR`
- Per-audit server footprint after cleanup: **0 bytes, 0 rows**

**Opt-out:** pass `auto_cleanup=False` to keep server-side state for inspection. Call `librecrawl_wipe_everything(confirm=True)` later to nuke everything.

**Nuclear reset:** `librecrawl_wipe_everything(confirm=True)` deletes every session row + every artifact file + every upstream crawl record. Used to return the MCP to a baseline of zero memory.

This matters when an agency is auditing client sites — no audit data persists where another operator could see it.

---

## Configuration reference

| Env var | Default | Purpose |
|---|---|---|
| `LIBRECRAWL_PORT` | `5080` | LibreCrawl backend port |
| `MCP_PORT` | `5081` | MCP wrapper port |
| `MCP_TRANSPORT` | `http` | `http` (streamable) or `stdio` |
| `REPORTS_DIR` | `~/librecrawl-reports` | Where audit artifacts land |
| `PAGESPEED_API_KEY` | unset | Optional — enables `librecrawl_pagespeed*` tools |
| `LIBRECRAWL_STATE_DB` | `~/librecrawl-state.db` | SQLite WAL state store for chunked audits |
| `LIBRECRAWL_UPSTREAM_DB` | `/home/.../webapps/librecrawl/data/users.db` | Direct path to upstream DB for ephemeral cleanup |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  MCP client (Claude Code / Desktop / Cursor / Codex …)      │
└────────────────────────────┬────────────────────────────────┘
                             │  streamable HTTP or stdio
                             ▼
┌─────────────────────────────────────────────────────────────┐
│  librecrawl-mcp wrapper  (server.py — FastMCP, 37 tools)    │
│  ┌─────────────────┐    ┌──────────────────────────────┐    │
│  │ runner.py       │    │ external_links / schema /    │    │
│  │ background      │    │ content_audit / extended_    │    │
│  │ worker thread   │    │ checks / sitemap_fill /      │    │
│  │ AIMD controller │    │ pdf_report                   │    │
│  └────────┬────────┘    └──────────────────────────────┘    │
│           │                                                  │
│  ┌────────▼────────┐    ┌──────────────────────────────┐    │
│  │ state.py        │    │ libreclient.py — typed       │    │
│  │ SQLite WAL      │    │ wrapper to upstream API      │    │
│  │ session state   │    └──────────────┬───────────────┘    │
│  └─────────────────┘                   │                    │
└─────────────────────────────────────────┼────────────────────┘
                                          │
                                          ▼
                          ┌──────────────────────────────┐
                          │  LibreCrawl Flask backend    │
                          │  :5080 — single-tenant       │
                          │  crawls + extracts SEO data  │
                          └──────────────────────────────┘
```

---

## Credits & upstream

- [LibreCrawl](https://github.com/PhialsBasement/LibreCrawl) — upstream crawler we wrap. MIT license. The hard work of HTML parsing, link extraction, robots.txt enforcement, depth-limited traversal lives here.
- [Anthropic Model Context Protocol](https://modelcontextprotocol.io) — the protocol this server speaks.
- [WeasyPrint](https://weasyprint.org/) — Markdown → HTML → PDF rendering.

---

## License

MIT. See [LICENSE](LICENSE).

---

*Report Generated by LibreCrawl MCP — [By Aditya Sharma](https://github.com/adityaarsharma) · github.com/adityaarsharma/librecrawl-mcp*

**Related search:** seo audit mcp · screaming frog alternative open source · self-hosted seo crawler · claude code seo audit · cursor seo mcp · codex seo audit · hreflang audit tool · canonical chain checker · broken link checker free · core web vitals audit cli · structured data validator command line · sitemap audit tool · WAF detection crawler · security headers checker · GSC integration crawler · soft 404 detection · chunked crawler no timeout MCP · technical SEO audit api
