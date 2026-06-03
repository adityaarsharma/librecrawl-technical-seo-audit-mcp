"""
Content quality audit module (librecrawl-mcp v1.5).

Operates on a finished crawl's page list. LibreCrawl's per-page export does
NOT include full body text — only title, meta description, headings, and
word_count — so to do paragraph-level audits we must fetch each page's HTML
ourselves with a bounded concurrent pool.

Checks computed per page:
  1. Flesch reading ease         (flag < 50)
  2. Avg sentence length         (flag > 25)
  3. Passive voice ratio         (flag > 25%)
  4. Missing terminal punctuation
  5. Double-space occurrences
  6. Smart-quote mismatches
  7. Boilerplate ratio           (shingle overlap across pages; flag > 70%)
  8. AI-tell tokens              (delve/unlock/seamlessly/leverage/...
                                  flag if 3+ present OR em-dash density > 1/100 words)
  9. Lorem ipsum detection

Designed to be heavy-cap-aware: by default only fetches first 50 pages per
audit to stay polite to the target. Caller can override via `limit`.

Public API:
    audit_content(pages, output_path, limit=50, ...) -> {summary dict}

Output CSV columns:
    url, status, word_count, flesch_score, avg_sentence_length,
    passive_voice_pct, missing_terminal_punctuation_count,
    double_space_count, smart_quote_mismatch_count, boilerplate_ratio,
    ai_tell_tokens_found, em_dash_density_per_100w, has_lorem_ipsum,
    failed_checks_count, failed_checks_list
"""

from __future__ import annotations

import asyncio
import csv
import re
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

import httpx


# ── Constants ─────────────────────────────────────────────────────────────────

AI_TELL_TOKENS = (
    "delve", "unlock", "seamlessly", "leverage", "unleash", "tapestry",
    "navigate the", "in today's fast-paced", "in the realm of",
    "it is important to note", "intricate", "elevate", "embark",
)
AI_TELL_THRESHOLD = 3  # flag if >= this many tokens found
EM_DASH_DENSITY_LIMIT = 1.0  # em dashes per 100 words

FLESCH_FLAG_BELOW         = 50
AVG_SENTENCE_LEN_FLAG_GT  = 25
PASSIVE_VOICE_FLAG_PCT    = 25.0
BOILERPLATE_FLAG_RATIO    = 0.70

# Past participle suffix shortcut for passive-voice heuristic. Not perfect, but
# good enough for a flag: be/been/being/was/were/is/are/am + word ending in
# -ed/-en/-t (with irregular allowances handled by a small allow-list below).
PASSIVE_AUX = {"is", "are", "was", "were", "be", "been", "being", "am"}
IRREGULAR_PP = {
    "done", "made", "seen", "given", "taken", "shown", "broken", "chosen",
    "spoken", "written", "stolen", "frozen", "found", "kept", "left",
    "lost", "sent", "set", "put", "cut", "shut", "built", "burnt", "held",
    "told", "sold", "felt", "meant", "thought", "brought", "caught", "taught",
    "bought", "fought", "sought", "got", "gotten", "had", "led", "read",
    "said", "paid", "laid",
}

LOREM_RE = re.compile(r"\blorem\s+ipsum\b", re.IGNORECASE)
SMART_OPEN  = "“"  # "
SMART_CLOSE = "”"  # "
EM_DASH     = "—"  # —

SHINGLE_SIZE = 5  # words per shingle for boilerplate detection
BOILERPLATE_MIN_OTHER_PAGES = 3  # shingle appears on this many OTHER pages

SKIP_SCHEMES_FOR_FETCH = {"mailto", "tel", "sms", "javascript", "data", "ftp"}


# ── Text extraction ───────────────────────────────────────────────────────────

