import importlib.util
import subprocess
import webbrowser
from pathlib import Path

import httpx
import httpx2
import pytest
import requests
import ynab

PLUGIN_PATH = Path(__file__).resolve().parent.parent / "plugins" / "amazon.com" / "plugin.py"


@pytest.fixture
def amazon_module(tmp_path, monkeypatch):
    """The Amazon plugin module, loaded fresh, with its files in tmp_path."""
    spec = importlib.util.spec_from_file_location("amazon_com_plugin_test", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ORDER_DATA_PATH", tmp_path / "amazon.com_order_data.json")
    monkeypatch.setattr(module, "PREFS_PATH", tmp_path / "amazon.com_user_prefs.json")
    monkeypatch.setattr(module.AmazonPlugin, "prefs_path", tmp_path / "amazon.com_user_prefs.json")
    monkeypatch.setattr(module, "COOKIE_JAR_PATH", tmp_path / ".amazon_cookies.json")
    monkeypatch.setattr(module, "_amazon_config", lambda: None)
    return module


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """No test reaches YNAB, Amazon or an AI provider, or opens a browser:
    stub the ynab_client/agents function, the SDK *Api class or the
    AmazonSession, or pass a MockTransport client."""
    def blocked(*args, **kwargs):
        raise RuntimeError("network blocked in tests: stub it")
    monkeypatch.setattr(ynab.ApiClient, "call_api", blocked)
    monkeypatch.setattr(requests.Session, "send", blocked)
    monkeypatch.setattr(httpx2.HTTPTransport, "handle_request", blocked)  # anthropic, openai
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)  # google-genai
    monkeypatch.setattr(webbrowser, "open", blocked)

    class NoBrowserPopen(subprocess.Popen):  # other commands still run
        def __init__(self, args, *rest, **kwargs):
            if args[0] == "xdg-open":
                blocked()
            super().__init__(args, *rest, **kwargs)
    monkeypatch.setattr(subprocess, "Popen", NoBrowserPopen)
