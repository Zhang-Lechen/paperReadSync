import httpx
import pytest

from paperread import network
from paperread.errors import AppError


@pytest.fixture
def routes(monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request, 'proxy_bypass', lambda host: host == 'bypass.example')


@pytest.mark.parametrize('url,proxies,expected', [
    ('https://weread.qq.com/', {}, None),
    ('https://weread.qq.com/', {'https':'http://localhost:7890'}, 'http://localhost:7890'),
    ('https://weread.qq.com/', {'all':'socks5://localhost:1080'}, 'socks5://localhost:1080'),
    ('https://weread.qq.com/', {'socks':'socks://localhost:1080'}, 'socks5://localhost:1080'),
    ('https://bypass.example/', {'https':'http://localhost:7890'}, None),
    ('http://127.0.0.1:8766/', {'all':'http://localhost:7890'}, None),
    ('http://[::1]:8766/', {'all':'http://localhost:7890'}, None),
    ('http://localhost:8766/', {'all':'http://localhost:7890'}, None),
])
def test_automatic_routing_and_local_bypass(routes, url, proxies, expected):
    assert network.proxy_for(url, proxies) == expected


def test_system_detection_and_proxy_failure_never_replays_directly(monkeypatch, routes):
    import urllib.request
    monkeypatch.setattr(urllib.request, 'getproxies', lambda: {'https':'http://localhost:7890'})
    created, requests, closed = [], [], []
    class Transport(httpx.BaseTransport):
        def __init__(self, **options):
            created.append(options)
        def handle_request(self, request):
            requests.append(request)
            raise httpx.ConnectError('synthetic-proxy-failure')
        def close(self):
            closed.append(True)
    monkeypatch.setattr(httpx, 'HTTPTransport', Transport)
    with network.online_client() as client:
        with pytest.raises(httpx.ConnectError):
            client.post('https://weread.qq.com/web/book/read', json={'synthetic':True})
    assert len(requests) == 1
    assert created == [{'proxy':'http://localhost:7890', 'retries':0, 'trust_env':False, 'verify':True}]
    assert closed == [True]


def test_transport_reuse_and_environment_bypass(monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request, 'proxy_bypass', lambda host: urllib.request.proxy_bypass_environment(
        host, {'no':'weread.qq.com'}))
    assert network.proxy_for('https://weread.qq.com/', {'https':'http://localhost:7890'}) is None


def test_invalid_proxy_is_redacted(routes):
    with pytest.raises(AppError) as exc:
        network.proxy_for('https://weread.qq.com/', {'https':'bad://synthetic-private:secret@proxy'})
    assert exc.value.code == 'PROXY_CONFIG'
    assert 'synthetic-private' not in str(exc.value)


def test_socks_dependency_available():
    # Construct only; no socket connections are made.
    with httpx.HTTPTransport(proxy='socks5://127.0.0.1:1080', retries=0):
        pass