def _strip_html_to_text(html_str: str) -> str:
    """Strip HTML to plain visible text. Removes script/style/nav/header/footer."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        # Fallback - crude regex strip
        s = re.sub(r"<script[^>]*>.*?</script>", " ", html_str,
                   flags=re.IGNORECASE | re.DOTALL)
        s = re.sub(r"<style[^>]*>.*?</style>", " ", s,
                   flags=re.IGNORECASE | re.DOTALL)
        s = re.sub(r"<[^>]+>", " ", s)
        return re.sub(r"\s+", " ", s).strip()

    soup = BeautifulSoup(html_str or "", "lxml")
    for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
        tag.decompose()

    # Try to isolate main content area - falls back to body
    main = (soup.find("main") or soup.find(attrs={"role": "main"}) or
            soup.find("article") or soup.body or soup)
    text = main.get_text(separator=" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


# ── Linguistic primitives ─────────────────────────────────────────────────────

_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")
_WORD_RE       = re.compile(r"\b\w+\b")
_VOWEL_GROUP_RE = re.compile(r"[aeiouy]+", re.IGNORECASE)


def _count_syllables(word: str) -> int:
    """Cheap heuristic - count vowel groups, min 1."""
    word = word.lower().strip("\"'.,;:!?()[]{}")
    if not word:
        return 0
    # Drop a trailing silent 'e' (but never the only vowel)
    if word.endswith("e") and len(word) > 2 and word[-2] not in "aeiou":
        word = word[:-1]
    count = len(_VOWEL_GROUP_RE.findall(word))
    return max(1, count)


def _flesch_reading_ease(text: str) -> float:
    """Standard Flesch formula. Returns 0.0 if not enough data to compute."""
    sentences = [s for s in _SENT_SPLIT_RE.split(text) if s.strip()]
    if not sentences:
        sentences = [text] if text.strip() else []
    if not sentences:
        return 0.0
    words = _WORD_RE.findall(text)
    if not words:
        return 0.0
    syllables = sum(_count_syllables(w) for w in words)
    sc = max(1, len(sentences))
    wc = max(1, len(words))
    return round(
        206.835 - 1.015 * (wc / sc) - 84.6 * (syllables / wc),
        1,
    )


def _avg_sentence_length(text: str) -> float:
    sentences = [s for s in _SENT_SPLIT_RE.split(text) if s.strip()]
    if not sentences:
        return 0.0
    word_counts = [len(_WORD_RE.findall(s)) for s in sentences]
    if not word_counts:
        return 0.0
    return round(sum(word_counts) / len(word_counts), 1)


def _passive_voice_pct(text: str) -> float:
    """Cheap heuristic: count sentences with passive-voice signature."""
    sentences = [s for s in _SENT_SPLIT_RE.split(text) if s.strip()]
    if not sentences:
        return 0.0
    passive = 0
    for s in sentences:
        tokens = [t.lower().strip("\"'.,;:!?()[]{}") for t in s.split()]
        for i, t in enumerate(tokens[:-1]):
            if t in PASSIVE_AUX:
                nxt = tokens[i + 1]
                if (nxt.endswith("ed") and len(nxt) > 3) or nxt in IRREGULAR_PP:
                    passive += 1
                    break
    return round((passive / len(sentences)) * 100, 1)


def _missing_terminal_punctuation(text: str) -> int:
    """Count sentence-like segments lacking a terminal . ! or ?."""
    # Split on newlines/double-newlines as a paragraph proxy, fall back to
    # last-12-words rule
    if not text:
        return 0
    bad = 0
    # Identify "sentence chunks" via length-based segmentation
    # A reliable check: count chunks between sentence delimiters that
    # end in a non-terminal character followed by EOF or paragraph break.
    chunks = re.split(r"[\r\n]+", text)
    for chunk in chunks:
        chunk = chunk.strip()
        if len(chunk) < 30:  # skip headings/labels
            continue
        if chunk[-1] not in ".!?":
            bad += 1
    return bad


def _double_spaces(text: str) -> int:
    return len(re.findall(r"  +", text))


def _smart_quote_mismatches(text: str) -> int:
    """Smart open " without smart close, or vice versa."""
    opens  = text.count(SMART_OPEN)
    closes = text.count(SMART_CLOSE)
    return abs(opens - closes)


def _em_dash_density(text: str) -> float:
    words = _WORD_RE.findall(text)
    if not words:
        return 0.0
    ed = text.count(EM_DASH)
    return round((ed / max(1, len(words))) * 100, 2)


def _ai_tells_in(text: str) -> list:
    text_lower = text.lower()
    found = [t for t in AI_TELL_TOKENS if t in text_lower]
    return found


def _has_lorem_ipsum(text: str) -> bool:
    return bool(LOREM_RE.search(text or ""))


# ── Boilerplate detection ─────────────────────────────────────────────────────

def _make_shingles(text: str, size: int = SHINGLE_SIZE) -> set:
    words = [w.lower() for w in _WORD_RE.findall(text)]
    if len(words) < size:
        return set()
    return {tuple(words[i:i + size]) for i in range(len(words) - size + 1)}


