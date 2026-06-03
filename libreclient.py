"""
Typed wrapper around the upstream LibreCrawl HTTP API.

server.py already has an authenticated httpx client (`get_client()` + `call()`)
for the legacy synchronous flow — this module reuses that exact client so we
share the auth cookie and don't double-login. New methods here:

  * mid-crawl settings update (so the runner can re-tune crawlDelay live)
  * structured status snapshot (typed fields, no .get() chains in runner)
  * a `compute_p95` helper that runs on the exported pages — we use this
    once per polling window to derive a latency signal for the AIMD sizer.

LibreCrawl is single-tenant: only one crawl can run at a time across the
whole upstream instance. The runner serialises sessions accordingly.
"""

import time
from typing import Optional

# Reuses server.py's get_client/call so we share the auth cookie.
# Import is delayed inside functions to avoid a circular import at module load.


def _client_call(method, path, **kw):
    from server import call
    return call(method, path, **kw)


def _client():
    from server import get_client
    return get_client()


def _base():
    from server import BASE
    return BASE


# ── Crawl lifecycle ───────────────────────────────────────────────────────────

def start_crawl(url: str, max_pages: int = 0, crawl_delay_s: float = 0.5,
                max_depth: int = 5) -> dict:
    """Kick off a new crawl on the upstream. Returns {success, crawl_id, message}.

    max_pages=0 → unlimited. The runner uses this with its own total_max_pages
    ceiling enforced post-fetch.
    """
    settings = {
        "enableJavaScript":   False,
        "maxDepth":           max_depth,
        "crawlDelay":         crawl_delay_s,
        "followRedirects":    True,
        "crawlExternalLinks": False,
    }
    if max_pages > 0:
        settings["maxUrls"] = max_pages
    _client_call("POST", "/api/save_settings", json=settings)
    return _client_call("POST", "/api/start_crawl", json={"url": url})


def stop_crawl() -> dict:
    try:
        return _client_call("POST", "/api/stop_crawl")
    except Exception as e:
        return {"success": False, "error": str(e)}


def pause_crawl() -> dict:
    try:
        return _client_call("POST", "/api/pause_crawl")
    except Exception as e:
        return {"success": False, "error": str(e)}


def resume_crawl() -> dict:
    try:
        return _client_call("POST", "/api/resume_crawl")
    except Exception as e:
        return {"success": False, "error": str(e)}


def update_crawl_delay(crawl_delay_s: float) -> dict:
    """Mid-crawl settings change. The upstream applies it on the next request."""
    return _client_call("POST", "/api/save_settings",
                        json={"crawlDelay": crawl_delay_s})


def status() -> dict:
    """Structured snapshot of the active upstream crawl.

    Returns:
      is_running:  bool — upstream says crawl thread is alive
      crawled:     int  — pages fetched so far this crawl
      queued:      int  — frontier remaining
      issues:      int  — upstream-detected issues count
      base_url:    str
      speed_rps:   float — req/sec the upstream reports
      status_str:  str — completed/idle/running/paused
      raw:         dict — the full upstream payload (for debugging)
    """
    try:
        d = _client_call("GET", "/api/crawl_status")
    except Exception as e:
        return {"is_running": False, "error": str(e), "raw": {}}
    stats = d.get("stats", {}) or {}
    return {
        "is_running": bool(d.get("is_running")),
        "crawled":    stats.get("crawled", 0) or 0,
        "queued":     stats.get("queued", 0) or 0,
        "issues":     stats.get("issues", 0) or 0,
        "base_url":   stats.get("baseUrl", ""),
        "speed_rps":  float(stats.get("speed", 0) or 0),
        "status_str": (d.get("status") or "").lower(),
        "raw":        d,
    }


def list_crawls() -> dict:
    return _client_call("GET", "/api/crawls/list")


def load_crawl(crawl_id: int) -> dict:
    return _client_call("POST", f"/api/crawls/{crawl_id}/load")


def export_pages(crawl_id: Optional[int] = None) -> tuple:
    """Pull the export and return (pages, links). Reuses server._parse_export."""
    from server import _parse_export, EXPORT_FIELDS
    if crawl_id is not None:
        try:
            load_crawl(crawl_id)
        except Exception:
            pass
    r = _client().post(f"{_base()}/api/export_data",
                       json={"format": "json", "fields": EXPORT_FIELDS},
                       timeout=300)
    r.raise_for_status()
    return _parse_export(r.json())


# ── Metrics derivation ────────────────────────────────────────────────────────

def compute_chunk_metrics(pages_in_chunk: list) -> dict:
    """Derive {p95_ms, err_rate} from a list of page dicts.

    Used after each polling window to feed the AIMD sizer. Tolerant of
    missing response_time_ms — caller passes whatever the chunk had.
    """
    rt = [int(p.get("response_time_ms") or 0) for p in pages_in_chunk
          if p.get("response_time_ms")]
    p95 = None
    if rt:
        rt_sorted = sorted(rt)
        idx = max(0, int(len(rt_sorted) * 0.95) - 1)
        p95 = rt_sorted[idx]

    err_count = sum(1 for p in pages_in_chunk
                    if str(p.get("status_code", "")).startswith(("4", "5")))
    err_rate = (err_count / len(pages_in_chunk)) if pages_in_chunk else 0.0

    return {"p95_ms": p95, "err_rate": err_rate, "pages": len(pages_in_chunk)}
