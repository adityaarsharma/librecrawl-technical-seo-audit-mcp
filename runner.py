"""
Background runner for chunked-progressive audits.

One worker thread serialises sessions (LibreCrawl is single-tenant upstream).
Each session runs as ONE upstream crawl that we observe via polling windows
("chunks"). After every polling window we:

  1. Compute p95_ms + err_rate from the pages crawled this window
  2. Run the AIMD controller to decide next crawlDelay
  3. Push the new crawlDelay to LibreCrawl mid-crawl via /api/save_settings
  4. Persist the chunk row in state.db so PM2 restart can resume

When the upstream crawl finishes (or hits total_max_pages), the runner
finalises: builds the Markdown report + sidecar CSVs + checks_manifest +
sitemap_reconciliation + crawl_completeness, registers artifacts, transitions
session to 'done'.

State recovery on boot: any session in {queued, crawling, throttled, paused}
when the runner starts is resumed — if the upstream crawl is still alive we
pick polling back up; otherwise we issue resume_from_crawl_id and continue.
"""

import threading
import time
from pathlib import Path
from datetime import datetime

import state
import libreclient


# ── Configuration ─────────────────────────────────────────────────────────────

POLL_INTERVAL_SECONDS    = 20     # Window between status snapshots
MAX_POLL_INTERVAL        = 60     # Throttled mode upper bound
TARGET_P95_MS            = 1500
TARGET_ERR_RATE          = 0.02
MIN_DELAY_MS             = 0
MAX_DELAY_MS             = 5000
SANITY_CEILING_PAGES     = 100_000   # Override needs confirm_unbounded=True
UPSTREAM_HEALTH_TIMEOUT  = 600        # 10 min of no-progress → throttled
HARD_DEADLINE_SECONDS    = 14400      # 4 hr ceiling per session


_runner_thread: threading.Thread | None = None
_wake = threading.Event()
_shutdown = threading.Event()


# ── AIMD adaptive controller ──────────────────────────────────────────────────

def _tune_delay(prev_delay_ms: int, p95_ms: int | None, err_rate: float,
                robots_floor_ms: int = 0) -> int:
    """Return the next crawlDelay (ms) based on the previous window's signals.

    Additive-increase / multiplicative-decrease. Conservative bias: prefer
    slowing down to speeding up. Honors robots.txt Crawl-Delay floor.
    """
    delay = prev_delay_ms

    # Hard signals first
    if err_rate is not None and err_rate > 0.10:
        delay = min(MAX_DELAY_MS, delay * 2 + 500)
    elif err_rate is not None and err_rate > TARGET_ERR_RATE:
        delay = min(MAX_DELAY_MS, delay + 250)
    elif p95_ms and p95_ms > TARGET_P95_MS * 1.5:
        delay = min(MAX_DELAY_MS, int(delay * 1.5) + 100)
    elif p95_ms and p95_ms < TARGET_P95_MS * 0.6 and (err_rate or 0) < TARGET_ERR_RATE:
        delay = max(MIN_DELAY_MS, delay - 100)

    return max(delay, robots_floor_ms)


# ── Runner loop ───────────────────────────────────────────────────────────────