def _compute_boilerplate_ratios(texts_by_url: dict) -> dict:
    """For each URL, compute the fraction of its shingles that appear on
    BOILERPLATE_MIN_OTHER_PAGES or more OTHER pages."""
    if not texts_by_url:
        return {}

    # Build shingle -> set of urls map
    url_shingles = {url: _make_shingles(t) for url, t in texts_by_url.items()}
    shingle_to_urls = defaultdict(set)
    for url, shingles in url_shingles.items():
        for sh in shingles:
            shingle_to_urls[sh].add(url)

    ratios = {}
    for url, shingles in url_shingles.items():
        if not shingles:
            ratios[url] = 0.0
            continue
        boiler_count = 0
        for sh in shingles:
            others = shingle_to_urls[sh] - {url}
            if len(others) >= BOILERPLATE_MIN_OTHER_PAGES:
                boiler_count += 1
        ratios[url] = round(boiler_count / len(shingles), 3)
    return ratios


# ── HTTP fetch (concurrent pool) ──────────────────────────────────────────────

async def _fetch_one(url: str, client: httpx.AsyncClient,
                     timeout_s: float) -> tuple:
    """Returns (url, html_or_None, error_or_None)."""
    try:
        r = await client.get(url, timeout=timeout_s, follow_redirects=True,
                              headers={
                                  "User-Agent": "LibreCrawl-MCP/1.5 (Content Audit; +https://github.com/adityaarsharma/librecrawl-mcp)",
                                  "Accept": "text/html,*/*;q=0.5",
                              })
        if r.status_code >= 400:
            return url, None, f"http_{r.status_code}"
        ct = r.headers.get("content-type", "").lower()
        if ct and "html" not in ct and "xml" not in ct:
            return url, None, f"non_html_{ct.split(';')[0]}"
        return url, r.text, None
    except httpx.ReadTimeout:
        return url, None, "timeout"
    except httpx.ConnectError:
        return url, None, "connect_error"
    except httpx.UnsupportedProtocol:
        return url, None, "unsupported_protocol"
    except Exception as e:
        return url, None, f"error: {type(e).__name__}"


async def _fetch_all(urls: list, max_workers: int, timeout_s: float) -> list:
    sem = asyncio.Semaphore(max_workers)
    async with httpx.AsyncClient(http2=False, verify=True) as client:
        async def _bounded(u):
            async with sem:
                return await _fetch_one(u, client, timeout_s)
        return await asyncio.gather(*(_bounded(u) for u in urls))


# ── Public entry point ───────────────────────────────────────────────────────

