"""All automated tests are offline, even if the developer has credentials set."""

from pathlib import Path
import socket
import ctypes as C
from types import SimpleNamespace
from paperread import macos_keychain
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolate_real_credentials(monkeypatch, tmp_path, keychain_backend):
    import paperread.credentials as credentials
    monkeypatch.setattr(credentials, "app_directory", lambda: tmp_path / "isolated-application")
    monkeypatch.delenv("WEREAD_API_KEY", raising=False)
    from paperread import proxy_dns
    # Real VPN DNS/DoH must never be consulted by offline tests.
    monkeypatch.setattr(proxy_dns, "uses_fake_dns", lambda host: False)


@pytest.fixture(autouse=True)
def prohibit_external_connections(monkeypatch):
    original_connect = socket.socket.connect
    original_pair = socket.socketpair
    internal = threading.local()

    def denied(*args, **kwargs):
        pytest.fail("Automated tests must never access external sockets")

    def connect(instance, address):
        # Windows implements socketpair using a temporary loopback TCP pair.
        # Only this stdlib call may connect; HTTP and arbitrary loopback stay denied.
        if getattr(internal, "socketpair", False) and address[0] in ("127.0.0.1", "::1"):
            return original_connect(instance, address)
        return denied()

    def socketpair(*args, **kwargs):
        internal.socketpair = True
        try:
            return original_pair(*args, **kwargs)
        finally:
            internal.socketpair = False

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "socketpair", socketpair)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


@pytest.fixture
def root():
    return ROOT


@pytest.fixture
def keychain_backend(monkeypatch):
    records = {}
    buffers = []
    handles = {}
    released = []
    freed = []

    def find(_, service_len, service, account_len, account, length, data, item):
        assert service_len == len(service) and account_len == len(account)
        if account not in records:
            return -25300
        handle = len(handles) + 1
        handles[handle] = account
        C.cast(item, C.POINTER(C.c_void_p))[0] = handle
        if data is not None:
            value = records[account]
            buffer = C.create_string_buffer(value)
            buffers.append(buffer)
            C.cast(length, C.POINTER(C.c_uint32))[0] = len(value)
            C.cast(data, C.POINTER(C.c_void_p))[0] = C.cast(buffer, C.c_void_p)
        return 0

    def add(_, sl, service, al, account, length, value, item):
        records[account] = value[:length]
        return 0

    def update(item, attributes, length, value):
        records[handles[item.value]] = value[:length]
        return 0

    security = SimpleNamespace(SecKeychainFindGenericPassword=find,
        SecKeychainAddGenericPassword=add, SecKeychainItemModifyAttributesAndData=update,
        SecKeychainItemFreeContent=lambda _, data: freed.append(data.value))
    core = SimpleNamespace(CFRelease=lambda item: released.append(item.value))
    monkeypatch.setattr(macos_keychain, "_libraries", lambda: (security, core))
    return records, security, released, freed
