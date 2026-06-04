"""
Extended SEO checks pack (librecrawl-mcp v1.5 + v2.0 easy half).

Closes the gap between LibreCrawl's upstream checks and Screaming-Frog parity
on the items that don't need a JS render. Each check writes findings to a
single combined CSV with columns:
    url, check_name, severity, finding_detail

CHECKS IMPLEMENTED:

  v1.5 Security & directives
    - missing_hsts_header
    - missing_csp_header
    - missing_x_frame_options
    - missing_x_content_type_options
    - missing_referrer_policy
    - x_robots_tag_vs_meta_mismatch
    - mixed_content (https page with http subresources)

  v1.5 Crawl integrity
    - hreflang_missing_return_tag (bidirectional graph)
    - sitemap_url_noindex
    - sitemap_url_3xx
    - sitemap_url_disallowed_in_robots
    - soft_404 (200 + thin body + telltale phrase)
    - canonical_chain_depth (> 1)

  v2.0-easy URL quality checks (per-page from the crawl export — no fetch)
    - url_contains_space
    - url_multiple_slashes
    - url_non_ascii
    - url_repetitive_path
    - url_underscores

  v2.0-easy link-quality checks (from the flat links list)
    - non_descriptive_anchor_text
    - empty_anchor_text

Public entry point:
    run_extended_checks(pages, base_url, output_path, links=None,
                          limit=50, max_workers=5, timeout=8.0) -> dict
"""

from __future__ import annotations

import asyncio
import csv
import re
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse, urljoin
from xml.etree import ElementTree as ET

import httpx


# ── Configuration ─────────────────────────────────────────────────────────────

SOFT_404_PHRASES = (
    "not found", "page does not exist", "no results", "404",
    "page you're looking for", "doesn't exist", "nothing here",
    "page you are looking for cannot",
)
SOFT_404_MAX_BODY_LEN = 800
SOFT_404_MIN_PHRASES = 1  # phrase + thin-content = high confidence

NON_DESCRIPTIVE_ANCHORS = (
    "click here", "read more", "more", "learn more", "here",
    "this", "click", "continue", "details", "info",
)

SECURITY_HEADERS = {
    "missing_hsts_header":             "strict-transport-security",
    "missing_csp_header":               "content-security-policy",
    "missing_x_frame_options":          "x-frame-options",
    "missing_x_content_type_options":   "x-content-type-options",
    "missing_referrer_policy":          "referrer-policy",
}

# ── v1.7 Tier 1 constants ─────────────────────────────────────────────────────

# WAF / bot-block challenge fingerprints. Each entry: (waf_name, [body_substr])
# Pages that match return 200 OK with these body markers but a real human / bot
# would see a JS challenge or CAPTCHA. Tool reports "clean" → wrong; flag it.
WAF_FINGERPRINTS = [
    ("cloudflare",  ["cf-browser-verification", "checking your browser before accessing",
                     "cf-challenge-running", "cloudflare ray id",
                     "ray id:", "/cdn-cgi/challenge-platform/",
                     "please enable cookies and reload the page"]),
    ("akamai",       ["pixel_d9c66e4d", "akamai bot manager", "ak-bmsc",
                     "reference&#32;number:", "_abck=", "bm-sz="]),
    ("datadome",     ["geo.captcha-delivery.com", "datadome", "dd_cookie_test"]),
    ("imperva",      ["imperva", "incapsula incident id", "_incap_ses",
                     "incident id:", "visid_incap"]),
    ("perimeterx",   ["perimeterx", "_px3=", "px-captcha",
                     "are you a robot?"]),
]

# Soft-redirect detection patterns
META_REFRESH_RE = re.compile(
    r'<meta[^>]+http-equiv\s*=\s*["\']refresh["\'][^>]+content\s*=\s*["\']\s*\d+\s*;\s*url\s*=\s*([^"\'>\s]+)',
    re.IGNORECASE,
)
JS_REDIRECT_RE = re.compile(
    r'(?:window|document|top|self|parent)\.location(?:\.href|\.replace\(|\s*=)\s*[\(=]?\s*["\']([^"\']+)["\']',
    re.IGNORECASE,
)

# Broken-bookmark detection: extract every <a href="#xyz"> and every id="xyz"
# (or name="xyz" for legacy <a name=…>) from the same HTML, diff.
ANCHOR_HREF_RE = re.compile(r'<a\s+[^>]*href\s*=\s*["\']#([^"\'\s]+)', re.IGNORECASE)
ID_NAME_RE     = re.compile(r'\b(?:id|name)\s*=\s*["\']([^"\'\s]+)', re.IGNORECASE)

# ── v1.8 Tier 2 patterns ──────────────────────────────────────────────────────

# Image-tag attribute probes for performance / CLS / format checks.
# These run over the page body once per page in _check_from_response.
IMG_TAG_RE        = re.compile(r'<img\b[^>]*>', re.IGNORECASE)
IMG_SRC_RE        = re.compile(r'\bsrc\s*=\s*["\']([^"\']+)', re.IGNORECASE)
IMG_LOADING_RE    = re.compile(r'\bloading\s*=\s*["\']lazy', re.IGNORECASE)
IMG_SRCSET_RE     = re.compile(r'\bsrcset\s*=', re.IGNORECASE)
IMG_WIDTH_RE      = re.compile(r'\bwidth\s*=', re.IGNORECASE)
IMG_HEIGHT_RE     = re.compile(r'\bheight\s*=', re.IGNORECASE)
NEXT_GEN_FMT_EXT  = (".webp", ".avif")
LEGACY_FMT_EXT    = (".jpg", ".jpeg", ".png", ".gif")

