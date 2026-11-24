"""Tests must never reach the real printer, Slack or anything else on the network.

A test once moved the real K1C's bed because an object kept the real Moonraker client after the test
swapped in a fake printer. Every real HTTP request now fails loudly instead. Tests that need HTTP stub
`session.request` on their instance, or the `requests` functions in the module under test.
"""
import pytest
import requests


class NetworkBlocked(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def blocked(self, method, url, *a, **k):
        raise NetworkBlocked(f"tests must not make real HTTP requests: {method} {url}")

    monkeypatch.setattr(requests.Session, "request", blocked)
