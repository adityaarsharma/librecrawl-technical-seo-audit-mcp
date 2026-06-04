"""
External link validator — closes the gap LibreCrawl's upstream leaves open.

LibreCrawl's export captures every outbound link on every page in `links_detailed`,
but the upstream doesn't actually fetch each external URL to verify the
target's HTTP status. The exported `target_status` field is null for external
links — so a Screaming-Frog "External" tab equivalent was missing.

This module fetches every unique external target with a concurrent HEAD pool
(GET fallback for HEAD-blocked servers), classifies the result into
SF-compatible status classes, and emits `<domain>-<ts>.external-links.csv`
as a sidecar artifact alongside the .md report.

USE FROM:
  - librecrawl_external_links_audit(crawl_id) MCP tool (re-run on past crawls)
  - runner._finalize_session  (auto-emit alongside per-page + sitemap CSVs)
"""

import asyncio
import csv
import time
from urllib.parse import urlparse, urljoin
from collections import defaultdict

import httpx


# URL schemes we will not validate over HTTP (not a defect, just not a webhit)
SKIP_SCHEMES = {"mailto", "tel", "sms", "javascript", "data", "ftp", "magnet"}


def _classify_status(status_code, error, redirect_count):
    """SF-compatible status_class. Single source of truth across CSV + summary."""
    if error:
        return error  # "timeout" / "dns_error" / "ssl_error" / "connection_refused" / "malformed_url"
    if status_code is None:
        return "no_response"
    if 200 <= status_code < 300:
        return "ok_after_redirect" if redirect_count > 0 else "ok"
    if 300 <= status_code < 400:
        return "redirect"  # only seen if follow_redirects=False, which we don't use
    if status_code == 403:
        return "forbidden"
    if status_code == 404:
        return "not_found"
    if status_code == 410:
        return "gone"
    if 400 <= status_code < 500:
        return "client_error_4xx"
    if 500 <= status_code < 600:
        return "server_error_5xx"
    return "unknown"


def _normalise_url(url: str) -> str:
    """Strip fragment + trailing whitespace. Don't lowercase the path."""
    if not url:
        return ""
    url = url.strip()
    if "#" in url:
        url = url.split("#", 1)[0]
    return url


def _extract_external_targets(pages: list, base_url: str, links: list | None = None) -> tuple:
    """
    Build the external-target → source-pages map from LibreCrawl's export.

    LibreCrawl ships outbound URLs in a SEPARATE flat list when `links_detailed`
    is in EXPORT_FIELDS — server.py's `_parse_export` returns it as the second
    tuple element. Per-link shape (from main.py generate_links_json_export):
      {source_url, target_url, anchor_text, is_internal, target_domain,
       target_status, placement}

    `is_internal=False` flags exactly the URLs we need to HEAD/GET — internal
    links already get status codes from LibreCrawl's crawl loop.

    Fallback: if no flat `links` list is provided (older LibreCrawl version
    or single-file export), we walk `pages[*].links_detailed` as a best-effort.

    Returns (validate_map, skip_map).
    """
    base_host = (urlparse(base_url).hostname or "").lower()
    validate_map = defaultdict(list)
    skip_map = {}

    def _record(target_raw: str, source: str, anchor: str, position: str) -> None:
        target = _normalise_url(target_raw)
        if not target:
            return
        if not urlparse(target).scheme and source:
            try:
                target = urljoin(source, target)
            except Exception:
                skip_map[target or target_raw] = "malformed_url"
                return
        scheme = (urlparse(target).scheme or "").lower()
        host   = (urlparse(target).hostname or "").lower()
        if scheme in SKIP_SCHEMES:
            skip_map[target] = f"scheme_{scheme}"
            return
        if scheme not in ("http", "https"):
            skip_map[target] = "non_http_scheme"
            return
        if not host:
            skip_map[target] = "no_host"
            return
        # Skip exact base host. We deliberately KEEP subdomains here so
        # cross-property links (cdn.example, docs.example, etc.) get validated.
        if host == base_host:
            return
        validate_map[target].append({
            "source":   source,
            "anchor":   (anchor or "")[:280].strip(),
            "position": (position or "").strip(),
        })

    # Primary path — LibreCrawl's flat links list
    if links and isinstance(links, list):
        for lk in links:
            if not isinstance(lk, dict):
                continue
            # is_internal can be True/False, "Yes"/"No", 1/0 depending on
            # whether the flat file came from CSV or JSON
            internal_flag = lk.get("is_internal")
            is_internal = (internal_flag is True or internal_flag == 1
                           or (isinstance(internal_flag, str)
                               and internal_flag.lower() in ("yes", "true", "1")))
            if is_internal:
                continue
            _record(
                target_raw=lk.get("target_url") or lk.get("url") or lk.get("href") or "",
                source=lk.get("source_url") or "",
                anchor=lk.get("anchor_text") or lk.get("anchor") or "",
                position=lk.get("placement") or lk.get("position") or "",
            )
        return dict(validate_map), skip_map

    # Fallback path — per-page links_detailed (older LibreCrawl)
    for p in pages:
        source = p.get("url") or ""
        per_page = p.get("links_detailed") or []
        if not isinstance(per_page, list):
            continue
        for lk in per_page:
            if not isinstance(lk, dict):
                continue
            _record(
                target_raw=lk.get("url") or lk.get("href") or lk.get("link") or "",
                source=source,
                anchor=lk.get("anchor") or lk.get("text") or lk.get("label") or "",
                position=lk.get("position") or lk.get("placement") or "",
            )

    return dict(validate_map), skip_map


