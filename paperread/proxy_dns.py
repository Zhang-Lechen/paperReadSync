"""Resolve WeRead Fake-IP destinations before dispatch, keeping end-to-end TLS.

Only the HTTP CONNECT authority changes. HTTP Host, TLS SNI and certificate
verification keep the original hostname. No application request is replayed.
"""
from contextlib import contextmanager

import ipaddress
import socket
import threading
import time

import httpcore
import httpx

from paperread.errors import AppError

HOSTS = frozenset({'weread.qq.com', 'i.weread.qq.com'})
FAKE_RANGE = ipaddress.ip_network('198.18.0.0/15')
_cache = {}
_lock = threading.Lock()


def uses_fake_dns(host):
    if host not in HOSTS:
        return False
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        return any(ipaddress.ip_address(row[4][0]) in FAKE_RANGE for row in addresses
                   if row[0] == socket.AF_INET)
    except OSError:
        return False


def resolve_public(host, proxy):
    if host not in HOSTS:
        raise AppError('PROXY_DNS', '不支持此域名的代理 DNS 适配')
    key = (host, proxy)
    with _lock:
        cached = _cache.get(key)
        if cached and cached[1] > time.monotonic():
            return cached[0]
    try:
        # Independent transport avoids recursive resolution. Sends only a public
        # hostname, never the user's Key, cookies, book IDs or application body.
        with httpx.Client(proxy=proxy, trust_env=False, timeout=10,
                          follow_redirects=False) as client:
            response = client.get('https://dns.google/resolve', params={'name':host, 'type':'A'})
            response.raise_for_status()
            value = response.json()
        if value.get('Status') != 0:
            raise ValueError()
        for answer in value.get('Answer', []):
            if answer.get('type') != 1:
                continue
            address = ipaddress.ip_address(answer['data'])
            if address.version != 4 or not address.is_global or address.is_multicast:
                continue
            ttl = answer.get('TTL', 0)
            if type(ttl) is not int or ttl < 0:
                raise ValueError()
            with _lock:
                _cache[key] = (str(address), time.monotonic() + min(ttl, 300))
            return str(address)
        raise ValueError()
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
        raise AppError('PROXY_DNS', '代理使用虚拟 DNS 地址，但真实地址解析失败；未发送本次请求，请检查代理连接') from None


class _TunnelStream(httpcore.NetworkStream):
    def __init__(self, stream, host, address):
        self.stream = stream
        self.authority = host.encode('ascii') + b':443'
        self.target = address.encode('ascii') + b':443'
        self.connected = False

    def write(self, buffer, timeout=None):
        if not self.connected:
            prefix = b'CONNECT ' + self.authority + b' HTTP/1.1\r\n'
            if not buffer.startswith(prefix):
                raise httpcore.ProxyError('Unexpected proxy tunnel request')
            buffer = buffer.replace(prefix, b'CONNECT ' + self.target + b' HTTP/1.1\r\n', 1)
            buffer = buffer.replace(b'\r\nHost: ' + self.authority + b'\r\n',
                                    b'\r\nHost: ' + self.target + b'\r\n', 1)
            self.connected = True
        return self.stream.write(buffer, timeout)

    def read(self, max_bytes, timeout=None):
        return self.stream.read(max_bytes, timeout)

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        # Preserve httpcore's original hostname and verified SSL context.
        self.stream = self.stream.start_tls(ssl_context, server_hostname, timeout)
        return self

    def get_extra_info(self, info):
        return self.stream.get_extra_info(info)

    def close(self):
        self.stream.close()


class _TunnelBackend(httpcore.SyncBackend):
    def __init__(self, host, address):
        self.host, self.address = host, address

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        stream = super().connect_tcp(host, port, timeout, local_address, socket_options)
        return _TunnelStream(stream, self.host, self.address)


class ResolvedProxyTransport(httpx.HTTPTransport):
    def __init__(self, proxy, host, address):
        # The HTTPX 0.28 transport adapter delegates to this httpcore 1.0 pool.
        # Use a dedicated pool per original hostname, never pool by resolved IP.
        settings = httpx.Proxy(proxy)
        self._pool = httpcore.HTTPProxy(
            proxy_url=httpcore.URL(scheme=settings.url.raw_scheme, host=settings.url.raw_host,
                                   port=settings.url.port, target=settings.url.raw_path),
            proxy_auth=settings.raw_auth, proxy_headers=settings.headers.raw,
            ssl_context=httpx.create_ssl_context(verify=True, trust_env=False),
            proxy_ssl_context=settings.ssl_context, retries=0,
            network_backend=_TunnelBackend(host, address))


