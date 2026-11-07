"""All automated tests are offline, even if the developer has credentials set."""

from pathlib import Path
import socket
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolate_real_credentials(monkeypatch, tmp_path):
    import paperread.credentials as credentials
    monkeypatch.setattr(credentials, "app_directory", lambda: tmp_path / "isolated-application")
    monkeypatch.delenv("WEREAD_API_KEY", raising=False)


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
