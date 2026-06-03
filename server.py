#!/usr/bin/env python3
"""
LibreCrawl MCP Server
Wraps LibreCrawl REST API as Claude MCP tools for full-site SEO auditing.
Source: https://github.com/adityaarsharma/librecrawl-mcp
"""

import os
import json
import time
import re
import threading
from datetime import datetime
from pathlib import Path
from collections import defaultdict
from html.parser import HTMLParser
from urllib.parse import urlparse, unquote
import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("librecrawl-mcp")

BASE            = f"http://127.0.0.1:{os.getenv('LIBRECRAWL_PORT', '5080')}"
MCP_PORT        = int(os.getenv('MCP_PORT', '5081'))
REPORTS_DIR     = Path(os.getenv('REPORTS_DIR', Path.home() / 'librecrawl-reports'))
PSI_API_KEY     = os.getenv('PAGESPEED_API_KEY', '')   # Google PageSpeed Insights
PSI_API_BASE    = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"

# Fields we request on every export
# LibreCrawl exposes all of these from its seo_extractor — we request the full set
EXPORT_FIELDS = [
    # Core
    "url", "status_code", "title", "meta_description", "h1",
    "word_count", "canonical_url", "depth", "issues_detected",
    "response_time_ms",
    # Headings
    "h2", "h3",
    # Links
    "links_detailed", "internal_links", "external_links", "linked_from",
    # Images
    "images", "broken_images",
    # Technical
    "robots", "lang", "charset", "viewport", "size", "redirects", "error_type",
    # Social / structured
    "og_tags", "twitter_tags", "json_ld", "hreflang",
    # Analytics fingerprint
    "analytics",
]

def _parse_export(export) -> tuple:
    """
    Parse LibreCrawl export response into (pages, links).
    Handles three response formats LibreCrawl uses depending on version:
      1. Direct list of pages
      2. Dict with data/urls/pages key
      3. Single-file: {"content": "...", "filename": "librecrawl_export_*.json"}
      4. Multi-file:  {"files": [...], "multiple_files": true}
    """
    import json as _json

    def _extract_from_parsed(parsed):
        for key in ("data", "urls", "pages"):
            if key in parsed and isinstance(parsed[key], list):
                return parsed[key]
        return []

    if isinstance(export, list):
        return export, []

    # Direct dict with known key
    for key in ("data", "urls", "pages"):
        if key in export and isinstance(export[key], list):
            return export[key], []

    # Single-file format: {"content": "...", "filename": "librecrawl_export_*.json", "success": True}
    if "content" in export and "filename" in export:
        filename = export.get("filename", "")
        raw      = export.get("content", "")
        if "export" in filename and raw:
            try:
                pages = _extract_from_parsed(_json.loads(raw))
                if pages:
                    return pages, []
            except Exception:
                pass

    # Multi-file format: {"files": [...], "multiple_files": true}
    pages, links = [], []
    for f in (export.get("files") or []):
        filename = f.get("filename", "")
        raw      = f.get("content", "")
        if not raw:
            continue
        try:
            parsed = _json.loads(raw)
        except Exception:
            continue
        if "export" in filename:
            found = _extract_from_parsed(parsed)
            if found:
                pages = found
        elif "links" in filename and isinstance(parsed, list):
            links = parsed
    return pages, links


_client = None
_client_lock = threading.Lock()


# ── HTTP client ───────────────────────────────────────────────────────────────

def get_client():
    """Return authenticated httpx.Client. Re-auths automatically on 401. Thread-safe."""
    global _client
    with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.Client(timeout=30, follow_redirects=True)
            _client.post(f"{BASE}/api/login", json={"username": "mcp-user"}).raise_for_status()
        return _client


def call(method, path, **kwargs):
    global _client
    r = get_client().request(method, f"{BASE}{path}", **kwargs)
    if r.status_code == 401:
        _client = None
        r = get_client().request(method, f"{BASE}{path}", **kwargs)
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        raise RuntimeError(f"LibreCrawl returned non-JSON ({r.status_code}): {r.text[:200]}")


def _ensure_crawler_ready() -> dict:
    """
    Make sure the upstream crawler is in a clean state before a new crawl.

    Recovers from three stale states people regularly hit:
      1. A previous crawl is still running (blocks start_crawl with "Crawl already in progress")
      2. A previous crawl is paused (same block, plus the resume button is dead from this session)
      3. The crawler is wedged with 0 progress for >60s but `is_running=True` (zombie thread)

    Returns a short status dict so callers can surface what was reset (helpful in logs).
    Never raises — best-effort cleanup.
    """
    state = {"was_running": False, "was_paused": False, "was_stale": False, "action": "none"}
    try:
        s = call("GET", "/api/crawl_status")
    except Exception:
        return state

    stats      = s.get("stats", {}) or {}
    is_running = bool(s.get("is_running"))
    status_str = (s.get("status") or "").lower()
    crawled    = stats.get("crawled", 0)

    # Paused crawl — upstream reports this as status="running" + is_running=True (paused
    # is an internal flag, not surfaced in get_status). Probe via crawl speed: a paused
    # crawler has speed=0 for sustained periods even though is_running=True.
    if status_str == "paused":
        state["was_paused"] = True
        try:
            call("POST", "/api/stop_crawl")
            time.sleep(1.5)
            state["action"] = "stopped_paused"
        except Exception:
            pass
        return state

    # Running — detect a stuck/paused/zombie crawler:
    #   - speed == 0 (no requests/sec) for an active crawl = paused or wedged
    #   - 0 pages crawled but is_running=True for >30s = zombie
    # In either case, force-stop so the next start_crawl is clean.
    if is_running or status_str in ("running", "crawling"):
        state["was_running"] = True
        speed   = stats.get("speed", 0) or 0
        elapsed = 0
        try:
            elapsed = time.time() - float(stats.get("start_time", 0) or 0)
        except Exception:
            pass
        is_likely_paused = (elapsed > 5 and speed == 0)
        is_zombie        = (crawled == 0 and elapsed > 30)
        if is_likely_paused or is_zombie:
            state["was_stale"] = True
            try:
                call("POST", "/api/stop_crawl")
                time.sleep(1.5)
                state["action"] = "stopped_likely_paused" if is_likely_paused else "stopped_zombie"
            except Exception:
                pass
    return state


# ── Site-level checks (robots, sitemap, HTTPS, www) ──────────────────────────

def _site_check(base_url: str) -> dict:
    """Fetch robots.txt, sitemap.xml, and check redirect behaviour."""
    parsed   = urlparse(base_url)
    scheme   = parsed.scheme or "https"
    host     = parsed.netloc or parsed.path.rstrip("/")
    root     = f"{scheme}://{host}"
    results  = {}

    # ── robots.txt ────────────────────────────────────────────────────────────
    try:
        r = httpx.get(f"{root}/robots.txt", timeout=10, follow_redirects=True)
        if r.status_code == 200:
            txt      = r.text
            lines    = txt.splitlines()
            disallow = [l.split(":",1)[1].strip() for l in lines
                        if l.lower().startswith("disallow:") and l.split(":",1)[1].strip()]
            sitemaps = [l.split(":",1)[1].strip() for l in lines
                        if l.lower().startswith("sitemap:")]
            crawl_delay = next(
                (l.split(":",1)[1].strip() for l in lines if l.lower().startswith("crawl-delay:")),
                None
            )
            # Check if important paths are blocked
            important_blocked = [d for d in disallow if d in ("/", "/wp-admin", "/wp-login.php")]
            results["robots_txt"] = {
                "found": True,
                "disallow_count": len(disallow),
                "disallow_rules": disallow[:20],
                "important_blocked": important_blocked,
                "sitemap_declared": sitemaps,
                "crawl_delay": crawl_delay,
                "raw_preview": txt[:500],
            }
        else:
            results["robots_txt"] = {"found": False, "status": r.status_code,
                                      "warning": "robots.txt missing — Googlebot has no crawl guidance."}
    except Exception as e:
        results["robots_txt"] = {"error": str(e)}

    # ── sitemap.xml ───────────────────────────────────────────────────────────
    sitemap_urls_to_try = [f"{root}/sitemap.xml", f"{root}/sitemap_index.xml",
                            f"{root}/sitemap-index.xml"]
    # also try any declared in robots
    sitemap_urls_to_try += results.get("robots_txt", {}).get("sitemap_declared", [])

    sitemap_found = False
    for sm_url in sitemap_urls_to_try:
        try:
            r = httpx.get(sm_url, timeout=15, follow_redirects=True)
            if r.status_code == 200 and ("<urlset" in r.text or "<sitemapindex" in r.text):
                url_count = r.text.count("<loc>")
                is_index  = "<sitemapindex" in r.text
                child_sitemaps = re.findall(r"<loc>(.*?)</loc>", r.text) if is_index else []
                results["sitemap"] = {
                    "found": True,
                    "url": sm_url,
                    "is_index": is_index,
                    "url_count": url_count,
                    "child_sitemaps": child_sitemaps[:10],
                }
                sitemap_found = True
                break
        except Exception:
            pass
    if not sitemap_found:
        results["sitemap"] = {
            "found": False,
            "warning": "No sitemap.xml found. Submit one to GSC to improve indexing.",
        }

    # ── HTTPS redirect ────────────────────────────────────────────────────────
    if scheme == "https":
        try:
            http_url = f"http://{host}/"
            r = httpx.get(http_url, timeout=10, follow_redirects=False)
            if r.status_code in (301, 302, 307, 308):
                loc = r.headers.get("location", "")
                results["https_redirect"] = {
                    "http_redirects_to_https": loc.startswith("https://"),
                    "redirect_code": r.status_code,
                    "location": loc,
                    "permanent": r.status_code in (301, 308),
                }
            else:
                results["https_redirect"] = {
                    "http_redirects_to_https": False,
                    "warning": f"http:// returns {r.status_code} without redirect — mixed content risk.",
                }
        except Exception as e:
            results["https_redirect"] = {"error": str(e)}

    # ── www vs non-www ────────────────────────────────────────────────────────
    try:
        is_www = host.startswith("www.")
        alt_host = host[4:] if is_www else f"www.{host}"
        r = httpx.get(f"{scheme}://{alt_host}/", timeout=10, follow_redirects=False)
        redirects_to_canonical = r.status_code in (301, 302, 307, 308)
        results["www_redirect"] = {
            "canonical_host": host,
            "alt_host": alt_host,
            "alt_redirects_properly": redirects_to_canonical,
            "alt_status": r.status_code,
            "warning": None if redirects_to_canonical else
                f"{scheme}://{alt_host}/ does not redirect to canonical host — duplicate content risk.",
        }
    except Exception as e:
        results["www_redirect"] = {"note": str(e)}

    return results


# ── Report generator ──────────────────────────────────────────────────────────