@contextmanager
def browser_network():
    """A temporary authenticated CONNECT relay preserves browser TLS/guards.

    Chrome otherwise asks the upstream HTTP proxy to resolve the broken hostname
    again. This relay only establishes tunnels; it never decrypts browser traffic.
    """
    import base64
    import http.client
    import secrets
    import select
    import socketserver
    from urllib.parse import urlsplit, unquote
    from paperread.network import system_proxies, proxy_for

    proxies = system_proxies()
    upstream = proxy_for('https://weread.qq.com/', proxies)
    if not upstream or not upstream.startswith('http://') or not uses_fake_dns('weread.qq.com'):
        yield {}
        return
    # Resolve before launching; no credentials or reading requests are involved.
    resolve_public('weread.qq.com', upstream)
    token = secrets.token_hex(24)
    expected_auth = 'Basic ' + base64.b64encode(f'{token}:{token}'.encode()).decode()
    active = set()
    active_lock = threading.Lock()

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            remote = None
            self.connection.settimeout(15)
            with active_lock:
                active.add(self.connection)
            try:
                line = self.rfile.readline(8193)
                if len(line) > 8192:
                    return
                method, authority, version = line.decode('ascii').strip().split(' ')
                headers = {}
                size = len(line)
                while True:
                    line = self.rfile.readline(8193)
                    size += len(line)
                    if size > 32768 or not line:
                        return
                    if line == b'\r\n':
                        break
                    key, value = line.decode('latin1').split(':', 1)
                    headers[key.lower()] = value.strip()
                if not secrets.compare_digest(headers.get('proxy-authorization', ''), expected_auth):
                    self.wfile.write(b'HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm="PaperRead"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                    return
                parsed = urlsplit('https://' + authority)
                host, port = parsed.hostname, parsed.port
                if method != 'CONNECT' or port != 443 or not host or parsed.username or parsed.path:
                    return
                selected = proxy_for('https://' + authority, proxies)
                address = resolve_public(host, selected) if selected and uses_fake_dns(host) else host
                if selected:
                    proxy = urlsplit(selected)
                    if proxy.scheme != 'http':
                        return
                    connection = http.client.HTTPConnection(proxy.hostname, proxy.port or 80, timeout=15)
                    auth = {}
                    if proxy.username is not None:
                        raw = f'{unquote(proxy.username)}:{unquote(proxy.password or "")}'.encode()
                        auth['Proxy-Authorization'] = 'Basic ' + base64.b64encode(raw).decode()
                    connection.set_tunnel(address, port, headers=auth)
                    connection.connect()
                    remote = connection.sock
                else:
                    remote = socket.create_connection((address, port), timeout=15)
                with active_lock:
                    active.add(remote)
                self.wfile.write(b'HTTP/1.1 200 Connection established\r\n\r\n')
                # Browser sends TLS only after receiving the CONNECT response.
                self.wfile.flush()
                while True:
                    readable, _, _ = select.select([self.connection, remote], [], [], 30)
                    if not readable:
                        return
                    for source in readable:
                        data = source.recv(65536)
                        if not data:
                            return
                        (remote if source is self.connection else self.connection).sendall(data)
            except Exception:
                # Network/parser exceptions may include private proxy addresses.
                return
            finally:
                with active_lock:
                    active.discard(self.connection)
                    active.discard(remote)
                if remote is not None:
                    remote.close()

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = False
        def handle_error(self, request, client_address):
            pass  # Never print raw proxy or browser exception data.

    server = Server(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval':.1}, daemon=True)
    worker.start()
    try:
        yield {'proxy': {'server': f'http://127.0.0.1:{server.server_address[1]}',
                         'username': token, 'password': token}}
    finally:
        server.shutdown()
        server.server_close()
        with active_lock:
            for stream in tuple(active):
                try:
                    stream.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                stream.close()
        worker.join(timeout=2)
