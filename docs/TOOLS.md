# Tools Reference

The server exposes **38 MCP tools**. In normal use you never call these by hand — your AI assistant picks them from your plain-language request. This reference is for understanding what's available and for power users scripting against the MCP directly.

> Each tool's exact input schema is advertised over MCP (your client can list it). Signatures below show the arguments you'll actually reach for; optional/advanced parameters are summarized in prose.

## Chunked audit — the 95% path

This is the modern flow: start → poll → zip. It never times out and survives client/process restarts.

| Tool | Signature | What it does |
|---|---|---|
| `librecrawl_start_chunked_audit` | `(url, total_max_pages=10000)` | Kicks off a full background audit. Returns a `session_id` in under 2 seconds. `total_max_pages=0` means unlimited. |
| `librecrawl_audit_status` | `(session_id)` | Poll this every ~25s. Reports `status` (queued/crawling/done), `pages_done`, `current_delay_ms`, chunk p95 latency, error rate, and `artifacts_ready`. |
| `librecrawl_audit_zip` | `(session_id, auto_cleanup=False)` | Returns the finished audit as a base64 zip (PDF + 7 CSVs) plus its sha256. Nothing is deleted yet unless `auto_cleanup=True`. |
| `librecrawl_audit_confirm_saved` | `(session_id, sha256)` | Call after saving the zip, with the hash of the saved bytes. On a match the server wipes the session, artifacts, zip and upstream crawl record; on a mismatch it deletes nothing. |
| `librecrawl_audit_pause` | `(session_id)` | Pause an in-progress crawl. |
| `librecrawl_audit_resume` | `(session_id)` | Resume a paused crawl. |
| `librecrawl_audit_cancel` | `(session_id)` | Stop and discard an in-progress audit. |
| `librecrawl_audit_force_advance` | `(session_id)` | Force the crawl past a stuck phase (e.g. finalize the current pages when discovery stalls). |
| `librecrawl_audit_artifacts` | `(session_id)` | List the artifact files produced, without downloading the zip. |
| `librecrawl_audit_pdf` | `(report_path, base_url="")` | Render a saved Markdown audit report as a branded PDF (WeasyPrint). |
| `librecrawl_report_content` | `(session_id)` | Return the Markdown report body for a finished audit. |

## Specialist

| Tool | Purpose |
|---|---|
| `librecrawl_external_links_audit` | Re-run outbound-link validation on a specific crawl (17 status classes). |
| `librecrawl_schema_validate` | Validate a block of structured data against schema.org + Google Rich Results. |
| `librecrawl_schema_check` | Quick schema presence/type check for a URL. |
| `librecrawl_schema_audit` | Full structured-data audit across a crawl (16 schema types, `@graph` aware). |
| `librecrawl_merge_gsc_data` | Merge Google Search Console rows into a crawl; emits GSC winners/losers/quick-wins CSVs. |
| `librecrawl_append_gsc_section` | Append a GSC analysis section to an existing report. |
| `librecrawl_pagespeed` | Single-URL PageSpeed Insights lookup (needs `PAGESPEED_API_KEY`). |
| `librecrawl_pagespeed_audit` | PSI audit for a set of URLs. |
| `librecrawl_pagespeed_audit_all_crawl_pages` | Run PSI across every page in a crawl. |
| `librecrawl_site_check` | Fast site-level sanity check (robots, sitemap, reachability). |
| `librecrawl_internal_links_analysis` | Internal linking graph — orphans, nofollow mix, inbound counts. |
| `librecrawl_filter_issues` | Filter a crawl's issues by type/severity. |
| `librecrawl_visualization_data` | Emit structured data for building crawl visualizations. |

## Maintenance

| Tool | Purpose |
|---|---|
| `librecrawl_wipe_everything` | Nuclear reset — clears all sessions, artifacts, and upstream crawl records back to zero state. |
| `librecrawl_brain_purge_audit` | Purge a single audit's data. |

## Legacy (kept for backwards compatibility)

Prefer the chunked-audit tools above — the legacy synchronous tools can time out on large sites.

`librecrawl_audit` · `librecrawl_full_audit_strict` · `librecrawl_generate_report` · `librecrawl_export_results` · `librecrawl_get_status` · `librecrawl_get_settings` · `librecrawl_list_crawls` · `librecrawl_start_crawl` · `librecrawl_stop_crawl` · `librecrawl_pause_crawl` · `librecrawl_resume_crawl` · `librecrawl_resume_from_crawl_id`
