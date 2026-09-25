"""
Outbound URL guard (SSRF protection).

Every fetch the MCP makes on behalf of a caller-supplied URL goes through here:
crawl seeds, robots/sitemap probes, schema extraction, external-link checks,
sitemap fill, content audit and extended checks.

Rules:
  - scheme must be http or https, host must be present
  - every resolved address must be globally routable (ip.is_global), which
    blocks loopback, RFC1918, link-local (cloud metadata), CGNAT, and the
    docker-internal names such as `librecrawl` that resolve to private space.
    Decimal / octal / hex IP spellings are caught because getaddrinfo
    normalises them before the check.
  - redirects are re-checked hop by hop via httpx event hooks.

Set LIBRECRAWL_ALLOW_PRIVATE_TARGETS=1 to audit an intranet site on purpose.
"""

import ipaddress
import os
import socket
from urllib.parse import urlparse

import httpx

ALLOWED_SCHEMES = ("http", "https")


class BlockedURL(ValueError):
    """Raised when a URL fails the guard. `reason` is a short machine code."""

    def __init__(self, url: str, reason: str, detail: str = ""):
        self.url = url
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {url}" + (f" ({detail})" if detail else ""))


def private_targets_allowed() -> bool:
    return os.getenv("LIBRECRAWL_ALLOW_PRIVATE_TARGETS", "").strip().lower() in ("1", "true", "yes")


def _resolve(host: str) -> list:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise BlockedURL(host, "dns_failed", str(e)) from None
    except UnicodeError as e:
        raise BlockedURL(host, "invalid_host", str(e)) from None
    return sorted({info[4][0] for info in infos})


def check_url(url, *, resolve: bool = True) -> str:
    """Validate a URL and return it normalised (stripped). Raises BlockedURL."""
    if not isinstance(url, str) or not url.strip():
        raise BlockedURL(str(url), "empty_url")
    url = url.strip()
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise BlockedURL(url, "unsupported_scheme", f"only http/https allowed, got {scheme or 'none'!r}")
    host = parsed.hostname
    if not host:
        raise BlockedURL(url, "missing_host")
    if not resolve or private_targets_allowed():
        return url
    for addr in _resolve(host):
        ip = ipaddress.ip_address(addr.split("%")[0])
        if not ip.is_global:
            raise BlockedURL(url, "blocked_private_address", f"{host} resolves to {ip}")
    return url


def _hook(request: httpx.Request) -> None:
    check_url(str(request.url))


async def _ahook(request: httpx.Request) -> None:
    check_url(str(request.url))


def guarded_client(**kwargs) -> httpx.Client:
    """httpx.Client that re-checks every request, including each redirect hop."""
    hooks = kwargs.pop("event_hooks", {}) or {}
    hooks.setdefault("request", []).insert(0, _hook)
    return httpx.Client(event_hooks=hooks, **kwargs)


def guarded_async_client(**kwargs) -> httpx.AsyncClient:
    hooks = kwargs.pop("event_hooks", {}) or {}
    hooks.setdefault("request", []).insert(0, _ahook)
    return httpx.AsyncClient(event_hooks=hooks, **kwargs)


def guarded_get(url: str, **kwargs) -> httpx.Response:
    """Drop-in for httpx.get with the guard applied to the URL and every redirect."""
    check_url(url)
    client_kwargs = {k: kwargs.pop(k) for k in ("timeout", "follow_redirects", "headers", "verify") if k in kwargs}
    with guarded_client(**client_kwargs) as c:
        return c.get(url, **kwargs)


def guarded_head(url: str, **kwargs) -> httpx.Response:
    check_url(url)
    client_kwargs = {k: kwargs.pop(k) for k in ("timeout", "follow_redirects", "headers", "verify") if k in kwargs}
    with guarded_client(**client_kwargs) as c:
        return c.head(url, **kwargs)


def preflight(url: str, timeout: float = 15.0) -> dict:
    """
    Guarded GET of a crawl seed before handing it to the crawler. Catches the
    cases the upstream crawler reports as a "successful" zero-page crawl:
    bad scheme, DNS failure, connection refused, redirect into private space.
    Returns {"ok": True, "final_url", "status_code"} or {"ok": False, "reason", "error"}.
    """
    try:
        check_url(url)
        r = guarded_get(url, timeout=timeout, follow_redirects=True,
                        headers={"User-Agent": "LibreCrawl-MCP preflight"})
        return {"ok": True, "final_url": str(r.url), "status_code": r.status_code}
    except BlockedURL as e:
        return {"ok": False, "reason": e.reason, "error": str(e)}
    except httpx.HTTPError as e:
        # A BlockedURL raised inside a redirect hook surfaces wrapped on some httpx versions.
        cause = e.__cause__ or e.__context__
        if isinstance(cause, BlockedURL):
            return {"ok": False, "reason": cause.reason, "error": str(cause)}
        return {"ok": False, "reason": "unreachable", "error": f"{type(e).__name__}: {e}"}