def audit_content(pages: list, output_path: Path, limit: int = 50,
                   max_workers: int = 5,
                   timeout_seconds: float = 8.0) -> dict:
    """
    Run paragraph-level content checks across the first `limit` pages of a
    crawl. Writes <output_path>.csv and returns a summary.

    Args:
        pages:           Per-page export rows from server._parse_export.
        output_path:     Where to write the content-audit CSV.
        limit:           Max pages to fetch+audit. Default 50.
        max_workers:     Concurrent HTTP fetches. Default 5.
        timeout_seconds: Per-request timeout. Default 8s.

    Returns: {
        path:              str,
        pages_audited:     int,
        pages_skipped:     int,
        cap_applied:       bool,
        by_check:          { check_name: count_of_pages_failing },
        top_issues:        [ {url, failed_checks} ... top 20 ],
        avg_flesch:        float,
    }
    """
    output_path = Path(output_path)

    # Filter to HTTP-200 HTML pages with a URL
    candidates = []
    for p in pages or []:
        url = (p.get("url") or "").strip()
        if not url:
            continue
        scheme = urlparse(url).scheme.lower()
        if scheme not in ("http", "https"):
            continue
        sc = str(p.get("status_code", ""))
        if sc and not sc.startswith("2"):
            continue
        candidates.append(p)

    cap_applied = len(candidates) > limit
    target_pages = candidates[:limit]
    target_urls = [p.get("url") for p in target_pages]

    # Fetch all pages concurrently
    fetch_results = []
    if target_urls:
        loop = asyncio.new_event_loop()
        try:
            fetch_results = loop.run_until_complete(
                _fetch_all(target_urls, max_workers, timeout_seconds)
            )
        finally:
            loop.close()

    # Build text-by-url map for boilerplate detection
    texts_by_url = {}
    errors_by_url = {}
    for url, html_str, err in fetch_results:
        if err:
            errors_by_url[url] = err
            continue
        text = _strip_html_to_text(html_str or "")
        if text:
            texts_by_url[url] = text

    boiler_ratios = _compute_boilerplate_ratios(texts_by_url)

    # Compute per-page metrics
    rows = []
    by_check = defaultdict(int)
    flesch_sum = 0.0
    flesch_n = 0
    page_meta = {p.get("url"): p for p in target_pages}

    for url in target_urls:
        page = page_meta.get(url, {})
        text = texts_by_url.get(url, "")
        err = errors_by_url.get(url, "")
        word_count = len(_WORD_RE.findall(text)) if text else (page.get("word_count") or 0)

        if not text:
            row = {
                "url": url,
                "status": f"fetch_failed: {err}" if err else "no_text_extracted",
                "word_count": word_count,
                "flesch_score": "",
                "avg_sentence_length": "",
                "passive_voice_pct": "",
                "missing_terminal_punctuation_count": "",
                "double_space_count": "",
                "smart_quote_mismatch_count": "",
                "boilerplate_ratio": "",
                "ai_tell_tokens_found": "",
                "em_dash_density_per_100w": "",
                "has_lorem_ipsum": "",
                "failed_checks_count": 0,
                "failed_checks_list": "",
            }
            rows.append(row)
            continue

        flesch = _flesch_reading_ease(text)
        asl    = _avg_sentence_length(text)
        passv  = _passive_voice_pct(text)
        mtp    = _missing_terminal_punctuation(text)
        ds     = _double_spaces(text)
        sqm    = _smart_quote_mismatches(text)
        br     = boiler_ratios.get(url, 0.0)
        ai_t   = _ai_tells_in(text)
        em_d   = _em_dash_density(text)
        lorem  = _has_lorem_ipsum(text)

        flesch_sum += flesch
        flesch_n += 1

        failed = []
        if flesch and flesch < FLESCH_FLAG_BELOW:
            failed.append("low_readability")
            by_check["low_readability"] += 1
        if asl and asl > AVG_SENTENCE_LEN_FLAG_GT:
            failed.append("long_sentences")
            by_check["long_sentences"] += 1
        if passv and passv > PASSIVE_VOICE_FLAG_PCT:
            failed.append("excess_passive_voice")
            by_check["excess_passive_voice"] += 1
        if mtp > 0:
            failed.append("missing_terminal_punctuation")
            by_check["missing_terminal_punctuation"] += 1
        if ds > 5:
            failed.append("double_spaces")
            by_check["double_spaces"] += 1
        if sqm > 0:
            failed.append("smart_quote_mismatch")
            by_check["smart_quote_mismatch"] += 1
        if br > BOILERPLATE_FLAG_RATIO:
            failed.append("high_boilerplate_ratio")
            by_check["high_boilerplate_ratio"] += 1
        if len(ai_t) >= AI_TELL_THRESHOLD or em_d > EM_DASH_DENSITY_LIMIT:
            failed.append("ai_writing_signals")
            by_check["ai_writing_signals"] += 1
        if lorem:
            failed.append("lorem_ipsum_detected")
            by_check["lorem_ipsum_detected"] += 1

        rows.append({
            "url": url,
            "status": "ok",
            "word_count": word_count,
            "flesch_score": flesch,
            "avg_sentence_length": asl,
            "passive_voice_pct": passv,
            "missing_terminal_punctuation_count": mtp,
            "double_space_count": ds,
            "smart_quote_mismatch_count": sqm,
            "boilerplate_ratio": br,
            "ai_tell_tokens_found": ";".join(ai_t),
            "em_dash_density_per_100w": em_d,
            "has_lorem_ipsum": 1 if lorem else 0,
            "failed_checks_count": len(failed),
            "failed_checks_list": ";".join(failed),
        })

    # Write CSV
    columns = [
        "url", "status", "word_count", "flesch_score", "avg_sentence_length",
        "passive_voice_pct", "missing_terminal_punctuation_count",
        "double_space_count", "smart_quote_mismatch_count",
        "boilerplate_ratio", "ai_tell_tokens_found",
        "em_dash_density_per_100w", "has_lorem_ipsum",
        "failed_checks_count", "failed_checks_list",
    ]
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    top_issues = sorted(
        [r for r in rows if r["failed_checks_count"] > 0],
        key=lambda r: -r["failed_checks_count"],
    )[:20]
    top_issues_brief = [
        {"url": r["url"], "failed_checks": r["failed_checks_list"]}
        for r in top_issues
    ]

    return {
        "path":          str(output_path),
        "pages_audited": flesch_n,
        "pages_skipped": len(rows) - flesch_n,
        "cap_applied":   cap_applied,
        "cap_limit":     limit,
        "candidates":    len(candidates),
        "by_check":      dict(by_check),
        "top_issues":    top_issues_brief,
        "avg_flesch":    round(flesch_sum / flesch_n, 1) if flesch_n else 0.0,
    }
