# Changelog

All notable changes to **librecrawl-mcp** are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning follows [SemVer](https://semver.org/).

---

## [2.0.0] — 2026-06-04

Finalisation release. No new features; this version marks the end of the v1.x build-out and stamps the project as **feature-complete for technical SEO auditing**. README rewritten for clarity + discoverability, a reusable Claude Code skill ships under `.claude/skills/`, repo cleanup, comprehensive smoke test on a real production domain. The audit toolchain remains the v1.9.1 set — 37 MCP tools, 50+ technical checks, ephemeral mode, branded PDF + 5 CSV sidecars per audit, sitemap-orphan fill, AIMD adaptive crawler.

### Why this is 2.0

The version arc from 1.0 → 1.9 added one major capability per minor release. v2.0 marks parity with paid Screaming-Frog-class audit tools across every non-JS-render dimension and the ephemeral-by-default privacy model. There is no v2.1 planned — additional checks would land as 2.x.

### Documentation

- README.md fully rewritten for SEO + clarity: H1/H2 hierarchy that hits the primary keyword cluster in the first 200 words, "vs Screaming Frog / Ahrefs / Sitebulb" feature matrix, install in one block, usage in five examples, ephemeral-mode workflow up front.
- Planning artifacts (HANDOFF.md, SPEC-v2.md) moved to docs/.
- .gitignore added.

### Smoke test (release gate)

Full audit on a real production domain with zero caps. All 7 sidecars emitted, every check class fired or correctly absent, zip download flow round-tripped, server returned to zero-memory baseline after the client downloaded the bundle.

---

## [1.9.1] — 2026-06-04

Polish from Aditya's v1.9.0 review.

### Fixed

- `librecrawl_audit_zip` `file_count` now matches the zip's namelist (was off-by-one — `SUMMARY.txt` wasn't counted).
- External-link status taxonomy: generic `connect_error` catch-all removed. `httpx.ConnectError` now maps to specific subtypes: `dns_error` / `connection_refused` / `network_unreachable` / `ssl_error` / `timeout` / `connection_failed`.
- External-link audit return now surfaces `unique_targets_found`, `validated_count`, `skipped_total`, `skipped_by_reason` (e.g. `{scheme_mailto: 12, no_host: 3}`) + `skipped_examples`. Old `skipped_non_http` field kept as alias.
- `librecrawl_audit_zip` returns `zip_path` alongside `content_base64`. Zip always written to `REPORTS_DIR`; auto-cleanup unlinks it after the response, opt-out persists it for filesystem retrieval.

### Changed

- `content_audit.audit_content` + `extended_checks.run_extended_checks` default `limit` bumped 50 → 250. `cap_applied` flag stays on the return.

---

## [1.9.0] — 2026-06-03

Ephemeral mode. The MCP retains zero memory of audited sites by default.

### Added

- **`librecrawl_audit_zip(session_id, auto_cleanup=True)`** — packages all 7 artifacts + `SUMMARY.txt` into a single zip, returns inline as base64, and (default) wipes session row + on-disk artifacts + upstream LibreCrawl crawl record.
- **`librecrawl_wipe_everything(confirm=True)`** — nuclear option: every session, every artifact file, every upstream crawl. Returns to zero-memory baseline.
- Direct SQLite cleanup of upstream LibreCrawl's `users.db` (`crawls` + `crawled_urls` + `crawl_links` + `crawl_issues` tables). Configurable via `LIBRECRAWL_UPSTREAM_DB` env var. Never touches the `users` table.

### Fixed

- `state.delete_session` column-name bug: `sessions.id` is PK; only FK tables use `session_id`.

---

## [1.8.0] — 2026-06-03

Tier 2 — 30+ Screaming-Frog-tier technical checks added.

### Added

- **Sitemap spec**: `sitemap_over_50k_urls`, `sitemap_over_50mb`, `sitemap_contains_canonicalized`.
- **Hreflang full audit**: `missing_self_reference`, `missing_x_default`, `invalid_codes`, `to_noindex`, `to_broken`, `conflicts_lang_attr` (extends v1.6 return-tag check).
- **Canonical health**: `canonical_to_relative`, `canonical_to_redirect`.
- **Internal nofollow patterns**: `internal_nofollow_outlinks`, `nofollow_only_inbound`, `follow_and_nofollow_mixed`.
- **Image perf + CLS**: `lazy_load_attr_missing`, `srcset_missing`, `image_dimensions_missing`, `next_gen_image_format`, `anchor_image_no_alt`.
- **HTML structure**: `html_over_2mb`, `noscript_in_head`, `broken_or_invalid_html`, `dom_size_excessive`, `canonical_outside_head`.
- **Accessibility / metadata**: `iframes_present`, `iframe_missing_title`, `missing_favicon`.
- **Crawl-budget killers**: `spider_trap_calendar`, `url_session_id_high_entropy`, `faceted_url_explosion`.
- **Dev leaks**: `outlinks_to_localhost`.

