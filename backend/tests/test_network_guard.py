"""
The two layers that keep tests away from real providers (SECURITY R25): no test sees a provider key, and no test can
connect outside this machine. See tests/conftest.py and tests/network_guard.py.

The addresses used here are in the documentation ranges reserved for examples (RFC 5737, 192.0.2.0/24 and the
.invalid top-level domain), so even a guard that failed would reach nothing that exists.
"""

import os
import socket
import threading

import pytest
from pydantic import SecretStr

from grounded_answers.answering.adapters import groq
from grounded_answers.answering.adapters.groq import GroqAnswerModel
from grounded_answers.answering.transport import make_client
from grounded_answers.answering.types import ProviderUnavailable
from grounded_answers.config import Settings, get_settings
from tests.answering_support import REQUEST
from tests.network_guard import NetworkBlocked, NetworkGuard, is_local_host, provider_key_names

EXTERNAL = ("192.0.2.1", 9)


# ---- layer 1: no provider key is visible ----


def test_no_provider_key_is_in_the_environment_or_in_settings() -> None:
    for name in provider_key_names():
        assert name not in os.environ
    assert (
        Settings.model_config["env_file"] is None
    )  # backend/.env, where real keys live, is not read
    settings = get_settings()
    assert settings.groq_api_key is None
    assert settings.gemini_api_key is None
    assert settings.anthropic_api_key is None


def test_a_test_that_sets_a_key_itself_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "a-key-made-up-by-the-test")
    get_settings.cache_clear()
    key = get_settings().groq_api_key
    assert key is not None and key.get_secret_value() == "a-key-made-up-by-the-test"


@pytest.mark.live
def test_a_live_test_keeps_the_real_configuration(network_guard: NetworkGuard) -> None:
    """The marker is the one way to opt out: the guard is off and Settings still reads backend/.env."""
    assert network_guard.enabled is False
    assert Settings.model_config["env_file"] is not None


def test_an_ordinary_test_has_the_guard_on(network_guard: NetworkGuard) -> None:
    assert network_guard.enabled is True


# ---- layer 2: nothing leaves the machine ----


@pytest.mark.parametrize(
    ("host", "local"),
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("localhost", True),
        ("127.5.5.5", True),
        ("", True),
        ("192.0.2.1", False),
        ("8.8.8.8", False),
        ("api.groq.com", False),
        ("generativelanguage.googleapis.com", False),
    ],
)
def test_which_hosts_count_as_this_machine(host: str, local: bool) -> None:
    assert is_local_host(host) is local


def test_a_connection_outside_the_machine_is_refused_and_recorded(
    network_guard: NetworkGuard,
) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(NetworkBlocked):
            sock.connect(EXTERNAL)
        with pytest.raises(NetworkBlocked):
            sock.connect_ex(EXTERNAL)
    finally:
        sock.close()
    assert network_guard.violations == ["connect to '192.0.2.1'", "connect_ex to '192.0.2.1'"]
    network_guard.violations.clear()  # expected here; an unexpected one fails the test at its end


def test_a_name_lookup_outside_the_machine_is_refused_and_recorded(
    network_guard: NetworkGuard,
) -> None:
    with pytest.raises(NetworkBlocked):
        socket.getaddrinfo("api.groq.com", 443)
    assert network_guard.violations == ["lookup of 'api.groq.com'"]
    network_guard.violations.clear()


def test_this_machine_stays_reachable(network_guard: NetworkGuard) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    accepted: list[socket.socket] = []
    thread = threading.Thread(target=lambda: accepted.append(listener.accept()[0]))
    thread.start()
    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    thread.join(timeout=5)
    client.close()
    for sock in accepted:
        sock.close()
    listener.close()
    assert socket.getaddrinfo("localhost", port)
    assert network_guard.violations == []


@pytest.mark.anyio
async def test_a_real_adapter_cannot_reach_a_provider(
    monkeypatch: pytest.MonkeyPatch, network_guard: NetworkGuard
) -> None:
    """The mistake the guard exists for: a real adapter, built with a key, asked to answer. It is stopped, and the
    attempt is recorded. The URL is a documentation address, so nothing is reached even if the guard were off."""
    monkeypatch.setattr(groq, "URL", "http://192.0.2.1:9/chat")
    client = make_client()
    try:
        model = GroqAnswerModel(SecretStr("a-key-made-up-by-the-test"), client)
        with pytest.raises(ProviderUnavailable):
            await model.answer(REQUEST)
    finally:
        await client.aclose()
    assert network_guard.violations, "the adapter's connection attempt was not recorded"
    assert all("192.0.2.1" in violation for violation in network_guard.violations)
    network_guard.violations.clear()


def test_an_unexpected_violation_fails_the_test_at_its_end() -> None:
    guard = NetworkGuard(enabled=True)
    guard.violations.append("connect to '192.0.2.1'")
    with pytest.raises(pytest.fail.Exception, match="network guard"):
        guard.assert_clean()
    assert guard.violations == []  # reported once


def test_a_clean_guard_passes() -> None:
    NetworkGuard(enabled=True).assert_clean()
