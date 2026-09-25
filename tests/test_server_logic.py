import hashlib
import json

import pytest

import server

# ── export mapping ───────────────────────────────────────────────────────────

def test_export_requests_upstream_latency_field():
    assert "response_time" in server.EXPORT_FIELDS


def test_parse_export_maps_response_time():
    pages, _ = server._parse_export([{"url": "https://a.test/", "response_time": 412.6}])
    assert pages[0]["response_time_ms"] == 413


def test_parse_export_keeps_existing_ms_and_tolerates_junk():
    pages, _ = server._parse_export({"pages": [
        {"url": "https://a.test/1", "response_time": 10, "response_time_ms": 99},
        {"url": "https://a.test/2", "response_time": "n/a"},
        {"url": "https://a.test/3"},
    ]})
    assert pages[0]["response_time_ms"] == 99
    assert "response_time_ms" not in pages[1]
    assert "response_time_ms" not in pages[2]


def test_parse_export_single_file_format():
    body = {"filename": "librecrawl_export_1.json", "content": json.dumps({"urls": [{"url": "u", "response_time": 5}]})}
    pages, _ = server._parse_export(body)
    assert pages[0]["response_time_ms"] == 5


# ── safe domain ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://example.com/path", "example.com"),
    ("https://example.com:8443/", "example.com"),
    ("ftp://evil/../../x", "evil"),
    ("", "unknown"),
    ("https://exa mple.com/", "exa-mple.com"),
    ("../../etc/passwd", "unknown"),
])
def test_safe_domain(url, expected):
    out = server._safe_domain(url)
    assert out == expected
    assert "/" not in out and ".." not in out


# ── seed / orphan / site hosts ───────────────────────────────────────────────

SEED = {"url": "https://www.site.test/", "depth": 0, "linked_from": []}
CHILD = {"url": "https://www.site.test/a", "depth": 1, "linked_from": ["https://www.site.test/"]}
ORPHAN = {"url": "https://www.site.test/lost", "depth": 0, "linked_from": [], "source": "sitemap_fill"}


def test_seed_is_never_an_orphan():
    assert not server._is_orphan(SEED)
    assert not server._is_orphan(CHILD)
    assert server._is_orphan(ORPHAN)


def test_sitemap_fill_depth0_is_not_treated_as_seed():
    assert not server._is_seed_page(ORPHAN)
    assert server._is_seed_page(ORPHAN, seed_url="https://www.site.test/lost/")


def test_site_hosts_include_www_twin_and_redirect_target():
    hosts = server._site_hosts("https://site.test", [{"url": "https://cdn-home.test/", "depth": 0}])
    assert {"site.test", "www.site.test", "cdn-home.test", "www.cdn-home.test"} <= hosts
    assert server._on_site("https://WWW.site.test/x", {h.lower() for h in hosts})
    assert not server._on_site("https://other.test/", hosts)


def test_build_report_does_not_list_homepage_as_orphan():
    md = server._build_report([dict(SEED, status_code=200, title="Home"),
                               dict(CHILD, status_code=200, title="A")],
                              "https://www.site.test", 1)
    assert "| Orphan pages | 0 |" in md


def test_status_zero_is_counted_broken_not_ok():
    md = server._build_report([{"url": "https://down.test/", "status_code": 0, "depth": 0}],
                              "https://down.test", 1)
    assert "No critical issues found" not in md


def test_checks_manifest_excludes_seed_from_orphans():
    pages = [dict(SEED, status_code=200), dict(ORPHAN, status_code=200)]
    per_page = {name: fn for name, fn in server.PER_PAGE_CHECKS} if hasattr(server, "PER_PAGE_CHECKS") else None
    if per_page and "orphan_page" in per_page:
        assert not per_page["orphan_page"](pages[0])
        assert per_page["orphan_page"](pages[1])


# ── sitemap strictness ───────────────────────────────────────────────────────

def test_missing_sitemap_is_a_finding_not_a_failure():
    r = {"sitemap_reconciliation": {"sitemap_fetch_errors": ["https://s.test/sitemap.xml → HTTP 404"]}}
    assert server._sitemap_missing(r) and not server._sitemap_fetch_failed(r)
    r2 = {"sitemap_reconciliation": {"sitemap_fetch_errors": ["https://s.test/sitemap.xml → HTTP 503"]}}
    assert server._sitemap_fetch_failed(r2) and not server._sitemap_missing(r2)


# ── entry-point gates ────────────────────────────────────────────────────────

def test_legacy_audit_refuses_unbounded_without_confirm():
    r = server.librecrawl_audit("https://example.com", max_pages=0)
    assert r["success"] is False and r["reason"] == "unbounded_needs_confirm"