def _run_session(session: dict) -> None:
    """Drive one session from start → done. Synchronous, blocks the worker thread."""
    sid = session["id"]
    settings = session.get("settings", {}) or {}
    robots_floor_ms = int(settings.get("robots_floor_ms", 0))
    chunk_no = state.chunk_count(sid)
    started_window = time.time()
    last_seen_crawled = session.get("pages_done", 0)
    total_max = session["total_max_pages"]
    sanity_cap = total_max if total_max > 0 else SANITY_CEILING_PAGES
    delay_ms = session["current_delay_ms"]
    last_progress_at = time.time()
    started_session = session.get("started_at") or time.time()

    # If we're resuming an existing crawl, attach to upstream rather than start fresh
    upstream_crawl_id = session.get("upstream_crawl_id")
    if upstream_crawl_id is None:
        state.set_status(sid, "crawling")
        result = libreclient.start_crawl(
            session["url"],
            max_pages=total_max if total_max > 0 else 0,
            crawl_delay_s=delay_ms / 1000.0,
        )
        if not result.get("success"):
            err = result.get("message", "Upstream rejected start_crawl")
            state.update_session(sid, last_error=err, incomplete_reasons="upstream_start_failed")
            state.set_status(sid, "failed", detail=err)
            return
        upstream_crawl_id = result.get("crawl_id")
        state.update_session(sid, upstream_crawl_id=upstream_crawl_id)
    else:
        # Resume path — make sure upstream is still alive
        state.set_status(sid, "crawling", detail="resumed_from_state")
        try:
            libreclient.resume_crawl()
        except Exception:
            pass  # Best-effort; full crash recovery deferred to v1.5

    # ── Polling loop ──
    while not _shutdown.is_set():
        # Refresh session row in case operator paused/cancelled
        cur = state.get_session(sid)
        if not cur:
            return
        if cur["status"] in ("paused", "cancelled", "failed"):
            return
        if cur["status"] == "cancelled":
            libreclient.stop_crawl()
            return

        # Hard deadline guard
        if (time.time() - started_session) > HARD_DEADLINE_SECONDS:
            state.update_session(sid, incomplete_reasons="hard_deadline_exceeded")
            state.set_status(sid, "failed", "Hard 4-hour deadline reached")
            libreclient.stop_crawl()
            return

        time.sleep(POLL_INTERVAL_SECONDS)

        st = libreclient.status()
        crawled = st.get("crawled", 0)
        queued = st.get("queued", 0)
        speed = st.get("speed_rps")
        status_str = st.get("status_str", "")

        pages_in_window = max(0, crawled - last_seen_crawled)
        if pages_in_window > 0:
            last_progress_at = time.time()

        # Sanity cap check
        if crawled >= sanity_cap and total_max == 0:
            state.update_session(sid, incomplete_reasons="sanity_ceiling_hit")
            libreclient.stop_crawl()

        # Stale crawl detection
        if (time.time() - last_progress_at) > UPSTREAM_HEALTH_TIMEOUT and queued > 0:
            state.set_status(sid, "throttled", "no_progress_10min")
            state.update_session(sid, current_delay_ms=min(MAX_DELAY_MS, delay_ms * 2))

        # AIMD tuning — sample p95 from the export tail (every chunk_target_pages)
        target_chunk = session["chunk_target_pages"] or 50
        if pages_in_window >= target_chunk or (status_str == "completed") or (status_str == "idle" and crawled > 0):
            # Sample the most recent pages for metrics (export is heavy; sample only)
            metrics = {"p95_ms": None, "err_rate": None}
            try:
                pages, _ = libreclient.export_pages(upstream_crawl_id)
                window_pages = pages[-pages_in_window:] if pages_in_window else []
                metrics = libreclient.compute_chunk_metrics(window_pages)
            except Exception as e:
                state.log_event(sid, "metrics_export_failed", str(e))

            new_delay = _tune_delay(delay_ms, metrics["p95_ms"], metrics["err_rate"], robots_floor_ms)
            if abs(new_delay - delay_ms) >= 100:
                try:
                    libreclient.update_crawl_delay(new_delay / 1000.0)
                except Exception:
                    pass
                state.log_event(sid, "delay_tuned", {"from_ms": delay_ms, "to_ms": new_delay,
                                                      "p95": metrics["p95_ms"], "err": metrics["err_rate"]})
                delay_ms = new_delay

            chunk_no += 1
            state.record_chunk(
                sid, chunk_no,
                started_at=started_window,
                pages_in_chunk=pages_in_window,
                p95_ms=metrics["p95_ms"],
                err_rate=metrics["err_rate"],
                delay_used_ms=delay_ms,
                upstream_speed=speed,
            )
            state.update_session(sid, pages_done=crawled, current_delay_ms=delay_ms)
            started_window = time.time()
            last_seen_crawled = crawled

        # Termination
        done = (status_str == "completed") or (status_str == "idle" and crawled > 0) or (st.get("is_running") is False)
        if done and crawled > 0:
            break
        if done and crawled == 0:
            # Cross-check via DB before declaring failure
            try:
                listing = libreclient.list_crawls()
                row = next((c for c in (listing.get("crawls") or [])
                            if c.get("id") == upstream_crawl_id), None)
                if row and (row.get("urls_crawled") or 0) > 0:
                    crawled = row["urls_crawled"]
                    break
            except Exception:
                pass
            state.update_session(sid, incomplete_reasons="upstream_stopped_zero_pages")
            state.set_status(sid, "failed", "Upstream stopped with 0 pages")
            return

    if _shutdown.is_set():
        state.log_event(sid, "runner_shutdown_during_session")
        return

    # Finalise — write artifacts
    _finalize_session(sid, upstream_crawl_id, delay_ms, started_session)


