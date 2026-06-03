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


# ── Canonical chain depth ─────────────────────────────────────────────────────

def _check_canonical_chains(pages: list) -> list:
    """Flag pages whose canonical points to another page that itself has a
    different canonical (chain depth > 1)."""
    canon_map = {}  # url -> canonical_url (both normalised)
    for p in pages or []:
        u = _normalise_for_match(p.get("url") or "")
        c = _normalise_for_match(p.get("canonical_url") or "")
        if u and c and u != c:
            canon_map[u] = c

    out = []
    for src, c1 in canon_map.items():
        # Look up one hop
        c2 = canon_map.get(c1)
        if c2 and c2 != c1:
            out.append((src, "canonical_chain_depth",
                        f"Canonical -> {c1} -> {c2} (depth > 1)"))
    return out


# ── Sitemap cross-checks (fetch sitemap, intersect with crawl) ───────────────

def _fetch_sitemap_urls(sitemap_url: str, timeout_s: float = 10.0) -> list:
    """Best-effort fetch of sitemap.xml. Returns list of URLs found."""
    try:
        r = httpx.get(sitemap_url, timeout=timeout_s, follow_redirects=True,
                      headers={"User-Agent": "LibreCrawl-MCP/1.5"})
        if r.status_code >= 400:
            return []
        root = ET.fromstring(r.content)
    except Exception:
        return []

    # Strip XML namespace - sitemap.xml uses sitemaps.org/schemas/sitemap/0.9
    def _local(tag):
        return tag.split("}", 1)[-1] if "}" in tag else tag

    urls = []
    # urlset > url > loc
    for el in root.iter():
        if _local(el.tag) == "loc":
            urls.append((el.text or "").strip())
    return urls


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
    """Cross-check sitemap URLs against the crawl's known statuses + noindex."""
    sitemap_url = f"{base_url.rstrip('/')}/sitemap.xml"
    sitemap_urls = _fetch_sitemap_urls(sitemap_url)
    if not sitemap_urls:
        return []

    # Map crawled URLs to their pages
    page_by_url = {_normalise_for_match(p.get("url") or ""): p for p in (pages or [])}
    robots_disallow = _fetch_robots_disallow(base_url)

    out = []
    sitemap_norm = [_normalise_for_match(u) for u in sitemap_urls]

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

    return findings


# ── Public entry point ────────────────────────────────────────────────────────

def run_extended_checks(pages: list, base_url: str, output_path: Path,
                         links: list | None = None,
                         limit: int = 50,
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

    # 3. Hreflang return-tag check (graph-only, no fetch)
    for src, check, detail in _check_hreflang_returns(pages or []):
        findings.append((src, check, "medium", detail))

    # 4. Canonical chain check (graph-only, no fetch)
    for src, check, detail in _check_canonical_chains(pages or []):
        findings.append((src, check, "medium", detail))

    # 5. Sitemap cross-checks (sitemap fetch + robots.txt fetch)
    try:
        for src, check, detail in _check_sitemap_crosschecks(pages or [], base_url):
            findings.append((src, check, "high", detail))
    except Exception:
        pass

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
                sev = "high" if check == "soft_404" else "medium"
                if check == "mixed_content":
                    sev = "high"
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