# Iframe + favicon + DOM
IFRAME_TAG_RE     = re.compile(r'<iframe\b[^>]*>', re.IGNORECASE)
IFRAME_TITLE_RE   = re.compile(r'\btitle\s*=\s*["\']', re.IGNORECASE)
LINK_TAG_RE       = re.compile(r'<link\b[^>]*>', re.IGNORECASE)
HEAD_BLOCK_RE     = re.compile(r'<head\b[^>]*>(.*?)</head>', re.IGNORECASE | re.DOTALL)
NOSCRIPT_IN_HEAD_RE = re.compile(r'<noscript\b', re.IGNORECASE)
LINK_REL_RE       = re.compile(r'\brel\s*=\s*["\']([^"\']+)', re.IGNORECASE)
LINK_HREF_RE      = re.compile(r'\bhref\s*=\s*["\']([^"\']+)', re.IGNORECASE)

# Approx DOM-size probe: count tag-open characters in body.
TAG_OPEN_RE       = re.compile(r'<[a-zA-Z][^>]*?>')
DOM_SIZE_THRESHOLD = 1500          # > this many tag-opens → excessive
HTML_OVER_2MB     = 2 * 1024 * 1024 # 2 MB — Google's parse limit

# Canonical position probe — used to detect canonical_outside_head
CANONICAL_LINK_RE = re.compile(
    r'<link\b[^>]+rel\s*=\s*["\']canonical["\'][^>]*>',
    re.IGNORECASE,
)

# Sitemap size + format checks
SITEMAP_URL_HARD_LIMIT = 50_000   # Google's per-file URL cap
SITEMAP_SIZE_HARD_LIMIT_BYTES = 50 * 1024 * 1024   # 50 MB uncompressed

# Hreflang code validation. Approximation: ISO 639-1 (2-letter) optional
# -Aa-Zz (script) optional -ZZ (region) or "x-default".
VALID_HREFLANG_RE = re.compile(
    r'^(?:x-default|[a-z]{2,3}(?:-[A-Z][a-z]{3})?(?:-(?:[A-Z]{2}|\d{3}))?)$',
)

# Localhost / private-IP outbound link detection
LOCALHOST_HOST_RE = re.compile(
    r'^(localhost|127\.0\.0\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}'
    r'|192\.168\.\d{1,3}\.\d{1,3}|169\.254\.\d{1,3}\.\d{1,3}'
    r'|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(?::\d+)?$',
    re.IGNORECASE,
)

