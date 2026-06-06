<div align="center">

# 🕷️ librecrawl-mcp

### **Screaming Frog meets Claude.** A self-hosted SEO crawler exposed as an MCP server — runs full technical SEO audits on any website, straight from your AI assistant.

**Free · MIT · Unlimited pages · 50+ checks · Ephemeral by design**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge)](LICENSE)
[![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-orange?style=for-the-badge&logo=anthropic)](https://modelcontextprotocol.io)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![Latest Release](https://img.shields.io/github/v/release/adityaarsharma/librecrawl-mcp?style=for-the-badge&color=brightgreen)](https://github.com/adityaarsharma/librecrawl-mcp/releases)
[![GitHub stars](https://img.shields.io/github/stars/adityaarsharma/librecrawl-mcp?style=for-the-badge&color=yellow)](https://github.com/adityaarsharma/librecrawl-mcp/stargazers)

[![Works With](https://img.shields.io/badge/Claude%20Code-supported-D97757?style=flat-square)](https://docs.anthropic.com/claude-code)
[![Works With](https://img.shields.io/badge/Claude%20Desktop-supported-D97757?style=flat-square)](https://claude.ai/download)
[![Works With](https://img.shields.io/badge/Cursor-supported-000000?style=flat-square)](https://cursor.com)
[![Works With](https://img.shields.io/badge/OpenAI%20Codex-supported-10A37F?style=flat-square)](https://github.com/openai/codex)
[![Works With](https://img.shields.io/badge/Windsurf-supported-00C2A8?style=flat-square)](https://codeium.com/windsurf)
[![Works With](https://img.shields.io/badge/Continue.dev-supported-7C3AED?style=flat-square)](https://continue.dev)
[![Powered by LibreCrawl](https://img.shields.io/badge/Powered%20by-LibreCrawl-green?style=flat-square)](https://github.com/PhialsBasement/LibreCrawl)

**[⚡ Install in 60s](#-install-in-60-seconds) · [🎯 Why librecrawl-mcp](#-why-librecrawl-mcp) · [🆚 vs Screaming Frog](#-vs-screaming-frog-sitebulb--ahrefs) · [🚧 Roadmap](#-roadmap--out-of-scope) · [📖 Docs](#-quick-start--your-first-audit)**

</div>

---

## 🪄 30-second pitch

> *"Audit `acme.com` and tell me what's broken."*

That's the entire workflow. Your AI agent (Claude Code, Cursor, Codex, Windsurf, anything MCP-compatible) calls `librecrawl_start_chunked_audit`, polls until done, downloads a zip with a branded PDF + 7 CSVs covering **50+ technical SEO checks** — broken links, hreflang errors, schema validation, soft-404s, WAF detection, mixed content, security headers, image performance, content quality, GSC merge, and 40 more — then the server forgets everything. **No database, no SaaS, no $200/month**.

What you get:
- 🚀 **A real Screaming Frog replacement** — unlimited pages, zero seat licenses, MIT-licensed, runs on your own server or laptop
- 🤖 **Agent-native** — 37 MCP tools your AI calls directly; no copy-paste-CSV nonsense
- 🧠 **Background crawling that doesn't time out** — chunked-progressive engine, polls SQLite, survives PM2/MCP restarts mid-crawl
- 🛡️ **WAF + bot-block fingerprinting** — catches Cloudflare/Akamai/DataDome challenges that other crawlers misreport as 200-OK
- 🔥 **Ephemeral by default** — zip downloads, server wipes the session, the upstream DB record, and every artifact on disk; **zero remote footprint after every audit**
- 📄 **Branded PDF reports** — open-in-any-viewer, A4, page numbers, ready to hand a client

---

## 🎯 Why librecrawl-mcp

Three things this project does that no comparable open-source SEO MCP server does:

### 1. ⏱️ Chunked-progressive crawling that never hits the MCP client timeout

Every other SEO MCP server (SiteAudit MCP, AgentAEO, SE Ranking MCP) runs a **synchronous** audit and disconnects on sites over a few hundred pages. librecrawl-mcp runs the crawl in a **background runner thread**, persists progress to SQLite WAL, and returns a `session_id` in under 2 seconds. Your agent polls a tiny status tool until done. Enterprise sites with 10,000+ pages work the same way as 50-page blogs — and survive PM2 / MCP-client restarts mid-crawl.

### 2. 🛡️ WAF / bot-block detection during the crawl

Cloudflare, Akamai, DataDome, Imperva and PerimeterX challenge pages are fingerprinted in the response body and flagged as `bot_block_challenge_detected`. **No other open-source SEO crawler does this**, and audits without it silently misreport 403-blocked pages as "200 OK clean".

### 3. 🧹 Ephemeral by default + AIMD adaptive crawl-delay

Once your client downloads the zip, the server deletes the session row, every artifact file on disk, AND the upstream LibreCrawl crawl record. The local client is the only memory of any audited site — **the agency-safe default**. While crawling, an **additive-increase / multiplicative-decrease** controller tunes the per-request delay live from the target's p95 latency + 5xx rate. Polite by construction. No rate-limit blow-ups. No manual tuning.

---

## ⚡ Install in 60 seconds

```bash
curl -fsSL https://raw.githubusercontent.com/adityaarsharma/librecrawl-mcp/main/install.sh | bash
```

The installer asks three questions (target client, optional Google PageSpeed API key, optional GSC integration) and produces a ready-to-use MCP entry in your Claude / Cursor / Codex / Windsurf config. **Done.**

Then talk to your agent:

```
You:   Audit https://example.com end-to-end, give me the zip
Agent: → librecrawl_start_chunked_audit
       → polls librecrawl_audit_status until done
       → librecrawl_audit_zip (auto_cleanup=True)
       → saves example.com-1780572742.zip locally (320 KB · sha256 verified)
       → Server forgot the session. It's not stored anywhere remote.

You:   Show me only the broken pages and the broken external links
Agent: → unzips, reads per-page.csv + external-links.csv
       → prints filtered tables
```

That's the entire user experience.

<details>
<summary><strong>Manual install (Python 3.10+, Docker for LibreCrawl backend)</strong></summary>

```bash
git clone https://github.com/adityaarsharma/librecrawl-mcp.git
cd librecrawl-mcp
python3 -m venv venv && source venv/bin/activate
pip install httpx mcp weasyprint markdown fpdf2
# Start LibreCrawl backend on :5080 (see install.sh for Docker compose)
# Then start the MCP wrapper on :5081:
python server.py
```

Add the MCP server to your client config. Example for Claude Desktop:

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

</details>

---

## 🆚 vs Screaming Frog, Sitebulb & Ahrefs

The pricing reality of technical SEO in 2026:

| Capability | Screaming Frog Free | Screaming Frog Paid | Sitebulb | Ahrefs Site Audit | **librecrawl-mcp** |
|---|:---:|:---:|:---:|:---:|:---:|
| **Pages** | 500 cap | Unlimited (£199/yr) | Unlimited (£35/mo) | 50K (Ahrefs sub) | **♾️ Unlimited, self-hosted** |
| **Price** | Free (capped) | £199/yr | £35–£140/mo | $99–$999/mo | **🆓 Free + MIT** |
| **Runs inside your AI assistant** | ❌ | ❌ | ❌ | ❌ | ✅ |
| **Chunked / background crawl (no timeout)** | ❌ | ❌ | ❌ | Cloud only | ✅ |
| **Auto-adaptive crawl delay (AIMD)** | ❌ | ❌ | Manual | Hidden | ✅ |
| **WAF / bot-block detection on 200-OK pages** | ❌ | ❌ | ❌ | ❌ | ✅ |
| **Sitemap-orphan fill (URLs not internally linked)** | ❌ | ❌ | ❌ | ❌ | ✅ |
| **Ephemeral by default (zero server footprint)** | N/A | N/A | N/A | N/A | ✅ |
| Broken-link audit (4xx/5xx/timeout/DNS/SSL) | ✅ | ✅ | ✅ | ✅ | ✅ |
| Redirect chains with destination | ✅ | ✅ | ✅ | ✅ | ✅ |
| Title / meta / H1 audits + duplicates | ✅ | ✅ | ✅ | ✅ | ✅ |
| Canonical chain depth + relative + → 3xx | ✅ | ✅ | ✅ | ✅ | ✅ |
| Hreflang return-tag bidirectional graph | ✅ | ✅ | ✅ | ✅ | ✅ |
| Hreflang full (self-ref · x-default · invalid codes · → noindex · → broken) | Partial | ✅ | ✅ | Partial | ✅ |
| Sitemap cross-checks (noindex · canonicalised · 3xx · robots-disallow · 50k+ · 50MB+) | Partial | ✅ | ✅ | Partial | ✅ |
| Schema.org validation (16 types · Rich Results) | Partial | ✅ | ✅ | Partial | ✅ |
| Soft-404 fingerprinting | ❌ | ✅ | ✅ | ✅ | ✅ |
| Mixed-content scanner (HTTPS → HTTP refs) | ✅ | ✅ | ✅ | ✅ | ✅ |
| Security headers pack (HSTS · CSP · X-Frame · X-Content · Referrer-Policy · X-Robots-Tag) | Partial | ✅ | ✅ | Partial | ✅ |
| Image performance + CLS (lazy-load · srcset · width/height · WebP/AVIF) | Partial | ✅ | ✅ | ✅ | ✅ |
| Content quality (Flesch · AI-tells · missing punct · boilerplate · lorem ipsum) | ❌ | ❌ | Partial | ❌ | ✅ |
| Crawl-budget traps (calendar future-year · session-id params · faceted explosion) | Manual | ✅ | ✅ | ✅ | ✅ |
| Branded PDF report | ❌ | ❌ | ✅ | ❌ | ✅ |
| GSC clicks/impressions merge with crawl data | Paid add-on | Paid add-on | Paid add-on | Native | ✅ |
| JS-render (DOM diff vs raw HTML) | ✅ | ✅ | ✅ | Cloud | 🚧 [Out of scope — see roadmap](#-roadmap--out-of-scope) |

**Bottom line:** every paid feature you actually use on Screaming Frog and Sitebulb is here, free, and your AI agent drives it.

---

## 🚀 What it does today (the "Powerful" column)

50+ technical SEO checks, all checked by every chunked audit, all surfaced in CSV + Markdown + PDF.

<table>
<tr>
<td valign="top" width="50%">

#### 🔒 Security & headers
- `missing_hsts_header`
- `missing_csp_header`
- `missing_x_frame_options`
- `missing_x_content_type_options`
- `missing_referrer_policy`
- `x_robots_tag_vs_meta_mismatch`
- `mixed_content` (HTTPS → HTTP refs)

#### 🛡️ Bot-block / WAF detection
- `bot_block_challenge_detected` — fingerprints **Cloudflare · Akamai · DataDome · Imperva · PerimeterX** challenge pages served as 200-OK

#### 🗺️ Sitemap & robots cross-checks
- `sitemap_url_noindex`
- `sitemap_url_3xx`
- `sitemap_url_disallowed_in_robots`
- `sitemap_contains_canonicalized`
- `sitemap_over_50k_urls`
- `sitemap_over_50mb`

#### 🌍 Hreflang full audit
- `hreflang_missing_return_tag`
- `hreflang_missing_self_reference`
- `hreflang_missing_x_default`
- `hreflang_invalid_codes`
- `hreflang_to_noindex`
- `hreflang_to_broken`
- `hreflang_conflicts_lang_attr`

#### 🔗 Canonical health
- `canonical_chain_depth` (>1 hop)
- `canonical_to_relative`
- `canonical_to_redirect`
- `canonical_outside_head`
- `bad_canonical` (→ broken)

#### 🔁 Redirects & soft redirects
- `redirect_chains` (full chain)
- `meta_refresh_redirect`
- `js_redirect`
- `http_refresh_redirect`

#### 🏷️ Schema.org validation (16 types)
- `schema_validation_errors`
- `schema_rich_result_errors`
- `schema_parse_errors`
- Article · Product · Recipe · FAQPage · BreadcrumbList · Event · JobPosting · VideoObject · HowTo · Organization · LocalBusiness · Person · Review · AggregateRating · Course · NewsArticle
- Validates against **schema.org spec** AND **Google Rich Results** required fields
- Handles `@graph` wrappers (Yoast / RankMath / WPRM)

</td>
<td valign="top" width="50%">

#### 🔤 URL quality
- `url_contains_space`
- `url_multiple_slashes`
- `url_non_ascii`
- `url_underscores`
- `url_repetitive_path`
- `long_urls` (>115c)
- `uppercase_urls`
- `url_params_heavy`

#### ⚓ Anchor text
- `non_descriptive_anchor_text` ("click here" / "read more")
- `empty_anchor_text`
- `anchor_image_no_alt`
- `broken_bookmarks` (`#fragment` → missing id)

#### 🕸️ Internal linking
- `internal_nofollow_outlinks`
- `nofollow_only_inbound`
- `follow_and_nofollow_mixed`
- `orphan_pages`

#### 🖼️ Image performance + CLS
- `lazy_load_attr_missing`
- `srcset_missing`
- `image_dimensions_missing` (CLS risk)
- `next_gen_image_format` (PNG/JPG → WebP/AVIF)
- `image_oversized_kb`
- `missing_alt_pages`
- `broken_img_pages`

#### 📐 HTML structure
- `html_over_2mb` (Google parse limit)
- `noscript_in_head`
- `broken_or_invalid_html`
- `dom_size_excessive` (>1500 nodes)
- `lorem_ipsum_detected`

#### ♿ Accessibility / metadata
- `iframes_present`
- `iframe_missing_title`
- `missing_favicon`
- `missing_html_lang` / `invalid_html_lang`
- `missing_charset` / `missing_viewport`

#### 🪤 Crawl-budget killers
- `spider_trap_calendar` (future-year params)
- `url_session_id_high_entropy` (PHPSESSID / JSESSIONID)
- `faceted_url_explosion`

#### ✍️ Content quality
- `low_readability` (Flesch reading ease)
- `long_sentences`
- `passive_voice_pct`
- `missing_terminal_punctuation`
- `double_space_count`
- `smart_quote_mismatch_count`
- `boilerplate_ratio` (5-word shingle overlap)
- `ai_tell_tokens_found` (*delve · unlock · seamlessly · leverage · em-dash density*)

#### 🚨 Dev leaks
- `outlinks_to_localhost` (RFC1918 hosts in production)

</td>
</tr>
</table>

#### 🔗 External link audit (every outbound URL, validated)

Every outbound URL is HEAD/GET-validated with a concurrent pool, classified into **17 status classes**: `ok` · `ok_after_redirect` · `redirect` · `forbidden` · `not_found` · `gone` · `client_error_4xx` · `server_error_5xx` · `timeout` · `dns_error` · `connection_refused` · `network_unreachable` · `ssl_error` · `connection_failed` · `malformed_url` · `protocol_error` · `skipped`. Per-target output: final URL after redirects · redirect count · source pages · anchor text · response time · server header · last-modified.

#### 📈 GSC clicks/impressions merge

Pull search-console data from `gsc-posi` / `aditya-gsc` / native GSC MCP, then call `librecrawl_merge_gsc_data(crawl_id, gsc_data)`. URLs normalised (scheme + www + trailing-slash) before joining. Emits **4 extra CSVs**: `.per-page-with-gsc.csv` · `.gsc-winners.csv` (top 50 by clicks) · `.gsc-losers.csv` (high impr + low CTR <2%) · `.gsc-quick-wins.csv` (position 11–20 + impressions ≥100).

---

## 🚧 Roadmap & "Out of Scope"

Honesty matters more than feature theatre. Here's what's **on the way**, what's **explicitly out of scope**, and why.

### 🛠️ Coming soon (planned)

| Feature | Status | Why it matters |
|---|:---:|---|
| **JavaScript rendering** (Playwright headless, DOM diff vs raw HTML) | 🟡 Designed, not built | Catches SPA / React / Vue / Next.js apps where the raw HTML is empty. Will be opt-in (`render_js=True`) because it 10× the crawl time. |
| **Core Web Vitals from CrUX (real-user data)** | 🟡 Designed | Today: `librecrawl_pagespeed` calls PageSpeed Insights (lab data). Adding the CrUX API for real-user 28-day field data. |
| **Lighthouse-style accessibility audit (axe-core)** | 🟡 Planned | A11y findings (contrast, ARIA, focus order, alt-text quality) bolted on top of the existing crawl. |
| **PDF report theming** (white-label per client) | 🟡 Planned | Right now the PDF footer is fixed. Add a `--brand-config` flag for agency white-labelling. |
| **Multi-tenant mode** (one server, multiple operators) | 🟡 Researching | Today the upstream LibreCrawl is single-tenant. Multi-tenant needs request isolation, per-operator API keys, and rate-limit fairness. |
| **Slack / Discord webhook on audit completion** | 🟡 Planned | Long crawls run unattended; ping when ready. |
| **Diff mode (audit A vs audit B)** | 🟡 Planned | Compare two audits of the same site over time. "What regressed since last week?" |
| **CrUX-by-URL pattern** (group findings by template) | 🟡 Planned | "All /blog/* pages have CLS > 0.25" instead of one row per URL. |

### 🛑 Out of scope (deliberate)

| Feature | Why not |
|---|---|
| **Keyword research / SERP scraping** | Not what this tool is for. Use DataForSEO MCP, Ahrefs MCP, or SE Ranking MCP. |
| **Backlink analysis** | Same — different data source. Use Ahrefs / Semrush MCP. |
| **Competitor analysis / SERP rank tracking** | Out of scope. This is a *technical* SEO crawler, not a SERP intelligence tool. |
| **Built-in dashboards / web UI** | Not the point. Reports are the deliverable. The web UI for ad-hoc crawling already exists in upstream [LibreCrawl](https://github.com/PhialsBasement/LibreCrawl). |
| **SaaS hosted version** | The whole point is *self-hosted, ephemeral*. If you want a SaaS, use Ahrefs or Sitebulb Cloud. |
| **Built-in fix suggestions / GPT-rewritten meta tags** | Your AI agent already does this — it has the audit data, let it reason. Baking LLM rewrites into the crawler bloats the tool. |
| **Authentication crawling** (logged-in member areas) | Adds session-management complexity that 95% of users don't need. Use Screaming Frog if you must. |
| **JavaScript engine reverse-engineering** (decoding bot challenges) | Not a Cloudflare-bypass tool. We **detect** WAF challenges and flag them; we don't try to defeat them. |
| **Heavy infra requirements** (Redis, Postgres, K8s) | SQLite WAL is enough. If you need bigger, fork it. |

### 💡 Want something here?

[Open an issue](https://github.com/adityaarsharma/librecrawl-mcp/issues/new) with the use case. Most of the "coming soon" list came from real Reddit / r/SEO / community asks. If you want it badly enough to PR it, even better — the codebase is **~5K lines of clean Python** with no magic.

---

## 📊 Reports — what comes out of every audit

Every chunked audit produces a zip with **8 files**:

| File | Format | Purpose |
|---|---|---|
| `SUMMARY.txt` | Plain text | One-page orientation: site, session ID, pages, audit_complete flag, artifact list |
| `<domain>-<ts>.pdf` | PDF | Branded human-readable audit (WeasyPrint, A4, page numbers). Footer on every page: *Report Generated by LibreCrawl MCP — By Aditya Sharma · github.com/adityaarsharma/librecrawl-mcp* |
| `<domain>-<ts>.md` | Markdown | Source of truth for the PDF — grep-friendly |
| `<domain>-<ts>.per-page.csv` | CSV | 1 row per URL × 30 columns (status, depth, word_count, title, meta, boolean per check + `failed_checks_list`) |
| `<domain>-<ts>.sitemap-recon.csv` | CSV | Sitemap-vs-crawl diff |
| `<domain>-<ts>.external-links.csv` | CSV | Every outbound URL + HEAD-validated status + redirect chain final URL + source pages + anchor text |
| `<domain>-<ts>.content-audit.csv` | CSV | Per-page readability + AI-tells + punctuation + boilerplate |
| `<domain>-<ts>.extended-checks.csv` | CSV | 1 row per (URL × check_name × severity × detail) across all 50+ checks |

---

## 🔄 Quick start — your first audit

```text
You:   Audit https://example.com — full site, no caps, give me the zip

Agent: → librecrawl_start_chunked_audit(url=..., total_max_pages=10000)
         → returns session_id in <2s
       → polls librecrawl_audit_status every 25s
         → status: crawling, pages_done: 47, current_delay_ms: 250
         → status: crawling, pages_done: 312, last chunk p95: 480ms, err_rate: 0%
         → status: done, pages_done: 534, artifacts_ready: true
       → librecrawl_audit_zip(session_id, auto_cleanup=True)
         → returns base64 zip (8 files, 320 KB)
         → SAVES LOCALLY as example.com-1780572742.zip
         → Server wiped: session_rows=4, files=8, upstream_crawl=1

You:   Show me only the broken pages and the broken external links

Agent: → unzips, reads per-page.csv, filters status_4xx == 1 OR status_5xx == 1
       → prints table
       → reads external-links.csv, filters status_class IN (not_found, forbidden,
         server_error_5xx, timeout, dns_error)
       → prints table
```

That's the whole workflow. **The local zip is the only copy.** The server is back to zero state.

---

## ⚙️ Chunked-progressive crawling (how big sites work)

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

The **AIMD controller** tunes `crawlDelay` after each chunk:
- error rate > 10% → multiplicative decrease (halve size, double delay)
- p95 latency > 1.5× target → 1.5× delay increase
- clean signals + p95 < 0.6× target → additive decrease
- respects `robots.txt` `Crawl-Delay` floor
- **PM2 restart mid-crawl** re-queues the session and resumes from SQLite state

---

## 🧹 Ephemeral mode (privacy by default)

After `librecrawl_audit_zip(session_id, auto_cleanup=True)` returns:

- ✅ Session row in `state.db` deleted (every chunk + artifact + event row)
- ✅ All artifact files on disk unlinked
- ✅ Upstream LibreCrawl crawl record + its `crawled_urls` + `crawl_links` + `crawl_issues` rows removed
- ✅ Zip file unlinked from `REPORTS_DIR`
- ✅ **Per-audit server footprint after cleanup: 0 bytes, 0 rows**

**Opt-out:** `auto_cleanup=False` keeps server-side state for inspection. Call `librecrawl_wipe_everything(confirm=True)` later.

**Nuclear reset:** `librecrawl_wipe_everything(confirm=True)` deletes every session + artifact + upstream crawl record. Returns the MCP to zero memory.

This is **the agency-safe default** — no audit data persists where another operator could see it.

---

## ⚙️ Configuration reference

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

## 🏗️ Architecture

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

## 🛠️ The 37 MCP tools (full surface area)

<details>
<summary><strong>Click to expand the full tool reference</strong></summary>

**Chunked audit (use these for 95% of work):**
- `librecrawl_start_chunked_audit` · `librecrawl_audit_status` · `librecrawl_audit_zip`
- `librecrawl_audit_pause` · `librecrawl_audit_resume` · `librecrawl_audit_cancel` · `librecrawl_audit_force_advance`
- `librecrawl_audit_artifacts` · `librecrawl_audit_pdf` · `librecrawl_report_content`

**Specialist:**
- `librecrawl_external_links_audit` — re-run external-link validation on a specific crawl
- `librecrawl_schema_validate` · `librecrawl_schema_check` · `librecrawl_schema_audit` — schema inspection
- `librecrawl_merge_gsc_data` · `librecrawl_append_gsc_section` — fold in GSC clicks/impressions
- `librecrawl_pagespeed` · `librecrawl_pagespeed_audit` · `librecrawl_pagespeed_audit_all_crawl_pages` — PSI for individual URLs or whole crawl
- `librecrawl_site_check` — instant site-level check (robots, sitemap, HTTPS)
- `librecrawl_internal_links_analysis` · `librecrawl_filter_issues` · `librecrawl_visualization_data`

**Maintenance:**
- `librecrawl_wipe_everything` — nuclear reset to zero
- `librecrawl_brain_purge_audit` — purge a single audit

**Legacy (avoid for production, kept for backwards compat):**
- `librecrawl_audit` · `librecrawl_full_audit_strict` — blocking variants (will time out on big sites)
- `librecrawl_generate_report` · `librecrawl_export_results` · `librecrawl_get_status` · `librecrawl_get_settings` · `librecrawl_list_crawls` · `librecrawl_start_crawl` · `librecrawl_stop_crawl` · `librecrawl_pause_crawl` · `librecrawl_resume_crawl` · `librecrawl_resume_from_crawl_id`

</details>

---

## 📜 License

**MIT.** Use it for client work, agency work, internal tools, SaaS, anything. No attribution required (but appreciated). See [LICENSE](LICENSE).

---

## 🙏 Credits & upstream

- **[LibreCrawl](https://github.com/PhialsBasement/LibreCrawl)** — upstream crawler we wrap. MIT. The hard work of HTML parsing, link extraction, robots.txt enforcement, depth-limited traversal lives here. Go star them.
- **[Anthropic Model Context Protocol](https://modelcontextprotocol.io)** — the protocol this server speaks
- **[WeasyPrint](https://weasyprint.org/)** — Markdown → HTML → PDF rendering
- **[FastMCP](https://github.com/jlowin/fastmcp)** — the Python MCP server framework

---

## ⭐ If this helps, star it

Stars actually move this up the GitHub MCP-server search. The more stars, the more SEO and dev folks find it instead of paying $999/mo for Ahrefs Site Audit. [⭐ Star librecrawl-mcp](https://github.com/adityaarsharma/librecrawl-mcp/stargazers).

[![Star History Chart](https://api.star-history.com/svg?repos=adityaarsharma/librecrawl-mcp&type=Date)](https://star-history.com/#adityaarsharma/librecrawl-mcp&Date)

---

<div align="center">

### Built by [Aditya Sharma](https://adityaarsharma.com) · MIT · No telemetry · No SaaS · No vendor lock-in

*If you ship something cool with librecrawl-mcp, [say hi](https://twitter.com/adityaarsharma).*

</div>

---

<sub>

**Keywords for search engines:** seo audit mcp server · screaming frog alternative open source · self-hosted seo crawler · claude code seo audit · cursor seo mcp · openai codex seo audit · windsurf seo crawler · continue.dev seo mcp · technical seo audit mcp · hreflang audit tool free · canonical chain checker · broken link checker free unlimited · core web vitals audit cli · structured data validator command line · schema.org rich results validator · sitemap audit tool · sitemap orphan detection · WAF detection crawler · cloudflare challenge detector · security headers checker · CSP HSTS audit · GSC integration crawler · soft 404 detection · chunked crawler no timeout MCP · technical SEO audit api · python seo crawler · self-hosted screaming frog · open source sitebulb alternative · ahrefs site audit alternative free · seo crawler for claude · seo crawler for cursor · model context protocol seo · ai assistant seo audit · ephemeral seo audit · agency-safe seo crawler · white-label seo report · pdf seo report generator · seo audit cli tool · mit-licensed seo crawler

</sub>
