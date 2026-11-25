import socket

import httpx
import pytest

from paperread import proxy_dns, network
from paperread.errors import AppError

detect_fake_dns = proxy_dns.uses_fake_dns


def test_fake_dns_only_applies_to_audited_hosts(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('198.18.0.20', 443))])
    assert detect_fake_dns('i.weread.qq.com')
    assert not detect_fake_dns('unrelated.example')
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('1.1.1.1', 443))])
    assert not detect_fake_dns('weread.qq.com')


@pytest.fixture
def dns_reply(monkeypatch):
    proxy_dns._cache.clear()
    state = {'answer': {'Status':0, 'Answer':[{'type':1,'data':'1.1.1.1','TTL':60}]}, 'calls':[]}
    class Client:
        def __init__(self, **options):
            assert options['trust_env'] is False
            assert options['follow_redirects'] is False
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, **kwargs):
            state['calls'].append((url,kwargs))
            return httpx.Response(200, json=state['answer'], request=httpx.Request('GET',url))
    monkeypatch.setattr(proxy_dns.httpx, 'Client', Client)
    return state


def test_resolution_is_cached_and_sends_only_public_hostname(dns_reply):
    assert proxy_dns.resolve_public('weread.qq.com','http://localhost:1082') == '1.1.1.1'
    assert proxy_dns.resolve_public('weread.qq.com','http://localhost:1082') == '1.1.1.1'
    assert dns_reply['calls'] == [('https://dns.google/resolve', {'params':{'name':'weread.qq.com','type':'A'}})]


@pytest.mark.parametrize('address',['127.0.0.1','198.18.0.2','192.168.0.1','224.0.0.1','::1'])
def test_resolver_rejects_nonpublic_destinations(dns_reply, address):
    dns_reply['answer']['Answer'][0]['data'] = address
    with pytest.raises(AppError,match='PROXY_DNS'):
        proxy_dns.resolve_public('weread.qq.com','http://localhost:1082')


def test_tunnel_changes_only_connect_authority_preserves_tls_hostname():
    writes, tls = [], []
    class Stream:
        def write(self, data, timeout): writes.append(data)
        def start_tls(self, context, hostname, timeout):
            tls.append((context,hostname))
            return self
        def close(self): pass
    stream=proxy_dns._TunnelStream(Stream(),'weread.qq.com','1.1.1.1')
    stream.write(b'CONNECT weread.qq.com:443 HTTP/1.1\r\nHost: weread.qq.com:443\r\nProxy-Authorization: synthetic-private\r\n\r\n')
    assert writes[0] == b'CONNECT 1.1.1.1:443 HTTP/1.1\r\nHost: 1.1.1.1:443\r\nProxy-Authorization: synthetic-private\r\n\r\n'
    context=httpx.create_ssl_context(verify=True,trust_env=False)
    stream.start_tls(context,'weread.qq.com',10)
    assert tls==[(context,'weread.qq.com')]
    assert context.check_hostname
    stream.write(b'GET / HTTP/1.1\r\nHost: weread.qq.com\r\n\r\n')
    assert b'Host: weread.qq.com' in writes[1]


def test_connection_failure_does_not_fallback_or_replay(monkeypatch):
    monkeypatch.setattr(network,'system_proxies',lambda: {'https':'http://localhost:1082'})
    monkeypatch.setattr(network,'proxy_for',lambda *args: 'http://localhost:1082')
    monkeypatch.setattr(proxy_dns,'uses_fake_dns',lambda host: True)
    monkeypatch.setattr(proxy_dns,'resolve_public',lambda *args:'1.1.1.1')
    calls=[]
    class Transport(httpx.BaseTransport):
        def __init__(self,*args): pass
        def handle_request(self,request):
            calls.append(request)
            raise httpx.ConnectError('synthetic-private')
    monkeypatch.setattr(proxy_dns,'ResolvedProxyTransport',Transport)
    with network.online_client() as client:
        with pytest.raises(httpx.ConnectError):
            client.post('https://weread.qq.com/web/book/read',content=b'synthetic-body')
    assert len(calls)==1
    assert calls[0].url.host=='weread.qq.com'


def test_resolution_failure_happens_before_application_dispatch(monkeypatch):
    monkeypatch.setattr(network,'system_proxies',lambda: {'https':'http://localhost:1082'})
    monkeypatch.setattr(network,'proxy_for',lambda *args: 'http://localhost:1082')
    monkeypatch.setattr(proxy_dns,'uses_fake_dns',lambda host: True)
    def denied(*args): raise AppError('PROXY_DNS','合成解析失败')
    monkeypatch.setattr(proxy_dns,'resolve_public',denied)
    with network.online_client() as client:
        with pytest.raises(AppError,match='PROXY_DNS'):
            client.post('https://weread.qq.com/web/book/read',content=b'synthetic-body')
        assert client._transport.transports=={}


def test_normal_browser_network_stays_native(monkeypatch):
    monkeypatch.setattr(network,'system_proxies',lambda: {})
    with proxy_dns.browser_network() as options:
        assert options=={}