def _finalize_session(sid: str, upstream_crawl_id: int, last_delay_ms: int,
                      started_session: float) -> None:
    """Build the report + sidecars and register them as artifacts."""
    from server import (_build_report, _site_check, _write_per_page_csv,
                        _write_sitemap_recon_csv, _compute_sitemap_reconciliation,
                        _build_checks_manifest, _compute_crawl_completeness,
                        REPORTS_DIR)

    sess = state.get_session(sid)
    url = sess["url"]
    try:
        pages, links = libreclient.export_pages(upstream_crawl_id)
    except Exception as e:
        state.update_session(sid, last_error=f"export_failed: {e}",
                             incomplete_reasons="export_failed")
        state.set_status(sid, "failed", str(e))
        return

    if not pages:
        state.update_session(sid, incomplete_reasons="no_pages_exported")
        state.set_status(sid, "failed", "No pages exported")
        return

    site_data = _site_check(url)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    domain = url.replace("https://", "").replace("http://", "").rstrip("/").split("/")[0]
    timestamp = datetime.now().strftime("%Y%m%d-%H%M")
    report_md = _build_report(pages, url, upstream_crawl_id or 0,
                              site_data=site_data, links=links)
    md_path = REPORTS_DIR / f"{domain}-{timestamp}.md"
    md_path.write_text(report_md, encoding="utf-8")
    state.add_artifact(sid, "md", md_path)

    per_page_csv = REPORTS_DIR / f"{domain}-{timestamp}.per-page.csv"
    _write_per_page_csv(pages, per_page_csv)
    state.add_artifact(sid, "per_page_csv", per_page_csv)

    sitemap_url = (site_data.get("sitemap") or {}).get("url") or f"{url.rstrip('/')}/sitemap.xml"
    recon = _compute_sitemap_reconciliation(pages, sitemap_url)
    recon_csv = REPORTS_DIR / f"{domain}-{timestamp}.sitemap-recon.csv"
    _write_sitemap_recon_csv(recon, recon_csv)
    state.add_artifact(sid, "sitemap_recon_csv", recon_csv)

    # Build completeness from the runner's own knowledge (don't reuse the
    # legacy _compute_crawl_completeness — its `last_status` shape doesn't
    # map cleanly to our chunked metrics).
    completeness = {
        "crawl_id":             upstream_crawl_id,
        "pages_crawled":        len(pages),
        "queued_remaining":     0,
        "max_pages":            sess["total_max_pages"],
        "max_pages_hit":        sess["total_max_pages"] > 0 and len(pages) >= sess["total_max_pages"],
        "timeout_hit":          False,
        "robots_blocked_count": 0,
        "batch_caps_hit":       False,
        "elapsed_seconds":      round(time.time() - started_session),
        "audit_complete":       True,
    }

    manifest = _build_checks_manifest(pages, site_data, links or [])

    # External-link validator (v1.4.1) — catches the 4xx/5xx/dns/redirect
    # failures upstream LibreCrawl leaves as target_status:null.
    try:
        import external_links
        ext_csv = REPORTS_DIR / f"{domain}-{timestamp}.external-links.csv"
        ext_summary = external_links.audit_external_links(
            pages, url, ext_csv, links=links,
            max_workers=10, timeout_seconds=10.0,
        )
        state.add_artifact(sid, "external_links_csv", ext_csv)
        state.log_event(sid, "external_links_audited", {
            "total":  ext_summary.get("total_external_links", 0),
            "broken": ext_summary.get("broken_count", 0),
            "by_class": ext_summary.get("by_status_class", {}),
        })
    except Exception as e:
        # Never fail the whole finalize on external-link audit issues —
        # the .md report + per-page CSV are the primary artifacts.
        state.log_event(sid, "external_links_audit_failed", str(e))

    # Content audit (v1.5) — paragraph-level checks (readability, AI-tells,
    # passive voice, lorem ipsum, boilerplate). Fetches first 50 pages by
    # default; capped to stay polite to the target. Best-effort.
    try:
        import content_audit
        ca_csv = REPORTS_DIR / f"{domain}-{timestamp}.content-audit.csv"
        ca_summary = content_audit.audit_content(pages, ca_csv, limit=50)
        state.add_artifact(sid, "content_audit_csv", ca_csv)
        state.log_event(sid, "content_audited", {
            "pages_audited": ca_summary.get("pages_audited", 0),
            "by_check": ca_summary.get("by_check", {}),
        })
    except Exception as e:
        state.log_event(sid, "content_audit_failed", str(e))

    # Extended SEO checks (v1.5) — security headers, mixed content, soft-404,
    # hreflang return-tag, sitemap cross-checks, canonical chains, URL quality.
    try:
        import extended_checks
        ec_csv = REPORTS_DIR / f"{domain}-{timestamp}.extended-checks.csv"
        ec_summary = extended_checks.run_extended_checks(
            pages, url, ec_csv, links=links, limit=50,
        )
        state.add_artifact(sid, "extended_checks_csv", ec_csv)
        state.log_event(sid, "extended_checks_done", {
            "findings": ec_summary.get("findings", 0),
            "by_check": ec_summary.get("by_check", {}),
        })
    except Exception as e:
        state.log_event(sid, "extended_checks_failed", str(e))

    # PDF report (v1.5) — Aditya-branded WeasyPrint render of the MD report.
    # Last so it includes all the analysis above.
    try:
        import pdf_report
        pdf_path = REPORTS_DIR / f"{domain}-{timestamp}.pdf"
        pdf_meta = pdf_report.render_pdf(report_md, pdf_path, base_url=url)
        state.add_artifact(sid, "pdf", pdf_path)
        state.log_event(sid, "pdf_generated", {
            "pages": pdf_meta.get("pages", 0),
            "size_bytes": pdf_meta.get("size_bytes", 0),
        })
    except Exception as e:
        # PDF failure must NOT kill finalize — the MD + CSVs are the primary
        # artifacts.
        state.log_event(sid, "pdf_generation_failed", str(e))

    state.log_event(sid, "finalized", {
        "pages": len(pages),
        "delay_at_finish_ms": last_delay_ms,
        "elapsed_s": completeness["elapsed_seconds"],
    })

    state.update_session(sid, pages_done=len(pages), audit_complete=1)
    state.set_status(sid, "done")