@pytest.mark.parametrize("fn", ["librecrawl_audit", "librecrawl_start_crawl", "librecrawl_full_audit_strict"])
def test_legacy_entry_points_block_private_seed(fn):
    r = getattr(server, fn)("http://169.254.169.254/")
    assert r["success"] is False and r["reason"] == "blocked_private_address"


def test_chunked_audit_blocks_bad_scheme():
    r = server.librecrawl_start_chunked_audit("ftp://example.com/")
    assert r["success"] is False and r["reason"] == "unsupported_scheme"


def test_report_content_rejects_sibling_prefix_dir(tmp_path):
    evil = server.REPORTS_DIR.parent / (server.REPORTS_DIR.name + "-evil")
    evil.mkdir(parents=True, exist_ok=True)
    f = evil / "x.md"
    f.write_text("secret")
    r = server.librecrawl_report_content(str(f))
    assert r["success"] is False


# ── filter_issues is local ───────────────────────────────────────────────────

def test_filter_issues_filters_locally(monkeypatch):
    issues = [{"url": "https://s.test/wp-admin/x", "issue": "Missing title"},
              {"url": "https://s.test/a", "issue": "Missing H1"}]
    monkeypatch.setattr(server, "call", lambda m, p, **k: {"issues": issues})
    r = server.librecrawl_filter_issues(["/wp-admin/"])
    assert r["total"] == 2 and r["excluded"] == 1 and r["issues"][0]["url"].endswith("/a")


def test_get_status_counts_issue_list(monkeypatch):
    monkeypatch.setattr(server, "call", lambda m, p, **k: {"stats": {"crawled": 3}, "issues": [{}, {}, {}, {}]})
    assert server.librecrawl_get_status()["issues"] == 4


# ── cleanup + confirm_saved ──────────────────────────────────────────────────

class _Resp:
    def __init__(self, code, body=None):
        self.status_code, self._b = code, body or {}
        self.text = json.dumps(self._b)

    def json(self):
        return self._b


def test_delete_upstream_rest_ok(monkeypatch):
    monkeypatch.setattr(server, "_upstream_request", lambda *a, **k: _Resp(200, {"success": True}))
    r = server._delete_upstream_crawl(7)
    assert r["ok"] and r["method"] == "rest"


def test_delete_upstream_404_is_already_absent(monkeypatch):
    monkeypatch.setattr(server, "_upstream_request", lambda *a, **k: _Resp(404, {"success": False}))
    r = server._delete_upstream_crawl(7)
    assert r["ok"] and r["method"] == "already_absent"


def test_delete_upstream_failure_is_not_ok(monkeypatch):
    monkeypatch.setattr(server, "_upstream_request", lambda *a, **k: _Resp(500, {"error": "boom"}))
    monkeypatch.setattr(server, "_wipe_upstream_crawl_record", lambda cid: {"error": "readonly database"})
    r = server._delete_upstream_crawl(7)
    assert r["ok"] is False


def _make_done_session(tmp_name="s.test"):
    sid = server._state.create_session(url=f"https://{tmp_name}/", total_max_pages=10,
                                       chunk_target_pages=5, politeness="auto", settings={})
    server._state.update_session(sid, upstream_crawl_id=42)
    server._state.set_status(sid, "done", "test")
    server.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    art = server.REPORTS_DIR / f"{tmp_name}-report.md"
    art.write_text("# report")
    server._state.add_artifact(sid, "report_md", art)
    return sid, art


def test_confirm_saved_mismatch_deletes_nothing(monkeypatch):
    monkeypatch.setattr(server, "_upstream_request", lambda *a, **k: _Resp(200, {"success": True}))
    sid, art = _make_done_session("mismatch.test")
    z = server.librecrawl_audit_zip(sid)
    assert z["success"], z
    r = server.librecrawl_audit_confirm_saved(sid, "0" * 64)
    assert r["success"] is False and "mismatch" in r["error"]
    assert art.exists() and server._state.get_session(sid)


def test_confirm_saved_match_wipes_everything(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_upstream_request",
                        lambda m, p, **k: calls.append((m, p)) or _Resp(200, {"success": True}))
    sid, art = _make_done_session("match.test")
    z = server.librecrawl_audit_zip(sid)
    import base64
    local_sha = hashlib.sha256(base64.b64decode(z["content_base64"])).hexdigest()
    assert local_sha == z["sha256"]
    r = server.librecrawl_audit_confirm_saved(sid, local_sha)
    assert r["success"] is True, r
    assert not art.exists()
    assert not server._state.get_session(sid)
    assert ("DELETE", "/api/crawls/42/delete") in calls
