"""
Sitemap-orphan filler (librecrawl-mcp v1.6).

LibreCrawl's upstream Flask crawler accepts only a single seed URL and
traverses internal links up to maxDepth. Sitemap URLs that are NOT reachable
via internal links from the seed (true orphans), or that sit deeper than
maxDepth from the seed, are never crawled — so a site with a 102-URL
sitemap may finish with only 12 pages in the report.

This module closes that coverage gap WITHOUT modifying LibreCrawl upstream:
after the main crawl finishes, we look at `sitemap_only` URLs from the
sitemap reconciliation step, run a concurrent lightweight HTTP fetch on
each, parse the same SEO fields the existing checks_manifest needs
(title, meta description, H1, canonical, robots, viewport, og tags,
images-with-alt, json-ld, word count, status code, response time), and
append the resulting page dicts to the `pages` list. The downstream
report engine + per-page CSV + extended checks + content audit then
treat sitemap-orphans as first-class crawled pages.

This is NOT a full Screaming-Frog parity replacement for LibreCrawl —
we don't capture inbound/outbound link graphs, deep DOM analysis, or
JS-render data on these pages. The flagged fields are tagged with
source="sitemap_fill" so downstream tools can opt-in/out as needed.
"""

import asyncio
import re
import time
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx


USER_AGENT = "LibreCrawl-MCP/1.6 (Sitemap-Fill; +https://github.com/adityaarsharma/librecrawl-mcp)"


