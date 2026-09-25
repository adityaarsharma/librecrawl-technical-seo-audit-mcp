import asyncio
import time

import pytest

import server


def test_every_tool_is_async_after_offload():
    sync = [t.name for t in server.mcp._tool_manager.list_tools() if not t.is_async]
    assert sync == []


def test_slow_sync_tool_does_not_block_the_event_loop():
    @server.mcp.tool()
    def _slow_probe() -> dict:
        time.sleep(0.6)
        return {"ok": True}

    server._offload_sync_tools()

    async def scenario():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            for _ in range(5):
                await asyncio.sleep(0.05)
                ticks += 1

        await asyncio.gather(server.mcp.call_tool("_slow_probe", {}), heartbeat())
        return ticks

    try:
        assert asyncio.run(scenario()) == 5
    finally:
        server.mcp._tool_manager._tools.pop("_slow_probe", None)


@pytest.fixture
def no_upstream(monkeypatch):
    touched = []
    monkeypatch.setattr(server, "_seed_gate", lambda url: None)
    monkeypatch.setattr(server, "_ensure_crawler_ready", lambda: touched.append("reset") or {})
    monkeypatch.setattr(server, "call", lambda *a, **k: touched.append(a) or {"success": True, "crawl_id": 1})
    return touched


@pytest.mark.parametrize("tool", ["librecrawl_audit", "librecrawl_start_crawl"])
def test_legacy_tools_refuse_while_a_chunked_audit_runs(monkeypatch, no_upstream, tool):
    monkeypatch.setattr(server, "_chunked_audit_busy", lambda: ["abc123"])
    r = getattr(server, tool)(url="https://a.test/", max_pages=5)
    assert r["success"] is False and r["reason"] == "crawler_busy"
    assert r["active_sessions"] == ["abc123"]
    assert no_upstream == []  # never reset or touched the running crawl


def test_second_legacy_audit_is_refused_not_stacked(monkeypatch, no_upstream):
    monkeypatch.setattr(server, "_chunked_audit_busy", list)
    assert server._LEGACY_CRAWL.acquire(blocking=False)
    try:
        r = server.librecrawl_audit(url="https://a.test/", max_pages=5)
    finally:
        server._LEGACY_CRAWL.release()
    assert r["reason"] == "crawler_busy" and no_upstream == []


def test_runner_defers_to_a_legacy_crawl(monkeypatch):
    import runner
    assert server._LEGACY_CRAWL.acquire(blocking=False)
    try:
        assert server._legacy_crawl_active() is True
    finally:
        server._LEGACY_CRAWL.release()
    monkeypatch.setattr(server, "_LEGACY_STARTED_AT", 0.0)
    assert server._legacy_crawl_active() is False
    monkeypatch.setattr(server, "_LEGACY_STARTED_AT", time.time())
    monkeypatch.setattr(server, "call", lambda *a, **k: {"status": "running"})
    assert server._legacy_crawl_active() is True
    assert runner._legacy_crawl_active() is True


def test_unbounded_chunked_crawl_still_sends_max_urls(monkeypatch):
    import libreclient
    sent = []
    monkeypatch.setattr(server, "call", lambda m, path, **k: sent.append((path, k.get("json"))) or {"success": True})
    libreclient.start_crawl("https://a.test/", max_pages=0)
    settings = dict(sent)["/api/save_settings"]
    assert settings["maxUrls"] >= 1_000_000
