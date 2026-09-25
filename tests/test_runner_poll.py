import pytest

import server


def _upstream(status, crawled):
    # Shape of upstream WebCrawler.get_status(): no is_running key at all.
    return {"status": status, "stats": {"crawled": crawled, "queued": 0}, "urls": []}


@pytest.mark.parametrize("status,expected", [
    ("running", True), ("completed", False), ("idle", False), ("demo_stopped", False), ("", False)])
def test_is_running_comes_from_status_string(status, expected):
    assert server._upstream_is_running(_upstream(status, 0)) is expected


def test_explicit_is_running_key_still_wins():
    assert server._upstream_is_running({"status": "running", "is_running": False}) is False


def test_status_poll_error_is_unknown_not_stopped(monkeypatch):
    import libreclient

    def boom(*a, **k):
        raise RuntimeError("read timeout")
    monkeypatch.setattr(server, "call", boom)
    st = libreclient.status()
    assert st["is_running"] is None and "read timeout" in st["error"]


@pytest.fixture
def run_env(monkeypatch):
    import libreclient
    import runner
    import state

    monkeypatch.setattr(runner, "POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    finalized = []
    monkeypatch.setattr(runner, "_finalize_session", lambda sid, cid, *a: finalized.append(cid))
    monkeypatch.setattr(libreclient, "start_crawl", lambda *a, **k: {"success": True, "crawl_id": 7})
    monkeypatch.setattr(libreclient, "export_pages", lambda cid=None: ([], []))
    monkeypatch.setattr(libreclient, "list_crawls", lambda: {"crawls": [{"id": 7, "urls_crawled": 0}]})
    monkeypatch.setattr(libreclient, "stop_crawl", lambda: None)

    def make(polls):
        it = iter(polls)

        def fake_call(method, path, **kw):
            nxt = next(it)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt
        monkeypatch.setattr(server, "call", fake_call)
        sid = state.create_session("https://a.test/", 60, 50, "normal", {})
        runner._run_session(state.get_session(sid))
        return state.get_session(sid), finalized
    return make


def test_sitemap_discovery_phase_is_not_a_stopped_crawl(run_env):
    # hostmy.blog 2026-09-25: first poll landed during sitemap parsing (running, 0 crawled).
    sess, finalized = run_env([_upstream("running", 0), _upstream("running", 12),
                               _upstream("running", 40), _upstream("completed", 60)])
    assert sess["status"] != "failed", sess["incomplete_reasons"]
    assert finalized == [7]


def test_running_crawl_is_not_finalized_early(run_env):
    sess, finalized = run_env([_upstream("running", 5), _upstream("running", 30),
                               _upstream("completed", 60)])
    assert finalized == [7]
    assert sess["pages_done"] == 60


def test_transient_poll_errors_are_tolerated(run_env):
    sess, finalized = run_env([RuntimeError("timeout"), RuntimeError("timeout"),
                               _upstream("running", 10), _upstream("completed", 20)])
    assert finalized == [7] and sess["status"] != "failed"


def test_persistent_poll_errors_fail_with_a_clear_reason(run_env):
    sess, finalized = run_env([RuntimeError("connection refused")] * 10)
    assert finalized == []
    assert sess["status"] == "failed"
    assert sess["incomplete_reasons"] == "upstream_unreachable"


def test_genuinely_empty_crawl_still_fails(run_env):
    sess, _ = run_env([_upstream("idle", 0)])
    assert sess["incomplete_reasons"] == "upstream_stopped_zero_pages"


@pytest.mark.parametrize("politeness,workers,min_delay_s", [
    ("polite", 2, 1.5), ("auto", 3, 0.5), ("fast", 5, 0.5)])
def test_politeness_sets_upstream_concurrency_and_delay(monkeypatch, politeness, workers, min_delay_s):
    import libreclient
    import runner
    import state

    sent = []
    monkeypatch.setattr(server, "call", lambda m, path, **kw: sent.append((path, kw.get("json"))) or
                        ({"success": True, "crawl_id": 7} if path == "/api/start_crawl"
                         else {"status": "idle", "stats": {"crawled": 0}}))
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    monkeypatch.setattr(libreclient, "list_crawls", lambda: {"crawls": []})
    sid = state.create_session("https://a.test/", 60, 50, politeness, {})
    runner._run_session(state.get_session(sid))
    saved = next(body for path, body in sent if path == "/api/save_settings")
    assert saved["concurrency"] == workers
    assert saved["crawlDelay"] >= min_delay_s
