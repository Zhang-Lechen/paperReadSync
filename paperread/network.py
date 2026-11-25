"""A transport-level circuit breaker, including requests made outside the CLI."""

import httpx

from paperread.safety import SafetyError


class OfflineTransport(httpx.BaseTransport):
    """Reject every method and host before any socket or delegate is used."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        raise SafetyError("NETWORK_DISABLED: phase 0 is offline")


def offline_client() -> httpx.Client:
    return httpx.Client(
        transport=OfflineTransport(), follow_redirects=False, trust_env=False,
    )


def system_proxies():
    """Read standard environment proxies or OS settings (including macOS)."""
    from urllib.request import getproxies
    from paperread.errors import AppError
    try:
        return getproxies()
    except (OSError, ValueError):
        raise AppError("PROXY_CONFIG", "无法读取系统代理，请检查系统网络设置后重试") from None


def proxy_for(url, proxies):
    from ipaddress import ip_address
    from urllib.parse import urlsplit
    from urllib.request import proxy_bypass
    from paperread.errors import AppError
    parsed = urlsplit(str(url))
    host = parsed.hostname or ""
    if host.lower().rstrip('.') == "localhost":
        return None
    try:
        if ip_address(host).is_loopback:
            return None
    except ValueError:
        pass
    if proxy_bypass(parsed.netloc):
        return None
    proxy = proxies.get(parsed.scheme) or proxies.get("all") or proxies.get("socks")
    if proxy:
        if proxy.startswith("socks://"):
            proxy = "socks5://" + proxy[len("socks://"):]
        try:
            value = urlsplit(proxy)
            if value.scheme not in {"http", "https", "socks5", "socks5h"} or not value.hostname:
                raise ValueError()
            if value.port is not None and not 1 <= value.port <= 65535:
                raise ValueError()
        except ValueError:
            raise AppError("PROXY_CONFIG", "系统代理地址无效，请检查代理服务后重试") from None
    return proxy


class SystemProxyTransport(httpx.BaseTransport):
    """Use one settings snapshot per client; no retry or direct fallback."""

    def __init__(self):
        from threading import Lock
        self.proxies = system_proxies()
        self.transports = {}
        self.lock = Lock()

    def handle_request(self, request):
        from paperread.errors import AppError
        proxy = proxy_for(request.url, self.proxies)
        from paperread.proxy_dns import uses_fake_dns, resolve_public, ResolvedProxyTransport
        resolved = None
        if (proxy and proxy.startswith(("http://", "https://"))
                and request.url.scheme == "https" and request.url.port in (None, 443)
                and uses_fake_dns(request.url.host)):
            resolved = resolve_public(request.url.host, proxy)
        key = (proxy, request.url.host, resolved) if resolved else proxy
        with self.lock:
            if key not in self.transports:
                try:
                    self.transports[key] = (ResolvedProxyTransport(proxy, request.url.host, resolved)
                        if resolved else httpx.HTTPTransport(
                            proxy=proxy, retries=0, trust_env=False, verify=True))
                except (ValueError, ImportError):
                    raise AppError("PROXY_CONFIG", "系统代理配置无法使用，请检查代理服务后重试") from None
            transport = self.transports[key]
        return transport.handle_request(request)

    def close(self):
        for transport in self.transports.values():
            transport.close()


def online_client(*, timeout=15):
    return httpx.Client(transport=SystemProxyTransport(), timeout=timeout,
                        follow_redirects=False, trust_env=False)
