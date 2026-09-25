# Changelog

## [2.3.2] — 2026-09-25

### Fixed
- Chunked audits ended on their first status poll. Upstream `get_status()` has no `is_running` key, so the runner always read the crawl as stopped: a slow first page (sitemap discovery) failed the audit with `upstream_stopped_zero_pages`, and a fast one finalized a partial crawl. Liveness now comes from the upstream `status` string (`running` vs `completed`/`idle`).
- A failed status poll no longer counts as "crawl finished". It is retried; six failures in a row fail the audit with `upstream_unreachable`.
- `librecrawl_get_status` reported `is_running: false` during every crawl, so agents following "poll until is_running=False" stopped at once. It now reports the real state plus the upstream `status`.

## [2.3.1] — 2026-09-25
### Fixed
- **Bot-challenge pages are no longer audited as the site.** A crawl behind Cloudflare (or
  another WAF) returned "Just a moment..." pages that were scored as broken pages, filled the
  Critical section and counted as sitemap coverage. Challenge pages are now removed from every
  count, reported as a `bot_challenge` incomplete reason, and treated as missed sitemap URLs. If
  more than 20% of pages are challenged, the sitemap fill stops sending requests. An audit where
  every page was challenged fails with a clear "allowlist the crawler" message.
- **Images and other files are no longer checked as pages.** URLs ending in image, font, media,
  document or asset extensions skip page-level checks (title, H1, viewport), so an image is not
  flagged for a missing H1.
- A page cap is now exact: upstream could return one page over `total_max_pages`.
- **Response times are server latency.** Upstream stamped `response_time` after parsing the page
  and HEAD-checking every image on it, so a page served in 2s read as 18s and 57 of 60 pages on a
  live audit were flagged slow. The bundled upstream patch now records `response.elapsed`.
- A page with status 0 now says why (`timeout`, `dns_not_found`, `ssl_error`, `no_response`) in
  the Broken Pages table and in a new `fetch_error` column of the per-page CSV.
- `librecrawl_get_settings` returns the crawl-shaping settings only. Pass `full=True` for the
  whole dict, including the ~2,500 token default exclusion lists.

## [2.3.0] — 2026-09-25
### Security
- **SSRF guard on every outbound fetch** (`url_guard.py`). Crawl seeds, robots and sitemap
  probes, schema extraction, external-link checks, sitemap fill, content audit and extended
  checks now accept only http/https and refuse loopback, private, link-local (cloud metadata)
  and docker-internal targets, including decimal and hex IP spellings. Redirects are re-checked
  hop by hop. `LIBRECRAWL_ALLOW_PRIVATE_TARGETS=1` opts in for intranet audits.
- `report_content` and the GSC merge path check use `Path.is_relative_to`, closing a
  sibling-directory prefix bypass (`/reports-evil/...`).
### Fixed
- **Cleanup actually deletes the upstream crawl.** Cleanup now goes through LibreCrawl's REST
  delete, and the bundled upstream patch deletes its child tables too, so no crawled pages,
  links, issues or queue rows survive. The old path wrote to a read-only mount and failed silently.
- **Watchdog purged unrelated rows.** Its fallback SQL matched `crawl_id = ? OR id = ?` on child
  tables; it now matches `crawl_id` only and prefers the REST delete.
- **Watchdog never purged anything.** Its state cleanup queried an `id` column the child tables do
  not have, so every run died with `no such column: id` before deleting a row. Child tables now
  key on `session_id`, and old report files are swept even in a run with no expired session.
- Content audit and extended checks cover every crawled page again (batched fetch, bounded
  memory) instead of the first 500, the fetch timeout is 45s, and a `schema-validation.csv`
  artifact is written for every audit. These were running in production but missing from the repo.
- Watchdog REST delete reopened an HTTP client it was already using and crashed, so upstream
  crawls were never removed through the API. It also now removes upstream crawls that no session
  owns once they are older than `TTL_UPSTREAM_S` (default 4h), so crawls whose session row is gone
  no longer pile up.
