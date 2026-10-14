from types import SimpleNamespace

import pytest
import ynab

from core import config, plugin_settings, ui, user_prefs, ynab_client
from tests.test_main import FakePlugin


def account(n):
    return SimpleNamespace(
        id=f"00000000-0000-0000-0000-{n:012d}", name=f"Account {n}", type=ynab.AccountType.CHECKING,
        balance=-1000 * n, on_budget=True, closed=False,
    )


@pytest.fixture
def mapping(monkeypatch):
    """configure_vendor_mapping for a FakePlugin with `count` accounts, the
    account mode chosen, and `answers` typed in turn; returns the plugin and
    each validate the prompts were given."""

    def run(count, *answers, confirms=()):
        accounts = [account(n) for n in range(1, count + 1)]
        monkeypatch.setattr(config, "get_ynab_access_token", lambda: "token")
        monkeypatch.setattr(user_prefs, "load_user_prefs", lambda: {"ynab_budget_id": "budget"})
        monkeypatch.setattr(ynab_client, "list_accounts", lambda token, budget_id: accounts)
        monkeypatch.setattr(ui, "choose", lambda *args, **kwargs: "account")
        typed, confirmed, validates = iter(answers), iter(confirms), []
        monkeypatch.setattr(ui, "text", lambda message, validate: validates.append(validate) or next(typed))
        monkeypatch.setattr(ui, "confirm", lambda message: next(confirmed))
        plugin = FakePlugin("shop", {})
        plugin_settings.configure_vendor_mapping(plugin)
        return plugin, validates

    return run


def test_any_of_40_accounts_can_be_picked_by_number(mapping):
    plugin, (validate,) = mapping(40, "40", confirms=[False])
    assert (plugin.prefs["account_ids"], plugin.prefs["account_names"]) == ([account(40).id], ["Account 40"])
    assert [validate(s) for s in ("", "1", "40")] == [True] * 3
    assert [validate(s) for s in ("0", "41", "abc")] == ["A number from 1 to 40."] * 3


def test_two_accounts_are_saved_in_the_order_picked(mapping):
    plugin, _ = mapping(3, "2", "1", confirms=[True, False])
    assert plugin.prefs["account_ids"] == [account(2).id, account(1).id]
    assert plugin.prefs["account_names"] == ["Account 2", "Account 1"]


def test_an_account_picked_twice_is_saved_once_and_shown_as_added(mapping, capsys):
    plugin, _ = mapping(2, "1", "1", confirms=[True, False])
    assert plugin.prefs["account_ids"] == [account(1).id]
    out = capsys.readouterr().out
    assert "Account 1 (added)" in out
    assert "Account 2 (added)" not in out


def test_going_back_after_adding_an_account_keeps_it(mapping):
    plugin, _ = mapping(2, "1", "", confirms=[True])
    assert plugin.prefs["account_ids"] == [account(1).id]


def test_going_back_from_the_list_saves_nothing(mapping):
    plugin, _ = mapping(2, "")
    assert plugin.prefs == {}