async def _validate_one(target: str, client: httpx.AsyncClient,
                         timeout_s: float) -> dict:
    """HEAD → GET fallback. Returns the row dict for CSV/summary."""
    started = time.monotonic()
    error = None
    status = None
    final_url = target
    redirect_count = 0
    content_type = None
    server = None
    last_modified = None

    async def _do(method: str) -> httpx.Response | None:
        return await client.request(
            method, target, timeout=timeout_s, follow_redirects=True,
            headers={
                "User-Agent": "LibreCrawl-MCP/1.4 (External Link Validator; +https://github.com/adityaarsharma/librecrawl-mcp)",
                "Accept": "text/html,*/*;q=0.5",
            },
        )

    try:
        # HEAD first
        try:
            r = await _do("HEAD")
        except httpx.ReadTimeout:
            error = "timeout"
            r = None
        except httpx.ConnectTimeout:
            error = "timeout"
            r = None
        except httpx.ConnectError as e:
            # v1.9.1 — replace the generic "connect_error" catch-all with
            # specific subtypes that match the documented status class set
            # so the CSV / report taxonomy stays consistent.
            msg = str(e).lower()
            if ("name or service not known" in msg or "nodename nor servname" in msg
                    or "name resolution" in msg or "no address associated" in msg
                    or "getaddrinfo failed" in msg or "temporary failure in name resolution" in msg):
                error = "dns_error"
            elif "refused" in msg:
                error = "connection_refused"
            elif "network is unreachable" in msg or "no route to host" in msg:
                error = "network_unreachable"
            elif "ssl" in msg or "certificate" in msg or "tlsv1" in msg or "handshake" in msg:
                error = "ssl_error"
            elif "timed out" in msg or "timeout" in msg:
                error = "timeout"
            else:
                # Unmatched ConnectError — record the raw classname for forensics
                # rather than dropping into a generic bucket.
                error = "connection_failed"
            r = None

        if r is None and error is None:
            r = None  # type: ignore

        # HEAD blocked or method-not-allowed → GET fallback
        if r is not None and r.status_code in (403, 405, 406, 501, 502):
            try:
                r2 = await _do("GET")
                if r2.status_code < r.status_code:  # GET succeeded
                    r = r2
            except Exception:
                pass

        if r is not None:
            status = r.status_code
            final_url = str(r.url)
            redirect_count = len(r.history)
            content_type = r.headers.get("content-type", "").split(";")[0].strip() or None
            server = r.headers.get("server", "").strip() or None
            last_modified = r.headers.get("last-modified", "").strip() or None

    except httpx.UnsupportedProtocol:
        error = "malformed_url"
    except httpx.InvalidURL:
        error = "malformed_url"
    except httpx.RemoteProtocolError:
        error = "protocol_error"
    except httpx.TransportError as e:
        msg = str(e).lower()
        if "ssl" in msg or "certificate" in msg:
            error = "ssl_error"
        else:
            error = "transport_error"
    except Exception as e:
        error = f"error: {type(e).__name__}"

    elapsed_ms = int((time.monotonic() - started) * 1000)
    status_class = _classify_status(status, error, redirect_count)

    return {
        "target_url":      target,
        "final_url":       final_url,
        "status_code":     status,
        "status_class":    status_class,
        "redirect_count":  redirect_count,
        "error_reason":    error or "",
        "content_type":    content_type or "",
        "server":          server or "",
        "last_modified":   last_modified or "",
        "response_time_ms": elapsed_ms,
    }


async def _validate_all(targets: list[str], max_workers: int,
                         timeout_s: float) -> list[dict]:
    """Bounded-concurrency validation over the unique target list."""
    sem = asyncio.Semaphore(max_workers)
    async with httpx.AsyncClient(http2=False, verify=True) as client:
        async def _bounded(t):
            async with sem:
                return await _validate_one(t, client, timeout_s)
        return await asyncio.gather(*(_bounded(t) for t in targets))


