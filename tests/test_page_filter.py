import pytest

import server


def _p(url, title="Real page", status=200, **kw):
    return {"url": url, "title": title, "status_code": status, "depth": 1, **kw}


@pytest.mark.parametrize("title,status", [
    ("Just a moment...", 403),
    ("Just a moment...", 200),
    ("Attention Required! | Cloudflare", 403),
    ("DDoS-Guard", 503),
    ("Access denied", 403),
    ("Please wait...", 429),
])
def test_challenge_titles_are_detected(title, status):
    assert server._is_challenge_page(_p("https://a.test/x", title, status))


@pytest.mark.parametrize("title,status", [
    ("Access denied errors in WordPress: how to fix them", 200),
    ("Security check for your WordPress site", 200),
    ("Please wait for our next release", 200),
    ("How we handle a Cloudflare 'just a moment' page", 200),
    ("", 403),
    (None, 503),
])
def test_real_pages_are_not_challenges(title, status):
    assert not server._is_challenge_page(_p("https://a.test/x", title, status))


def test_non_page_files_are_split_out():
    pages = [
        _p("https://a.test/"),
        _p("https://a.test/wp-content/uploads/hero.JPG", title=""),
        _p("https://a.test/file.pdf?v=2", title=""),
        _p("https://a.test/x", error_type="non_html_content"),
        _p("https://a.test/blog/", "Just a moment...", 403),
        _p("https://a.test/images-guide/"),
    ]
    real, challenged, assets = server._split_audit_pages(pages)
    assert [p["url"] for p in real] == ["https://a.test/", "https://a.test/images-guide/"]
    assert [p["url"] for p in challenged] == ["https://a.test/blog/"]
    assert len(assets) == 3


def test_get_settings_omits_full_dict_by_default(monkeypatch):
    big = {"maxUrls": 10, "excludePatterns": ["x"] * 500}
    monkeypatch.setattr(server, "call", lambda *a, **k: {"success": True, "settings": big})
    lean = server.librecrawl_get_settings()
    assert "settings" not in lean and lean["summary"] == {"maxUrls": 10}
    assert server.librecrawl_get_settings(full=True)["settings"] is big


# ── finalize: challenge pages never reach the report ─────────────────────────

@pytest.fixture
def finalize_env(monkeypatch):
    import runner
    import state
    import libreclient
    import external_links, content_audit, extended_checks, pdf_report, sitemap_fill

    def boom(*a, **k):
        raise RuntimeError("network disabled in tests")

    for mod, fn in ((external_links, "audit_external_links"), (content_audit, "audit_content"),
                    (extended_checks, "run_extended_checks"), (pdf_report, "render_pdf")):
        monkeypatch.setattr(mod, fn, boom)
    fill_calls = []
    monkeypatch.setattr(sitemap_fill, "fill_sitemap_orphans",
                        lambda urls, **k: fill_calls.append(urls) or {"pages_added": []})
    monkeypatch.setattr(server, "_site_check", lambda url: {})

    def make(pages, sitemap_urls=(), max_pages=0):
        monkeypatch.setattr(libreclient, "export_pages", lambda cid=None: (pages, []))
        monkeypatch.setattr(server, "_compute_sitemap_reconciliation",
                            lambda pgs, sm: _recon(pgs, sitemap_urls))
        sid = state.create_session("https://a.test/", max_pages, 50, "normal", {})
        runner._finalize_session(sid, 1, 500, 0)
        return state.get_session(sid), fill_calls
    return make


def _recon(pages, sitemap_urls):
    crawled = {p["url"] for p in pages}
    only = [u for u in sitemap_urls if u not in crawled]
    return {"sitemap_total": len(sitemap_urls), "sitemap_only_count": len(only),
            "sitemap_only": only, "crawl_only": [], "both": []}


def test_all_challenge_pages_fail_the_audit(finalize_env):
    pages = [dict(_p("https://a.test/", "Just a moment...", 403), depth=0),
             _p("https://a.test/a", "Just a moment...", 403)]
    sess, _ = finalize_env(pages)
    assert sess["status"] == "failed"
    assert "bot_challenge" in (sess["incomplete_reasons"] or "")


def test_mostly_challenged_site_is_incomplete_and_fill_is_skipped(finalize_env):
    pages = [dict(_p("https://a.test/"), depth=0)] + [
        _p(f"https://a.test/p{i}", "Just a moment...", 403) for i in range(5)]
    sitemap = [f"https://a.test/p{i}" for i in range(5)] + ["https://a.test/new"]
    sess, fill_calls = finalize_env(pages, sitemap)
    assert sess["status"] != "failed"
    assert not sess["audit_complete"]
    assert "bot_challenge: 5 URLs" in sess["incomplete_reasons"]
    # blocked URLs count as missed, not covered
    assert "0/6 sitemap URLs" in sess["incomplete_reasons"]
    assert fill_calls == []
    md = next(a for a in __import__("state").list_artifacts(sess["id"]) if a["kind"] == "md")
    text = open(md["path"], encoding="utf-8").read()
    assert "Just a moment" not in text


def test_cap_overshoot_is_trimmed(finalize_env):
    pages = [dict(_p("https://a.test/"), depth=0), _p("https://a.test/a"), _p("https://a.test/b")]
    sess, _ = finalize_env(pages, max_pages=2)
    assert sess["pages_done"] == 2


def test_status_zero_shows_its_fetch_error(tmp_path):
    import csv
    pages = [_p("https://a.test/", depth=0), _p("https://a.test/t", "", 0, error_type="timeout"),
             _p("https://a.test/u", "", 0)]
    out = tmp_path / "pp.csv"
    server._write_per_page_csv(pages, out)
    rows = {r["url"]: r["fetch_error"] for r in csv.DictReader(open(out))}
    assert rows == {"https://a.test/": "", "https://a.test/t": "timeout", "https://a.test/u": "no_response"}
    md = server._build_report(pages, "https://a.test/", 1, site_data={}, links=[])
    assert "| `https://a.test/t` | 0 (timeout) |" in md
