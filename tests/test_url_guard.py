import socket

import pytest

import url_guard
from url_guard import BlockedURL, check_url

_REAL_GAI = socket.getaddrinfo


def _fake_resolve(mapping):
    def fake(host, *a, **k):
        if host[:1].isdigit() or ":" in host:
            return _REAL_GAI(host, *a, **k)  # IP literals resolve locally, no network
        if host not in mapping:
            raise socket.gaierror(8, "nodename nor servname provided")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (mapping[host], 0))]
    return fake


@pytest.mark.parametrize("url,reason", [
    ("", "empty_url"),
    ("   ", "empty_url"),
    (None, "empty_url"),
    ("ftp://example.com/", "unsupported_scheme"),
    ("file:///etc/passwd", "unsupported_scheme"),
    ("javascript:alert(1)", "unsupported_scheme"),
    ("example.com", "unsupported_scheme"),
    ("http://", "missing_host"),
])
def test_rejects_malformed(url, reason):
    with pytest.raises(BlockedURL) as e:
        check_url(url)
    assert e.value.reason == reason


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/",
    "http://localhost:5000/",
    "http://2130706433/",          # decimal spelling of 127.0.0.1
    "http://0x7f000001/",          # hex spelling
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.5/",
    "http://192.168.1.1/",
    "http://[::1]/",
])
def test_blocks_private_targets(url):
    with pytest.raises(BlockedURL) as e:
        check_url(url)
    assert e.value.reason == "blocked_private_address"


def test_docker_service_name_resolving_private_is_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_resolve({"librecrawl": "172.18.0.2"}))
    with pytest.raises(BlockedURL) as e:
        check_url("http://librecrawl:5000/api/crawls/list")
    assert e.value.reason == "blocked_private_address"


def test_dns_failure_is_reported(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_resolve({}))
    with pytest.raises(BlockedURL) as e:
        check_url("https://no-such-host.invalid/")
    assert e.value.reason == "dns_failed"


def test_public_host_passes_and_is_stripped(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_resolve({"example.com": "93.184.215.14"}))
    assert check_url("  https://example.com/  ") == "https://example.com/"


def test_override_allows_private(monkeypatch):
    monkeypatch.setenv("LIBRECRAWL_ALLOW_PRIVATE_TARGETS", "1")
    assert check_url("http://127.0.0.1:8080/") == "http://127.0.0.1:8080/"


def test_redirect_hook_rechecks_every_hop():
    import httpx
    with pytest.raises(BlockedURL):
        url_guard._hook(httpx.Request("GET", "http://169.254.169.254/"))


def test_preflight_reports_block_without_network():
    r = url_guard.preflight("http://127.0.0.1:1/")
    assert r == {"ok": False, "reason": "blocked_private_address", "error": r["error"]}


def test_preflight_catches_redirect_into_private_space(monkeypatch):
    import httpx
    monkeypatch.setattr(socket, "getaddrinfo",
                        _fake_resolve({"public.test": "93.184.215.14"}))

    def handler(request):
        if request.url.host == "public.test":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1/admin"})
        return httpx.Response(200)

    real = url_guard.guarded_client

    def client(**kw):
        return real(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(url_guard, "guarded_client", client)
    r = url_guard.preflight("http://public.test/")
    assert r["ok"] is False and r["reason"] == "blocked_private_address"