def _write_csv(rows: list[dict], source_map: dict, skip_map: dict,
               output_path) -> dict:
    """Write the external-links.csv. Returns {path, rows, validated, skipped}."""
    cols = [
        "target_url", "status_code", "status_class", "final_url",
        "redirect_count", "error_reason", "content_type",
        "response_time_ms", "server", "last_modified",
        "source_pages_count", "first_source", "first_anchor",
        "first_20_sources_pipe",
    ]
    SOURCES_TRUNCATE = 20
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for row in rows:
            sources = source_map.get(row["target_url"], [])
            first_source = sources[0]["source"] if sources else ""
            first_anchor = sources[0]["anchor"] if sources else ""
            sources_capped = "|".join(s["source"] for s in sources[:SOURCES_TRUNCATE])
            if len(sources) > SOURCES_TRUNCATE:
                sources_capped += f"|...+{len(sources)-SOURCES_TRUNCATE}_more"
            w.writerow([
                row["target_url"], row["status_code"] or "",
                row["status_class"], row["final_url"],
                row["redirect_count"], row["error_reason"],
                row["content_type"], row["response_time_ms"],
                row["server"], row["last_modified"],
                len(sources), first_source, first_anchor, sources_capped,
            ])
        # Append skipped entries with skip_reason in error_reason column
        for url, reason in skip_map.items():
            w.writerow([url, "", "skipped", "", 0, reason, "", 0, "", "", 0, "", "", ""])

    return {
        "path":      str(output_path),
        "rows":      len(rows) + len(skip_map),
        "validated": len(rows),
        "skipped":   len(skip_map),
        "columns":   len(cols),
    }


def audit_external_links(pages: list, base_url: str, output_path,
                          links: list | None = None,
                          max_workers: int = 10, timeout_seconds: float = 10.0) -> dict:
    """
    Public entry point. Synchronous — drives the async pool internally so callers
    (including the runner thread + MCP tool) don't need to manage asyncio.

    Args:
        pages:     server._parse_export's first return value (per-page export).
        base_url:  Base URL of the crawl (e.g. https://uichemy.com).
        output_path: Path for the external-links.csv sidecar.
        links:     The FLAT LINKS LIST — server._parse_export's second return.
                   When present (multi-file LibreCrawl export), this is the
                   authoritative source of outbound URLs with is_internal flags
                   already computed by LibreCrawl. If None or empty, falls
                   back to per-page links_detailed.
        max_workers: HEAD-pool concurrency. Default 10.
        timeout_seconds: Per-request timeout. Default 10s.

    Returns:
      external_links_csv:    sidecar info (path/rows/validated/skipped)
      total_external_links:  unique target URL count
      by_status_class:       count map (ok / not_found / forbidden / 5xx / ...)
      broken_count:          4xx + 5xx + dns + timeout + ssl + connection
      top_broken:            list of {target, status_class, source, anchor} for first 50 broken
      redirect_count:        unique targets that 3xx'd to a different URL
    """
    validate_map, skip_map = _extract_external_targets(pages, base_url, links=links)
    targets = list(validate_map.keys())

    if targets:
        loop = asyncio.new_event_loop()
        try:
            results = loop.run_until_complete(
                _validate_all(targets, max_workers, timeout_seconds)
            )
        finally:
            loop.close()
    else:
        results = []

    by_class = defaultdict(int)
    for r in results:
        by_class[r["status_class"]] += 1

    broken_classes = {
        "not_found", "forbidden", "gone", "client_error_4xx", "server_error_5xx",
        "timeout", "dns_error", "ssl_error", "connection_refused",
        "malformed_url", "protocol_error", "transport_error", "no_response",
    }
    broken = [r for r in results if r["status_class"] in broken_classes]

    top_broken = []
    for r in broken[:50]:
        sources = validate_map.get(r["target_url"], [])
        top_broken.append({
            "target":       r["target_url"],
            "status_code":  r["status_code"],
            "status_class": r["status_class"],
            "error_reason": r["error_reason"],
            "source":       sources[0]["source"] if sources else "",
            "anchor":       sources[0]["anchor"] if sources else "",
        })

    csv_meta = _write_csv(results, validate_map, skip_map, output_path)

    # v1.9.1 — surface skip-reason breakdown so the caller can see WHY
    # external links were excluded (mailto / tel / scheme_javascript /
    # non_http_scheme / malformed_url / no_host). Counts every reason
    # without truncation. Sample URLs per reason capped to 5 for brevity.
    skipped_by_reason = defaultdict(int)
    skipped_examples = defaultdict(list)
    for url, reason in skip_map.items():
        skipped_by_reason[reason] += 1
        if len(skipped_examples[reason]) < 5:
            skipped_examples[reason].append(url)

    return {
        "external_links_csv":     csv_meta,
        "total_external_links":   len(targets),
        "unique_targets_found":   len(targets) + len(skip_map),  # incl. skipped
        "validated_count":        len(results),
        "skipped_total":          len(skip_map),
        "skipped_by_reason":      dict(sorted(skipped_by_reason.items(),
                                              key=lambda x: -x[1])),
        "skipped_examples":       {k: v for k, v in skipped_examples.items()},
        "by_status_class":        dict(by_class),
        "broken_count":           len(broken),
        "redirect_count":         sum(1 for r in results if r["redirect_count"] > 0),
        "top_broken":             top_broken,
        # Back-compat alias — older callers still read this
        "skipped_non_http":       len(skip_map),
    }
