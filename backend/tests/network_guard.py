"""
The network guard of the test suite (Step 5c-2, SECURITY R25).

During a test, any attempt to connect to an address that is not this machine, or to look up a name that is not this
machine, is refused and recorded, and the test fails at its end if anything was recorded, even when the code under test
swallowed the error (an adapter turns a refused connection into "provider unavailable"). Tests talk to PostgreSQL and
Valkey on 127.0.0.1 and to the app in process, so nothing legitimate is blocked.

Why: the first run of the /ask HTTP tests reached a real Gemini adapter, because the app builds its adapters from the
real keys in backend/.env. The keys are now removed for every test (conftest.py) and this guard is the second layer: a
mistake in the first cannot send a request out.

Off only for a test marked `live` (the provider evaluation and the live /ask check, which run with `-m eval` and call a
real provider on purpose). The guard records host names and ports only, never a payload.
"""

import ipaddress
import socket
from typing import Any

import pytest


class NetworkBlocked(OSError):
    """A test tried to reach an address that is not this machine."""


def is_local_host(host: object) -> bool:
    if host in (None, "", b"", "localhost", b"localhost"):
        return True
    text = host.decode("ascii", "replace") if isinstance(host, bytes) else str(host)
    try:
        return ipaddress.ip_address(text.split("%", 1)[0]).is_loopback
    except ValueError:
        return text.lower() == "localhost"


class NetworkGuard:
    def __init__(self, *, enabled: bool) -> None:
        self.enabled = enabled
        self.violations: list[str] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        if not self.enabled:
            return
        original_connect = socket.socket.connect
        original_connect_ex = socket.socket.connect_ex
        original_getaddrinfo = socket.getaddrinfo

        def check_address(sock: socket.socket, address: Any, how: str) -> None:
            if sock.family == getattr(socket, "AF_UNIX", None):
                return
            host = address[0] if isinstance(address, tuple) and address else address
            if not is_local_host(host):
                self.violations.append(f"{how} to {host!r}")
                raise NetworkBlocked(f"network guard: a test tried to connect to {host!r}")

        def connect(sock: socket.socket, address: Any) -> Any:
            check_address(sock, address, "connect")
            return original_connect(sock, address)

        def connect_ex(sock: socket.socket, address: Any) -> Any:
            check_address(sock, address, "connect_ex")
            return original_connect_ex(sock, address)

        def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
            if not is_local_host(host):
                self.violations.append(f"lookup of {host!r}")
                raise NetworkBlocked(f"network guard: a test tried to look up {host!r}")
            return original_getaddrinfo(host, *args, **kwargs)

        monkeypatch.setattr(socket.socket, "connect", connect)
        monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
        monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)

    def assert_clean(self) -> None:
        if self.violations:
            attempts = ", ".join(self.violations)
            self.violations.clear()
            pytest.fail(
                f"network guard: the test tried to reach outside this machine ({attempts}). "
                "Use a fake. A test that must call a real service is marked `live`.",
                pytrace=False,
            )


def provider_key_names() -> tuple[str, ...]:
    return ("GROQ_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY")
