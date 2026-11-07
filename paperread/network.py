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