def _build_report(pages: list, base_url: str, crawl_id: int,
                  site_data: dict = None, links: list = None) -> str:
    """Generate a structured Markdown SEO audit report from crawl export data."""

    domain = base_url.replace("https://", "").replace("http://", "").rstrip("/")
    now    = datetime.now().strftime("%Y-%m-%d %H:%M")
    total  = len(pages)

    parsed_base = urlparse(base_url)
    base_host   = parsed_base.netloc or domain

    # ── Build reverse link map (source of broken links) ──────────────────────
    # Prefer flat links file (LibreCrawl multi-file export); fall back to links_detailed per page
    inbound = defaultdict(list)   # target_url → [source_urls]
    if links:
        for lk in links:
            src = lk.get("source_url", "")
            tgt = lk.get("target_url", "")
            if src and tgt:
                inbound[tgt].append(src)
    else:
        for p in pages:
            src      = p.get("url", "")
            pg_links = p.get("links_detailed") or []
            if isinstance(pg_links, list):
                for lk in pg_links:
                    tgt = lk.get("url") or lk.get("href") or ""
                    if tgt:
                        inbound[tgt].append(src)

    # ── Categorise pages ──────────────────────────────────────────────────────
    status_buckets   = defaultdict(list)
    missing_title    = []
    missing_meta     = []
    missing_h1       = []
    long_title       = []
    short_title      = []
    long_meta        = []      # >160 chars
    short_meta       = []      # 1–70 chars
    thin_content     = []
    dup_titles       = defaultdict(list)
    dup_metas        = defaultdict(list)
    slow_pages         = []
    no_canonical       = []      # missing canonical tag
    self_canonical     = []      # canonical == self (good, counted)
    non_self_canonical = []      # canonical points elsewhere
    bad_canonical      = []      # canonical → broken URL
    uppercase_urls     = []
    long_urls          = []      # >115 chars
    deep_pages         = []      # depth > 4
    h1_title_mismatch  = []      # H1 and title share 0 meaningful words
    url_params_heavy   = []      # >3 query params
    # New from full field set
    noindex_pages      = []      # robots meta = noindex
    large_pages        = []      # page body > 500KB
    missing_alt_pages  = []      # (url, count_missing) — images with no alt
    broken_img_pages   = []      # (url, count) — broken image srcs
    orphan_pages       = []      # linked_from empty (no inbound links)
    redirect_chains    = []      # redirect depth > 1 hop
    missing_og_pages   = []      # no og:title or og:description
    missing_viewport   = []      # no viewport meta (mobile-hostile)
    hreflang_pages     = []      # pages declaring hreflang
    issues_type_count  = defaultdict(int)
    all_page_urls      = set()   # all crawled URLs (for orphan check)

    for p in pages:
        url         = p.get("url", "")
        status      = p.get("status_code", 0)
        title       = (p.get("title") or "").strip()
        meta        = (p.get("meta_description") or "").strip()
        h1          = (p.get("h1") or "").strip()
        words       = p.get("word_count", 0) or 0
        rt          = p.get("response_time_ms", 0) or 0
        canonical   = (p.get("canonical_url") or "").strip()
        depth       = p.get("depth") or 0
        issues      = p.get("issues_detected") or []
        robots_meta = (p.get("robots") or "").lower()
        page_size   = p.get("size") or 0
        images      = p.get("images") or []
        b_images    = p.get("broken_images") or []
        linked_from = p.get("linked_from") or []
        redirects_chain = p.get("redirects") or []
        og_tags     = p.get("og_tags") or {}
        viewport    = (p.get("viewport") or "").strip()
        hreflang    = p.get("hreflang") or []

        all_page_urls.add(url)
        status_str = str(status)
        status_buckets[status_str[:1] + "xx"].append(url)

        # On-page checks (2xx pages only)
        if status_str.startswith("2"):
            if not title:             missing_title.append(url)
            if not meta:              missing_meta.append(url)
            if not h1:                missing_h1.append(url)
            if title and len(title) > 60:   long_title.append((url, title))
            if title and len(title) < 30:   short_title.append((url, title))
            if meta and len(meta) > 160:    long_meta.append((url, meta))
            if meta and 0 < len(meta) < 70: short_meta.append((url, meta))
            if 0 < words < 300:             thin_content.append((url, words))
            if rt > 3000:                   slow_pages.append((url, rt))

            if title:  dup_titles[title].append(url)
            if meta:   dup_metas[meta].append(url)

            # Canonical
            if not canonical:
                no_canonical.append(url)
            elif canonical == url:
                self_canonical.append(url)
            else:
                non_self_canonical.append((url, canonical))

            # H1 vs title mismatch
            if title and h1:
                stopwords = {'the','a','an','and','or','for','in','on','at','to','of','is','are'}
                t_kw = set(re.sub(r'[^a-z0-9 ]','',title.lower()).split()) - stopwords
                h_kw = set(re.sub(r'[^a-z0-9 ]','',h1.lower()).split()) - stopwords
                if t_kw and h_kw and not t_kw.intersection(h_kw):
                    h1_title_mismatch.append((url, title[:60], h1[:60]))

            # Depth
            if depth > 4:
                deep_pages.append((url, depth))

            # Noindex via robots meta tag
            if "noindex" in robots_meta:
                noindex_pages.append((url, robots_meta))

            # Page size > 500KB (heavy page)
            if page_size > 500_000:
                large_pages.append((url, page_size))

            # Image alt text
            if isinstance(images, list):
                no_alt = sum(1 for img in images
                             if isinstance(img, dict) and not (img.get("alt") or "").strip())
                if no_alt:
                    missing_alt_pages.append((url, no_alt))

            # Broken images
            if isinstance(b_images, list) and b_images:
                broken_img_pages.append((url, len(b_images), b_images[:5]))

            # Orphan page (no inbound links at all)
            if not linked_from:
                orphan_pages.append(url)

            # Redirect chain (page itself underwent >1 redirect to get here)
            if isinstance(redirects_chain, list) and len(redirects_chain) > 1:
                redirect_chains.append((url, len(redirects_chain), redirects_chain[:3]))

            # Open Graph tags
            og_title = og_tags.get("og:title") or og_tags.get("title") or ""
            og_desc  = og_tags.get("og:description") or og_tags.get("description") or ""
            if not og_title or not og_desc:
                missing_og_pages.append(url)

            # Viewport (mobile friendliness)
            if not viewport:
                missing_viewport.append(url)

            # Hreflang
            if isinstance(hreflang, list) and hreflang:
                hreflang_pages.append((url, hreflang))

        # URL quality (all status codes)
        parsed_url = urlparse(url)
        upath      = parsed_url.path
        query      = parsed_url.query
        if upath != upath.lower():
            uppercase_urls.append(url)
        if len(url) > 115:
            long_urls.append((url, len(url)))
        if query.count("=") > 3:
            url_params_heavy.append(url)

        # Issues breakdown
        if isinstance(issues, list):
            for iss in issues:
                if isinstance(iss, str):
                    issues_type_count[iss] += 1
                elif isinstance(iss, dict):
                    issues_type_count[iss.get("type","unknown")] += 1
        elif isinstance(issues, str) and issues:
            issues_type_count[issues] += 1

    # Filter duplicates (2+ pages with same value)
    dup_titles = {t: urls for t, urls in dup_titles.items() if len(urls) > 1}
    dup_metas  = {m: urls for m, urls in dup_metas.items()  if len(urls) > 1}

    # Cross-check: canonical pointing to broken URL
    broken_urls = set(status_buckets.get("4xx", []) + status_buckets.get("5xx", []))
    for url, canonical in non_self_canonical:
        if canonical in broken_urls:
            bad_canonical.append((url, canonical))

    broken   = status_buckets.get("4xx", []) + status_buckets.get("5xx", [])
    redirect = status_buckets.get("3xx", [])
    ok       = status_buckets.get("2xx", [])

    # ── Build Markdown ────────────────────────────────────────────────────────
    lines = []
    def h(level, text): lines.append(f"\n{'#' * level} {text}\n")
    def li(text):       lines.append(f"- {text}")
    def sep():          lines.append("\n---\n")

    # Header
    lines.append(f"# SEO Audit Report — {domain}")
    lines.append(f"**Generated:** {now}  |  **Crawl ID:** {crawl_id}  |  **Pages:** {total}\n")
    sep()

    # ── Summary scorecard ─────────────────────────────────────────────────────
    h(2, "📊 Summary")
    lines.append(f"| Metric | Count | Status |")
    lines.append(f"|--------|-------|--------|")
    lines.append(f"| Pages crawled | {total} | |")
    lines.append(f"| 200 OK | {len(ok)} | {'✅' if len(ok) == total else '⚠️'} |")
    lines.append(f"| Broken (4xx/5xx) | {len(broken)} | {'✅' if not broken else '🔴'} |")
    lines.append(f"| Redirects (3xx) | {len(redirect)} | {'✅' if not redirect else '⚠️'} |")
    lines.append(f"| Missing title | {len(missing_title)} | {'✅' if not missing_title else '🔴'} |")
    lines.append(f"| Missing meta desc | {len(missing_meta)} | {'✅' if not missing_meta else '🔴'} |")
    lines.append(f"| Missing H1 | {len(missing_h1)} | {'✅' if not missing_h1 else '🔴'} |")
    lines.append(f"| Duplicate titles | {len(dup_titles)} | {'✅' if not dup_titles else '🔴'} |")
    lines.append(f"| Duplicate meta desc | {len(dup_metas)} | {'✅' if not dup_metas else '🔴'} |")
    lines.append(f"| Title too long (>60) | {len(long_title)} | {'✅' if not long_title else '⚠️'} |")
    lines.append(f"| Title too short (<30) | {len(short_title)} | {'✅' if not short_title else '⚠️'} |")
    lines.append(f"| Meta too long (>160) | {len(long_meta)} | {'✅' if not long_meta else '⚠️'} |")
    lines.append(f"| Meta too short (<70) | {len(short_meta)} | {'✅' if not short_meta else '⚠️'} |")
    lines.append(f"| Missing canonical | {len(no_canonical)} | {'✅' if not no_canonical else '⚠️'} |")
    lines.append(f"| Non-self canonical | {len(non_self_canonical)} | {'✅' if not non_self_canonical else '⚠️'} |")
    lines.append(f"| Bad canonical (→ 4xx) | {len(bad_canonical)} | {'✅' if not bad_canonical else '🔴'} |")
    lines.append(f"| Thin content (<300w) | {len(thin_content)} | {'✅' if not thin_content else '⚠️'} |")
    lines.append(f"| Slow pages (>3s) | {len(slow_pages)} | {'✅' if not slow_pages else '⚠️'} |")
    lines.append(f"| H1 ↔ Title mismatch | {len(h1_title_mismatch)} | {'✅' if not h1_title_mismatch else '⚠️'} |")
    lines.append(f"| Deep pages (depth >4) | {len(deep_pages)} | {'✅' if not deep_pages else '⚠️'} |")
    lines.append(f"| Uppercase in URL | {len(uppercase_urls)} | {'✅' if not uppercase_urls else '⚠️'} |")
    lines.append(f"| URL too long (>115c) | {len(long_urls)} | {'✅' if not long_urls else '⚠️'} |")
    lines.append(f"| Noindex pages | {len(noindex_pages)} | {'✅' if not noindex_pages else '⚠️ check each'} |")
    lines.append(f"| Images missing alt | {len(missing_alt_pages)} pages | {'✅' if not missing_alt_pages else '⚠️'} |")
    lines.append(f"| Broken images | {len(broken_img_pages)} pages | {'✅' if not broken_img_pages else '🔴'} |")
    lines.append(f"| Orphan pages | {len(orphan_pages)} | {'✅' if not orphan_pages else '⚠️'} |")
    lines.append(f"| Redirect chains (>1 hop) | {len(redirect_chains)} | {'✅' if not redirect_chains else '⚠️'} |")
    lines.append(f"| Missing OG tags | {len(missing_og_pages)} | {'✅' if not missing_og_pages else '⚠️'} |")
    lines.append(f"| Missing viewport meta | {len(missing_viewport)} | {'✅' if not missing_viewport else '🔴'} |")
    lines.append("")
    sep()

    # ── CRITICAL ──────────────────────────────────────────────────────────────
    h(2, "🔴 Critical — Fix First")

    # Broken pages with source
    if broken:
        h(3, f"Broken Pages ({len(broken)})")
        lines.append("> **Fix:** 301 to the correct URL, or remove internal links pointing here.\n")
        lines.append("| URL | Status | Linked From |")
        lines.append("|-----|--------|-------------|")
        for url in broken:
            s = next((p.get("status_code","?") for p in pages if p.get("url") == url), "?")
            sources = inbound.get(url, [])
            src_str = ", ".join(f"`{s}`" for s in sources[:3])
            if len(sources) > 3:
                src_str += f" +{len(sources)-3} more"
            lines.append(f"| `{url}` | {s} | {src_str or '—'} |")
        lines.append("")

    # Bad canonical (pointing to 4xx/5xx)
    if bad_canonical:
        h(3, f"Canonical Points to Broken URL ({len(bad_canonical)})")
        lines.append("> **Fix:** Update canonical to point to a live 200 page. A canonical to a 4xx = Google ignores it.\n")
        lines.append("| Page | Broken Canonical Target |")
        lines.append("|------|------------------------|")
        for url, canonical in bad_canonical[:20]:
            lines.append(f"| `{url}` | `{canonical}` |")
        lines.append("")

    # Duplicate titles
    if dup_titles:
        h(3, f"Duplicate Titles ({len(dup_titles)} groups)")
        lines.append("> **Fix:** Every page needs a unique title. Redirect or merge pages if they cover the same topic.\n")
        for title, urls in list(dup_titles.items())[:10]:
            lines.append(f"**\"{title[:70]}\"**")
            for u in urls:
                li(f"`{u}`")
            lines.append("")

    # Missing titles
    if missing_title:
        h(3, f"Missing Title Tag ({len(missing_title)} pages)")
        lines.append("> **Fix:** Add a unique `<title>` tag (50–60 chars) to each page.\n")
        for url in missing_title[:20]:
            li(f"`{url}`")
        if len(missing_title) > 20:
            lines.append(f"… and {len(missing_title)-20} more")
        lines.append("")

    if not broken and not bad_canonical and not dup_titles and not missing_title:
        lines.append("✅ No critical issues found.\n")

    sep()

    # ── WARNINGS ──────────────────────────────────────────────────────────────
    h(2, "⚠️ Warnings — High Impact")

    # Missing meta descriptions
    if missing_meta:
        h(3, f"Missing Meta Description ({len(missing_meta)} pages)")
        lines.append("> **Fix:** Add a unique meta description (120–155 chars). Directly improves click-through rate.\n")
        for url in missing_meta[:30]:
            li(f"`{url}`")
        if len(missing_meta) > 30:
            lines.append(f"… and {len(missing_meta)-30} more")
        lines.append("")

    # Duplicate meta descriptions
    if dup_metas:
        h(3, f"Duplicate Meta Descriptions ({len(dup_metas)} groups)")
        lines.append("> **Fix:** Write unique meta descriptions for each page. Duplicates waste click-through potential.\n")
        for meta, urls in list(dup_metas.items())[:8]:
            lines.append(f"**\"{meta[:80]}\"**")
            for u in urls[:5]:
                li(f"`{u}`")
            lines.append("")

    # Meta too long
    if long_meta:
        h(3, f"Meta Description Too Long — over 160 chars ({len(long_meta)} pages)")
        lines.append("> **Fix:** Shorten to 120–155 chars. Google truncates longer descriptions with '…'\n")
        lines.append("| URL | Length | Preview |")
        lines.append("|-----|--------|---------|")
        for url, meta in long_meta[:20]:
            lines.append(f"| `{url}` | {len(meta)} | {meta[:80]}… |")
        lines.append("")

    # Meta too short
    if short_meta:
        h(3, f"Meta Description Too Short — under 70 chars ({len(short_meta)} pages)")
        lines.append("> **Fix:** Expand to 120–155 chars. Short descriptions leave SERP real estate empty.\n")
        lines.append("| URL | Length | Current |")
        lines.append("|-----|--------|---------|")
        for url, meta in short_meta[:15]:
            lines.append(f"| `{url}` | {len(meta)} | {meta} |")
        lines.append("")

    # Missing H1
    if missing_h1:
        h(3, f"Missing H1 ({len(missing_h1)} pages)")
        lines.append("> **Fix:** Add exactly one `<h1>` per page matching the primary keyword.\n")
        for url in missing_h1[:20]:
            li(f"`{url}`")
        if len(missing_h1) > 20:
            lines.append(f"… and {len(missing_h1)-20} more")
        lines.append("")

    # Long titles
    if long_title:
        h(3, f"Title Too Long — over 60 chars ({len(long_title)} pages)")
        lines.append("> **Fix:** Shorten to 50–60 chars. Google truncates anything longer.\n")
        lines.append("| URL | Title (truncated) | Length |")
        lines.append("|-----|-------------------|--------|")
        for url, title in long_title[:20]:
            lines.append(f"| `{url}` | {title[:60]}… | {len(title)} |")
        if len(long_title) > 20:
            lines.append(f"| … | {len(long_title)-20} more | |")
        lines.append("")

    # Short titles
    if short_title:
        h(3, f"Title Too Short — under 30 chars ({len(short_title)} pages)")
        lines.append("> **Fix:** Expand to 50–60 chars. Include the primary keyword.\n")
        lines.append("| URL | Title | Length |")
        lines.append("|-----|-------|--------|")
        for url, title in short_title[:15]:
            lines.append(f"| `{url}` | {title} | {len(title)} |")
        lines.append("")

    # H1 ↔ Title mismatch
    if h1_title_mismatch:
        h(3, f"H1 and Title Share No Keywords ({len(h1_title_mismatch)} pages)")
        lines.append("> **Fix:** Align H1 and `<title>` on the same primary keyword. Google expects them to be consistent.\n")
        lines.append("| URL | Title | H1 |")
        lines.append("|-----|-------|----|")
        for url, title, h1 in h1_title_mismatch[:15]:
            lines.append(f"| `{url}` | {title} | {h1} |")
        lines.append("")

    # Thin content
    if thin_content:
        h(3, f"Thin Content — under 300 words ({len(thin_content)} pages)")
        lines.append("> **Fix:** Expand with useful content, or add `noindex` if it's a utility/pagination page.\n")
        lines.append("| URL | Words |")
        lines.append("|-----|-------|")
        for url, words in sorted(thin_content, key=lambda x: x[1])[:20]:
            lines.append(f"| `{url}` | {words} |")
        lines.append("")

    # Slow pages
    if slow_pages:
        h(3, f"Slow Server Response — over 3s ({len(slow_pages)} pages)")
        lines.append("> **Fix:** Check server caching, image optimisation, and plugin bloat. Target <1s TTFB.\n")
        lines.append("| URL | Response Time |")
        lines.append("|-----|--------------|")
        for url, rt in sorted(slow_pages, key=lambda x: -x[1])[:20]:
            lines.append(f"| `{url}` | {rt:,}ms |")
        lines.append("")

    sep()

    # ── CANONICAL ─────────────────────────────────────────────────────────────
    h(2, "🔗 Canonical Analysis")

    # Summary line
    self_can_count = len(self_canonical)
    lines.append(f"| Type | Count | Notes |")
    lines.append(f"|------|-------|-------|")
    lines.append(f"| Self-referencing (correct) | {self_can_count} | ✅ Standard best practice |")
    lines.append(f"| Missing canonical | {len(no_canonical)} | {'✅' if not no_canonical else '⚠️ Duplicate content risk'} |")
    lines.append(f"| Non-self canonical | {len(non_self_canonical)} | {'✅' if not non_self_canonical else '⚠️ These pages are canonicalized away'} |")
    lines.append(f"| Canonical → broken URL | {len(bad_canonical)} | {'✅' if not bad_canonical else '🔴 Fix immediately'} |")
    lines.append("")

    if no_canonical:
        h(3, f"Missing Canonical Tag ({len(no_canonical)} pages)")
        lines.append("> **Fix:** Add `<link rel=\"canonical\" href=\"{page_url}\">` to each page.\n")
        for url in no_canonical[:20]:
            li(f"`{url}`")
        if len(no_canonical) > 20:
            lines.append(f"… and {len(no_canonical)-20} more")
        lines.append("")

    if non_self_canonical:
        h(3, f"Pages Canonicalized to Other URLs ({len(non_self_canonical)})")
        lines.append("> These pages signal to Google: 'don't index me, index this other URL instead.' "
                     "Verify this is intentional — if not, update the canonical.\n")
        lines.append("| Page | Canonical Points To |")
        lines.append("|------|---------------------|")
        for url, canonical in non_self_canonical[:20]:
            lines.append(f"| `{url}` | `{canonical}` |")
        if len(non_self_canonical) > 20:
            lines.append(f"| … | {len(non_self_canonical)-20} more |")
        lines.append("")

    sep()

    # ── TECHNICAL / URL QUALITY ───────────────────────────────────────────────
    h(2, "🔧 Technical SEO")

    if uppercase_urls:
        h(3, f"Uppercase Letters in URL ({len(uppercase_urls)} pages)")
        lines.append("> **Fix:** Redirect uppercase URLs to lowercase equivalents. `URL` and `url` are treated as different pages.\n")
        for url in uppercase_urls[:15]:
            li(f"`{url}`")
        lines.append("")

    if long_urls:
        h(3, f"URL Too Long — over 115 chars ({len(long_urls)} pages)")
        lines.append("> **Fix:** Shorten slugs. Long URLs are harder to share and may signal keyword stuffing.\n")
        lines.append("| URL | Length |")
        lines.append("|-----|--------|")
        for url, length in sorted(long_urls, key=lambda x: -x[1])[:15]:
            lines.append(f"| `{url[:100]}…` | {length} |")
        lines.append("")

    if url_params_heavy:
        h(3, f"URLs with Excessive Query Parameters ({len(url_params_heavy)} pages)")
        lines.append("> **Fix:** Use canonical tags or robots.txt to prevent Googlebot wasting crawl budget on param variants.\n")
        for url in url_params_heavy[:10]:
            li(f"`{url}`")
        lines.append("")

    if deep_pages:
        h(3, f"Pages Too Deep — depth > 4 ({len(deep_pages)} pages)")
        lines.append("> **Fix:** Restructure navigation so important pages are reachable in ≤3 clicks from homepage.\n")
        lines.append("| URL | Depth |")
        lines.append("|-----|-------|")
        for url, depth in sorted(deep_pages, key=lambda x: -x[1])[:20]:
            lines.append(f"| `{url}` | {depth} |")
        lines.append("")

    if not uppercase_urls and not long_urls and not url_params_heavy and not deep_pages:
        lines.append("✅ No URL quality issues found.\n")

    # Site-level checks
    if site_data:
        h(3, "Site-Level Checks")

        robots = site_data.get("robots_txt", {})
        sitemap = site_data.get("sitemap", {})
        https_r = site_data.get("https_redirect", {})
        www_r   = site_data.get("www_redirect", {})

        lines.append("| Check | Result |")
        lines.append("|-------|--------|")
        lines.append(f"| robots.txt | {'✅ Found' if robots.get('found') else '⚠️ Missing'} |")
        if robots.get("found"):
            lines.append(f"| Disallow rules | {robots.get('disallow_count', 0)} rules |")
            lines.append(f"| Sitemap in robots.txt | {'✅ Yes' if robots.get('sitemap_declared') else '⚠️ Not declared'} |")
        lines.append(f"| sitemap.xml | {'✅ Found' if sitemap.get('found') else '⚠️ Missing'} |")
        if sitemap.get("found"):
            lines.append(f"| Sitemap URLs | {sitemap.get('url_count', '?')} |")
        lines.append(f"| HTTPS redirect | {'✅ Correct' if https_r.get('http_redirects_to_https') else ('⚠️ Missing/broken' if not https_r.get('error') else '—')} |")
        lines.append(f"| www redirect | {'✅ Correct' if www_r.get('alt_redirects_properly') else '⚠️ Not set up'} |")
        lines.append("")

        if robots.get("important_blocked"):
            lines.append("> ⚠️ **Potential over-blocking in robots.txt:**")
            for rule in robots["important_blocked"][:5]:
                li(f"`Disallow: {rule}`")
            lines.append("")

        if not sitemap.get("found"):
            lines.append(f"> ⚠️ **No sitemap found** — submit one to GSC at {base_url}/sitemap.xml\n")

        www_warning = www_r.get("warning")
        if www_warning:
            lines.append(f"> ⚠️ {www_warning}\n")

        https_warning = https_r.get("warning")
        if https_warning:
            lines.append(f"> ⚠️ {https_warning}\n")

    sep()

    # ── Redirects ─────────────────────────────────────────────────────────────
    if redirect:
        h(2, f"↪️ Redirects ({len(redirect)} pages)")
        lines.append("> **Fix:** Update internal links to point to the final destination URL. "
                     "Each redirect wastes crawl budget and loses a fraction of link equity.\n")
        for url in redirect[:20]:
            li(f"`{url}`")
        if len(redirect) > 20:
            lines.append(f"… and {len(redirect)-20} more")
        lines.append("")
        sep()

    # ── Images ────────────────────────────────────────────────────────────────
    if missing_alt_pages or broken_img_pages:
        h(2, "🖼️ Images")
        if missing_alt_pages:
            h(3, f"Images Missing Alt Text ({len(missing_alt_pages)} pages)")
            lines.append("> **Fix:** Add descriptive `alt` attributes. Critical for accessibility and image search.\n")
            lines.append("| URL | Images Missing Alt |")
            lines.append("|-----|-------------------|")
            total_missing = sum(c for _, c in missing_alt_pages)
            for url, count in sorted(missing_alt_pages, key=lambda x: -x[1])[:20]:
                lines.append(f"| `{url}` | {count} |")
            lines.append(f"\n**Total images missing alt:** {total_missing}\n")
        if broken_img_pages:
            h(3, f"Broken Images ({len(broken_img_pages)} pages)")
            lines.append("> **Fix:** Upload missing images or update src URLs.\n")
            for url, count, samples in broken_img_pages[:15]:
                lines.append(f"**{url}** — {count} broken image(s)")
                for img in samples[:3]:
                    src = img.get("src") or img if isinstance(img, str) else str(img)
                    li(f"`{src}`")
                lines.append("")
        sep()

    # ── Noindex pages ─────────────────────────────────────────────────────────
    if noindex_pages:
        h(2, f"🚫 Noindex Pages ({len(noindex_pages)})")
        lines.append("> Review each — noindex intentionally hides a page from Google. "
                     "Accidental noindex on important pages = invisible to search.\n")
        lines.append("| URL | Robots Meta |")
        lines.append("|-----|------------|")
        for url, robots_val in noindex_pages[:30]:
            lines.append(f"| `{url}` | `{robots_val}` |")
        if len(noindex_pages) > 30:
            lines.append(f"| … | {len(noindex_pages)-30} more |")
        lines.append("")
        sep()

    # ── Orphan pages ─────────────────────────────────────────────────────────
    if orphan_pages:
        h(2, f"👻 Orphan Pages — No Inbound Links ({len(orphan_pages)})")
        lines.append("> These pages have zero internal links pointing to them. "
                     "Google rarely discovers or ranks pages it can't reach via internal linking.\n")
        lines.append("> **Fix:** Add internal links from relevant pages, or noindex if they're utility pages.\n")
        for url in orphan_pages[:20]:
            li(f"`{url}`")
        if len(orphan_pages) > 20:
            lines.append(f"… and {len(orphan_pages)-20} more")
        lines.append("")
        sep()

    # ── Redirect chains ───────────────────────────────────────────────────────
    if redirect_chains:
        h(2, f"⛓️ Redirect Chains — More Than 1 Hop ({len(redirect_chains)})")
        lines.append("> **Fix:** Redirect directly to the final URL. Each extra hop wastes crawl budget and loses link equity.\n")
        lines.append("| Final URL | Chain Length | Chain |")
        lines.append("|-----------|-------------|-------|")
        for url, depth_val, chain in redirect_chains[:15]:
            chain_str = " → ".join(f"`{u}`" for u in chain[:3])
            lines.append(f"| `{url}` | {depth_val} | {chain_str} |")
        lines.append("")
        sep()

    # ── Open Graph ────────────────────────────────────────────────────────────
    if missing_og_pages:
        h(2, f"📱 Missing Open Graph Tags ({len(missing_og_pages)} pages)")
        lines.append("> **Fix:** Add `og:title`, `og:description`, `og:image` to every page. "
                     "Controls how pages appear when shared on Facebook, LinkedIn, Slack, etc.\n")
        for url in missing_og_pages[:20]:
            li(f"`{url}`")
        if len(missing_og_pages) > 20:
            lines.append(f"… and {len(missing_og_pages)-20} more")
        lines.append("")
        sep()

    # ── Viewport / mobile ─────────────────────────────────────────────────────
    if missing_viewport:
        h(2, f"📵 Missing Viewport Meta ({len(missing_viewport)} pages)")
        lines.append("> **Fix:** Add `<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">`. "
                     "Without it, Google treats the page as non-mobile-friendly.\n")
        for url in missing_viewport[:20]:
            li(f"`{url}`")
        lines.append("")
        sep()

    # ── Hreflang ─────────────────────────────────────────────────────────────
    if hreflang_pages:
        h(2, f"🌍 Hreflang ({len(hreflang_pages)} pages declare it)")
        lines.append("> Pages using hreflang — verify each language variant has a reciprocal return tag.\n")
        lang_count = defaultdict(int)
        for url, tags in hreflang_pages:
            for tag in (tags if isinstance(tags, list) else []):
                lang = tag.get("lang") or tag.get("hreflang") or str(tag)
                lang_count[lang] += 1
        lines.append("| Language | Pages |")
        lines.append("|----------|-------|")
        for lang, count in sorted(lang_count.items(), key=lambda x: -x[1])[:15]:
            lines.append(f"| `{lang}` | {count} |")
        lines.append("")
        sep()

    # ── Analytics coverage ────────────────────────────────────────────────────
    # analytics field: {ga4_id, gtm_id, fb_pixel, hotjar, mixpanel, ...}
    pages_no_analytics   = []
    pages_no_ga4         = []
    pages_no_gtm         = []
    analytics_tool_count = defaultdict(int)

    for p in pages:
        if not str(p.get("status_code","")).startswith("2"):
            continue
        analytics = p.get("analytics") or {}
        url = p.get("url", "")
        if analytics:
            # Count which tools are present across site
            tool_keys = {
                "ga4_id": "ga4_id", "gtm_id": "gtm_id",
                "fb_pixel": "facebook_pixel",   # LibreCrawl uses facebook_pixel
                "hotjar": "hotjar", "mixpanel": "mixpanel",
            }
            for label, key in tool_keys.items():
                if analytics.get(key):
                    analytics_tool_count[label] += 1
            if not analytics.get("ga4_id"):
                pages_no_ga4.append(url)
            if not analytics.get("gtm_id"):
                pages_no_gtm.append(url)
        else:
            pages_no_analytics.append(url)

    if analytics_tool_count or pages_no_analytics:
        h(2, "📈 Analytics Coverage")
        if analytics_tool_count:
            lines.append("| Tool | Pages Detected |")
            lines.append("|------|---------------|")
            tool_labels = {"ga4_id":"Google Analytics 4","gtm_id":"Google Tag Manager",
                           "fb_pixel":"Facebook Pixel","hotjar":"Hotjar","mixpanel":"Mixpanel"}
            for tool, count in sorted(analytics_tool_count.items(), key=lambda x: -x[1]):
                label = tool_labels.get(tool, tool)
                lines.append(f"| {label} | {count} / {len(ok)} pages |")
            lines.append("")
            if pages_no_ga4 and analytics_tool_count.get("ga4_id"):
                h(3, f"Pages Missing GA4 ({len(pages_no_ga4)})")
                lines.append("> **Fix:** Ensure GA4 fires on every page — check tag triggers in GTM.\n")
                for url in pages_no_ga4[:15]:
                    li(f"`{url}`")
                lines.append("")
        if pages_no_analytics:
            h(3, f"Pages with No Analytics Detected ({len(pages_no_analytics)})")
            lines.append("> **Fix:** Add GA4 / GTM to these pages. Untracked pages = blind spots in reporting.\n")
            for url in pages_no_analytics[:15]:
                li(f"`{url}`")
            lines.append("")
        sep()

    # ── Heading structure ─────────────────────────────────────────────────────
    pages_no_h2        = []   # has content but no H2
    pages_h2_no_h1     = []   # has H2 but no H1 (skipped H1)
    pages_heading_rich = []   # >10 H2s (possibly over-structured or auto-generated)

    for p in pages:
        if not str(p.get("status_code","")).startswith("2"):
            continue
        url   = p.get("url","")
        h1    = (p.get("h1") or "").strip()
        h2s   = p.get("h2") or []
        words = p.get("word_count", 0) or 0
        h2_count = len(h2s) if isinstance(h2s, list) else (1 if h2s else 0)

        if words > 200 and h2_count == 0:
            pages_no_h2.append((url, words))
        if h2_count > 0 and not h1:
            pages_h2_no_h1.append(url)
        if h2_count > 10:
            pages_heading_rich.append((url, h2_count))

    if pages_no_h2 or pages_h2_no_h1:
        h(2, "🏗️ Heading Structure")
        if pages_no_h2:
            h(3, f"Pages with Content but No H2 ({len(pages_no_h2)})")
            lines.append("> **Fix:** Break long content into sections with H2 subheadings. "
                         "H2s are the primary way crawlers and readers understand page structure.\n")
            lines.append("| URL | Words |")
            lines.append("|-----|-------|")
            for url, words in sorted(pages_no_h2, key=lambda x: -x[1])[:15]:
                lines.append(f"| `{url}` | {words} |")
            lines.append("")
        if pages_h2_no_h1:
            h(3, f"Has H2 but No H1 ({len(pages_h2_no_h1)} pages)")
            lines.append("> **Fix:** Add an H1. Having H2s without H1 breaks heading hierarchy.\n")
            for url in pages_h2_no_h1[:10]:
                li(f"`{url}`")
            lines.append("")
        if pages_heading_rich:
            h(3, f"Unusually High H2 Count — over 10 ({len(pages_heading_rich)} pages)")
            lines.append("> Review: many H2s on one page can dilute keyword focus.\n")
            for url, count in sorted(pages_heading_rich, key=lambda x: -x[1])[:10]:
                li(f"`{url}` — {count} H2s")
            lines.append("")
        sep()

    # ── Issues breakdown (LibreCrawl's own detector) ──────────────────────────
    if issues_type_count:
        h(2, "🐛 Issue Type Breakdown (LibreCrawl detector)")
        lines.append("| Issue Type | Count |")
        lines.append("|------------|-------|")
        for issue_type, count in sorted(issues_type_count.items(), key=lambda x: -x[1])[:30]:
            lines.append(f"| {issue_type} | {count} |")
        lines.append("")
        sep()

    # ── All Pages ─────────────────────────────────────────────────────────────
    h(2, "📋 All Pages")
    lines.append("| Status | Depth | URL | Title | Words | Canon |")
    lines.append("|--------|-------|-----|-------|-------|-------|")

    sorted_pages = sorted(pages, key=lambda p: (
        0 if str(p.get("status_code","")).startswith("4") else
        1 if str(p.get("status_code","")).startswith("5") else
        2 if str(p.get("status_code","")).startswith("3") else 3,
        p.get("depth", 99)
    ))

    for p in sorted_pages[:300]:
        url    = p.get("url", "")
        status = p.get("status_code", "?")
        title  = (p.get("title") or "")[:45] or "—"
        words  = p.get("word_count", 0) or 0
        depth  = p.get("depth", "?")
        canonical = (p.get("canonical_url") or "").strip()
        canon_icon = "✅" if canonical == url else ("—" if not canonical else "↪️")
        status_icon = "🔴" if str(status).startswith(("4","5")) else "↪️" if str(status).startswith("3") else "✅"
        lines.append(f"| {status_icon} {status} | {depth} | `{url}` | {title} | {words} | {canon_icon} |")

    if len(pages) > 300:
        lines.append(f"| … | | {len(pages)-300} more pages not shown | | | |")

    lines.append("")
    sep()

    # ── Fix Priority Checklist ────────────────────────────────────────────────
    h(2, "✅ Fix Priority Checklist")
    lines.append("Copy this into your task tracker:\n")

    priority = 1
    checks = [
        (broken,           f"Fix {len(broken)} broken pages (4xx/5xx)"),
        (bad_canonical,    f"Fix {len(bad_canonical)} canonical tags pointing to broken URLs"),
        (dup_titles,       f"Resolve {len(dup_titles)} duplicate title groups"),
        (missing_title,    f"Add title tags to {len(missing_title)} pages"),
        (missing_meta,     f"Add meta descriptions to {len(missing_meta)} pages"),
        (missing_h1,       f"Add H1 to {len(missing_h1)} pages"),
        (long_title,       f"Shorten {len(long_title)} titles to ≤60 chars"),
        (short_title,      f"Expand {len(short_title)} short titles to 50–60 chars"),
        (long_meta,        f"Shorten {len(long_meta)} meta descriptions to ≤160 chars"),
        (dup_metas,        f"Unique-ify {len(dup_metas)} duplicate meta descriptions"),
        (no_canonical,     f"Add canonical tags to {len(no_canonical)} pages"),
        (non_self_canonical, f"Review {len(non_self_canonical)} pages canonicalized to other URLs"),
        (thin_content,     f"Address {len(thin_content)} thin content pages (<300 words)"),
        (slow_pages,       f"Fix {len(slow_pages)} slow pages (>3s response time)"),
        (h1_title_mismatch, f"Align H1 and title keywords on {len(h1_title_mismatch)} pages"),
        (redirect,         f"Update internal links for {len(redirect)} redirect targets"),
        (broken_img_pages, f"Fix broken images on {len(broken_img_pages)} pages"),
        (missing_alt_pages, f"Add alt text to images on {len(missing_alt_pages)} pages"),
        (orphan_pages,     f"Add internal links to {len(orphan_pages)} orphan pages"),
        (redirect_chains,  f"Collapse {len(redirect_chains)} redirect chains to single hops"),
        (missing_viewport, f"Add viewport meta to {len(missing_viewport)} pages"),
        (missing_og_pages, f"Add OG tags to {len(missing_og_pages)} pages"),
        (noindex_pages,    f"Review {len(noindex_pages)} noindex pages — verify intentional"),
        (uppercase_urls,   f"Lowercase {len(uppercase_urls)} URLs with uppercase characters"),
        (long_urls,        f"Shorten {len(long_urls)} URLs over 115 chars"),
        (deep_pages,       f"Improve crawlability: {len(deep_pages)} pages at depth >4"),
    ]
    for condition, label in checks:
        if condition:
            lines.append(f"- [ ] **P{priority}** {label}")
            priority += 1

    lines.append("")
    lines.append(f"---\n*Generated by [librecrawl-mcp](https://github.com/adityaarsharma/librecrawl-mcp)*")

    return "\n".join(lines)


# ── Strict-audit helpers (v1.2.0) ─────────────────────────────────────────────
# These exist to remove "silent caps": every audit now produces a per-page
# issue matrix, a sitemap reconciliation table, and a completeness manifest
# so the caller can verify the audit was actually exhaustive.

# Canonical check inventory — the 37 named checks the audit runs and whether
# each one is computed for every crawled page or only at the site level.
# Used by _build_checks_manifest() to label coverage in strict-audit output.
CHECKS_INVENTORY = [
    # name, section, scope ("all_pages" | "site" | "subset")
    ("status_codes_4xx",     "Status Codes",   "all_pages"),
    ("status_codes_5xx",     "Status Codes",   "all_pages"),
    ("status_codes_3xx",     "Status Codes",   "all_pages"),
    ("missing_title",        "On-Page Meta",   "all_pages"),
    ("missing_meta",         "On-Page Meta",   "all_pages"),
    ("missing_h1",           "On-Page Meta",   "all_pages"),
    ("long_title",           "On-Page Meta",   "all_pages"),
    ("short_title",          "On-Page Meta",   "all_pages"),
    ("long_meta",            "On-Page Meta",   "all_pages"),
    ("short_meta",           "On-Page Meta",   "all_pages"),
    ("thin_content",         "Content",        "all_pages"),
    ("duplicate_titles",     "Duplicates",     "all_pages"),
    ("duplicate_metas",      "Duplicates",     "all_pages"),
    ("slow_pages",           "Performance",    "all_pages"),
    ("missing_canonical",    "Canonical",      "all_pages"),
    ("non_self_canonical",   "Canonical",      "all_pages"),
    ("bad_canonical",        "Canonical",      "all_pages"),
    ("uppercase_urls",       "URL Quality",    "all_pages"),
    ("long_urls",            "URL Quality",    "all_pages"),
    ("deep_pages",           "Site Depth",     "all_pages"),
    ("h1_title_mismatch",    "On-Page Meta",   "all_pages"),
    ("url_params_heavy",     "URL Quality",    "all_pages"),
    ("noindex_pages",        "Indexability",   "all_pages"),
    ("large_pages",          "Performance",    "all_pages"),
    ("missing_alt_pages",    "Images",         "all_pages"),
    ("broken_img_pages",     "Images",         "all_pages"),
    ("orphan_pages",         "Internal Links", "all_pages"),
    ("redirect_chains",      "Redirects",      "all_pages"),
    ("missing_og_pages",     "Social",         "all_pages"),
    ("missing_viewport",     "Mobile",         "all_pages"),
    ("hreflang_pages",       "Internationalisation", "all_pages"),
    ("robots_txt_found",     "Site",           "site"),
    ("sitemap_found",        "Site",           "site"),
    ("https_redirect",       "Site",           "site"),
    ("www_redirect",         "Site",           "site"),
    ("broken_inbound_links", "Internal Links", "all_pages"),
    ("issues_detected",      "Upstream",       "all_pages"),
]


def _build_checks_manifest(pages: list, site_data: dict, links: list) -> dict:
    """Compute pass/fail counts for every named check on the actual crawl output.

    For 'all_pages' scope checks, fail_count = number of pages that tripped the
    rule, pass_count = total - fail. For 'site' scope, fail=0/1 and pass=0/1.

    Used by strict audit to prove the audit's coverage to the caller.
    """
    total = len([p for p in pages if str(p.get("status_code","")).startswith("2")])
    fails = defaultdict(int)

    for p in pages:
        url    = p.get("url", "")
        status = str(p.get("status_code", ""))
        if status.startswith("4"):
            fails["status_codes_4xx"] += 1
        elif status.startswith("5"):
            fails["status_codes_5xx"] += 1
        elif status.startswith("3"):
            fails["status_codes_3xx"] += 1
        if not status.startswith("2"):
            continue

        title = (p.get("title") or "").strip()
        meta  = (p.get("meta_description") or "").strip()
        h1    = (p.get("h1") or "").strip()
        words = p.get("word_count", 0) or 0
        rt    = p.get("response_time_ms", 0) or 0
        canon = (p.get("canonical_url") or "").strip()
        depth = p.get("depth") or 0
        size  = p.get("size") or 0
        rmeta = (p.get("robots") or "").lower()
        og    = p.get("og_tags") or {}
        vp    = (p.get("viewport") or "").strip()
        imgs  = p.get("images") or []
        bimg  = p.get("broken_images") or []
        lkfrm = p.get("linked_from") or []
        rdr   = p.get("redirects") or []
        hl    = p.get("hreflang") or []

        if not title: fails["missing_title"] += 1
        if not meta:  fails["missing_meta"]  += 1
        if not h1:    fails["missing_h1"]    += 1
        if title and len(title) > 60:   fails["long_title"]  += 1
        if title and len(title) < 30:   fails["short_title"] += 1
        if meta and len(meta) > 160:    fails["long_meta"]   += 1
        if meta and 0 < len(meta) < 70: fails["short_meta"]  += 1
        if 0 < words < 300:             fails["thin_content"] += 1
        if rt > 3000:                   fails["slow_pages"]   += 1
        if not canon:                   fails["missing_canonical"] += 1
        elif canon != p.get("url",""):  fails["non_self_canonical"] += 1
        if depth > 4:                   fails["deep_pages"] += 1
        if "noindex" in rmeta:          fails["noindex_pages"] += 1
        if size > 500_000:              fails["large_pages"] += 1
        if isinstance(imgs, list):
            no_alt = sum(1 for i in imgs if isinstance(i, dict) and not (i.get("alt") or "").strip())
            if no_alt: fails["missing_alt_pages"] += 1
        if isinstance(bimg, list) and bimg: fails["broken_img_pages"] += 1
        if not lkfrm:                   fails["orphan_pages"] += 1
        if isinstance(rdr, list) and len(rdr) > 1: fails["redirect_chains"] += 1
        og_t = og.get("og:title") or og.get("title") or ""
        og_d = og.get("og:description") or og.get("description") or ""
        if not og_t or not og_d:        fails["missing_og_pages"] += 1
        if not vp:                      fails["missing_viewport"] += 1
        if isinstance(hl, list) and hl: fails["hreflang_pages"] += 1

        parsed = urlparse(url)
        if parsed.path != parsed.path.lower(): fails["uppercase_urls"] += 1
        if len(url) > 115:                     fails["long_urls"] += 1
        if parsed.query.count("=") > 3:        fails["url_params_heavy"] += 1

        if title:
            stop = {'the','a','an','and','or','for','in','on','at','to','of','is','are'}
            tk = set(re.sub(r'[^a-z0-9 ]','',title.lower()).split()) - stop
            hk = set(re.sub(r'[^a-z0-9 ]','',h1.lower()).split()) - stop
            if tk and hk and not tk.intersection(hk):
                fails["h1_title_mismatch"] += 1

        iss = p.get("issues_detected") or []
        if isinstance(iss, list) and iss:
            fails["issues_detected"] += 1

    # Site-level
    sd = site_data or {}
    site_fails = {
        "robots_txt_found": 0 if sd.get("robots_txt", {}).get("found") else 1,
        "sitemap_found":    0 if sd.get("sitemap",    {}).get("found") else 1,
        "https_redirect":   0 if sd.get("https_redirect", {}).get("http_redirects_to_https") else 1,
        "www_redirect":     0 if sd.get("www_redirect",   {}).get("ok", True) else 1,
    }

    manifest = {}
    for name, section, scope in CHECKS_INVENTORY:
        if scope == "site":
            f = site_fails.get(name, 0)
            manifest[name] = {
                "section": section, "scope": scope,
                "fail": f, "pass": 1 - f, "ran_on_all_pages": True,
            }
        else:
            f = fails.get(name, 0)
            manifest[name] = {
                "section": section, "scope": scope,
                "fail": f, "pass": max(0, total - f),
                "ran_on_all_pages": True,
            }

    return {
        "total_checks":  len(CHECKS_INVENTORY),
        "pages_in_scope": total,
        "checks":        manifest,
    }


def _compute_crawl_completeness(crawl_id, requested_max, pages_count, status_resp, started_at, deadline) -> dict:
    """Build the completeness certificate dict for an audit run.

    Inputs:
      crawl_id      — upstream crawl ID (int or None)
      requested_max — value caller passed for max_pages (0 = unlimited)
      pages_count   — len(pages) actually exported
      status_resp   — last /api/crawl_status snapshot dict
      started_at    — time.time() when the audit started
      deadline      — time.time() value the poll loop was watching
    """
    stats   = (status_resp or {}).get("stats", {}) or {}
    queued  = stats.get("queued", 0) or 0
    crawled = stats.get("crawled", pages_count) or 0
    elapsed = max(0, time.time() - started_at) if started_at else None
    timeout_hit = bool(elapsed is not None and deadline and time.time() >= deadline and queued > 0)
    robots_blocked = stats.get("robots_blocked", 0) or stats.get("blocked_by_robots", 0) or 0
    max_hit = (requested_max > 0 and crawled >= requested_max)

    return {
        "crawl_id":             crawl_id,
        "pages_crawled":        crawled,
        "queued_remaining":     queued,
        "max_pages":            requested_max,
        "max_pages_hit":        max_hit,
        "timeout_hit":          timeout_hit,
        "robots_blocked_count": robots_blocked,
        "batch_caps_hit":       False,    # set by callers that apply their own caps
        "elapsed_seconds":      round(elapsed) if elapsed else None,
        "audit_complete":       (queued == 0 and not timeout_hit and crawled > 0),
    }


def _fetch_sitemap_urls(sitemap_url: str, _depth: int = 0) -> tuple:
    """Recursively fetch loc entries from a sitemap (handles sitemap-index nesting).

    Returns (urls_list, errors_list). Cap recursion at 3 levels to avoid loops.
    Best-effort: a failed fetch returns the partial set + the error.
    """
    if _depth > 3:
        return [], [f"sitemap-index recursion >3 levels at {sitemap_url}"]
    out, errors = [], []
    try:
        r = httpx.get(sitemap_url, timeout=20, follow_redirects=True,
                      headers={"User-Agent": "librecrawl-mcp/sitemap-recon"})
        if r.status_code >= 400:
            errors.append(f"{sitemap_url} → HTTP {r.status_code}")
            return [], errors
        body = r.text
    except Exception as e:
        return [], [f"{sitemap_url} → {e}"]

    # Sitemap index?
    if "<sitemapindex" in body:
        nested = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body)
        for sm in nested:
            sub_urls, sub_err = _fetch_sitemap_urls(sm, _depth + 1)
            out.extend(sub_urls)
            errors.extend(sub_err)
        return out, errors

    # Plain urlset
    out.extend(re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body))
    return out, errors


def _compute_sitemap_reconciliation(crawl_pages: list, sitemap_url: str) -> dict:
    """Diff crawled URLs against sitemap URLs.

    Returns:
      sitemap_only          — URLs in sitemap but not crawled (potential orphans)
      crawl_only            — URLs crawled but missing from sitemap
      both                  — present in both
      non_indexable_in_sitemap — sitemap URLs that returned 4xx/5xx or noindex when crawled
      sitemap_total / crawl_total / sitemap_fetch_errors
    """
    sm_urls, errors = _fetch_sitemap_urls(sitemap_url)
    sm_set    = set(u.rstrip("/") for u in sm_urls if u)
    crawled   = {(p.get("url") or "").rstrip("/"): p for p in crawl_pages if p.get("url")}
    crawl_set = set(crawled.keys())

    both          = sorted(sm_set & crawl_set)
    sitemap_only  = sorted(sm_set - crawl_set)
    crawl_only    = sorted(crawl_set - sm_set)
    non_indexable = []
    for u in both:
        p = crawled.get(u, {})
        status = str(p.get("status_code", ""))
        robots = (p.get("robots") or "").lower()
        if status.startswith(("4","5")) or "noindex" in robots:
            non_indexable.append({"url": u, "status_code": p.get("status_code"), "robots": robots or None})

    return {
        "sitemap_url":              sitemap_url,
        "sitemap_total":            len(sm_set),
        "crawl_total":              len(crawl_set),
        "both_count":               len(both),
        "sitemap_only_count":       len(sitemap_only),
        "crawl_only_count":         len(crawl_only),
        "non_indexable_in_sitemap": non_indexable,
        "sitemap_only":             sitemap_only,
        "crawl_only":               crawl_only,
        "sitemap_fetch_errors":     errors,
    }


# Inventory of per-page checks for CSV emission. Each tuple = (column_name, predicate)
# where predicate takes the page dict and returns True for "this page failed this check".
_PER_PAGE_CHECKS = [
    ("missing_title",       lambda p: not (p.get("title") or "").strip() and str(p.get("status_code","")).startswith("2")),
    ("missing_meta",        lambda p: not (p.get("meta_description") or "").strip() and str(p.get("status_code","")).startswith("2")),
    ("missing_h1",          lambda p: not (p.get("h1") or "").strip() and str(p.get("status_code","")).startswith("2")),
    ("long_title",          lambda p: bool((p.get("title") or "").strip()) and len((p.get("title") or "").strip()) > 60),
    ("short_title",         lambda p: bool((p.get("title") or "").strip()) and len((p.get("title") or "").strip()) < 30),
    ("long_meta",           lambda p: bool((p.get("meta_description") or "").strip()) and len((p.get("meta_description") or "").strip()) > 160),
    ("short_meta",          lambda p: bool((p.get("meta_description") or "").strip()) and 0 < len((p.get("meta_description") or "").strip()) < 70),
    ("thin_content",        lambda p: 0 < (p.get("word_count") or 0) < 300 and str(p.get("status_code","")).startswith("2")),
    ("slow_page_3s",        lambda p: (p.get("response_time_ms") or 0) > 3000),
    ("missing_canonical",   lambda p: not (p.get("canonical_url") or "").strip() and str(p.get("status_code","")).startswith("2")),
    ("non_self_canonical",  lambda p: bool((p.get("canonical_url") or "").strip()) and (p.get("canonical_url") or "").strip() != (p.get("url") or "")),
    ("noindex",             lambda p: "noindex" in (p.get("robots") or "").lower()),
    ("large_page_500kb",    lambda p: (p.get("size") or 0) > 500_000),
    ("missing_viewport",    lambda p: not (p.get("viewport") or "").strip() and str(p.get("status_code","")).startswith("2")),
    ("orphan_page",         lambda p: not (p.get("linked_from") or []) and str(p.get("status_code","")).startswith("2")),
    ("redirect_chain",      lambda p: isinstance(p.get("redirects"), list) and len(p.get("redirects") or []) > 1),
    ("status_4xx",          lambda p: str(p.get("status_code","")).startswith("4")),
    ("status_5xx",          lambda p: str(p.get("status_code","")).startswith("5")),
    ("uppercase_url",       lambda p: bool(urlparse(p.get("url","")).path) and urlparse(p.get("url","")).path != urlparse(p.get("url","")).path.lower()),
    ("long_url_115",        lambda p: len(p.get("url","")) > 115),
    ("url_params_heavy",    lambda p: urlparse(p.get("url","")).query.count("=") > 3),
]


def _write_per_page_csv(pages: list, output_path: "Path") -> dict:
    """Write a CSV with one row per crawled URL × all failed checks.

    Columns: url, status_code, depth, word_count, response_time_ms, title, meta_description,
             then a column for each check (1=failed, 0=passed), then 'failed_checks_count'
             and 'failed_checks_list' (semicolon-separated).
    """
    import csv
    check_cols = [name for name, _ in _PER_PAGE_CHECKS]
    cols = ["url", "status_code", "depth", "word_count", "response_time_ms",
            "title", "meta_description"] + check_cols + ["failed_checks_count", "failed_checks_list"]

    rows = 0
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for p in pages:
            failed = [name for name, pred in _PER_PAGE_CHECKS if _safe_pred(pred, p)]
            checks_bits = [1 if name in failed else 0 for name in check_cols]
            w.writerow([
                p.get("url",""),
                p.get("status_code",""),
                p.get("depth",""),
                p.get("word_count",""),
                p.get("response_time_ms",""),
                (p.get("title") or "").replace("\n"," ")[:300],
                (p.get("meta_description") or "").replace("\n"," ")[:500],
                *checks_bits,
                len(failed),
                ";".join(failed),
            ])
            rows += 1
    return {"path": str(output_path), "rows": rows, "columns": len(cols)}


def _safe_pred(pred, page) -> bool:
    try:
        return bool(pred(page))
    except Exception:
        return False


def _write_sitemap_recon_csv(recon: dict, output_path: "Path") -> dict:
    """Write the sitemap reconciliation result to CSV (one row per URL with status)."""
    import csv
    sm_set = set(recon.get("sitemap_only", [])) | {x for x in recon.get("crawl_only", [])}
    # Build a flat row list: in-both, sitemap-only, crawl-only
    rows = []
    for u in recon.get("crawl_only", []):
        rows.append([u, "crawl_only", "", ""])
    for u in recon.get("sitemap_only", []):
        rows.append([u, "sitemap_only", "", ""])
    for item in recon.get("non_indexable_in_sitemap", []):
        rows.append([item["url"], "non_indexable_in_sitemap", item.get("status_code",""), item.get("robots","") or ""])

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["url", "diff_status", "status_code", "robots"])
        w.writerows(rows)
    return {"path": str(output_path), "rows": len(rows)}


# ── MCP Tools ─────────────────────────────────────────────────────────────────

@mcp.tool()
def librecrawl_audit(url: str, max_pages: int = 0) -> dict:
    """
    Full SEO / technical site audit in one call — the Screaming Frog alternative.

    Crawls the entire site, runs 35+ checks across 17 categories (broken links,
    duplicate titles, missing H1, thin content, canonical issues, redirect chains,
    orphan pages, image alt text, Open Graph, hreflang, analytics coverage, and
    more), then saves a structured Markdown report with a prioritised fix checklist.

    USE THIS when asked to:
    - "audit [site]", "run an SEO audit", "technical SEO check", "site health check"
    - "find broken links / 404s", "check title tags / meta descriptions"
    - "analyse internal linking", "find orphan pages", "crawl [site]"
    - "give me a Screaming Frog report", "full site analysis"

    Args:
        url:       Full URL to crawl (e.g. https://example.com)
        max_pages: Max pages to crawl. 0 = unlimited (default). Set e.g. 500 to
                   cap a large site. Crawls up to 2 hours before timing out.
    """
    started_at = time.time()
    # Reset any stale crawler state so this run starts clean
    reset_info = _ensure_crawler_ready()

    # Start crawl
    settings = {
        "enableJavaScript": False,
        "maxDepth": 5,
        "crawlDelay": 0.5,
        "followRedirects": True,
        "crawlExternalLinks": False,
    }
    if max_pages > 0:
        settings["maxUrls"] = max_pages
    call("POST", "/api/save_settings", json=settings)
    result   = call("POST", "/api/start_crawl", json={"url": url})
    crawl_id = result.get("crawl_id")

    # If start_crawl was rejected because of a lingering "already running" state we missed,
    # force-stop and retry once before giving up.
    if not result.get("success") and "already in progress" in str(result.get("message", "")).lower():
        try:
            call("POST", "/api/stop_crawl")
            time.sleep(2)
            result   = call("POST", "/api/start_crawl", json={"url": url})
            crawl_id = result.get("crawl_id")
            reset_info["action"] = "stopped_and_retried"
        except Exception:
            pass

    if not result.get("success"):
        return {"success": False, "error": result.get("message", "Failed to start crawl")}

    # Run site-level checks in parallel with crawl (no wait needed)
    site_data = _site_check(url)

    # Poll until done (max 2 hrs — large sites with 1000+ pages can take >20 min)
    deadline = time.time() + 7200
    crawled  = 0
    last_status = None
    while time.time() < deadline:
        time.sleep(8)
        d          = call("GET", "/api/crawl_status")
        last_status = d
        stats      = d.get("stats", {})
        crawled    = stats.get("crawled", 0)
        # LibreCrawl uses status="completed"/"idle"/"running" — is_running is always None
        status_str = d.get("status", "")
        is_running = d.get("is_running")
        done = (status_str == "completed") or (status_str == "idle" and crawled > 0) or (is_running is False)
        if done and crawled > 0:
            break
        if done and crawled == 0:
            # Verify via DB before declaring failure (fast crawls save to DB before poll fires)
            try:
                crawls_resp = call("GET", "/api/crawls/list")
                saved = next((c for c in crawls_resp.get("crawls", [])
                              if c.get("id") == crawl_id), None)
                if saved and saved.get("urls_crawled", 0) > 0:
                    crawled = saved["urls_crawled"]
                    break  # Crawl succeeded — data is in DB
            except Exception:
                pass
            return {
                "success": False,
                "crawl_id": crawl_id,
                "error": "Crawl stopped with 0 pages crawled. Check the URL is reachable and LibreCrawl is running.",
            }

    # Export
    if crawl_id is not None:
        call("POST", f"/api/crawls/{crawl_id}/load")
    else:
        # crawl_id missing — LibreCrawl may return stale data from a prior crawl
        pass  # surfaced in return value below

    r = get_client().post(f"{BASE}/api/export_data", json={
        "format": "json",
        "fields": EXPORT_FIELDS,
    }, timeout=300)
    r.raise_for_status()
    pages, links = _parse_export(r.json())

    if not pages:
        return {
            "success": False,
            "crawl_id": crawl_id,
            "crawled": crawled,
            "error": "Export returned no pages. Try librecrawl_generate_report(crawl_id) in 30s.",
        }

    # Generate and save report
    report_md   = _build_report(pages, url, crawl_id or 0, site_data=site_data, links=links)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    domain      = url.replace("https://","").replace("http://","").rstrip("/").split("/")[0]
    timestamp   = datetime.now().strftime("%Y%m%d-%H%M")
    report_path = REPORTS_DIR / f"{domain}-{timestamp}.md"
    report_path.write_text(report_md, encoding="utf-8")

    # Sidecar CSVs — per-page issue matrix + sitemap reconciliation (best-effort)
    per_page_csv  = REPORTS_DIR / f"{domain}-{timestamp}.per-page.csv"
    recon_csv     = REPORTS_DIR / f"{domain}-{timestamp}.sitemap-recon.csv"
    per_page_meta = _write_per_page_csv(pages, per_page_csv)
    sitemap_url   = (site_data.get("sitemap") or {}).get("url") or f"{url.rstrip('/')}/sitemap.xml"
    recon         = _compute_sitemap_reconciliation(pages, sitemap_url)
    recon_meta    = _write_sitemap_recon_csv(recon, recon_csv)

    # Completeness + checks manifest
    completeness = _compute_crawl_completeness(
        crawl_id, max_pages, len(pages), last_status, started_at, deadline
    )
    manifest = _build_checks_manifest(pages, site_data, links or [])

    broken = sum(1 for p in pages if str(p.get("status_code","")).startswith(("4","5")))
    no_meta = sum(1 for p in pages if not (p.get("meta_description") or "").strip())
    no_h1   = sum(1 for p in pages if not (p.get("h1") or "").strip())
    no_can  = sum(1 for p in pages if not (p.get("canonical_url") or "").strip())

    return {
        "success": True,
        "crawl_id": crawl_id,
        "pages_crawled": len(pages),
        "report_file": str(report_path),
        "per_page_csv": per_page_meta,
        "sitemap_reconciliation_csv": recon_meta,
        "sitemap_reconciliation": {
            "sitemap_url":             recon.get("sitemap_url"),
            "sitemap_total":           recon.get("sitemap_total"),
            "crawl_total":             recon.get("crawl_total"),
            "both_count":              recon.get("both_count"),
            "sitemap_only_count":      recon.get("sitemap_only_count"),
            "crawl_only_count":        recon.get("crawl_only_count"),
            "non_indexable_in_sitemap_count": len(recon.get("non_indexable_in_sitemap", [])),
            "sitemap_fetch_errors":    recon.get("sitemap_fetch_errors", []),
        },
        "crawl_completeness": completeness,
        "checks_manifest":    manifest,
        "summary": {
            "broken_pages": broken,
            "missing_meta_description": no_meta,
            "missing_h1": no_h1,
            "missing_canonical": no_can,
            "robots_txt_found": site_data.get("robots_txt", {}).get("found", False),
            "sitemap_found": site_data.get("sitemap", {}).get("found", False),
            "https_ok": site_data.get("https_redirect", {}).get("http_redirects_to_https", False),
        },
        "next": f"Open {report_path} for the full report with fix checklist.",
    }


@mcp.tool()
def librecrawl_site_check(url: str) -> dict:
    """
    Instant technical SEO health check — no crawl needed, results in seconds.

    Checks robots.txt (rules, disallow, sitemap declaration, crawl-delay),
    sitemap.xml (existence, URL count, sitemap index), HTTPS redirect
    (http → https), and www/non-www canonicalisation.

    USE THIS when asked:
    - "check robots.txt", "is the sitemap set up", "quick site health"
    - "does [site] redirect http to https", "www vs non-www check"
    - Fast pre-audit sanity check before a full librecrawl_audit()

    Args:
        url: Site root URL (e.g. https://example.com)
    """
    return _site_check(url)


@mcp.tool()
def librecrawl_generate_report(crawl_id: int = None) -> dict:
    """
    Generate a full Markdown SEO audit report from a completed crawl.
    Runs 35+ checks (broken links, titles, metas, canonicals, images, orphan pages,
    redirect chains, analytics coverage, etc.) and saves as a .md file.

    USE THIS after librecrawl_start_crawl() finishes, or to re-generate a report
    from any past crawl_id via librecrawl_list_crawls().

    Args:
        crawl_id: ID from librecrawl_start_crawl (optional — uses current crawl if omitted)
    """
    base_url = ""

    if crawl_id is not None:
        call("POST", f"/api/crawls/{crawl_id}/load")
        try:
            d = call("GET", "/api/crawl_status")
            base_url = d.get("stats", {}).get("baseUrl", "")
        except Exception:
            pass

    r = get_client().post(f"{BASE}/api/export_data", json={
        "format": "json",
        "fields": EXPORT_FIELDS,
    }, timeout=300)
    r.raise_for_status()
    pages, links = _parse_export(r.json())

    if not pages:
        return {"success": False, "error": "No pages found. Is the crawl complete?"}

    if not base_url and pages:
        parsed   = urlparse(pages[0].get("url", ""))
        base_url = f"{parsed.scheme}://{parsed.netloc}"

    report_md   = _build_report(pages, base_url, crawl_id or 0, links=links)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    domain      = base_url.replace("https://","").replace("http://","").rstrip("/").split("/")[0]
    timestamp   = datetime.now().strftime("%Y%m%d-%H%M")
    report_path = REPORTS_DIR / f"{domain}-{timestamp}.md"
    report_path.write_text(report_md, encoding="utf-8")

    # Inline markdown content so MCP clients without filesystem access can still
    # read the report. Truncated to 50k chars with a clear flag; full file always
    # available on disk + via librecrawl_report_content(report_path).
    INLINE_CAP = 50_000
    inline = report_md if len(report_md) <= INLINE_CAP else report_md[:INLINE_CAP]
    truncated = len(report_md) > INLINE_CAP

    return {
        "success": True,
        "report_file": str(report_path),
        "report_markdown": inline,
        "report_markdown_truncated": truncated,
        "report_total_chars": len(report_md),
        "pages": len(pages),
    }


@mcp.tool()
def librecrawl_start_crawl(url: str, max_pages: int = 0) -> dict:
    """
    Start an async site crawl. Returns crawl_id immediately — does NOT wait for completion.
    Poll librecrawl_get_status() until done, then librecrawl_generate_report(crawl_id).

    Prefer librecrawl_audit() for a one-call full SEO audit with auto-report.
    Use this tool when you need step-by-step control or want to crawl in the background.

    Args:
        url:       Full URL to crawl (e.g. https://example.com)
        max_pages: Max pages to crawl. 0 = unlimited (default). Set e.g. 500 to cap.
    """
    settings = {
        "enableJavaScript": False,
        "maxDepth": 5,
        "crawlDelay": 0.5,
        "followRedirects": True,
        "crawlExternalLinks": False,
    }
    if max_pages > 0:
        settings["maxUrls"] = max_pages
    _ensure_crawler_ready()
    call("POST", "/api/save_settings", json=settings)
    result   = call("POST", "/api/start_crawl", json={"url": url})
    crawl_id = result.get("crawl_id")
    return {
        "success": result.get("success"),
        "crawl_id": crawl_id,
        "message": result.get("message"),
        "next": f"Poll librecrawl_get_status() until is_running=False, then librecrawl_generate_report({crawl_id})",
    }


@mcp.tool()
def librecrawl_get_status() -> dict:
    """
    Poll current crawl progress. Repeat until is_running=False.
    Returns: is_running, crawled, queued, issues, base_url
    """
    d     = call("GET", "/api/crawl_status")
    stats = d.get("stats", {})
    return {
        "is_running": d.get("is_running", False),
        "crawled":    stats.get("crawled", 0),
        "queued":     stats.get("queued", 0),
        "issues":     stats.get("issues", 0),
        "base_url":   stats.get("baseUrl", ""),
    }


@mcp.tool()
def librecrawl_export_results(crawl_id: int = None) -> dict:
    """
    Export raw crawl JSON. For a formatted report use librecrawl_generate_report() instead.

    Args:
        crawl_id: ID from librecrawl_start_crawl (optional)
    """
    if crawl_id is not None:
        call("POST", f"/api/crawls/{crawl_id}/load")

    r = get_client().post(f"{BASE}/api/export_data", json={
        "format": "json",
        "fields": EXPORT_FIELDS,
    }, timeout=300)
    r.raise_for_status()
    pages, links = _parse_export(r.json())
    return {"pages": pages, "links": links, "total": len(pages)}


@mcp.tool()
def librecrawl_list_crawls() -> dict:
    """List all saved crawls with URL, crawl_id, and timestamp."""
    return call("GET", "/api/crawls/list")


@mcp.tool()
def librecrawl_stop_crawl() -> dict:
    """Stop the currently running crawl."""
    return call("POST", "/api/stop_crawl")


@mcp.tool()
def librecrawl_pause_crawl() -> dict:
    """Pause the currently running crawl. Resume with librecrawl_resume_crawl()."""
    try:
        return call("POST", "/api/pause_crawl")
    except Exception as e:
        return {"success": False, "error": str(e),
                "note": "This endpoint may not be available in your LibreCrawl version."}


@mcp.tool()
def librecrawl_resume_crawl() -> dict:
    """Resume a paused crawl in the current session."""
    try:
        return call("POST", "/api/resume_crawl")
    except Exception as e:
        return {"success": False, "error": str(e),
                "note": "This endpoint may not be available in your LibreCrawl version."}


@mcp.tool()
def librecrawl_resume_from_crawl_id(crawl_id: int) -> dict:
    """
    Resume a previously interrupted crawl from the database — even from a NEW MCP session,
    days later, after server restarts. Continues from where the crawl left off,
    re-using all already-crawled URLs (no duplicate work, no extra load on the target).

    This is the way to handle "however big, in chunks" without overloading the target:

      Day 1: librecrawl_audit("https://huge-site.com", max_pages=5000)
             — runs until time/limits or you pause it
      Day 2: librecrawl_list_crawls() → find your crawl_id
             librecrawl_resume_from_crawl_id(crawl_id)
             — picks up where it left off, polite rate-limit intact

    USE THIS when asked:
    - "resume that crawl", "continue the audit from yesterday"
    - "pick up where we left off", "finish the [site] crawl"

    Args:
        crawl_id: ID from librecrawl_list_crawls() of a paused/failed/interrupted crawl

    Returns success + a `next` hint telling you to poll librecrawl_get_status()
    until is_running=False, then call librecrawl_generate_report(crawl_id).
    """
    # Two scenarios to handle:
    #
    # (a) "In-session resume" — the crawl was paused in the same upstream Flask session
    #     (e.g. paused 5 min ago, now resuming). The in-memory crawler is still alive
    #     with is_running=True. /api/crawls/<id>/resume would reject this with
    #     "Crawl already in progress". The right call is /api/resume_crawl, which
    #     just flips is_paused=False on the existing crawler.
    #
    # (b) "Cross-session resume" — the upstream restarted (or the MCP's httpx.Client
    #     was reset), so the crawler instance is gone but the DB record survives.
    #     /api/crawls/<id>/resume reads from DB and rebuilds the crawler.
    #
    # We try (a) first because it's a no-op when wrong (returns 400 if nothing paused
    # in memory) and safe when right. Then fall back to (b).
    #
    # NEVER calling /api/stop_crawl here — that would set DB status to 'stopped',
    # which permanently blocks resume_from_database() for the same crawl_id.

    # Attempt (a): in-session resume
    try:
        in_session = call("POST", "/api/resume_crawl")
        if in_session.get("success"):
            in_session["next"] = (
                f"Poll librecrawl_get_status() until is_running=False, "
                f"then librecrawl_generate_report({crawl_id})."
            )
            in_session["mode"] = "in_session_resume"
            return in_session
    except Exception:
        pass  # fall through to DB resume

    # Attempt (b): cross-session resume from DB
    try:
        result = call("POST", f"/api/crawls/{crawl_id}/resume")
        if result.get("success"):
            result["next"] = (
                f"Poll librecrawl_get_status() until is_running=False, "
                f"then librecrawl_generate_report({crawl_id})."
            )
            result["mode"] = "db_resume"
        return result
    except Exception as e:
        return {"success": False, "error": str(e),
                "hint": "Verify crawl_id with librecrawl_list_crawls(). "
                        "Status must be paused/failed/running (not 'completed' or 'stopped')."}


@mcp.tool()
def librecrawl_get_settings() -> dict:
    """
    Get current crawler settings (maxUrls, maxDepth, crawlDelay, JS rendering, etc).
    Useful to confirm settings before starting a crawl.
    """
    return call("GET", "/api/get_settings")


@mcp.tool()
def librecrawl_filter_issues(patterns: list) -> dict:
    """
    Filter crawl issues by exclusion patterns — useful to suppress known false positives.
    Pass a list of URL patterns or issue types to exclude from results.

    Args:
        patterns: List of strings to exclude (e.g. ["/wp-admin/", "cdn.example.com"])
    """
    try:
        return call("POST", "/api/filter_issues", json={"patterns": patterns})
    except Exception as e:
        return {"success": False, "error": str(e),
                "note": "This endpoint may not be available in your LibreCrawl version."}


@mcp.tool()
def librecrawl_visualization_data() -> dict:
    """
    Get site link graph data from the current crawl — nodes (pages) and edges (links).
    Useful for understanding site architecture, identifying link clusters, and finding
    isolated sections of the site that Googlebot may not reach efficiently.

    Returns: nodes list (url, depth, status) and edges list (source → target links).
    """
    try:
        return call("GET", "/api/visualization_data")
    except Exception as e:
        return {"success": False, "error": str(e),
                "note": "This endpoint may not be available in your LibreCrawl version."}


@mcp.tool()
def librecrawl_internal_links_analysis(crawl_id: int = None) -> dict:
    """
    Deep internal linking analysis — maps internal authority distribution across the site.

    Answers:
    - Which pages get the most internal links? (= Google considers them most important)
    - Which pages are orphans? (zero inbound internal links — invisible to Googlebot)
    - Which pages have zero outgoing internal links? (dead ends — no crawl flow out)
    - Which pages are crawl budget hubs? (link out to the most others)
    - What anchor text patterns dominate? (keyword signal analysis)

    USE THIS when asked:
    - "internal linking audit", "orphan pages", "link equity distribution"
    - "which pages need more internal links", "crawl budget analysis"
    - "anchor text audit", "internal link map"

    Args:
        crawl_id: ID from librecrawl_start_crawl (optional — uses current crawl)
    """
    if crawl_id is not None:
        call("POST", f"/api/crawls/{crawl_id}/load")

    r = get_client().post(f"{BASE}/api/export_data", json={
        "format": "json",
        "fields": ["url", "status_code", "title", "depth",
                   "internal_links", "external_links", "links_detailed", "linked_from"],
    }, timeout=300)
    r.raise_for_status()
    pages, links = _parse_export(r.json())
    # Merge flat links file into per-page links_detailed for analysis
    if links and not any(p.get("links_detailed") for p in pages):
        from collections import defaultdict as _dd
        src_links = _dd(list)
        for lk in links:
            src_links[lk.get("source_url","")].append({
                "url":         lk.get("target_url",""),
                "anchor_text": lk.get("anchor_text",""),
                "is_internal": bool(lk.get("is_internal", 0)),
            })
        for p in pages:
            p["links_detailed"] = src_links.get(p.get("url",""), [])

    if not pages:
        return {"success": False, "error": "No pages found."}

    # Build inbound link map from links_detailed
    inbound_count = defaultdict(int)   # url → how many pages link TO it
    outbound_count = {}                # url → how many internal links it sends out
    anchor_text_count = defaultdict(int)

    for p in pages:
        src   = p.get("url","")
        links = p.get("links_detailed") or []
        out   = 0
        if isinstance(links, list):
            for lk in links:
                tgt    = lk.get("url") or lk.get("href") or ""
                anchor = (lk.get("anchor_text") or lk.get("text") or "").strip().lower()
                is_int = lk.get("is_internal", True)
                if tgt and is_int:
                    inbound_count[tgt] += 1
                    out += 1
                if anchor and len(anchor) > 2:
                    anchor_text_count[anchor] += 1
        # Fallback: use internal_links count field if links_detailed empty
        if out == 0:
            out = p.get("internal_links") or 0
        outbound_count[src] = out

    ok_pages = [p for p in pages if str(p.get("status_code","")).startswith("2")]

    # Top pages by inbound links (internal authority)
    top_linked = sorted(
        [(url, cnt) for url, cnt in inbound_count.items()],
        key=lambda x: -x[1]
    )[:20]

    # Pages with zero outgoing internal links (dead ends — crawl flow stops here)
    dead_ends = [
        p.get("url") for p in ok_pages
        if (outbound_count.get(p.get("url",""), 0) == 0)
        and p.get("word_count", 0) > 100   # ignore tiny pages
    ]

    # Pages with zero inbound links (no internal authority — orphans)
    orphans = [
        p.get("url") for p in ok_pages
        if inbound_count.get(p.get("url",""), 0) == 0
    ]

    # Top anchors
    top_anchors = sorted(anchor_text_count.items(), key=lambda x: -x[1])[:20]

    # Pages with most outbound links
    top_senders = sorted(
        [(url, cnt) for url, cnt in outbound_count.items() if cnt > 0],
        key=lambda x: -x[1]
    )[:10]

    return {
        "success": True,
        "pages_analysed": len(pages),
        "top_linked_pages": [
            {"url": url, "inbound_internal_links": cnt,
             "title": next((p.get("title","") for p in pages if p.get("url")==url), "")}
            for url, cnt in top_linked
        ],
        "dead_end_pages": {
            "count": len(dead_ends),
            "note": "These pages have no outgoing internal links — crawl flow stops here.",
            "urls": dead_ends[:20],
        },
        "orphan_pages": {
            "count": len(orphans),
            "note": "No internal links point to these pages — Google may not discover them.",
            "urls": orphans[:20],
        },
        "top_outbound_pages": [
            {"url": url, "internal_links_out": cnt} for url, cnt in top_senders
        ],
        "top_anchor_texts": [
            {"anchor": anchor, "count": cnt} for anchor, cnt in top_anchors
        ],
    }


# ── PageSpeed Insights ────────────────────────────────────────────────────────

def _fetch_psi(url: str, strategy: str = "mobile") -> dict:
    """Fetch Core Web Vitals + performance score from Google PSI API."""
    if not PSI_API_KEY:
        return {"error": "PAGESPEED_API_KEY not set."}
    params = {"url": url, "key": PSI_API_KEY, "strategy": strategy,
              "category": ["performance", "seo", "accessibility", "best-practices"]}
    try:
        r = httpx.get(PSI_API_BASE, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return {"error": str(e)}

    lhr    = data.get("lighthouseResult", {})
    cats   = lhr.get("categories", {})
    audits = lhr.get("audits", {})
    field_metrics = data.get("loadingExperience", {}).get("metrics", {})

    def score(cat): return round((cats.get(cat, {}).get("score") or 0) * 100)
    def ms(audit_id):
        v = audits.get(audit_id, {}).get("numericValue")
        return round(v) if v else None

    field = {}
    for metric, key in [("LCP","LARGEST_CONTENTFUL_PAINT_MS"), ("FID","FIRST_INPUT_DELAY_MS"),
                         ("CLS","CUMULATIVE_LAYOUT_SHIFT_SCORE"), ("INP","INTERACTION_TO_NEXT_PAINT"),
                         ("FCP","FIRST_CONTENTFUL_PAINT_MS"), ("TTFB","EXPERIMENTAL_TIME_TO_FIRST_BYTE")]:
        m = field_metrics.get(key, {})
        if m:
            field[metric] = {"value": m.get("percentile"), "category": m.get("category")}

    lab = {k: v for k, v in {
        "FCP_ms":  ms("first-contentful-paint"),
        "LCP_ms":  ms("largest-contentful-paint"),
        "TBT_ms":  ms("total-blocking-time"),
        "CLS":     audits.get("cumulative-layout-shift", {}).get("numericValue"),
        "Speed_Index_ms": ms("speed-index"),
        "TTI_ms":  ms("interactive"),
    }.items() if v is not None}

    opps = []
    for audit_id, audit in audits.items():
        if audit.get("details", {}).get("type") == "opportunity":
            savings = audit.get("details", {}).get("overallSavingsMs", 0) or 0
            if savings > 200:
                opps.append({"title": audit.get("title"), "savings_ms": round(savings)})
    opps.sort(key=lambda x: -x["savings_ms"])

    return {
        "url": url, "strategy": strategy,
        "scores": {
            "performance":    score("performance"),
            "seo":            score("seo"),
            "accessibility":  score("accessibility"),
            "best_practices": score("best-practices"),
        },
        "field_data_cwv": field,
        "lab_data": lab,
        "top_opportunities": opps[:5],
    }


@mcp.tool()
def librecrawl_pagespeed(url: str, strategy: str = "mobile") -> dict:
    """
    Core Web Vitals and Lighthouse scores via Google PageSpeed Insights API.
    Returns LCP, CLS, FCP, TBT, INP, performance/SEO/accessibility scores,
    and real-user CrUX field data where available.

    USE THIS when asked:
    - "Core Web Vitals", "page speed", "Lighthouse score", "CWV check"
    - "how fast is [page]", "LCP / CLS issues", "performance audit"

    Args:
        url:      Full URL to test
        strategy: "mobile" (default) or "desktop"
    """
    return _fetch_psi(url, strategy)


@mcp.tool()
def librecrawl_pagespeed_audit(urls: list, strategy: str = "mobile") -> dict:
    """
    Batch Core Web Vitals audit — runs PageSpeed Insights on multiple pages,
    ranked worst to best performance score. Identifies your slowest pages first.
    Throttled to 1 req/sec to stay within the free 25k/day PSI quota.

    USE THIS when asked:
    - "check CWV across all pages", "which pages are slowest", "batch page speed audit"
    - "performance audit for the whole site" (pass top URLs from librecrawl_audit export)

    Args:
        urls:     List of URLs to test (recommend top 10–25 pages)
        strategy: "mobile" (default) or "desktop"
    """
    if not PSI_API_KEY:
        return {"error": "PAGESPEED_API_KEY not set."}

    results = []
    for url in urls[:25]:
        results.append(_fetch_psi(url, strategy))
        time.sleep(1.1)

    results.sort(key=lambda x: x.get("scores", {}).get("performance", 100))
    valid = [r for r in results if "scores" in r]

    summary = {
        "tested": len(results),
        "avg_performance": round(sum(r["scores"]["performance"] for r in valid) / len(valid)) if valid else 0,
        "poor_performance": sum(1 for r in valid if r["scores"]["performance"] < 50),
        "needs_improvement": sum(1 for r in valid if 50 <= r["scores"]["performance"] < 90),
        "good": sum(1 for r in valid if r["scores"]["performance"] >= 90),
    }
    return {"summary": summary, "results": results}


# ── Schema.org / JSON-LD ──────────────────────────────────────────────────────

class _JsonLdExtractor(HTMLParser):
    """Extract all <script type="application/ld+json"> blocks from HTML."""
    def __init__(self):
        super().__init__()
        self._in_ld = False
        self._buf   = []
        self.schemas = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("type") == "application/ld+json":
            self._in_ld = True
            self._buf   = []

    def handle_endtag(self, tag):
        if tag == "script" and self._in_ld:
            self._in_ld = False
            raw = "".join(self._buf).strip()
            if raw:
                try:
                    obj = json.loads(raw)
                    for item in (obj if isinstance(obj, list) else [obj]):
                        self._absorb(item)
                except Exception:
                    pass

    def _absorb(self, item):
        # Walk JSON-LD @graph containers (Yoast, RankMath, WPRM, Schema Pro).
        # Without this, the container's @type='Unknown' masks real inner types
        # like Recipe / Article / FAQPage that live inside @graph.
        if not isinstance(item, dict):
            return
        graph = item.get("@graph")
        if isinstance(graph, list):
            for inner in graph:
                self._absorb(inner)
            if item.get("@type"):
                self.schemas.append({"type": item.get("@type"), "data": item})
            return
        self.schemas.append({"type": item.get("@type", "Unknown"), "data": item})

    def handle_data(self, data):
        if self._in_ld:
            self._buf.append(data)


def _validate_public_url(url: str) -> str | None:
    """Return error string if URL is unsafe, else None."""
    import ipaddress
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return f"URL scheme must be http or https, got: {parsed.scheme!r}"
        host = parsed.hostname or ""
        # Block private/loopback/link-local addresses
        try:
            addr = ipaddress.ip_address(host)
            if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
                return f"Private/internal IP not allowed: {host}"
        except ValueError:
            # It's a hostname — block obvious internal names
            blocked = ("localhost", "metadata.google.internal")
            if any(host == b or host.endswith("." + b) for b in blocked):
                return f"Internal hostname not allowed: {host}"
    except Exception as e:
        return f"Invalid URL: {e}"
    return None


def _extract_schema(url: str) -> list:
    err = _validate_public_url(url)
    if err:
        return [{"error": err}]
    try:
        r = httpx.get(url, follow_redirects=True, timeout=15,
                      headers={"User-Agent": "Mozilla/5.0 (compatible; SEO-bot/1.0)"})
        parser = _JsonLdExtractor()
        parser.feed(r.text)
        return parser.schemas
    except Exception as e:
        return [{"error": str(e)}]


RICH_RESULTS_MAP = {
    "Article":           "Article rich result — date, author, image in SERP",
    "BlogPosting":       "Article rich result",
    "Product":           "Product snippet — price, availability, ratings",
    "Review":            "Review snippet — star rating in SERP",
    "AggregateRating":   "Star ratings in SERP",
    "FAQPage":           "FAQ accordion in SERP (huge CTR boost)",
    "HowTo":             "How-to steps in SERP",
    "BreadcrumbList":    "Breadcrumb path shown in SERP URL",
    "WebSite":           "Sitelinks searchbox in SERP",
    "Organization":      "Knowledge panel — logo, contacts",
    "Person":            "Author knowledge panel",
    "LocalBusiness":     "Local business panel — address, hours, maps",
    "SoftwareApplication": "App rating + price in SERP",
    "VideoObject":       "Video thumbnail in SERP",
    "Event":             "Event date/location in SERP",
    "JobPosting":        "Job listing in Google Jobs",
    "Recipe":            "Recipe rich card — time, calories, ratings",
    "Course":            "Course info in SERP",
}


@mcp.tool()
def librecrawl_schema_check(url: str) -> dict:
    """
    Extract and classify all Schema.org / JSON-LD structured data from a page.
    No API key required — fetches and parses the live page directly.

    Returns schema types found, which Google rich results they unlock
    (FAQ accordion, star ratings, breadcrumbs, product snippets, etc.),
    and which high-value schema types are missing.

    USE THIS when asked:
    - "does [page] have schema markup", "check structured data", "JSON-LD audit"
    - "what rich results is this page eligible for", "add FAQ schema"

    Args:
        url: Full URL to check
    """
    schemas = _extract_schema(url)
    found_types = [s.get("type") for s in schemas if "type" in s]
    return {
        "url": url,
        "schema_count": len(schemas),
        "types_found": found_types,
        "rich_results_enabled": [RICH_RESULTS_MAP[t] for t in found_types if t in RICH_RESULTS_MAP],
        "missing_opportunities": [
            f"{t}: {desc}" for t, desc in RICH_RESULTS_MAP.items()
            if t not in found_types and t in ["FAQPage","BreadcrumbList","Article","Product","Review"]
        ],
        "schemas": schemas,
    }


@mcp.tool()
def librecrawl_schema_audit(urls: list, batch_size: int = 50, batch_delay: float = 0.3) -> dict:
    """
    Schema.org / structured data coverage audit across multiple pages.
    No API key required. Identifies pages with no schema, shows schema type
    breakdown across the site, and flags missing rich result opportunities.

    NO SILENT CAPS: the whole list is processed. Internally batched in groups
    of `batch_size` with `batch_delay` seconds between requests to stay polite
    to the target server. Walks JSON-LD @graph containers (Yoast / RankMath /
    WPRM) so Recipe / Article / FAQPage types inside @graph are correctly
    extracted instead of being labelled "Unknown".

    USE THIS when asked:
    - "schema coverage across the site", "which pages have structured data"
    - "structured data audit", "rich results audit"

    Args:
        urls:        List of URLs to check (any length — all are processed).
        batch_size:  Pages between status messages, default 50.
        batch_delay: Sleep between requests in seconds, default 0.3.
    """
    results    = []
    no_schema  = []
    type_count = defaultdict(int)
    total      = len(urls)

    for i, url in enumerate(urls):
        schemas = _extract_schema(url)
        types   = [s.get("type") for s in schemas if "type" in s]
        for t in types:
            type_count[t] += 1
        if not types:
            no_schema.append(url)
        results.append({"url": url, "types": types, "count": len(schemas)})
        if i < total - 1:
            time.sleep(batch_delay)

    return {
        "pages_checked":         len(results),
        "pages_no_schema":       len(no_schema),
        "schema_type_breakdown": dict(sorted(type_count.items(), key=lambda x: -x[1])),
        "pages_missing_schema":  no_schema,
        "results": results,
        "batch_caps_hit":        False,
        "audit_complete":        len(results) == total,
    }


# ── GSC section appender ──────────────────────────────────────────────────────

@mcp.tool()
def librecrawl_append_gsc_section(report_path: str, gsc_data: dict) -> dict:
    """
    Append a Google Search Console errors section to an existing MD audit report.

    Workflow:
      1. librecrawl_audit(url) → get report_path
      2. Use gsc-posi connector to pull GSC coverage/crawl errors for the domain
      3. Pass both here — GSC section gets appended to the report

    gsc_data keys (any/all):
      coverage_errors  — list of {url, type, last_crawled}
      crawl_errors     — list of {url, response_code, last_crawled}
      search_issues    — list of strings (manual actions, security issues)
      performance      — dict {clicks, impressions, ctr, position}
                         (or pass these keys flat at the top level — both shapes work)
      top_queries      — list of {query, clicks, impressions, ctr, position}
                         from gsc search_analytics_query with dimensions=["query"]
      date_range       — optional human label e.g. "last 30 days" (shown in header)

    Args:
        report_path: Path returned by librecrawl_audit() or librecrawl_generate_report()
        gsc_data:    GSC data dict from the gsc-posi connector
    """
    path = Path(report_path).resolve()
    if not str(path).startswith(str(REPORTS_DIR.resolve())):
        return {"success": False, "error": "report_path must be within REPORTS_DIR"}
    if not path.exists():
        return {"success": False, "error": f"Report not found: {report_path}"}

    now  = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = ["\n\n---\n", f"## 🔍 Google Search Console\n", f"*Pulled: {now}*\n"]

    coverage = gsc_data.get("coverage_errors") or gsc_data.get("indexing_errors") or []
    if coverage:
        by_type = defaultdict(list)
        for item in coverage:
            err_type = item.get("type") or item.get("reason") or "Unknown"
            url      = item.get("url") or item.get("inspectionUrl") or ""
            by_type[err_type].append(url)

        FIX_HINTS = {
            "Submitted URL not found (404)":          "Remove from sitemap, or 301 redirect.",
            "Submitted URL seems to be a Soft 404":   "Return real content or proper 404.",
            "Redirect error":                          "Fix redirect chain to resolve in ≤2 hops.",
            "Server error (5xx)":                     "Check server logs — 5xx blocks indexing.",
            "Blocked by robots.txt":                  "Remove Disallow rule if page should be indexed.",
            "Blocked due to access forbidden (403)":  "Allow Googlebot access or remove from sitemap.",
            "Crawled - currently not indexed":        "Improve content quality, add internal links.",
            "Discovered - currently not indexed":     "Add internal links so Googlebot crawls it.",
            "Alternate page with proper canonical tag": "Verify canonicalization is intentional.",
            "Duplicate without user-selected canonical": "Add canonical tag pointing to preferred URL.",
            "Page with redirect":                     "Update internal links to final URL.",
            "Excluded by 'noindex' tag":              "Verify these should be noindexed.",
        }

        lines.append(f"### Indexing / Coverage Errors ({len(coverage)} URLs)\n")
        for err_type, urls in sorted(by_type.items(), key=lambda x: -len(x[1])):
            hint = FIX_HINTS.get(err_type, "Investigate in GSC Coverage report.")
            lines.append(f"**{err_type}** — {len(urls)} URLs")
            lines.append(f"> Fix: {hint}\n")
            for u in urls[:10]:
                if u: lines.append(f"- `{u}`")
            if len(urls) > 10:
                lines.append(f"- … and {len(urls)-10} more")
            lines.append("")
    else:
        lines.append("### Indexing Errors\n✅ No coverage errors in provided GSC data.\n")

    crawl_errors = gsc_data.get("crawl_errors") or []
    if crawl_errors:
        lines.append(f"### Crawl Errors ({len(crawl_errors)})\n")
        lines.append("| URL | Status | Last Crawled |")
        lines.append("|-----|--------|-------------|")
        for item in crawl_errors[:20]:
            url  = item.get("url","")
            code = item.get("response_code") or item.get("status","?")
            last = item.get("last_crawled") or item.get("lastCrawled","—")
            lines.append(f"| `{url}` | {code} | {last} |")
        if len(crawl_errors) > 20:
            lines.append(f"| … | {len(crawl_errors)-20} more | |")
        lines.append("")

    issues = gsc_data.get("search_issues") or gsc_data.get("manual_actions") or []
    if issues:
        lines.append("### ⚠️ Manual Actions / Security Issues\n")
        for issue in issues:
            lines.append(f"- 🚨 {issue}")
        lines.append("")

    # Performance metrics — accept nested gsc_data["performance"] OR flat top-level keys
    perf = gsc_data.get("performance") or {}
    if not perf and any(k in gsc_data for k in ("clicks","impressions","ctr","position")):
        perf = {k: gsc_data[k] for k in ("clicks","impressions","ctr","position") if k in gsc_data}

    if perf:
        date_range = gsc_data.get("date_range") or "last 28 days"
        lines.append(f"### Search Performance — {date_range}\n")
        lines.append("| Metric | Value |")
        lines.append("|--------|-------|")
        if "clicks" in perf:      lines.append(f"| Clicks | {int(perf['clicks']):,} |")
        if "impressions" in perf: lines.append(f"| Impressions | {int(perf['impressions']):,} |")
        if "ctr" in perf:         lines.append(f"| CTR | {float(perf['ctr']):.2%} |")
        if "position" in perf:    lines.append(f"| Avg Position | {float(perf['position']):.1f} |")
        lines.append("")

    # Top queries — most actionable GSC data (where users actually find you)
    top_queries = gsc_data.get("top_queries") or []
    if top_queries:
        lines.append(f"### Top Search Queries ({len(top_queries)})\n")
        lines.append("> Queries driving impressions/clicks. Optimise the matching pages.\n")
        lines.append("| Query | Clicks | Impressions | CTR | Position |")
        lines.append("|-------|--------|-------------|-----|----------|")
        for q in top_queries[:25]:
            query  = q.get("query", "")
            clicks = q.get("clicks", 0)
            impr   = q.get("impressions", 0)
            ctr    = q.get("ctr", 0)
            pos    = q.get("position", 0)
            try:
                lines.append(f"| `{query}` | {int(clicks)} | {int(impr):,} | {float(ctr):.2%} | {float(pos):.1f} |")
            except Exception:
                lines.append(f"| `{query}` | {clicks} | {impr} | {ctr} | {pos} |")
        lines.append("")

        # Quick wins: page-2 queries (pos 6-20) with significant impressions
        opps = [q for q in top_queries
                if 5 < q.get("position", 0) < 21 and q.get("impressions", 0) > 50]
        if opps:
            lines.append("**🎯 Quick wins — page 2 keywords with high impressions:**\n")
            lines.append("> These rank position 6-20 with significant impressions. Optimise the matching page and you could jump to page 1.\n")
            for q in opps[:10]:
                lines.append(f"- `{q['query']}` — position **{float(q['position']):.1f}**, **{int(q['impressions']):,}** impressions, {int(q['clicks'])} clicks")
            lines.append("")

    # GSC checklist
    coverage_by_type = defaultdict(int)
    for item in coverage:
        t = item.get("type") or item.get("reason") or "Unknown"
        coverage_by_type[t] += 1

    checklist = []
    p = 1
    for t in ["Submitted URL not found (404)", "Server error (5xx)", "Redirect error"]:
        if t in coverage_by_type:
            checklist.append(f"- [ ] **P{p} (GSC)** Fix {coverage_by_type[t]}x '{t}'")
            p += 1
    for t in ["Submitted URL seems to be a Soft 404", "Crawled - currently not indexed",
              "Duplicate without user-selected canonical"]:
        if t in coverage_by_type:
            checklist.append(f"- [ ] **P{p} (GSC)** Resolve {coverage_by_type[t]}x '{t}'")
            p += 1

    if checklist:
        lines.append("### GSC Fix Checklist\n")
        lines.extend(checklist)
        lines.append("")

    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return {
        "success": True,
        "report_file": str(path),
        "gsc_section_added": True,
        "coverage_errors": len(coverage),
        "crawl_errors": len(crawl_errors),
        "manual_actions": len(issues),
    }


# ── v1.4.0 Chunked-progressive audit tools ────────────────────────────────────
# Background worker runs in a single thread (LibreCrawl is single-tenant
# upstream — one crawl at a time). Sessions persist in SQLite WAL so PM2
# restart doesn't lose progress. Adaptive crawlDelay tuning per polling
# window keeps the target server happy on slow infrastructure.

import state as _state           # noqa: E402  — late import to avoid circular
import runner as _runner          # noqa: E402

_state.init_db()
_runner.start_runner()


@mcp.tool()
def librecrawl_start_chunked_audit(url: str, total_max_pages: int = 10000,
                                    chunk_target_pages: int = 50,
                                    politeness: str = "auto",
                                    confirm_unbounded: bool = False) -> dict:
    """
    NEW IN v1.4.0 — Start a chunked-progressive audit. Returns IMMEDIATELY with
    a session_id; the crawl runs in the background and survives PM2 restart.

    The runner polls upstream LibreCrawl every 20s, computes p95 latency +
    error rate per window, and re-tunes crawlDelay live (AIMD controller).
    Slow target → smaller per-second rate. Fast target → speeds up. Never
    overloads the site you're auditing.

    Returns:
      session_id          — opaque id to poll librecrawl_audit_status() with
      status              — initial state ("queued" → quickly becomes "crawling")
      url                 — echo of the input
      sanity_ceiling      — hard upper bound enforced when total_max_pages=0
      total_max_pages     — your cap, or the sanity ceiling

    USE THIS for any audit on a site you don't already know is small. Plain
    librecrawl_audit() still works (it wraps this with a 110s hard timeout
    for backwards-compat), but for sites > a few hundred pages, polling
    is the only way to avoid MCP client disconnects.

    Args:
        url:                Full URL (e.g. https://example.com).
        total_max_pages:    Hard ceiling on pages. 0 = unlimited (requires
                            confirm_unbounded=True). Default 10,000.
        chunk_target_pages: Polling-window size — how often the controller
                            re-tunes crawlDelay. Default 50.
        politeness:         "auto" (adaptive — default), "polite" (slow start
                            + conservative tune), or "fast" (aggressive).
        confirm_unbounded:  Pass True only if you genuinely want total_max_pages=0.
                            Protects you from accidentally crawling Wikipedia.
    """
    if total_max_pages > 100_000 and not confirm_unbounded:
        return {"success": False, "error":
                f"total_max_pages={total_max_pages} exceeds soft ceiling 100k. "
                f"Pass confirm_unbounded=True if you truly need this."}

    sess = _runner.enqueue_session(
        url=url, total_max_pages=total_max_pages,
        chunk_target_pages=chunk_target_pages, politeness=politeness,
        confirm_unbounded=confirm_unbounded,
    )
    if "error" in (sess or {}):
        return {"success": False, **sess}
    return {
        "success":         True,
        "session_id":      sess["id"],
        "status":          sess["status"],
        "url":             sess["url"],
        "total_max_pages": sess["total_max_pages"],
        "next":            f"Poll librecrawl_audit_status('{sess['id']}') every 20-30s.",
    }


@mcp.tool()
def librecrawl_audit_status(session_id: str) -> dict:
    """
    Poll progress on a chunked audit. Safe to call as often as you like —
    reads from local SQLite state, doesn't hit the upstream every time.

    Returns:
      status               — queued/crawling/throttled/paused/done/cancelled/failed
      pages_done           — pages crawled so far
      total_max_pages      — the cap you set
      current_delay_ms     — current crawlDelay the controller has tuned to
      eta_seconds          — rough estimate based on current speed (None if unknown)
      last_chunks          — last 3 polling-window metrics (p95, err_rate, delay)
      recent_events        — last 5 state transitions
      audit_complete       — True only on clean finish (everything crawled, no caps hit)
      incomplete_reasons   — list of what went wrong, if anything
      artifacts_ready      — True once finalize has run and PDF/MD/CSVs are on disk
    """
    s = _state.get_session(session_id)
    if not s:
        return {"success": False, "error": f"Unknown session_id: {session_id}"}

    chunks = _state.last_chunks(session_id, n=3)
    events = _state.recent_events(session_id, n=5)
    arts = _state.list_artifacts(session_id)

    # ETA
    eta = None
    if chunks and s["status"] in ("crawling", "throttled"):
        recent_speed = sum(c.get("pages_in_chunk", 0) or 0 for c in chunks) / max(1, sum(
            (c.get("ended_at", 0) - c.get("started_at", 0)) or 1 for c in chunks
        ))
        if recent_speed > 0 and s["total_max_pages"] > 0:
            remaining = max(0, s["total_max_pages"] - s["pages_done"])
            eta = int(remaining / recent_speed)

    return {
        "success":            True,
        "session_id":         session_id,
        "status":             s["status"],
        "url":                s["url"],
        "pages_done":         s["pages_done"],
        "total_max_pages":    s["total_max_pages"],
        "current_delay_ms":   s["current_delay_ms"],
        "upstream_crawl_id":  s.get("upstream_crawl_id"),
        "started_at":         s["started_at"],
        "updated_at":         s["updated_at"],
        "finished_at":        s.get("finished_at"),
        "eta_seconds":        eta,
        "audit_complete":     bool(s.get("audit_complete")),
        "incomplete_reasons": (s.get("incomplete_reasons") or "").split(",") if s.get("incomplete_reasons") else [],
        "last_chunks":        chunks,
        "recent_events":      events,
        "artifacts_ready":    s["status"] == "done" and len(arts) > 0,
        "artifacts":          [{"kind": a["kind"], "path": a["path"], "size_bytes": a["size_bytes"]} for a in arts],
    }


@mcp.tool()
def librecrawl_audit_artifacts(session_id: str) -> dict:
    """
    Return the file paths of artifacts produced for a finished session
    (MD report + per-page.csv + sitemap-recon.csv; PDF in v1.5+).

    Safe to call any time after status becomes 'done'. If called earlier
    returns success=False with a hint to wait.

    Args:
        session_id: From librecrawl_start_chunked_audit().
    """
    s = _state.get_session(session_id)
    if not s:
        return {"success": False, "error": f"Unknown session_id: {session_id}"}
    if s["status"] != "done":
        return {
            "success": False,
            "session_id": session_id,
            "status": s["status"],
            "error": f"Artifacts not ready — status is '{s['status']}'. Poll librecrawl_audit_status() first.",
        }
    arts = _state.list_artifacts(session_id)
    return {
        "success":     True,
        "session_id":  session_id,
        "artifacts":   {a["kind"]: a["path"] for a in arts},
        "size_bytes":  {a["kind"]: a["size_bytes"] for a in arts},
    }


@mcp.tool()
def librecrawl_audit_pause(session_id: str) -> dict:
    """Pause a crawling session. Resume with librecrawl_audit_resume()."""
    return _runner.pause_session(session_id)


@mcp.tool()
def librecrawl_audit_resume(session_id: str) -> dict:
    """Resume a paused or throttled session. Runner picks it back up on the next loop."""
    return _runner.resume_session(session_id)


@mcp.tool()
def librecrawl_audit_cancel(session_id: str) -> dict:
    """Terminal stop. Upstream crawl is stopped; partial artifacts are kept where written."""
    return _runner.cancel_session(session_id)


@mcp.tool()
def librecrawl_audit_force_advance(session_id: str) -> dict:
    """
    Stuck-recovery escape hatch: force-finalise from whatever pages have been
    crawled so far. Stops the upstream, builds the report + sidecar CSVs from
    the current export, marks the session 'done'.

    Only use when status has been 'crawling'/'throttled' for an unusually long
    time and recent_events shows no progress.
    """
    return _runner.force_advance(session_id)


# ── v1.2.0 Screaming-Frog parity tools ────────────────────────────────────────

@mcp.tool()
def librecrawl_full_audit_strict(url: str, max_pages: int = 0, auto_purge: bool = True,
                                  keep_for_days: int = 0) -> dict:
    """
    STRICT-MODE site audit — Screaming Frog parity. Same as librecrawl_audit
    but the response is annotated with an audit_complete flag and the run is
    marked incomplete if ANY of these happen:
      • crawl timed out before queue drained
      • max_pages cap was hit
      • upstream batch caps tripped
      • report file or sidecar CSV failed to write
      • sitemap reconciliation could not be computed

    Use this when you need a defensible audit certificate — every cap, sample,
    or partial result is surfaced loudly rather than hidden in summary counts.

    On success the run produces 3 files in REPORTS_DIR:
      • <domain>-<ts>.md                — full markdown report
      • <domain>-<ts>.per-page.csv      — one row per crawled URL × failed checks
      • <domain>-<ts>.sitemap-recon.csv — sitemap-vs-crawl diff

    Args:
        url:           Full URL to crawl.
        max_pages:     Hard ceiling on pages. 0 = unlimited (default — recommended).
        auto_purge:    If True (default), the upstream crawl record is deleted
                       from the LibreCrawl DB once the report is written. Set
                       False (or keep_for_days>0) to retain it for re-export.
        keep_for_days: Retention override. >0 disables auto_purge.
    """
    result = librecrawl_audit(url=url, max_pages=max_pages)
    if not result.get("success"):
        result["strict_mode"] = True
        result["audit_complete"] = False
        return result

    completeness = result.get("crawl_completeness") or {}
    # Strict mode is harder than the soft audit_complete: ANY failure mode flips it.
    strict_ok = bool(
        completeness.get("audit_complete")
        and not completeness.get("timeout_hit")
        and not completeness.get("max_pages_hit")
        and not completeness.get("batch_caps_hit")
        and (result.get("per_page_csv") or {}).get("rows", 0) > 0
        and not (result.get("sitemap_reconciliation") or {}).get("sitemap_fetch_errors")
    )
    result["strict_mode"]    = True
    result["audit_complete"] = strict_ok
    result["incomplete_reasons"] = [
        k for k, v in {
            "queued_remaining":     completeness.get("queued_remaining", 0) > 0,
            "timeout_hit":          completeness.get("timeout_hit"),
            "max_pages_hit":        completeness.get("max_pages_hit"),
            "batch_caps_hit":       completeness.get("batch_caps_hit"),
            "per_page_csv_empty":   (result.get("per_page_csv") or {}).get("rows", 0) == 0,
            "sitemap_fetch_errors": bool((result.get("sitemap_reconciliation") or {}).get("sitemap_fetch_errors")),
        }.items() if v
    ]

    # Auto-purge the upstream crawl record now that the report + sidecars are on disk.
    # Default-on per Aditya's decision (2026-06-03). Override with keep_for_days>0 or
    # auto_purge=False to retain the upstream DB record for re-export.
    if auto_purge and keep_for_days <= 0 and result.get("crawl_id") and strict_ok:
        try:
            purge = librecrawl_brain_purge_audit(crawl_id=result["crawl_id"])
            result["brain_purge"] = purge
        except Exception as e:
            result["brain_purge"] = {"success": False, "error": str(e)}
    else:
        result["brain_purge"] = {"success": False, "skipped": True,
                                  "reason": "auto_purge=False or keep_for_days>0 or audit incomplete"}

    return result


@mcp.tool()
def librecrawl_report_content(report_path: str, max_chars: int = 200_000) -> dict:
    """
    Return the contents of a saved audit report file. The MCP wrapper runs on
    a remote VPS, so callers cannot read REPORTS_DIR over the filesystem —
    this tool serves the file back through MCP.

    Path-restricted to REPORTS_DIR for safety; anything outside returns an error.

    Args:
        report_path: Path returned by librecrawl_audit / generate_report / full_audit_strict.
                     Accepts the .md report, the .per-page.csv, or .sitemap-recon.csv.
        max_chars:   Max characters to return. Default 200k. Content beyond
                     this is truncated; set higher only if your client can handle it.
    """
    p = Path(report_path).resolve()
    if not str(p).startswith(str(REPORTS_DIR.resolve())):
        return {"success": False, "error": f"Path must be within REPORTS_DIR ({REPORTS_DIR})"}
    if not p.exists():
        return {"success": False, "error": f"File not found: {report_path}"}
    try:
        body = p.read_text(encoding="utf-8")
    except Exception as e:
        return {"success": False, "error": f"Read failed: {e}"}
    truncated = len(body) > max_chars
    return {
        "success":      True,
        "path":         str(p),
        "total_chars":  len(body),
        "truncated":    truncated,
        "content":      body[:max_chars],
    }


@mcp.tool()
def librecrawl_pagespeed_audit_all_crawl_pages(crawl_id: int, strategy: str = "mobile",
                                                limit: int = 0, delay_seconds: float = 1.0) -> dict:
    """
    Full PageSpeed Insights audit across EVERY crawled URL from a saved crawl.
    Makes the performance audit explicit — no sampling, no hidden caps. Each
    call to Google PSI counts against your daily 25k-call quota; budget
    accordingly (1 strategy = 1 call per URL).

    Use this when you need defensible CWV coverage for the entire site,
    not just the top N pages from librecrawl_pagespeed_audit.

    Args:
        crawl_id:      The crawl ID to pull URLs from (librecrawl_list_crawls).
        strategy:      "mobile" (default) or "desktop".
        limit:         Hard ceiling on URLs to audit. 0 = no limit (recommended).
                       If limit > 0 and the crawl has more, batch_caps_hit=True.
        delay_seconds: Sleep between PSI calls. Default 1.0 (under PSI rate limit).
    """
    if not PSI_API_KEY:
        return {"success": False, "error": "PAGESPEED_API_KEY not set in environment."}

    # Load the crawl and export
    try:
        call("POST", f"/api/crawls/{crawl_id}/load")
        r = get_client().post(f"{BASE}/api/export_data", json={
            "format": "json", "fields": ["url", "status_code"],
        }, timeout=300)
        r.raise_for_status()
        pages, _ = _parse_export(r.json())
    except Exception as e:
        return {"success": False, "error": f"Could not load crawl {crawl_id}: {e}"}

    # Only test 2xx URLs (4xx/5xx have no PSI value)
    urls = [p.get("url") for p in pages if str(p.get("status_code","")).startswith("2") and p.get("url")]
    total_available = len(urls)
    if limit > 0 and len(urls) > limit:
        urls = urls[:limit]
    batch_caps_hit = bool(limit > 0 and total_available > limit)

    results, errors = [], []
    for i, u in enumerate(urls):
        try:
            res = _fetch_psi(u, strategy)
            if "error" in res:
                errors.append({"url": u, "error": res["error"]})
            else:
                results.append(res)
        except Exception as e:
            errors.append({"url": u, "error": str(e)})
        if i < len(urls) - 1:
            time.sleep(delay_seconds)

    results.sort(key=lambda r: r.get("scores", {}).get("performance", 100))

    return {
        "success":               True,
        "crawl_id":              crawl_id,
        "strategy":              strategy,
        "total_urls_in_crawl":   total_available,
        "urls_audited":          len(results),
        "errors":                errors,
        "batch_caps_hit":        batch_caps_hit,
        "audit_complete":        not batch_caps_hit and len(errors) == 0,
        "results_worst_first":   results,
    }


@mcp.tool()
def librecrawl_brain_purge_audit(crawl_id: int) -> dict:
    """
    Delete a crawl's data from the upstream LibreCrawl database after the
    report has been consumed. Use this for post-audit cleanup so the DB
    doesn't accumulate stale crawl payloads.

    The Markdown report and sidecar CSV files in REPORTS_DIR are NOT touched —
    they live on disk independently of the DB record.

    Args:
        crawl_id: ID returned by librecrawl_audit / start_crawl / list_crawls.
    """
    # Try the canonical REST delete first. If upstream LibreCrawl doesn't
    # expose it, fall back to /api/clear_data (clears the active in-memory
    # crawler, leaving the DB record but freeing memory).
    try:
        r = get_client().delete(f"{BASE}/api/crawls/{crawl_id}", timeout=30)
        if r.status_code in (200, 204):
            return {"success": True, "crawl_id": crawl_id, "method": "delete_endpoint"}
        # 404/405 — fall through
    except Exception:
        pass

    try:
        result = call("POST", "/api/clear_data")
        return {
            "success":  True,
            "crawl_id": crawl_id,
            "method":   "clear_active_buffer",
            "note":     "Upstream does not expose DELETE /api/crawls/<id>; cleared the active in-memory crawler. DB record remains.",
            "upstream": result,
        }
    except Exception as e:
        return {
            "success":  False,
            "crawl_id": crawl_id,
            "error":    f"Both delete and clear_data failed: {e}",
        }


if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "http")
    if transport == "stdio":
        # Stdio mode: works with Cursor, Windsurf, Codex, Continue.dev, and any MCP client.
        # Set MCP_TRANSPORT=stdio in your client config (see README for examples).
        mcp.run()
    else:
        # HTTP/streamable mode: default for Claude Desktop + Claude Code via PM2.
        import uvicorn
        uvicorn.run(mcp.streamable_http_app(), host="127.0.0.1", port=MCP_PORT, log_level="info")
