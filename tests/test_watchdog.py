"""watchdog.py purges expired sessions, their files and upstream rows."""
import importlib
import os
import sqlite3
import time

import state

UPSTREAM_SCHEMA = """
CREATE TABLE crawls (id INTEGER PRIMARY KEY, base_url TEXT, started_at TIMESTAMP,
                     completed_at TIMESTAMP, last_saved_at TIMESTAMP);
CREATE TABLE crawled_urls (id INTEGER PRIMARY KEY, crawl_id INTEGER);
CREATE TABLE crawl_links (id INTEGER PRIMARY KEY, crawl_id INTEGER);
CREATE TABLE crawl_issues (id INTEGER PRIMARY KEY, crawl_id INTEGER);
CREATE TABLE crawl_queue (id INTEGER PRIMARY KEY, crawl_id INTEGER);
"""


def _load(monkeypatch, tmp_path):
    state_db = tmp_path / "state.db"
    upstream = tmp_path / "users.db"
    reports = tmp_path / "reports"
    reports.mkdir()
    monkeypatch.setenv("LIBRECRAWL_STATE_DB", str(state_db))
    monkeypatch.setenv("LIBRECRAWL_UPSTREAM_DB", str(upstream))
    monkeypatch.setenv("REPORTS_DIR", str(reports))
    monkeypatch.setenv("LIBRECRAWL_WATCHDOG_LOG", str(tmp_path / "wd.log"))
    monkeypatch.setenv("LIBRECRAWL_URL", "http://127.0.0.1:9")
    import watchdog
    return importlib.reload(watchdog), state_db, upstream, reports


def _session(db, sid, started, crawl_id):
    db.execute(
        "INSERT INTO sessions (id, url, status, upstream_crawl_id, total_max_pages, "
        "chunk_target_pages, politeness, current_delay_ms, started_at, updated_at, settings_json) "
        "VALUES (?, 'https://example.com', 'done', ?, 5, 5, 'polite', 1000, ?, ?, '{}')",
        (sid, crawl_id, started, started))


def test_purges_expired_done_session_everywhere(monkeypatch, tmp_path):
    wd, state_db, upstream, reports = _load(monkeypatch, tmp_path)
    db = sqlite3.connect(state_db)
    db.executescript(state.SCHEMA)
    old = time.time() - 3 * 3600
    _session(db, "s-old", old, 7)
    _session(db, "s-new", time.time(), 8)
    db.execute("INSERT INTO events (session_id, at, kind, detail) VALUES ('s-old', ?, 'x', '{}')", (old,))
    db.commit()
    db.close()

    u = sqlite3.connect(upstream)
    u.executescript(UPSTREAM_SCHEMA)
    for cid in (7, 8):
        u.execute("INSERT INTO crawls (id, base_url, started_at) "
                  "VALUES (?, 'https://example.com', datetime('now'))", (cid,))
        for t in ("crawled_urls", "crawl_links", "crawl_issues", "crawl_queue"):
            u.execute(f"INSERT INTO {t} (crawl_id) VALUES (?)", (cid,))
    u.commit()
    u.close()

    stale = reports / "old.md"
    stale.write_text("x")
    os.utime(stale, (old, old))

    assert wd.main() == 0

    db = sqlite3.connect(state_db)
    assert [r[0] for r in db.execute("SELECT id FROM sessions")] == ["s-new"]
    assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    u = sqlite3.connect(upstream)
    assert [r[0] for r in u.execute("SELECT id FROM crawls")] == [8]
    for t in ("crawled_urls", "crawl_links", "crawl_issues", "crawl_queue"):
        assert [r[0] for r in u.execute(f"SELECT crawl_id FROM {t}")] == [8]
    assert not stale.exists()


def test_sweeps_orphan_files_with_no_sessions(monkeypatch, tmp_path):
    wd, state_db, _, reports = _load(monkeypatch, tmp_path)
    sqlite3.connect(state_db).executescript(state.SCHEMA)
    stale = reports / "orphan.zip"
    stale.write_text("x")
    old = time.time() - 3 * 3600
    os.utime(stale, (old, old))
    assert wd.main() == 0
    assert not stale.exists()


def _upstream(path, crawls):
    u = sqlite3.connect(path)
    u.executescript(UPSTREAM_SCHEMA)
    for cid, age_s in crawls:
        u.execute("INSERT INTO crawls (id, base_url, started_at, last_saved_at) VALUES "
                  "(?, 'https://example.com', datetime('now', ?), datetime('now', ?))",
                  (cid, f"-{age_s} seconds", f"-{age_s} seconds"))
        u.execute("INSERT INTO crawl_links (crawl_id) VALUES (?)", (cid,))
    u.commit()
    u.close()


def test_purges_old_upstream_crawls_no_session_owns(monkeypatch, tmp_path):
    wd, state_db, upstream, _ = _load(monkeypatch, tmp_path)
    db = sqlite3.connect(state_db)
    db.executescript(state.SCHEMA)
    _session(db, "live", time.time(), 3)   # fresh session still owns crawl 3
    db.commit()
    db.close()
    day = 24 * 3600
    _upstream(upstream, [(1, day), (2, 60), (3, day)])

    assert wd.main() == 0

    u = sqlite3.connect(upstream)
    assert sorted(r[0] for r in u.execute("SELECT id FROM crawls")) == [2, 3]
    assert sorted(r[0] for r in u.execute("SELECT crawl_id FROM crawl_links")) == [2, 3]


def test_rest_delete_uses_one_client_and_closes_it(monkeypatch, tmp_path):
    wd, *_ = _load(monkeypatch, tmp_path)
    import httpx
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/delete") and "/9/" in request.url.path:
            return httpx.Response(500)
        return httpx.Response(200, json={"success": True})

    real = httpx.Client
    made = []

    def client(**kw):
        c = real(transport=httpx.MockTransport(handler), **kw)
        made.append(c)
        return c

    monkeypatch.setattr(httpx, "Client", client)
    assert wd._rest_delete([7, 9]) == [9]
    assert calls == [("POST", "/api/login"), ("DELETE", "/api/crawls/7/delete"),
                     ("DELETE", "/api/crawls/9/delete")]
    assert len(made) == 1 and made[0].is_closed