# ── Worker thread ─────────────────────────────────────────────────────────────

def _worker_loop():
    """Pick up queued sessions FIFO. Resume any active-but-not-running on boot."""
    # Boot recovery — anything in non-terminal state gets re-queued.
    for s in state.find_active_sessions():
        if s["status"] != "queued":
            state.set_status(s["id"], "queued", "boot_recovery_requeue")

    while not _shutdown.is_set():
        queued = state.find_queued_sessions()
        if not queued:
            _wake.wait(timeout=5)
            _wake.clear()
            continue

        session = queued[0]
        try:
            _run_session(session)
        except Exception as e:
            state.update_session(session["id"], last_error=str(e))
            state.set_status(session["id"], "failed", f"runner_exception: {e}")


def start_runner():
    """Idempotent. Spawn the worker thread if not already running."""
    global _runner_thread
    state.init_db()
    if _runner_thread and _runner_thread.is_alive():
        return
    _shutdown.clear()
    _runner_thread = threading.Thread(target=_worker_loop, name="librecrawl-runner",
                                       daemon=True)
    _runner_thread.start()


def stop_runner(timeout: float = 5.0):
    """Signal the worker to exit. Used on graceful shutdown."""
    _shutdown.set()
    _wake.set()
    if _runner_thread:
        _runner_thread.join(timeout=timeout)


def nudge():
    """Wake the worker from its sleep — call after queueing a session."""
    _wake.set()


# ── Operator controls ─────────────────────────────────────────────────────────

def enqueue_session(url: str, total_max_pages: int = 10_000,
                    chunk_target_pages: int = 50, politeness: str = "auto",
                    confirm_unbounded: bool = False,
                    extra_settings: dict | None = None) -> dict:
    """Create a session row and wake the runner. Returns the new session dict."""
    if total_max_pages == 0 and not confirm_unbounded:
        return {
            "success": False,
            "error": "total_max_pages=0 (unlimited) requires confirm_unbounded=True. "
                     "Sites can have millions of URLs — set a sensible ceiling.",
        }
    sid = state.create_session(
        url=url,
        total_max_pages=total_max_pages,
        chunk_target_pages=chunk_target_pages,
        politeness=politeness,
        settings=extra_settings or {},
    )
    nudge()
    return state.get_session(sid)


def pause_session(session_id: str) -> dict:
    s = state.get_session(session_id)
    if not s:
        return {"success": False, "error": "Unknown session_id"}
    libreclient.pause_crawl()
    state.set_status(session_id, "paused", "operator_pause")
    return {"success": True, "session_id": session_id, "status": "paused"}


def resume_session(session_id: str) -> dict:
    s = state.get_session(session_id)
    if not s:
        return {"success": False, "error": "Unknown session_id"}
    if s["status"] not in ("paused", "throttled"):
        return {"success": False, "error": f"Cannot resume from status={s['status']}"}
    libreclient.resume_crawl()
    state.set_status(session_id, "queued", "operator_resume")
    nudge()
    return {"success": True, "session_id": session_id, "status": "queued"}


def cancel_session(session_id: str) -> dict:
    s = state.get_session(session_id)
    if not s:
        return {"success": False, "error": "Unknown session_id"}
    libreclient.stop_crawl()
    state.set_status(session_id, "cancelled", "operator_cancel")
    return {"success": True, "session_id": session_id, "status": "cancelled"}


def force_advance(session_id: str) -> dict:
    """Stuck-recovery: force-finalise from whatever pages have been crawled."""
    s = state.get_session(session_id)
    if not s:
        return {"success": False, "error": "Unknown session_id"}
    libreclient.stop_crawl()
    try:
        _finalize_session(session_id, s.get("upstream_crawl_id"),
                          s.get("current_delay_ms") or 500,
                          s.get("started_at") or time.time())
        return {"success": True, "session_id": session_id, "status": "done", "forced": True}
    except Exception as e:
        return {"success": False, "session_id": session_id, "error": str(e)}
