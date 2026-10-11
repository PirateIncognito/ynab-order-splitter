"""The no_network guard stops real requests before they leave, and browsers before they start."""

import subprocess
import webbrowser

import httpx
import httpx2
import pytest
import requests

URL = "https://example.invalid/"


@pytest.mark.parametrize("get", [httpx2.get, httpx.get, requests.get],
                         ids=["httpx2", "httpx", "requests"])
def test_real_request_is_blocked(get):
    with pytest.raises(RuntimeError, match="network blocked"):
        get(URL)


@pytest.mark.parametrize("start", [lambda url: webbrowser.open(url), lambda url: subprocess.Popen(["xdg-open", url])],
                         ids=["webbrowser", "xdg-open"])
def test_browser_is_blocked(start):
    with pytest.raises(RuntimeError, match="network blocked"):
        start(URL)