# Crawl-trap detection
CALENDAR_FUTURE_YEAR_RE = re.compile(r'[?&](?:year|y|yr)=(\d{4})', re.IGNORECASE)
SESSION_PARAM_NAMES = {
    "phpsessid", "jsessionid", "aspsessionid", "sid", "sessionid",
    "session_id", "sess_id", "ssid",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_http(u: str) -> bool:
    return urlparse(u).scheme.lower() in ("http", "https")


def _normalise_for_match(u: str) -> str:
    """Strip fragment & trailing slash; lowercase host."""
    if not u:
        return ""
    p = urlparse(u.strip())
    host = (p.hostname or "").lower()
    path = (p.path or "/").rstrip("/") or "/"
    return f"{p.scheme}://{host}{path}"


# ── Per-page URL-quality checks (synchronous, no fetch) ───────────────────────

def _check_url_quality(url: str) -> list:
    """Run URL-string-only checks. Returns list of (check_name, detail)."""
    findings = []
    parsed = urlparse(url)
    path   = parsed.path or "/"

    if " " in url:
        findings.append(("url_contains_space", "Whitespace in URL"))

    # Collapse leading double slash from protocol; check path-only
    if "//" in path:
        findings.append(("url_multiple_slashes", "Consecutive slashes in URL path"))

    try:
        url.encode("ascii")
    except UnicodeEncodeError:
        findings.append(("url_non_ascii", "URL contains non-ASCII characters"))

    if "_" in path:
        findings.append(("url_underscores",
                         "Underscores in URL path - Google prefers hyphens"))

    # Repetitive path segments like /blog/blog/post
    segments = [s for s in path.split("/") if s]
    if segments:
        for i in range(len(segments) - 1):
            if segments[i] and segments[i] == segments[i + 1]:
                findings.append(("url_repetitive_path",
                                 f"Repeated path segment: {segments[i]}"))
                break

    return findings


# ── Anchor-text quality from flat links list ──────────────────────────────────

def _check_anchor_quality(links: list | None) -> list:
    """Returns list of (url, check, detail) tuples."""
    if not links:
        return []
    out = []
    for lk in links:
        if not isinstance(lk, dict):
            continue
        anchor = (lk.get("anchor_text") or lk.get("anchor") or "").strip()
        target = (lk.get("target_url") or lk.get("url") or "").strip()
        source = (lk.get("source_url") or "").strip()
        if not target:
            continue
        if not anchor:
            # Skip pure-image anchors (would also be flagged anchor_image_no_alt
            # elsewhere). The link CSV's `placement` field is unreliable across
            # LibreCrawl versions, so we flag empty anchor regardless.
            out.append((source or target,
                        "empty_anchor_text",
                        f"Empty anchor text linking to {target}"))
            continue
        if anchor.lower() in NON_DESCRIPTIVE_ANCHORS:
            out.append((source or target,
                        "non_descriptive_anchor_text",
                        f'Non-descriptive anchor "{anchor}" -> {target}'))
    return out


# ── Hreflang return-tag verification ─────────────────────────────────────────

def _check_hreflang_returns(pages: list) -> list:
    """Build the hreflang directed graph and flag asymmetric edges.

    LibreCrawl's per-page export includes `hreflang` as a list of dicts:
        [{"lang": "en-US", "href": "https://..."}, ...]
    (Older versions ship it as a comma-separated string; we handle both.)
    """
    graph = defaultdict(set)  # url -> {targets}
    pages_url_set = set()

    for p in pages or []:
        u = _normalise_for_match(p.get("url") or "")
        if not u:
            continue
        pages_url_set.add(u)
        hl = p.get("hreflang")
        targets = set()
        if isinstance(hl, list):
            for entry in hl:
                if isinstance(entry, dict):
                    href = (entry.get("href") or entry.get("url") or "").strip()
                    if href:
                        targets.add(_normalise_for_match(href))
                elif isinstance(entry, str):
                    targets.add(_normalise_for_match(entry))
        elif isinstance(hl, str):
            for part in hl.split(","):
                part = part.strip()
                # Format may be "en-US:https://..." or just URLs
                if ":" in part and "://" in part:
                    href = part.split(":", 1)[1].strip()
                    targets.add(_normalise_for_match(href))
                elif "://" in part:
                    targets.add(_normalise_for_match(part))
        graph[u] = targets

    out = []
    for src, targets in graph.items():
        for tgt in targets:
            if tgt == src:
                continue
            if tgt not in pages_url_set:
                continue  # outside-of-crawl target - can't verify return
            return_targets = graph.get(tgt, set())
            if src not in return_targets:
                out.append((src, "hreflang_missing_return_tag",
                            f"Links via hreflang to {tgt} but no return tag"))
    return out


# ── Canonical chain depth + canonical-target health ──────────────────────────

def _check_canonical_chains(pages: list) -> list:
    """Flag canonical pathologies — depth, relative paths, points-to-redirect."""
    canon_map = {}  # url -> canonical_url (both normalised, for chain detection)
    status_by_url = {}  # normalised url -> status_code
    for p in pages or []:
        u = _normalise_for_match(p.get("url") or "")
        c_raw = (p.get("canonical_url") or "").strip()
        c_norm = _normalise_for_match(c_raw)
        if u:
            status_by_url[u] = str(p.get("status_code", ""))
        if u and c_norm and u != c_norm:
            canon_map[u] = c_norm

    out = []
    for p in pages or []:
        u_raw = (p.get("url") or "").strip()
        u = _normalise_for_match(u_raw)
        c_raw = (p.get("canonical_url") or "").strip()
        if not u or not c_raw:
            continue
        # canonical_to_relative: href without scheme is relative
        if not urlparse(c_raw).scheme:
            out.append((u_raw, "canonical_to_relative",
                        f"Canonical uses relative path '{c_raw}' — should be absolute"))
        # canonical_to_redirect: canonical target itself returns 3xx in our crawl
        c_norm = _normalise_for_match(c_raw)
        tgt_status = status_by_url.get(c_norm, "")
        if tgt_status.startswith("3"):
            out.append((u_raw, "canonical_to_redirect",
                        f"Canonical {c_norm} returns {tgt_status} — link equity wasted"))

    # canonical_chain_depth — chain hop
    for src, c1 in canon_map.items():
        c2 = canon_map.get(c1)
        if c2 and c2 != c1:
            out.append((src, "canonical_chain_depth",
                        f"Canonical -> {c1} -> {c2} (depth > 1)"))
    return out


# ── Hreflang full audit (v1.8) ────────────────────────────────────────────────

def _check_hreflang_full(pages: list) -> list:
    """Extends the v1.6 return-tag-only check with 6 more hreflang validations.

    Required-field tables apply per Google docs:
      https://developers.google.com/search/docs/specialty/international/localized-versions
    """
    # Build map first
    page_by_norm = {_normalise_for_match(p.get("url") or ""): p for p in (pages or [])}
    out = []
    cluster_by_node = defaultdict(set)  # node URL -> set of hreflang targets (norm)

    for p in pages or []:
        u_raw = (p.get("url") or "").strip()
        u = _normalise_for_match(u_raw)
        if not u:
            continue
        hl = p.get("hreflang")
        page_lang_attr = (p.get("lang") or "").strip().lower()

        entries = []  # list of (lang_code, href_norm, href_raw)
        if isinstance(hl, list):
            for e in hl:
                if isinstance(e, dict):
                    lang = (e.get("hreflang") or e.get("lang") or "").strip()
                    href = (e.get("href") or e.get("url") or "").strip()
                    if href:
                        entries.append((lang, _normalise_for_match(href), href))
                elif isinstance(e, str) and "://" in e:
                    entries.append(("", _normalise_for_match(e), e))
        elif isinstance(hl, str):
            for part in hl.split(","):
                part = part.strip()
                if "://" in part:
                    href = part.split(":", 1)[1].strip() if ":" in part.split("://", 1)[0] else part
                    entries.append(("", _normalise_for_match(href), href))

        if not entries:
            continue

        langs = [e[0] for e in entries]
        targets = {e[1] for e in entries}
        cluster_by_node[u] |= targets

        # hreflang_missing_self_reference: self-ref required per Google
        if u not in targets:
            out.append((u_raw, "hreflang_missing_self_reference",
                        "Page in hreflang cluster but no self-referential hreflang"))
        # hreflang_missing_x_default: cluster must have x-default fallback
        if not any(l.lower() == "x-default" for l in langs):
            out.append((u_raw, "hreflang_missing_x_default",
                        "Hreflang cluster lacks x-default fallback"))
        # hreflang_invalid_codes
        for lang in langs:
            if lang and not VALID_HREFLANG_RE.match(lang):
                out.append((u_raw, "hreflang_invalid_codes",
                            f"Invalid hreflang code: '{lang}' (expected ISO 639-1 / 3166-1)"))
                break
        # hreflang_conflicts_lang_attr: hreflang language disagrees with <html lang=>
        if page_lang_attr:
            page_lang_base = page_lang_attr.split("-")[0]
            self_entry = next((e for e in entries if e[1] == u), None)
            if self_entry and self_entry[0] and not self_entry[0].lower().startswith(page_lang_base):
                out.append((u_raw, "hreflang_conflicts_lang_attr",
                            f"hreflang='{self_entry[0]}' but <html lang='{page_lang_attr}'>"))
        # hreflang_to_broken: target in crawl returned 4xx/5xx
        for lang, tgt_norm, tgt_raw in entries:
            tgt_page = page_by_norm.get(tgt_norm)
            if tgt_page:
                tsc = str(tgt_page.get("status_code", ""))
                if tsc.startswith(("4", "5")):
                    out.append((u_raw, "hreflang_to_broken",
                                f"hreflang points to {tgt_raw} which returns {tsc}"))
                # hreflang_to_noindex
                if "noindex" in (tgt_page.get("robots") or "").lower():
                    out.append((u_raw, "hreflang_to_noindex",
                                f"hreflang points to {tgt_raw} which is noindex"))

    return out


# ── Internal nofollow patterns ─────────────────────────────────────────────────

def _check_nofollow_patterns(links: list | None, pages: list) -> list:
    """Detect three internal-nofollow anti-patterns:

      internal_nofollow_outlinks  — page emits at least one nofollow internal link
      nofollow_only_inbound        — page only RECEIVES nofollow internal links
      follow_and_nofollow_mixed    — page receives both follow + nofollow internal
                                      (confusing signal — Google may discount the page)
    """
    if not links:
        return []
    page_url_set = {_normalise_for_match(p.get("url") or "") for p in (pages or [])}
    outbound_nofollow = defaultdict(int)   # source page → nofollow internal count
    inbound_follow    = defaultdict(int)   # target page → follow internal count
    inbound_nofollow  = defaultdict(int)   # target page → nofollow internal count

    for lk in links:
        if not isinstance(lk, dict):
            continue
        is_internal = lk.get("is_internal")
        if isinstance(is_internal, str):
            is_internal = is_internal.lower() in ("yes", "true", "1")
        if not is_internal:
            continue
        src = _normalise_for_match(lk.get("source_url") or "")
        tgt = _normalise_for_match(lk.get("target_url") or lk.get("url") or "")
        rel = (lk.get("rel") or "").lower()
        is_nofollow = "nofollow" in rel
        if src in page_url_set:
            if is_nofollow:
                outbound_nofollow[src] += 1
        if tgt in page_url_set:
            if is_nofollow:
                inbound_nofollow[tgt] += 1
            else:
                inbound_follow[tgt] += 1

    out = []
    for src, n in outbound_nofollow.items():
        out.append((src, "internal_nofollow_outlinks",
                    f"Page emits {n} nofollow internal link(s) — Google may discount these targets"))

    for tgt in page_url_set:
        nf = inbound_nofollow.get(tgt, 0)
        fo = inbound_follow.get(tgt, 0)
        if nf > 0 and fo > 0:
            out.append((tgt, "follow_and_nofollow_mixed",
                        f"Page receives both follow ({fo}) and nofollow ({nf}) internal links — mixed signal"))
        elif nf > 0 and fo == 0:
            out.append((tgt, "nofollow_only_inbound",
                        f"Page receives only nofollow ({nf}) internal links — likely won't get indexed"))
    return out


def _check_anchor_image_no_alt(links: list | None) -> list:
    """anchor_image_no_alt: <a><img></a> where img has no alt and the anchor
    has no other text — Google sees an empty-anchor link."""
    if not links:
        return []
    out = []
    for lk in links:
        if not isinstance(lk, dict):
            continue
        anchor = (lk.get("anchor_text") or lk.get("anchor") or "").strip()
        # Heuristic: empty/whitespace anchor + the source link came from an img.
        # Flat links list doesn't include the inner-HTML detail, so this is
        # best-effort: anchor empty + anchor contains-only-whitespace or
        # was tagged "image" by upstream. Conservative: only flag when anchor
        # is completely empty AND link has no rel attribute (real links almost
        # always have anchor text or rel).
        if anchor == "" and not (lk.get("rel") or ""):
            src = (lk.get("source_url") or "").strip()
            tgt = (lk.get("target_url") or lk.get("url") or "").strip()
            if src and tgt:
                out.append((src, "anchor_image_no_alt",
                            f"Empty-anchor link to {tgt} (likely <a><img></a> with no alt)"))
    return out


# ── Crawl-trap detection ──────────────────────────────────────────────────────

def _check_crawl_traps(pages: list) -> list:
    """Detect three crawl-budget killers:

      spider_trap_calendar       — URLs with future-year params (calendar widget trap)
      url_session_id_high_entropy — URLs carrying session-id params (PHPSESSID, etc.)
      faceted_url_explosion      — URL-param cluster: same path + >3 different
                                    parameter combinations (filter explosion)
    """
    from datetime import datetime
    this_year = datetime.now().year
    out = []
    path_param_clusters = defaultdict(set)  # path -> set of param signatures

    for p in pages or []:
        url = (p.get("url") or "").strip()
        if not url:
            continue
        parsed = urlparse(url)

        # spider_trap_calendar: future year in query
        for m in CALENDAR_FUTURE_YEAR_RE.finditer(url):
            try:
                yr = int(m.group(1))
                if yr > this_year + 2:
                    out.append((url, "spider_trap_calendar",
                                f"Calendar URL points to future year {yr}"))
                    break
            except ValueError:
                pass

        # url_session_id_high_entropy
        query_keys = {k.split("=")[0].lower() for k in parsed.query.split("&") if k}
        sess_hits = query_keys & SESSION_PARAM_NAMES
        if sess_hits:
            out.append((url, "url_session_id_high_entropy",
                        f"Session-id param in URL: {', '.join(sess_hits)}"))

        # faceted_url_explosion — bucket by path, track distinct query signatures
        if parsed.query:
            sig = tuple(sorted(k.split("=", 1)[0].lower() for k in parsed.query.split("&") if k))
            path_param_clusters[parsed.path].add(sig)

    # Flag paths with > 3 distinct parameter signatures (rough threshold)
    for path, sigs in path_param_clusters.items():
        if len(sigs) > 3:
            out.append((path, "faceted_url_explosion",
                        f"Path {path} has {len(sigs)} distinct query-parameter combinations"))
    return out


def _check_localhost_outlinks(links: list | None) -> list:
    """outlinks_to_localhost — flag any outbound link with localhost / private IP host."""
    if not links:
        return []
    out = []
    seen = set()
    for lk in links:
        if not isinstance(lk, dict):
            continue
        tgt = (lk.get("target_url") or lk.get("url") or "").strip()
        if not tgt:
            continue
        host = (urlparse(tgt).hostname or "")
        if host and LOCALHOST_HOST_RE.match(host):
            key = (lk.get("source_url"), tgt)
            if key in seen:
                continue
            seen.add(key)
            out.append((lk.get("source_url", ""), "outlinks_to_localhost",
                        f"Page links to localhost/private-IP host: {tgt}"))
    return out


# ── Sitemap cross-checks (fetch sitemap, intersect with crawl) ───────────────

def _fetch_sitemap_urls(sitemap_url: str, timeout_s: float = 10.0) -> tuple:
    """Best-effort fetch of sitemap.xml. Returns (urls_list, size_bytes).

    v1.8: also returns the raw fetched-size so size-based checks can fire.
    """
    try:
        r = httpx.get(sitemap_url, timeout=timeout_s, follow_redirects=True,
                      headers={"User-Agent": "LibreCrawl-MCP/1.5"})
        if r.status_code >= 400:
            return [], 0
        size_bytes = len(r.content)
        root = ET.fromstring(r.content)
    except Exception:
        return [], 0

    # Strip XML namespace - sitemap.xml uses sitemaps.org/schemas/sitemap/0.9
    def _local(tag):
        return tag.split("}", 1)[-1] if "}" in tag else tag

    urls = []
    # urlset > url > loc
    for el in root.iter():
        if _local(el.tag) == "loc":
            urls.append((el.text or "").strip())
    return urls, size_bytes


def _fetch_robots_disallow(base_url: str, timeout_s: float = 10.0) -> list:
    """Best-effort robots.txt fetch. Returns list of Disallow path prefixes
    that apply to user-agent: *."""
    parsed = urlparse(base_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        r = httpx.get(robots_url, timeout=timeout_s, follow_redirects=True)
        if r.status_code >= 400:
            return []
    except Exception:
        return []

    disallows = []
    in_wildcard = False
    for line in r.text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k = k.strip().lower()
        v = v.strip()
        if k == "user-agent":
            in_wildcard = (v == "*")
        elif k == "disallow" and in_wildcard and v:
            disallows.append(v)
    return disallows


def _check_sitemap_crosschecks(pages: list, base_url: str) -> list:
    """Cross-check sitemap URLs against the crawl's known statuses + noindex.

    v1.8: also flags sitemap-spec violations — over 50k URLs, over 50 MB,
    URLs that canonicalise away (sitemap shouldn't list non-canonical pages).
    """
    sitemap_url = f"{base_url.rstrip('/')}/sitemap.xml"
    sitemap_urls, size_bytes = _fetch_sitemap_urls(sitemap_url)
    if not sitemap_urls:
        return []

    # Map crawled URLs to their pages
    page_by_url = {_normalise_for_match(p.get("url") or ""): p for p in (pages or [])}
    robots_disallow = _fetch_robots_disallow(base_url)

    out = []
    sitemap_norm = [_normalise_for_match(u) for u in sitemap_urls]

    # v1.8 sitemap-spec checks (Google's per-file limits)
    if len(sitemap_urls) > SITEMAP_URL_HARD_LIMIT:
        out.append((sitemap_url, "sitemap_over_50k_urls",
                    f"Sitemap contains {len(sitemap_urls)} URLs (Google cap: 50,000)"))
    if size_bytes > SITEMAP_SIZE_HARD_LIMIT_BYTES:
        out.append((sitemap_url, "sitemap_over_50mb",
                    f"Sitemap is {size_bytes/1024/1024:.1f} MB uncompressed (Google cap: 50 MB)"))

    for orig_url, norm_url in zip(sitemap_urls, sitemap_norm):
        page = page_by_url.get(norm_url)

        # Is this URL disallowed in robots.txt?
        parsed = urlparse(orig_url)
        for prefix in robots_disallow:
            if parsed.path.startswith(prefix):
                out.append((orig_url, "sitemap_url_disallowed_in_robots",
                            f"In sitemap but robots.txt disallows path: {prefix}"))
                break

        if page is None:
            continue  # not in crawl, can't cross-check

        # noindex?
        robots = (page.get("robots") or "").lower()
        if "noindex" in robots:
            out.append((orig_url, "sitemap_url_noindex",
                        "URL in sitemap but has noindex directive"))

        # 3xx?
        sc = str(page.get("status_code", ""))
        if sc.startswith("3"):
            out.append((orig_url, "sitemap_url_3xx",
                        f"URL in sitemap but returns {sc}"))

        # canonicalised away?  v1.8 — sitemap should list canonical URLs only.
        canon = _normalise_for_match(page.get("canonical_url") or "")
        if canon and canon != norm_url:
            out.append((orig_url, "sitemap_contains_canonicalized",
                        f"URL in sitemap canonicalises to {canon} — sitemap should list canonical only"))

    return out


# ── Soft-404 fingerprinting + headers + mixed content (concurrent fetch) ────

async def _fetch_for_checks(url: str, client: httpx.AsyncClient,
                              timeout_s: float) -> tuple:
    """Returns (url, response_object_or_None, error)."""
    try:
        r = await client.get(url, timeout=timeout_s, follow_redirects=True,
                              headers={
                                  "User-Agent": "LibreCrawl-MCP/1.5 (Extended Checks; +https://github.com/adityaarsharma/librecrawl-mcp)",
                                  "Accept": "text/html,*/*;q=0.5",
                              })
        return url, r, None
    except httpx.ReadTimeout:
        return url, None, "timeout"
    except httpx.ConnectError:
        return url, None, "connect_error"
    except Exception as e:
        return url, None, f"error: {type(e).__name__}"


async def _fetch_all(urls: list, max_workers: int, timeout_s: float) -> list:
    sem = asyncio.Semaphore(max_workers)
    async with httpx.AsyncClient(http2=False, verify=True) as client:
        async def _bounded(u):
            async with sem:
                return await _fetch_for_checks(u, client, timeout_s)
        return await asyncio.gather(*(_bounded(u) for u in urls))


# Pattern: src="http://..." / href="http://..." on a (presumably) HTTPS page.
_HTTP_RES_RE = re.compile(
    r'(?:src|href)\s*=\s*[\'"]\s*(http://[^\s\'"]+)',
    re.IGNORECASE,
)


def _check_from_response(url: str, resp, status: int, headers: dict,
                          body: str) -> list:
    """Per-URL checks against a fetched response."""
    findings = []
    # Security headers (only on 2xx HTTPS pages)
    is_https = urlparse(url).scheme == "https"
    if is_https and 200 <= status < 300:
        for check_name, header_name in SECURITY_HEADERS.items():
            if header_name not in headers:
                findings.append((check_name, f"Missing {header_name} header"))

        # X-Robots-Tag vs meta robots cross-check
        xrt = headers.get("x-robots-tag", "").lower()
        if xrt and body:
            # Naive: pull <meta name="robots" content="...">
            m = re.search(
                r'<meta[^>]+name=["\']robots["\'][^>]+content=["\']([^"\']+)',
                body, re.IGNORECASE,
            )
            meta_robots = (m.group(1).lower() if m else "")
            if meta_robots and xrt != meta_robots:
                # Only flag actual semantic disagreement
                xrt_tokens = set(t.strip() for t in xrt.split(","))
                mr_tokens  = set(t.strip() for t in meta_robots.split(","))
                if ("noindex" in xrt_tokens) != ("noindex" in mr_tokens):
                    findings.append(("x_robots_tag_vs_meta_mismatch",
                                     f'X-Robots-Tag="{xrt}" vs meta robots="{meta_robots}"'))

        # Mixed content
        if body:
            insecure = _HTTP_RES_RE.findall(body)
            if insecure:
                # Dedupe and cap to first 5 for the finding detail
                uniq = list(dict.fromkeys(insecure))[:5]
                findings.append(("mixed_content",
                                 f"HTTPS page references HTTP resources: {', '.join(uniq)}"))

    # Soft-404 detection: 200 status + thin body + telltale phrases
    if 200 <= status < 300 and body:
        body_text = re.sub(r"<[^>]+>", " ", body[:50_000]).lower()
        body_text = re.sub(r"\s+", " ", body_text).strip()
        body_len = len(body_text)
        if body_len < SOFT_404_MAX_BODY_LEN:
            phrase_hits = [p for p in SOFT_404_PHRASES if p in body_text]
            if len(phrase_hits) >= SOFT_404_MIN_PHRASES:
                findings.append(("soft_404",
                                 f"200 status + {body_len}-char body + signal: {phrase_hits[0]}"))

    # ── v1.7 Tier 1 ──────────────────────────────────────────────────────────

    # HTTP `Refresh:` response header — soft redirect at the header layer
    refresh_hdr = headers.get("refresh") or headers.get("Refresh") or ""
    if refresh_hdr:
        findings.append((
            "http_refresh_redirect",
            f"HTTP Refresh header present: {refresh_hdr[:200]}",
        ))

    # `<meta http-equiv="refresh" content="N; url=...">` soft redirect
    if body:
        mr_match = META_REFRESH_RE.search(body)
        if mr_match:
            findings.append((
                "meta_refresh_redirect",
                f"<meta refresh> to {mr_match.group(1)[:200]}",
            ))

        # JS-based redirects — `window.location = "..."` and friends.
        # Heuristic only (we don't render JS). Cap at first 3 matches.
        js_hits = JS_REDIRECT_RE.findall(body)
        if js_hits:
            uniq = list(dict.fromkeys(js_hits))[:3]
            findings.append((
                "js_redirect",
                f"JS location-assignment to: {', '.join(uniq)}",
            ))

        # WAF / bot-block challenge fingerprint. Any matching marker = the
        # bot/audit may have been served a challenge page rather than the
        # real content. Tool reports "200 OK" but the page isn't useful.
        body_lower = body.lower()
        for waf_name, markers in WAF_FINGERPRINTS:
            hits = [m for m in markers if m in body_lower]
            if hits:
                findings.append((
                    "bot_block_challenge_detected",
                    f"{waf_name} challenge page detected (markers: {hits[0]})",
                ))
                break   # one WAF flag is enough per page

        # Broken bookmarks — <a href="#xyz"> with no matching id/name on the
        # same page. Caps at first 5 in the detail string to keep CSVs tidy.
        if 200 <= status < 300:
            hrefs = set(ANCHOR_HREF_RE.findall(body))
            ids   = set(ID_NAME_RE.findall(body))
            broken = sorted(hrefs - ids - {"", "top"})  # "#top" usually scrolls home
            if broken:
                findings.append((
                    "broken_bookmarks",
                    f"{len(broken)} broken #fragments — first: "
                    f"{', '.join('#' + b for b in broken[:5])}",
                ))

    # ── v1.8 Tier 2 HTML-response-layer checks ──────────────────────────────
    if body and 200 <= status < 300:
        body_bytes = len(body.encode("utf-8", errors="ignore"))

        # html_over_2mb — Google's parse-size limit
        if body_bytes > HTML_OVER_2MB:
            findings.append((
                "html_over_2mb",
                f"HTML body {body_bytes/1024/1024:.1f} MB > Google's 2 MB parse limit",
            ))

        # dom_size_excessive — approximation via tag-open count
        tag_count = len(TAG_OPEN_RE.findall(body))
        if tag_count > DOM_SIZE_THRESHOLD:
            findings.append((
                "dom_size_excessive",
                f"~{tag_count} DOM nodes (> {DOM_SIZE_THRESHOLD}) — affects PageSpeed",
            ))

        # ── <head> block analysis ──
        head_match = HEAD_BLOCK_RE.search(body)
        head_block = head_match.group(1) if head_match else ""

        # noscript_in_head — invalid HTML structure (some parsers stop parsing)
        if head_block and NOSCRIPT_IN_HEAD_RE.search(head_block):
            findings.append((
                "noscript_in_head",
                "<noscript> tag inside <head> — invalid HTML5, can break parsers",
            ))

        # missing_favicon — neither <link rel="icon"> nor <link rel="shortcut icon"></link>
        # Guard on head_match (truthy when <head> exists) not head_block (which is
        # empty-string-falsy for <head></head>). Empty head ⇒ no favicon ⇒ missing.
        if head_match:
            has_favicon = False
            for link_tag in LINK_TAG_RE.findall(head_block):
                rel_m = LINK_REL_RE.search(link_tag)
                if rel_m:
                    rel = rel_m.group(1).lower()
                    if "icon" in rel:   # matches "icon", "shortcut icon", "apple-touch-icon"
                        has_favicon = True
                        break
            if not has_favicon:
                findings.append((
                    "missing_favicon",
                    "No <link rel=\"icon\"> (or shortcut/apple-touch-icon) in <head>",
                ))

        # canonical_outside_head — <link rel=canonical> found outside <head>
        body_minus_head = body
        if head_match:
            body_minus_head = body[:head_match.start()] + body[head_match.end():]
        canon_in_body = CANONICAL_LINK_RE.search(body_minus_head)
        if canon_in_body and head_block and not CANONICAL_LINK_RE.search(head_block):
            findings.append((
                "canonical_outside_head",
                "<link rel=canonical> found in <body>, not <head>",
            ))

        # broken_or_invalid_html — pragmatic heuristic: missing required tags
        # or mismatched open/close counts on common containers.
        html_open = body.lower().count("<html")
        html_close = body.lower().count("</html>")
        body_open = body.lower().count("<body")
        body_close = body.lower().count("</body>")
        if html_open == 0 or body_open == 0:
            findings.append((
                "broken_or_invalid_html",
                f"Missing <html> ({html_open}) or <body> ({body_open}) tag",
            ))
        elif html_close == 0 or body_close == 0:
            findings.append((
                "broken_or_invalid_html",
                f"Unclosed <html> or <body> (open={html_open}/{body_open}, "
                f"close={html_close}/{body_close})",
            ))

        # ── Image checks (loop over all <img> tags in body) ──
        no_lazy = 0
        no_srcset = 0
        no_dims = 0
        legacy_fmt = 0
        for img_tag in IMG_TAG_RE.findall(body):
            if not IMG_LOADING_RE.search(img_tag):
                no_lazy += 1
            if not IMG_SRCSET_RE.search(img_tag):
                no_srcset += 1
            if not (IMG_WIDTH_RE.search(img_tag) and IMG_HEIGHT_RE.search(img_tag)):
                no_dims += 1
            src_m = IMG_SRC_RE.search(img_tag)
            if src_m:
                src_low = src_m.group(1).lower().split("?", 1)[0]
                if src_low.endswith(LEGACY_FMT_EXT):
                    legacy_fmt += 1
        # Only flag if 3+ images on the page show the issue — single-image
        # decoration shouldn't dominate the checks_manifest.
        if no_lazy >= 3:
            findings.append((
                "lazy_load_attr_missing",
                f"{no_lazy} <img> tags missing loading=\"lazy\"",
            ))
        if no_srcset >= 3:
            findings.append((
                "srcset_missing",
                f"{no_srcset} <img> tags missing srcset (responsive delivery)",
            ))
        if no_dims >= 3:
            findings.append((
                "image_dimensions_missing",
                f"{no_dims} <img> tags missing width/height — CLS risk",
            ))
        if legacy_fmt >= 3:
            findings.append((
                "next_gen_image_format",
                f"{legacy_fmt} <img> tags using PNG/JPG/GIF — consider WebP/AVIF",
            ))

        # ── Iframes ──
        iframe_count = 0
        iframe_no_title = 0
        for iframe_tag in IFRAME_TAG_RE.findall(body):
            iframe_count += 1
            if not IFRAME_TITLE_RE.search(iframe_tag):
                iframe_no_title += 1
        if iframe_count > 0:
            findings.append((
                "iframes_present",
                f"{iframe_count} iframe(s) on page",
            ))
        if iframe_no_title > 0:
            findings.append((
                "iframe_missing_title",
                f"{iframe_no_title} iframe(s) missing title attribute — accessibility",
            ))

    return findings


# ── Public entry point ────────────────────────────────────────────────────────

def run_extended_checks(pages: list, base_url: str, output_path: Path,
                         links: list | None = None,
                         limit: int = 250,
                         max_workers: int = 5,
                         timeout_seconds: float = 8.0) -> dict:
    """
    Run all extended checks and write findings to a single CSV.

    Args:
        pages:        Per-page export from server._parse_export.
        base_url:     Root URL of the crawl (used for sitemap fetch).
        output_path:  Where to write extended-checks.csv.
        links:        Flat outbound links list from _parse_export.
        limit:        Max pages to fetch for response-level checks. Default 50.
        max_workers:  Concurrent HTTP fetches. Default 5.
        timeout_seconds: Per-request timeout. Default 8s.

    Returns: { path, findings, by_check, top_urls, cap_applied }
    """
    output_path = Path(output_path)
    findings = []  # list of (url, check_name, severity, detail)

    # 1. URL-quality checks (synchronous, no fetch)
    for p in pages or []:
        u = (p.get("url") or "").strip()
        if not u:
            continue
        for check, detail in _check_url_quality(u):
            findings.append((u, check, "low", detail))

    # 2. Anchor-quality checks
    for src, check, detail in _check_anchor_quality(links):
        findings.append((src, check, "low", detail))

    # 3. Hreflang full audit (v1.8 — return tag + self-ref + x-default +
    #     invalid codes + to-noindex + to-broken + conflicts with html lang)
    for src, check, detail in _check_hreflang_full(pages or []):
        findings.append((src, check, "medium", detail))

    # 4. Canonical chain + canonical-target health (v1.8 added)
    for src, check, detail in _check_canonical_chains(pages or []):
        findings.append((src, check, "medium", detail))

    # 5. Sitemap cross-checks (sitemap fetch + robots.txt fetch + v1.8 spec limits)
    try:
        for src, check, detail in _check_sitemap_crosschecks(pages or [], base_url):
            findings.append((src, check, "high", detail))
    except Exception:
        pass

    # 5b. Internal nofollow patterns + anchor-image (v1.8 — graph-only, no fetch)
    for src, check, detail in _check_nofollow_patterns(links, pages or []):
        findings.append((src, check, "medium", detail))
    for src, check, detail in _check_anchor_image_no_alt(links):
        findings.append((src, check, "low", detail))

    # 5c. Crawl-trap detection (v1.8 — URL-pattern-only, no fetch)
    for src, check, detail in _check_crawl_traps(pages or []):
        findings.append((src, check, "high", detail))

    # 5d. Localhost / private-IP outlinks (v1.8 — dev leak)
    for src, check, detail in _check_localhost_outlinks(links):
        findings.append((src, check, "high", detail))

    # 6. Response-level checks (security headers, soft-404, mixed content)
    candidates = []
    for p in pages or []:
        u = (p.get("url") or "").strip()
        if not u or not _is_http(u):
            continue
        sc = str(p.get("status_code", ""))
        if sc and not sc.startswith("2"):
            continue
        candidates.append(u)

    cap_applied = len(candidates) > limit
    fetch_urls = candidates[:limit]

    if fetch_urls:
        loop = asyncio.new_event_loop()
        try:
            results = loop.run_until_complete(
                _fetch_all(fetch_urls, max_workers, timeout_seconds)
            )
        finally:
            loop.close()

        for url, resp, err in results:
            if err or resp is None:
                continue
            status = resp.status_code
            headers = {k.lower(): v for k, v in resp.headers.items()}
            body = ""
            try:
                body = resp.text
            except Exception:
                body = ""
            for check, detail in _check_from_response(url, resp, status, headers, body):
                # Severity tiering. high = audit data suspect or page broken;
                # medium = real-issue surface; low = stylistic / count-based.
                if check in ("soft_404", "mixed_content",
                             "bot_block_challenge_detected",
                             "broken_or_invalid_html", "html_over_2mb"):
                    sev = "high"
                elif check in ("meta_refresh_redirect", "js_redirect",
                               "http_refresh_redirect", "broken_bookmarks",
                               "canonical_outside_head", "noscript_in_head",
                               "dom_size_excessive", "image_dimensions_missing",
                               "iframe_missing_title"):
                    sev = "medium"
                else:
                    sev = "low"
                findings.append((url, check, sev, detail))

    # Write CSV
    by_check = defaultdict(int)
    top_urls = defaultdict(int)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["url", "check_name", "severity", "finding_detail"])
        for url, check, sev, detail in findings:
            w.writerow([url, check, sev, detail])
            by_check[check] += 1
            top_urls[url] += 1

    top_urls_list = sorted(top_urls.items(), key=lambda x: -x[1])[:20]
    top_urls_brief = [{"url": u, "findings": n} for u, n in top_urls_list]

    return {
        "path":         str(output_path),
        "findings":     len(findings),
        "by_check":     dict(by_check),
        "top_urls":     top_urls_brief,
        "cap_applied":  cap_applied,
        "cap_limit":    limit,
    }