JS-render delta + AMP + per-URL screenshots + mobile/desktop diff explicitly REMOVED from the roadmap (no Playwright dependency).

---

## [1.7.0] — 2026-06-03

Tier 1 "fix broken" checks.

### Added

- Redirects section in the MD report now shows source → destination → hops (was URL list only).
- `meta_refresh_redirect`, `js_redirect`, `http_refresh_redirect` — soft-redirect detection at the HTML + JS + header layer.
- `bot_block_challenge_detected` — Cloudflare / Akamai / DataDome / Imperva / PerimeterX fingerprints on 200-OK challenge pages.
- `broken_bookmarks` — `<a href="#x">` vs `id="x"` diff on same page.

---

## [1.6.2] — 2026-06-03

### Added

- `sitemap_fill.py` `_SEOExtractor` now captures every `<a href>` in the page body. Sitemap-filled pages contribute first-class data to the inbound-link graph.
- `_build_report` walks BOTH the LibreCrawl flat links list AND each page's `links_detailed` (was if/else). Each page's `linked_from` is augmented from the unified inbound map.

### Removed

- The `source != "sitemap_fill"` exclusion in the orphan check. Data is real now — orphan detection applies uniformly.

---

## [1.6.1] — 2026-06-03

### Fixed

- False-positive orphan flagging on sitemap-filled pages (their `linked_from` was `[]` because we only fetched them, didn't crawl from them). Workaround until v1.6.2 ships the proper fix.
- Coverage warning banner at the top of the MD report when `audit_complete=False`. Surfaces sitemap_total / sitemap_only_count / sitemap_coverage_pct + incomplete_reasons so the Summary scorecard isn't mistaken for site-wide truth.

---

## [1.6.0] — 2026-06-03

Sitemap-orphan fill — closes coverage gap from LibreCrawl's `maxDepth` + internal-link traversal model.

### Added

- `sitemap_fill.py` — concurrent lightweight HTTP fetch on URLs in the sitemap but not reachable via internal links. Each fetched URL parsed into a LibreCrawl-export-shaped page dict (title / meta / H1 / canonical / robots / viewport / lang / og / images / json-ld / word count / status code) and appended to the pages list BEFORE `_build_report` runs.
- New `librecrawl_start_chunked_audit` params: `fill_sitemap_orphans: bool = True`, `sitemap_fill_cap: int = 500`.

### Fixed

- Sitemap reconciliation recomputed AFTER fill — `.sitemap-recon.csv` + `completeness.sitemap_only_count` reflect actual coverage.

---

## [1.5.1] — 2026-06-03

### Fixed

- `audit_complete` was hardcoded `True` on the runner's success path. Now derived from sitemap coverage + max_pages_hit + timeout_hit. HARD RULE: `if sitemap_total > crawl_total and sitemap_only_count > 0, audit_complete MUST be False with incomplete_reasons explainer`.
- Completeness block gains 3 fields: `sitemap_total`, `sitemap_only_count`, `sitemap_coverage_pct`. DB row's `audit_complete` + `incomplete_reasons` columns now reflect the computed truth.

---

## [1.5.0] — 2026-06-03

The "deliverable" release. Branded PDF, content-quality audit, 30+ extended SEO checks, GSC clicks/impressions integration, and schema.org + Google Rich-Results validation.

### Added

- **PDF reports** — `librecrawl_audit_pdf(report_path, base_url="")` renders any saved Markdown audit as a branded PDF via WeasyPrint. Footer on every page: *Report Generated by LibreCrawl MCP — By Aditya Sharma · github.com/adityaarsharma/librecrawl-mcp*. Auto-emitted as a `.pdf` sidecar on every chunked audit.
- **Content audit** (`content_audit.py`, auto-wired) — Flesch reading-ease, avg sentence length, passive-voice ratio, missing terminal punctuation, double spaces, smart-quote mismatches, AI-tell token detection (`delve` / `unlock` / `seamlessly` / em-dash density), lorem-ipsum detection, boilerplate ratio via 5-word shingle overlap across the site. Writes `.content-audit.csv` (50-page cap by default to stay polite).
- **Extended SEO checks** (`extended_checks.py`, auto-wired) — 30+ Screaming-Frog-tier checks:
  - **Security headers**: missing HSTS / CSP / X-Frame-Options / X-Content-Type-Options / Referrer-Policy; X-Robots-Tag header parsing
  - **Mixed content**: HTTPS pages loading HTTP `src` / `href`
  - **Hreflang return-tag**: bidirectional graph check (A→B but B doesn't link A flagged)
  - **Sitemap cross-checks**: sitemap URLs that are noindex / canonicalised away / 3xx / robots.txt-disallowed
  - **Soft-404 fingerprinting**: HTTP 200 + "not found" body phrase + thin content
  - **Canonical chain**: depth > 1 detection
  - **URL quality**: spaces, multiple slashes, non-ASCII chars, underscores, repetitive path segments
  - **Anchor text quality**: non-descriptive ("click here" / "read more"), empty
- **GSC merge** — `librecrawl_merge_gsc_data(crawl_id, gsc_data)`. Accepts rows from the `gsc-posi` MCP, normalises URLs, joins against the crawl. Emits 4 CSV sidecars: `.per-page-with-gsc.csv`, `.gsc-winners.csv` (top 50 by clicks), `.gsc-losers.csv` (high impressions / low CTR), `.gsc-quick-wins.csv` (position 11–20).
- **Schema validation** — `librecrawl_schema_validate(crawl_id)`. Required-fields tables for 16 schema types (Article / Product / Recipe / FAQPage / BreadcrumbList / Event / JobPosting / VideoObject / HowTo / Organization / LocalBusiness / Person / Review / AggregateRating / Course / NewsArticle). Validates against schema.org spec AND Google Rich Results required fields. Handles `@graph` wrappers (Yoast / RankMath / WPRM). Live-fetches up to 50 pages when `structured_data` isn't in LibreCrawl's export. Writes `.schema-validation.csv`.

### Changed

- Tool count: **32 → 35**.
- `runner._finalize_session` now emits 7 artifacts per chunked audit: `.md` + `.pdf` + `.per-page.csv` + `.sitemap-recon.csv` + `.external-links.csv` + `.content-audit.csv` + `.extended-checks.csv`.

### Deferred (v2.1+)

- **JS-render delta** via Playwright (raw HTML vs rendered DOM diff for canonical / title / noindex / internal-links present only post-hydration). Requires ~300MB Chromium install + new rendering pipeline — separate atomic chunk.
- **AMP validation**, image-format upgrade suggestions (PNG → WebP), DOM-size checks, JS console errors. All Playwright-dependent.

---

## [1.4.1] — 2026-06-03

External-link validator. Closes the "target_status: null" gap that hid broken outbound URLs.

### Added

- **`librecrawl_external_links_audit(crawl_id, max_workers=10, timeout_seconds=10.0)`** — concurrent HEAD pool (GET fallback for HEAD-blocked servers) against every unique external URL. Follows redirects, classifies SF-style: `ok` / `ok_after_redirect` / `forbidden` / `not_found` / `gone` / `client_error_4xx` / `server_error_5xx` / `timeout` / `dns_error` / `ssl_error` / `connection_refused` / `malformed_url` / `protocol_error` / `skipped`.
- **Auto-wired** into `runner._finalize_session` as the `.external-links.csv` sidecar.

### Smoke test

Fresh chunked audit on theculinarypeace.com: 71 external links validated, 7 broken — 5 forbidden (NDTV-style 403), 1 not_found (Amazon 404), 1 connect_error, 1 malformed_url. These were invisible in v1.4.0's report.

---

## [1.4.0] — 2026-06-03

Chunked-progressive audit engine. No more MCP client timeouts on big-site audits.

### Added

- **`librecrawl_start_chunked_audit(url, total_max_pages=10000, chunk_target_pages=50, politeness="auto", confirm_unbounded=False)`** — returns `session_id` in under 2s. Crawl runs in background, survives PM2 restart.
- **`librecrawl_audit_status(session_id)`** — reads SQLite, safe to poll often. Returns status, pages_done, current_delay_ms, last 3 chunk metrics, recent events, ETA, artifacts_ready.
- **`librecrawl_audit_artifacts(session_id)`** — paths to all sidecars once status=done.
- **`librecrawl_audit_pause / _resume / _cancel / _force_advance`** — operator controls.
- **AIMD adaptive controller** — additive-increase / multiplicative-decrease on `crawlDelay` from observed p95 latency + 5xx rate. Honours robots.txt Crawl-Delay floor.
- **`state.py`** — SQLite WAL store for sessions / chunks / artifacts / events. Boot recovery re-queues active sessions.
- **`libreclient.py`** — typed wrapper around LibreCrawl Flask API with metrics derivation.
- **`runner.py`** — single worker thread (LibreCrawl is single-tenant upstream). Polls every 20s, computes metrics per `chunk_target_pages` worth of progress, pushes new `crawlDelay` via `/api/save_settings` live.

### Smoke tests

- Live audit on uichemy.com max=15: queued → crawling → done in 32s, 19 pages crawled, 3 artifacts written.
- PM2 kill mid-crawl recovery: session survived in SQLite, runner fired `boot_recovery_requeue` then `resumed_from_state`. State preserved (upstream in-memory crawler limitation is documented in v1.4.0 commit).

---

## [1.2.0] — 2026-06-03

Screaming-Frog parity release. Closes the "silent caps" gap.

### Added

- **`librecrawl_full_audit_strict(url, max_pages=0, auto_purge=True, keep_for_days=0)`** — strict mode. `audit_complete` flips False on any cap / timeout / partial result. Auto-purges upstream DB record after success.
- **`librecrawl_report_content(report_path, max_chars=200_000)`** — serves the .md / .csv content directly through MCP for clients that can't read REPORTS_DIR.
- **`librecrawl_pagespeed_audit_all_crawl_pages(crawl_id, strategy, limit, delay_seconds)`** — full PSI across every crawled URL, explicit batch_caps_hit + audit_complete flags.
- **`librecrawl_brain_purge_audit(crawl_id)`** — DELETE on upstream crawl record after report consumed.
- **`crawl_completeness` on every audit return** — `pages_crawled`, `queued_remaining`, `max_pages_hit`, `timeout_hit`, `robots_blocked_count`, `batch_caps_hit`, `audit_complete`.
- **`checks_manifest` on every audit return** — 37 named checks with section, pass/fail count, `ran_on_all_pages` flag.
- **Sidecar CSVs**: `.per-page.csv` (one row per URL × failed checks), `.sitemap-recon.csv` (sitemap-vs-crawl drift).

### Fixed

- **JSON-LD `@graph` parser** — Yoast / RankMath / WPRM Recipe / Article / FAQPage now surface correctly instead of being labelled "Unknown".
- **`librecrawl_schema_audit` silent 50-URL cap removed** — now processes the full list with configurable `batch_delay`.
- **`librecrawl_generate_report`** returns inline Markdown (50k char cap + truncation flag).

---

## [1.1.1] — 2026-05-28

Chunked-crawling for huge sites (single-day v0, not session-resumable).

### Added

- **`librecrawl_resume_from_crawl_id(crawl_id)`** — picks up an interrupted crawl from the LibreCrawl DB across server restarts and days. Uses LibreCrawl's `/api/crawls/<id>/resume` (DB resume) with fallback to `/api/resume_crawl` (in-session resume).
- Improved auto-recovery from stuck/paused/zombie upstream crawler.

---

## [1.1.0] — 2026-05-28

### Added

- Auto-recovery from stuck crawler — `librecrawl_audit` and `librecrawl_start_crawl` silently reset paused/stale crawler state before starting.
- GSC top-queries section in the report via `librecrawl_append_gsc_section`.
- Page-2 keyword "quick wins" detection (positions 6–20 with high impressions).

---

## [1.0.0] — 2026-05-21

Initial release. Self-hosted SEO crawler wrapping [LibreCrawl](https://github.com/PhialsBasement/LibreCrawl) as an MCP server. 19 tools, runs anywhere Claude / Cursor / Windsurf / Codex / Continue.dev / VS Code Copilot can connect.

---

*Generated by [LibreCrawl MCP](https://github.com/adityaarsharma/librecrawl-mcp) — By Aditya Sharma*
