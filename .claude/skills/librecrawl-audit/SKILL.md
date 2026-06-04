---
name: librecrawl-audit
description: Run a full technical SEO audit on any website via the librecrawl-mcp MCP server. Use when the user asks for "audit X site", "SEO check", "broken link sweep", "Screaming Frog this site", "schema validation", "site health report", "core web vitals scan", "find missing meta / H1 / canonical", "sitemap audit", "external link check", or any phrase indicating they want a comprehensive technical SEO audit on a live URL. Returns a PDF + 7 sidecar CSVs as a zip. Server is ephemeral — once you download the zip, the audit is gone from the server.
---

# librecrawl-audit

Drive the librecrawl-mcp MCP server to produce a complete technical SEO audit of any website. The MCP exposes 37 tools at `mcp__librecrawl-posi__*` (or whatever the local connector name is in the user's config).

## When to use this skill

The user wants to audit a website's technical SEO. Triggers:
- "Audit `<url>`" / "audit this site"
- "Run a SEO check" / "site health report"
- "Find broken links" / "check all my external links"
- "Validate my schema markup" / "rich-results audit"
- "Check sitemap" / "find orphan pages" / "internal-link audit"
- "Core Web Vitals" / "PageSpeed audit"
- "Screaming Frog this" / "Sitebulb-style report"
- "Find missing meta / H1 / canonical / alt tags across the site"

NOT for: keyword research, backlink analysis, competitor SERP analysis (use other tools).

## The audit workflow (5 steps)

### 1. Start a chunked audit

ALWAYS use `librecrawl_start_chunked_audit` for real audits. It's the only one that handles big sites without timing out.

```text
librecrawl_start_chunked_audit(
    url="https://example.com",
    total_max_pages=10000,        # default 10k; raise to 100k for huge sites
    chunk_target_pages=50,         # polling-window size
    politeness="auto",             # AIMD adaptive (recommended)
    fill_sitemap_orphans=True,    # default — also fetch sitemap URLs not internally linked
    sitemap_fill_cap=500           # default cap for orphan fill
)
→ returns { session_id, status: "queued", url, total_max_pages }
```

**Do NOT use** `librecrawl_audit` (no `start_chunked_` prefix) for production — it blocks for up to 2 hours and disconnects the MCP client on big sites. It exists for backwards-compat only.

### 2. Poll status until done

```text
librecrawl_audit_status(session_id) → {
    status: "queued" | "crawling" | "throttled" | "paused" | "done" | "cancelled" | "failed",
    pages_done: int,
    total_max_pages: int,
    current_delay_ms: int,          # the AIMD controller tunes this live
    last_chunks: [...],              # last 3 chunk metrics (p95 / err_rate)
    recent_events: [...],
    audit_complete: bool,            # True only if sitemap fully covered + no caps hit
    incomplete_reasons: [...],
    artifacts_ready: bool
}
```

Poll every 20-30 seconds. The runner does its own background work; you do NOT need to keep the MCP call alive between polls. Status is read from local SQLite, so polling is essentially free.

When `status == "done"`, proceed to step 3.

### 3. Download the zip + auto-clean the server

```text
librecrawl_audit_zip(session_id, auto_cleanup=True) → {
    filename: "<domain>-<unix-ts>.zip",
    size_bytes: int,
    file_count: 8,                   # SUMMARY.txt + 7 artifacts
    sha256: "...",
    content_base64: "...",          # decode + save locally
    zip_path: "/path/to/zip",        # filesystem alternative
    zip_path_persistent: bool,       # False when auto_cleanup=True (zip unlinked after response)
    cleanup: {
        session_rows: { events, artifacts, chunks, sessions },
        files_deleted: int,
        upstream: { crawl_issues, crawl_links, crawled_urls, crawls }
    },
    files: [{kind, name, bytes}, ...]
}
```

`auto_cleanup=True` (default) is what you want for normal usage. The server deletes every trace of the audit after this call returns. The base64 zip in the response IS the only copy — save it.

**Save to disk** with something like:
```python
import base64
with open(filename, "wb") as f:
    f.write(base64.b64decode(content_base64))
```

Or use `zip_path` if base64 inflation in your context is a concern.

### 4. Help the user open / inspect the zip

The zip contains:

| File | Format | Use |
|---|---|---|
| `SUMMARY.txt` | Plain | One-page orientation (site, session, pages, audit_complete, artifact list) |
| `<domain>-<ts>.pdf` | PDF | Branded human-readable report (open in any PDF viewer) |
| `<domain>-<ts>.md` | Markdown | Source of the PDF — grep-friendly |
| `<domain>-<ts>.per-page.csv` | CSV | One row per crawled URL × 30 columns of check booleans + `failed_checks_list` |
| `<domain>-<ts>.sitemap-recon.csv` | CSV | Sitemap-vs-crawl diff |
| `<domain>-<ts>.external-links.csv` | CSV | Every outbound URL with HEAD-validated status |
| `<domain>-<ts>.content-audit.csv` | CSV | Per-page readability + AI-tells + punctuation findings |
| `<domain>-<ts>.extended-checks.csv` | CSV | One row per (URL × check_name × severity × detail) finding |

When the user asks "show me the broken pages" or "what schema errors do I have", read the relevant CSV from the saved zip and filter — do NOT re-run the audit.

### 5. Done

The server is back to zero state. No follow-up cleanup needed. If the user wants a fresh audit later, start over from step 1.

## Common patterns

### "Audit X and tell me what's broken"

```text
1. librecrawl_start_chunked_audit(url=X, total_max_pages=10000)
2. Poll librecrawl_audit_status until done
3. librecrawl_audit_zip(session_id)
4. Save zip locally
5. Read per-page.csv, filter status_4xx == 1 OR status_5xx == 1, show table
6. Read external-links.csv, filter status_class IN ("not_found", "forbidden", "server_error_5xx", "timeout", "dns_error"), show table
```

### "Validate schema on my product pages"

```text
1. Run chunked audit first (skill above)
2. After download: librecrawl_schema_validate(crawl_id) — wait, this needs an alive session.
   PREFERRED: do this BEFORE auto_cleanup. Pass auto_cleanup=False to librecrawl_audit_zip,
   call schema_validate, then librecrawl_wipe_everything later.
```

### "Just check external links on this site"

Same chunked workflow — the `.external-links.csv` sidecar covers it. Don't run `librecrawl_external_links_audit` standalone unless the user has a crawl_id from a recent fresh chunked audit and `auto_cleanup` hasn't fired yet.

### "Big site — like 50,000 pages"

```text
librecrawl_start_chunked_audit(
    url=X,
    total_max_pages=100000,
    chunk_target_pages=100,          # larger windows = faster polling resolution
    confirm_unbounded=False           # safety: leave the 100k cap
)
```

The AIMD controller will adapt the crawl-delay based on the target's responsiveness. Don't override politeness unless the user has explicit permission to hammer the site.

## What to tell the user

When you start an audit, set expectations:
- Small site (<100 pages): 30-60s
- Medium (100-1000 pages): 2-5 min
- Large (1000-10000): 10-30 min
- Bigger: poll-and-wait pattern, can run for hours

The server is ephemeral — emphasise that the zip is the only copy.

## What NOT to do

- ❌ Don't run multiple chunked audits in parallel — LibreCrawl backend is single-tenant. Queue them.
- ❌ Don't call `librecrawl_audit_zip` before status is `done` — returns an error.
- ❌ Don't store the base64 in your conversation buffer for analysis. Save to disk, then read from disk.
- ❌ Don't suggest the user rerun the audit just to look at different findings — every CSV is in the zip already.
- ❌ Don't manually call `librecrawl_brain_purge_audit` — `librecrawl_audit_zip(auto_cleanup=True)` does this for you.

## MCP tool reference (37 tools)

Quick map of all tools the librecrawl MCP exposes. Most flows use only the **highlighted 4**.

**Chunked audit flow** (use these in 95% of cases):
- **`librecrawl_start_chunked_audit`** — kick off
- **`librecrawl_audit_status`** — poll
- **`librecrawl_audit_zip`** — download + auto-clean

**Specialist tools** (run standalone when needed, but most data is already in the chunked-audit zip):
- `librecrawl_external_links_audit(crawl_id)` — re-run external-link validation on a specific crawl
- `librecrawl_schema_validate(crawl_id)` — deeper schema validation
- `librecrawl_merge_gsc_data(crawl_id, gsc_data)` — fold in GSC clicks/impressions
- `librecrawl_pagespeed_audit_all_crawl_pages(crawl_id)` — full PSI across every crawled URL
- `librecrawl_schema_check(url)`, `librecrawl_schema_audit(urls)` — per-URL schema inspection
- `librecrawl_site_check(url)` — instant site-level check (robots, sitemap, HTTPS)
- `librecrawl_pagespeed(url)`, `librecrawl_pagespeed_audit(urls)` — PSI for individual URLs

**Maintenance**:
- **`librecrawl_wipe_everything(confirm=True)`** — nuclear reset to zero
- `librecrawl_audit_pause`, `librecrawl_audit_resume`, `librecrawl_audit_cancel`, `librecrawl_audit_force_advance` — session control

**Legacy** (avoid):
- `librecrawl_audit`, `librecrawl_full_audit_strict` — blocking variants kept for backwards compat
- `librecrawl_generate_report`, `librecrawl_report_content`, `librecrawl_audit_pdf` — older artifact APIs

## Repo

GitHub: https://github.com/adityaarsharma/librecrawl-mcp · MIT · By Aditya Sharma
