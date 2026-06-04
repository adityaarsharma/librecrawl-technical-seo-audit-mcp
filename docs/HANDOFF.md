# HANDOFF — librecrawl-mcp v1.7 backlog

**Last shipped:** v1.6.0 (commit [`179ece0`](https://github.com/adityaarsharma/librecrawl-mcp/commit/179ece0), tag [`v1.6.0`](https://github.com/adityaarsharma/librecrawl-mcp/releases/tag/v1.6.0)) — 2026-06-03.

**Live tool count:** 35 at `https://brain.posimyth.com/librecrawl/mcp`.

This file is the starting brief for a fresh `/clear`'d session. The full context is in CHANGELOG.md + SPEC-v2.md + the POSIMYTH brain notes (`drawer_brain_general_*` from 2026-06-03).

---

## What ships in v1.6.1 (small docs + maxDepth bump, ~25 min)

These three items are documented gaps the current session ran out of energy to land. They should ship together as v1.6.1.

### 1. CHANGELOG.md is stale — add v1.5.1 + v1.6.0 sections

Last entry in `CHANGELOG.md` is v1.5.0. Append:

- **v1.5.1 (hotfix)** — `audit_complete` respects sitemap coverage + `max_pages_hit`. Adds `incomplete_reasons[]` list + `sitemap_total` / `sitemap_only_count` / `sitemap_coverage_pct` fields to the completeness block. Status stays `done` either way (artifacts ARE on disk); the truth lives in `audit_complete` + `incomplete_reasons` surfaced via `librecrawl_audit_status`.
- **v1.6.0 (feature)** — `sitemap_fill.py` module + `runner._finalize_session` wiring. After LibreCrawl finishes, lightweight HTTP fetch on `recon.sitemap_only` URLs; appended to the `pages` list before `_build_report` runs. Recomputes recon AFTER fill. New params on `librecrawl_start_chunked_audit`: `fill_sitemap_orphans: bool = True`, `sitemap_fill_cap: int = 500`. Smoke test: theculinarypeace.com 102-URL sitemap, max_pages=15 → 15 LibreCrawl-crawled + 84 sitemap-fill = 103 pages in report, sitemap_coverage_pct=100.0.

### 2. README.md is stale — add v1.5.1 + v1.6.0 sections

Mirror the CHANGELOG additions in the `What's new in v1.X` block at the top. Verify the "35 tools" count throughout the file.

### 3. Bump LibreCrawl `maxDepth` default from 5 → 20

In `libreclient.py` line ~37 (default `max_depth=5`), change to `max_depth=20`. Smoke-test on a real site (theculinarypeace.com or uichemy.com with max_pages>=100) — confirm LibreCrawl natively crawls more pages BEFORE the sitemap_fill fallback kicks in. The bump gives:
- Better data for deep-but-linked sitemap URLs (LibreCrawl's full SEO extractor vs sitemap_fill's minimal extractor)
- Inbound/outbound link graph data for those pages (sitemap_fill pages don't have linked_from / links_detailed)
- Upstream issue-detector findings on those pages

Sitemap_fill becomes the fallback for TRUE orphans (no internal links to them at all) rather than the primary coverage source.

Tag the result `v1.6.1`. Push.

---

## Real bugs queued for v1.7

### 4. PM2 restart upstream-resume fallback

Session state persists in `librecrawl-state.db` and the runner correctly fires `boot_recovery_requeue` → `resumed_from_state` events on PM2 restart. **But** the upstream LibreCrawl in-memory crawler is lost when PM2 dies, so `pages_done` restarts from 0.

Fix in `runner._run_session` resume path (around line ~108):
```python
# Resume path — make sure upstream is still alive
state.set_status(sid, "crawling", detail="resumed_from_state")
try:
    in_session = libreclient.resume_crawl()
    if not in_session.get("success"):
        # Fallback: DB resume — rebuilds crawler from upstream SQLite
        libreclient.call("POST", f"/api/crawls/{upstream_crawl_id}/resume")
except Exception:
    pass
```

Needs a deliberate kill-test on a real running audit to verify end-to-end recovery.

### 5. Past-crawl `librecrawl_external_links_audit` returns 0

LibreCrawl's per-session crawler instances (`main.py crawler_instances` dict keyed by Flask `session_id`) don't fully restore `link_manager.all_links` after `/api/crawls/<id>/load`. Re-running `librecrawl_external_links_audit(crawl_id=39)` 24h later returns 0 links even though the crawl has 6,652 links in DB.

Fresh chunked audits work fine because `link_manager.all_links` is in memory throughout the crawl. The bug only affects RE-RUNNING the tool on a past crawl.

Investigation pointer: `/home/posimyth-brain/webapps/librecrawl/src/crawler.py` + `main.py /api/crawls/<int:crawl_id>/load` at line ~1098. The load endpoint sets `crawler.link_manager.all_links = links` but something is wiping it before `/api/export_data` reads `crawler.get_status()`. Possibly `link_manager.update_link_statuses(crawl_results)` mutates `all_links` when `crawl_results` is empty? Look at `src/link_manager.py update_link_statuses`.

### 6. No-sitemap edge-case guard

When a site has no `sitemap.xml` (or it returns 404), `site_data["sitemap"]["url"]` is None and runner falls back to `f"{url.rstrip('/')}/sitemap.xml"`. If that URL 404s, `_compute_sitemap_reconciliation` returns `sitemap_total=0` and `sitemap_only=[]` — sitemap_fill is correctly skipped. But: verify the recon CSV doesn't crash with an empty list, and the completeness block doesn't report `sitemap_coverage_pct=100` misleadingly when sitemap_total=0 (should be `None` or `n/a`).

Quick test: chunked audit on a site without a sitemap.xml, confirm no crash and reasonable values in the completeness block.

---

## Explicitly deferred — v2.1 territory

These are NOT bugs; they're separate scope chunks that need their own session.

- **Playwright JS-render delta** — raw HTML vs rendered DOM diff for canonical / title / noindex / internal-links present only post-hydration. ~300MB Chromium install + new rendering pipeline architecture. Most-requested gap vs Screaming Frog Pro.
- **AMP validation** — `<link rel=amphtml>` + AMP-specific schema + canonical reciprocity.
- **PNG → WebP / next-gen image format suggestions** — already detect image_oversized_kb; this layer adds the "you could save X KB by re-encoding" recommendation.
- **DOM size checks** — > 1500 nodes or > 32 depth.
- **JS console errors** — fetch each page via Playwright, capture errors.
- **Per-URL screenshots** — Playwright PNG capture per audited URL.
- **Mobile vs desktop content diff** — fetch each page twice with different UA + viewport, diff the rendered text.

All Playwright-dependent. Best done in one focused v2.1 session.

---

## Known limitations, low priority

- Sitemap-filled pages don't have inbound/outbound link graph data (we only fetched them once, didn't crawl FROM them). Solving this means "sitemap as multi-seed" — running LibreCrawl iteratively per orphan. Heavy. Defer until anyone actually complains.
- Sitemap-filled pages don't carry LibreCrawl's `issues_detected` field (upstream's own issue heuristics never ran on them). Tagged with `source="sitemap_fill"` for filtering.
- `librecrawl_pagespeed_audit_all_crawl_pages` uses Google PSI API — counts against the 25k/day quota. Documented in the tool's docstring.

---

## Quick context for a fresh session

- **Source:** `/Users/adityasharma/Claude Projects/POSIMYTH/...` is the project workspace; the librecrawl-mcp repo lives at `/tmp/librecrawl-mcp-work/` (git clone of `adityaarsharma/librecrawl-mcp`).
- **SSH alias:** `hetzner-brain` (49.13.66.133, user `posimyth-brain`, ControlMaster active).
- **MCP runtime:** `/home/posimyth-brain/webapps/librecrawl/mcp-server/` on hetzner-brain.
- **PM2 process:** `librecrawl-mcp` (id 15).
- **State DB:** `/home/posimyth-brain/librecrawl-state.db` (SQLite WAL).
- **Reports:** `/home/posimyth-brain/librecrawl-reports/`.
- **MCP endpoint:** `https://brain.posimyth.com/librecrawl/mcp` (bearer `0eed0e5f9b9440dfbfe84e237e10200b61d617c1ece91b0eab8587331b99c696`).
- **Branding rule:** PDF footer is exactly `"Report Generated by LibreCrawl MCP — By Aditya Sharma · github.com/adityaarsharma/librecrawl-mcp"`. **NO POSIMYTH branding** anywhere in user-visible artifacts.

Start by reading this file + SPEC-v2.md, then the v1.6.0 brain note (`drawer_brain_general_4e81e617b5a632176cd30074`).