- `docker/patch-librecrawl.py` keeps upstream's CRLF line endings instead of rewriting the whole
  file, and stops if the second half of the session patch cannot apply.
- Response times were always blank: the export asked for a field upstream does not have.
  Now read from `response_time` (ms).
- The homepage was counted as an orphan page. The seed is excluded everywhere orphans are counted.
- A missing sitemap (404/410) no longer marks a strict audit as failed; it is reported as a finding.
  Auto-purge now runs whether or not the strict audit passed.
- Off-site URLs (other hosts listed in a sitemap, off-site redirect targets) no longer enter the
  crawl results or the sitemap reconciliation.
- A seed that never answered (status 0) is reported as a failure, not an audit with zero issues.
- Sitemap fill could overshoot `total_max_pages`; it now fills only the remaining budget.
- Reports derive the site from the crawl's own base URL instead of the first exported page.
- `filter_issues` filters locally by substring across url, type, category, issue and details
  (upstream's endpoint ignored the patterns). `get_status` counts the real issue list.
- `schema_audit` separates fetch errors from "no schema" and marks the audit incomplete.
- PDF: emoji render as `[OK]`/`[WARN]`/`[FAIL]` labels instead of empty boxes, the page count
  is correct, and the footer is configurable via `LIBRECRAWL_PDF_FOOTER`.
- PageSpeed tools work without an API key (keyless quota), with a 60s timeout and a clear
  `rate_limited` reason on 429.
- Legacy `librecrawl_audit`, `librecrawl_start_crawl` and `librecrawl_full_audit_strict` take
  `max_pages` (default 500) and refuse an unbounded crawl unless `confirm_unbounded=True`.
### Added
- **`librecrawl_audit_confirm_saved(session_id, sha256)`.** The zip is no longer deleted the
  moment it is returned. The client saves it, sends back the sha256 of the saved bytes, and only
  a match wipes the session, artifacts and upstream crawl. A mismatch deletes nothing.
- `tests/`: 53 pytest cases for the guard, export mapping, orphan logic, sitemap strictness,
  entry-point gates, local issue filtering and the confirm-then-wipe flow.
### Changed
- `mcp` pinned to `>=1.2.0,<2`.

## [2.2.0] — 2026-07-14
### Added
- **One-command Docker deploy.** New `docker-compose.yml` brings up the LibreCrawl
  engine and the MCP server together (`docker compose up --build`), with the MCP
  gated on LibreCrawl's healthcheck and a shared volume for upstream-DB checks.
  Includes a dedicated `Dockerfile` (MCP, with WeasyPrint system libs) and
  `docker/librecrawl.Dockerfile` (builds upstream LibreCrawl with the
  session-persistence patch applied).
- **`requirements.txt`.** Runtime deps (`mcp`, `httpx`, `uvicorn`, `weasyprint`,
  `markdown`) are now declared, so a fresh clone installs with
  `pip install -r requirements.txt`.
- **`LIBRECRAWL_URL` + `MCP_HOST` env vars.** The MCP can now reach a LibreCrawl
  backend by hostname (e.g. a Docker service or remote host), and bind its HTTP
  transport to a configurable address — required for containerized/VPS runs.
### Changed
- **`LIBRECRAWL_UPSTREAM_DB` default is now portable** (`~/.librecrawl/upstream/users.db`)
  instead of a machine-specific absolute path. Set it explicitly (compose does) to
  enable orphan/cleanup checks; absent, those checks degrade gracefully.
- `server.json` version corrected (was stale at 2.0.5).
### Housekeeping
- Removed internal operator/handoff docs and deployment-specific references from
  the public repo; the tree is now environment-agnostic.

## [2.1.1] — 2026-06-13
### Fixed
- **OOM crash-loop on large heavy sites.** v2.1.0 made content/extended checks
  run on EVERY page; on a 1900+ page site with 4-5 MB pages that exhausted
  memory and looped. Deep-checks (content-audit, extended-checks) now cap at
  500 pages by default (tunable up via `content_check_limit` /
  `extended_check_limit`), while per-page core checks + external-link
  validation still cover 100% of pages. Verified: full 1,942-page audit
  completes cleanly.

## [2.1.0] — 2026-06-12
### Added
- **FULL audit by default — every page, every text, every link.** No caps to
  remember, no flags to pass. `sitemap_fill_cap` defaults to 0 (= entire
  sitemap, bounded by total_max_pages). Word-by-word content analysis and
  extended SEO checks run across the whole crawl, not a sample.
### Changed
- HARD_DEADLINE 4h → 12h (polite full crawls of very large sites run long).

## [2.0.9] — 2026-06-11
### Fixed
- **Screaming-Frog-grade politeness — never overload an origin.** Fetch
  concurrency lowered to 4 workers + 500ms jittered delay (was 16/no-delay,
  which slowed a heavy origin). Heavy pages get more TIME (25s timeout), not
  more PARALLELISM. External-link concurrency capped at 8.

## [2.0.8] — 2026-06-11
### Fixed
- **Heavy / large websites now crawlable.** Fetch timeout 8s → 25s so 4-5 MB
  pages actually load instead of timing out to status 0. Deep-check sample
  raised.

## [2.0.7] — 2026-06-11
### Fixed
- **Async modules work under force_advance (event-loop fix).** Finalize
  reached via the force-advance tool runs inside the async MCP handler;
  asyncio.run() crashed there, silently dropping content-audit /
  extended-checks / external-links / sitemap-fill. Added _run_coro() to all
  four modules so finalize works from any context (8 files always produced,
  full sitemap coverage).

## [2.0.5] — 2026-06-05
### Fixed
- **hreflang false positives.** Case-insensitive region codes (de-de, zh-cn)
  no longer flagged; x-default excluded from lang-attr conflict check.

All notable changes to **librecrawl-technical-seo-audit-mcp** are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning follows [SemVer](https://semver.org/).

---

## [2.0.0] — 2026-06-04

Finalisation release. No new features; this version marks the end of the v1.x build-out and stamps the project as **feature-complete for technical SEO auditing**. README rewritten for clarity + discoverability, a reusable Claude Code skill ships under `.claude/skills/`, repo cleanup, comprehensive smoke test on a real production domain. The audit toolchain remains the v1.9.1 set — 37 MCP tools, 50+ technical checks, ephemeral mode, branded PDF + 5 CSV sidecars per audit, sitemap-orphan fill, AIMD adaptive crawler.

### Why this is 2.0

The version arc from 1.0 → 1.9 added one major capability per minor release. v2.0 marks parity with paid Screaming-Frog-class audit tools across every non-JS-render dimension and the ephemeral-by-default privacy model. There is no v2.1 planned — additional checks would land as 2.x.

### Documentation

- README.md fully rewritten for SEO + clarity: H1/H2 hierarchy that hits the primary keyword cluster in the first 200 words, "vs Screaming Frog / Ahrefs / Sitebulb" feature matrix, install in one block, usage in five examples, ephemeral-mode workflow up front.
- Documentation reorganized under docs/.
- .gitignore added.

### Smoke test (release gate)

Full audit on a real production domain with zero caps. All 7 sidecars emitted, every check class fired or correctly absent, zip download flow round-tripped, server returned to zero-memory baseline after the client downloaded the bundle.

---

## [1.9.1] — 2026-06-04

Polish from the v1.9.0 review.

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

- **PDF reports** — `librecrawl_audit_pdf(report_path, base_url="")` renders any saved Markdown audit as a branded PDF via WeasyPrint. Footer on every page: *Report Generated by LibreCrawl MCP — By Aditya Sharma · github.com/adityaarsharma/librecrawl-technical-seo-audit-mcp*. Auto-emitted as a `.pdf` sidecar on every chunked audit.
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

- Live audit on a real site max=15: queued → crawling → done in 32s, 19 pages crawled, 3 artifacts written.
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

*Generated by [LibreCrawl MCP](https://github.com/adityaarsharma/librecrawl-technical-seo-audit-mcp) — By Aditya Sharma*