class _SEOExtractor(HTMLParser):
    """Single-pass HTML scraper for the SEO fields LibreCrawl exports.

    Avoids BeautifulSoup so the venv install footprint stays small.
    Captures: title, meta(name=description|robots|viewport), <html lang>,
    <link rel=canonical>, og:* + twitter:*, h1/h2/h3 text, images-with-alt,
    JSON-LD script blocks, total word count.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta_description = ""
        self.canonical_url = ""
        self.robots = ""
        self.viewport = ""
        self.lang = ""
        self.charset = ""
        self.og_tags = {}
        self.twitter_tags = {}
        self.hreflang = []
        self.h1 = ""
        self.h2 = []
        self.h3 = []
        self.images = []
        self.json_ld = []
        self.word_count = 0
        self._in_title = False
        self._in_h = None              # "h1"/"h2"/"h3"
        self._h_buf = []
        self._in_script_jsonld = False
        self._script_buf = []
        self._body_text_buf = []
        self._in_head = False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "html":
            self.lang = a.get("lang", "")
        elif tag == "head":
            self._in_head = True
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = (a.get("name") or "").lower()
            prop = (a.get("property") or "").lower()
            content = a.get("content", "")
            if name == "description":
                self.meta_description = content
            elif name == "robots":
                self.robots = content
            elif name == "viewport":
                self.viewport = content
            elif name == "charset":
                self.charset = content
            elif a.get("charset"):
                self.charset = a.get("charset")
            elif a.get("http-equiv", "").lower() == "content-type":
                m = re.search(r"charset=([^;]+)", content, re.I)
                if m:
                    self.charset = m.group(1).strip()
            elif prop.startswith("og:"):
                self.og_tags[prop] = content
            elif name.startswith("twitter:"):
                self.twitter_tags[name] = content
        elif tag == "link":
            rel = (a.get("rel") or "").lower()
            href = a.get("href", "")
            if rel == "canonical":
                self.canonical_url = href
            elif rel == "alternate" and a.get("hreflang"):
                self.hreflang.append({"hreflang": a["hreflang"], "href": href})
        elif tag in ("h1", "h2", "h3"):
            self._in_h = tag
            self._h_buf = []
        elif tag == "img":
            self.images.append({
                "src": a.get("src", "") or a.get("data-src", ""),
                "alt": a.get("alt", ""),
            })
        elif tag == "script":
            if (a.get("type") or "").lower() == "application/ld+json":
                self._in_script_jsonld = True
                self._script_buf = []

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "head":
            self._in_head = False
        elif tag in ("h1", "h2", "h3") and self._in_h == tag:
            text = "".join(self._h_buf).strip()
            if tag == "h1" and not self.h1:
                self.h1 = text[:300]
            elif tag == "h2":
                self.h2.append(text[:300])
            elif tag == "h3":
                self.h3.append(text[:300])
            self._in_h = None
            self._h_buf = []
        elif tag == "script" and self._in_script_jsonld:
            self._in_script_jsonld = False
            raw = "".join(self._script_buf).strip()
            if raw:
                self.json_ld.append(raw)

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif self._in_h:
            self._h_buf.append(data)
        elif self._in_script_jsonld:
            self._script_buf.append(data)
        elif not self._in_head:
            self._body_text_buf.append(data)

    def finalize(self):
        text = " ".join(self._body_text_buf)
        self.word_count = len(re.findall(r"\b\w+\b", text))
        self.title = (self.title or "").strip()


async def _fetch_one(url: str, client: httpx.AsyncClient,
                     timeout_s: float) -> dict:
    """Build a LibreCrawl-export-shaped page dict from a single HTTP fetch."""
    started = time.monotonic()
    # Defaults — downstream report/manifest code does `int > N` comparisons
    # on depth / response_time_ms / size / word_count / internal_links /
    # external_links and `or 0` doesn't catch every None comparison path.
    # Initialise int fields to 0, not None, so a fetch failure still
    # produces a comparable page row.
    page = {
        "url":              url,
        "status_code":      0,
        "title":            "",
        "meta_description": "",
        "h1":               "",
        "word_count":       0,
        "canonical_url":    "",
        "depth":            0,        # sitemap-orphans treated as depth-0
        "response_time_ms": 0,
        "h2":               [],
        "h3":               [],
        "internal_links":   0,
        "external_links":   0,
        "linked_from":      [],
        "images":           [],
        "broken_images":    [],
        "robots":           "",
        "lang":             "",
        "charset":          "",
        "viewport":         "",
        "size":             0,
        "redirects":        [],
        "error_type":       None,    # only set if the fetch failed
        "og_tags":          {},
        "twitter_tags":     {},
        "json_ld":          [],
        "hreflang":         [],
        "analytics":        {},
        "source":           "sitemap_fill",
    }

    try:
        r = await client.get(
            url, timeout=timeout_s, follow_redirects=True,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.5",
                "Accept-Language": "en",
            },
        )
        page["status_code"] = r.status_code
        page["response_time_ms"] = int((time.monotonic() - started) * 1000)
        page["size"] = len(r.content or b"")
        page["redirects"] = [str(h.url) for h in r.history]

        if r.history and str(r.url) != url:
            page["url"] = str(r.url)  # final URL after redirect

        content_type = r.headers.get("content-type", "").lower()
        if "html" not in content_type:
            page["error_type"] = "non_html_content"
            return page

        # Parse HTML
        extractor = _SEOExtractor()
        try:
            extractor.feed(r.text)
            extractor.close()
        except Exception:
            pass  # tolerate malformed HTML
        extractor.finalize()

        page["title"]            = extractor.title
        page["meta_description"] = extractor.meta_description
        page["h1"]                = extractor.h1
        page["h2"]                = extractor.h2[:20]
        page["h3"]                = extractor.h3[:20]
        page["canonical_url"]     = extractor.canonical_url
        page["robots"]            = extractor.robots
        page["viewport"]          = extractor.viewport
        page["lang"]              = extractor.lang
        page["charset"]           = extractor.charset
        page["og_tags"]           = extractor.og_tags
        page["twitter_tags"]      = extractor.twitter_tags
        page["json_ld"]           = extractor.json_ld
        page["hreflang"]          = extractor.hreflang
        page["images"]            = extractor.images[:200]
        page["word_count"]        = extractor.word_count

    except httpx.ReadTimeout:
        page["error_type"] = "timeout"
    except httpx.ConnectTimeout:
        page["error_type"] = "timeout"
    except httpx.ConnectError as e:
        msg = str(e).lower()
        if "name or service" in msg or "name resolution" in msg:
            page["error_type"] = "dns_error"
        else:
            page["error_type"] = "connect_error"
    except httpx.UnsupportedProtocol:
        page["error_type"] = "malformed_url"
    except httpx.TransportError as e:
        page["error_type"] = "ssl_error" if "ssl" in str(e).lower() else "transport_error"
    except Exception as e:
        page["error_type"] = f"error_{type(e).__name__}"

    return page


async def _fill_async(urls: list, max_workers: int,
                       timeout_s: float) -> list:
    sem = asyncio.Semaphore(max_workers)
    async with httpx.AsyncClient(http2=False, verify=True) as client:
        async def _bounded(u):
            async with sem:
                return await _fetch_one(u, client, timeout_s)
        return await asyncio.gather(*(_bounded(u) for u in urls),
                                    return_exceptions=False)


def fill_sitemap_orphans(missed_urls: list,
                          max_workers: int = 10,
                          timeout_seconds: float = 8.0,
                          cap: int = 500) -> dict:
    """Synchronous entry — drives the async pool internally.

    Args:
        missed_urls:  recon.sitemap_only URL list.
        max_workers:  concurrent fetches (default 10).
        timeout_seconds: per-request timeout (default 8s).
        cap:          hard ceiling on URLs to fetch (default 500). Avoids
                      runaway cost on huge sitemaps. Set higher only when
                      you actively want coverage of all sitemap entries.

    Returns:
        pages_added: list of page dicts (LibreCrawl-export-shaped)
        attempted:   how many URLs we actually fetched
        cap_hit:     True if missed > cap
        success_count: 2xx responses
        broken_count:  4xx + 5xx + error_type set
    """
    if not missed_urls:
        return {"pages_added": [], "attempted": 0, "cap_hit": False,
                "success_count": 0, "broken_count": 0}

    cap_hit = len(missed_urls) > cap
    targets = missed_urls[:cap]

    loop = asyncio.new_event_loop()
    try:
        pages = loop.run_until_complete(
            _fill_async(targets, max_workers, timeout_seconds)
        )
    finally:
        loop.close()

    success = sum(1 for p in pages
                  if str(p.get("status_code", "")).startswith("2"))
    broken = sum(1 for p in pages
                 if p.get("error_type") or
                 str(p.get("status_code", "")).startswith(("4", "5")))

    return {
        "pages_added":    pages,
        "attempted":      len(targets),
        "cap_hit":        cap_hit,
        "missed_total":   len(missed_urls),
        "success_count":  success,
        "broken_count":   broken,
    }
