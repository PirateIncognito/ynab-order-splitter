import json
import logging

import pytest

from ynab.exceptions import ApiException

from core import agents, categorize, config, main, match, plugin_settings, ui, user_prefs, ynab_client

HAIKU = {"model": "claude-haiku-5-5", "model_name": "Claude Haiku 5.5", "effort": True}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """core/.env as a dict; user prefs in tmp_path; each provider lists Haiku."""
    env = {}
    monkeypatch.setattr(config, "get_env_var", env.get)
    monkeypatch.setattr(config, "set_env_var", env.__setitem__)
    monkeypatch.setattr(user_prefs, "USER_PREFS_PATH", tmp_path / "user_prefs.json")
    for provider in agents.PROVIDERS:
        monkeypatch.setattr(agents, f"list_{provider}_models", lambda: [HAIKU])
    return env


def pick(monkeypatch, *choices, key=None):
    """Answer the menus in turn: the provider, then the model (None: Back)."""
    answers = iter(choices)
    monkeypatch.setattr(ui, "choose", lambda *args, **kwargs: next(answers))
    monkeypatch.setattr(ui, "secret", lambda message: key)


def test_turning_ai_on_saves_the_key_under_the_providers_name(env, monkeypatch):
    pick(monkeypatch, "openai", None, key=" sk-new \n")
    main._ai_agent_menu()
    assert env == {"OPENAI_API_KEY": "sk-new"}
    assert main._agent_label() == "On, OpenAI (untested), default model gpt-6-astra"


def test_cancelling_the_key_prompt_leaves_ai_off(env, monkeypatch):
    pick(monkeypatch, "claude", key=None)
    main._ai_agent_menu()
    assert env == {}
    assert main._agent_label() == "Off"


def test_switching_provider_asks_for_that_providers_key(env, monkeypatch):
    env["ANTHROPIC_API_KEY"] = "sk-claude"
    pick(monkeypatch, "claude", None)
    main._ai_agent_menu()
    assert main._agent_label() == "On, Claude, default model claude-opus-5-5"
    pick(monkeypatch, "gemini", key=None)
    main._ai_agent_menu()
    assert main._agent_label() == "Off"


def test_the_picked_model_is_kept_until_the_provider_changes(env, monkeypatch):
    env["ANTHROPIC_API_KEY"], env["OPENAI_API_KEY"] = "sk-claude", "sk-openai"
    pick(monkeypatch, "claude", HAIKU)
    main._ai_agent_menu()
    assert main._agent_label() == "On, Claude, Claude Haiku 5.5"
    pick(monkeypatch, "off")
    main._ai_agent_menu()
    pick(monkeypatch, "claude", None)
    main._ai_agent_menu()
    assert main._agent_label() == "On, Claude, Claude Haiku 5.5"
    pick(monkeypatch, "openai", None)
    main._ai_agent_menu()
    assert user_prefs.load_user_prefs()["agent"] == {"enabled": True, "provider": "openai"}


def test_failed_listing_leaves_the_model_unchanged(env, monkeypatch, caplog):
    env["ANTHROPIC_API_KEY"] = "sk-claude"
    pick(monkeypatch, "claude", HAIKU)
    main._ai_agent_menu()

    def offline():
        raise ConnectionError("no network")

    monkeypatch.setattr(agents, "list_claude_models", offline)
    pick(monkeypatch, "claude")
    caplog.set_level(logging.INFO)
    main._ai_agent_menu()
    assert user_prefs.load_user_prefs()["agent"] == {"enabled": True, "provider": "claude", **HAIKU}
    assert [r.message for r in caplog.records if r.message.startswith("Failed")] == ["Failed listing models: no network"]


def test_prefs_saved_before_models_still_work(env):
    user_prefs.USER_PREFS_PATH.write_text(json.dumps({"agent": {"enabled": True, "provider": "claude"}}))
    assert main._agent_label() == "On, Claude, default model claude-opus-5-5"


class FakePlugin:
    """A plugin whose prefs file is a dict and that has no settings of its own."""

    def __init__(self, name, prefs):
        self.name = name
        self.prefs = prefs

    def load_own_prefs(self):
        return dict(self.prefs)

    def save_own_prefs(self, prefs):
        self.prefs = prefs

    def settings(self):
        return []


OLD = ynab_client.Budget(id="old-id", name="Old")
NEW = ynab_client.Budget(id="new-id", name="New")
NEW_CATEGORIES = [
    ynab_client.Category(id="groceries-new", name="Groceries", group_name="Food"),
    ynab_client.Category(id="household-food-new", name="Food", group_name="Household"),
]


@pytest.fixture
def budget_change(env, monkeypatch, tmp_path):
    """Budget Old saved with a tax category; one account-mode and one
    payee-mode plugin, their category tables and ynab_categories.json in
    tmp_path; mapping questions recorded instead of asked; category fetches
    recorded and answered with NEW_CATEGORIES."""
    env["YNAB_ACCESS_TOKEN"] = "token"
    user_prefs.save_user_prefs(
        {**user_prefs.DEFAULT_PREFS, "ynab_budget_id": OLD.id, "ynab_budget_name": OLD.name, "sales_tax_category_id": "tax"}
    )
    plugins = [
        FakePlugin("by-account", {"mode": "account", "account_ids": ["a1"], "account_names": ["Card"]}),
        FakePlugin("by-payee", {"mode": "payee", "payee_name": "Shop"}),
    ]
    for plugin in plugins:
        plugin.categories_path = tmp_path / f"{plugin.name}_categories.json"
    monkeypatch.setattr(plugin_settings, "list_plugins", lambda: plugins)
    monkeypatch.setattr(categorize, "list_plugins", lambda: plugins)
    monkeypatch.setattr(match, "CATEGORIES_PATH", tmp_path / "ynab_categories.json")
    monkeypatch.setattr(ynab_client, "list_budgets", lambda token: [OLD, NEW])
    asked = []
    monkeypatch.setattr(plugin_settings, "configure_vendor_mapping", lambda plugin: asked.append(plugin.name))
    fetched = []
    monkeypatch.setattr(ynab_client, "list_categories", lambda token, budget_id: fetched.append(budget_id) or NEW_CATEGORIES)
    return plugins, asked, fetched


def fail(token, budget_id):
    raise ApiException(status=500, reason="Server Error")


def test_a_new_budget_clears_account_mappings_and_asks_for_new_accounts(budget_change, monkeypatch):
    (by_account, by_payee), asked, fetched = budget_change
    pick(monkeypatch, NEW)
    main._change_budget()
    assert fetched == ["new-id"]
    assert by_account.prefs == {**plugin_settings.SHARED_DEFAULTS, "mode": "account"}
    assert by_payee.prefs == {"mode": "payee", "payee_name": "Shop"}
    assert asked == ["by-account"]
    prefs = user_prefs.load_user_prefs()
    assert (prefs["ynab_budget_id"], prefs["sales_tax_category_id"]) == ("new-id", None)


@pytest.mark.parametrize(
    "choice, name",
    [(ynab_client.Budget(id=OLD.id, name="Old renamed"), "Old renamed"), (None, "Old")],
    ids=["same budget", "back"],
)
def test_keeping_the_budget_clears_nothing(budget_change, monkeypatch, choice, name):
    (by_account, _), asked, fetched = budget_change
    pick(monkeypatch, choice)
    main._change_budget()
    assert fetched == []
    assert by_account.prefs["account_ids"] == ["a1"]
    assert asked == []
    prefs = user_prefs.load_user_prefs()
    assert (prefs["sales_tax_category_id"], prefs["ynab_budget_name"]) == ("tax", name)


def test_a_failed_category_fetch_still_asks_for_new_accounts(budget_change, monkeypatch, caplog):
    _, asked, _ = budget_change
    monkeypatch.setattr(ynab_client, "list_categories", fail)
    pick(monkeypatch, NEW)
    main._change_budget()
    assert asked == ["by-account"]
    assert "Failed categories: Server Error; category tables follow the new budget on the next fetch" in caplog.messages


TABLE = {
    "milk": {"category": "groceries-old", "by": "user"},
    "bread": {"category": "groceries-old", "by": "agent"},
    "dog food": {"category": "pets-food-old", "by": "user"},  # New has Household: Food, not Pets: Food
    "eggs": {"category": "groceries-new", "by": "user"},  # already New's
}
REMAPPED = {
    "milk": {"category": "groceries-new", "by": "user"},
    "bread": {"category": "groceries-new", "by": "agent"},
    "eggs": {"category": "groceries-new", "by": "user"},
}


def _saved_before_change(plugins):
    """Each plugin's table is TABLE; ynab_categories.json holds Old's categories."""
    for plugin in plugins:
        plugin.categories_path.write_text(json.dumps(TABLE))
    match.CATEGORIES_PATH.write_text(json.dumps({"budget_id": OLD.id, "categories": [
        {"id": "groceries-old", "name": "Groceries", "group_name": "Food"},
        {"id": "pets-food-old", "name": "Food", "group_name": "Pets"},
    ]}))


def test_a_new_budget_moves_table_entries_to_same_named_categories(budget_change, monkeypatch, caplog):
    plugins, _, _ = budget_change
    _saved_before_change(plugins)
    pick(monkeypatch, NEW)
    caplog.set_level(logging.INFO)
    main._change_budget()
    for plugin in plugins:
        assert json.loads(plugin.categories_path.read_text()) == REMAPPED
    assert "by-payee: category table updated for the new budget: 3 kept, 1 removed" in caplog.messages


@pytest.mark.parametrize("choice", [NEW, ynab_client.Budget(id=OLD.id, name="Old renamed"), None],
                         ids=["fetch fails", "same budget", "back"])
def test_tables_are_untouched_without_a_change_or_new_categories(budget_change, monkeypatch, choice):
    plugins, _, _ = budget_change
    _saved_before_change(plugins)
    if choice is NEW:
        monkeypatch.setattr(ynab_client, "list_categories", fail)
    pick(monkeypatch, choice)
    main._change_budget()
    for plugin in plugins:
        assert json.loads(plugin.categories_path.read_text()) == TABLE


def test_after_a_failed_fetch_the_next_fetch_moves_the_tables(budget_change, monkeypatch):
    plugins, _, _ = budget_change
    _saved_before_change(plugins)
    monkeypatch.setattr(ynab_client, "list_categories", fail)
    pick(monkeypatch, NEW)
    main._change_budget()
    monkeypatch.setattr(ynab_client, "list_categories", lambda token, budget_id: NEW_CATEGORIES)
    match.fetch_categories("token", NEW.id)  # as Match and Split does
    for plugin in plugins:
        assert json.loads(plugin.categories_path.read_text()) == REMAPPED


def test_a_fetch_for_the_saved_budget_leaves_the_tables(budget_change, monkeypatch):
    """Every Match and Split fetches the categories again: same budget, no remap."""
    plugins, _, _ = budget_change
    _saved_before_change(plugins)
    monkeypatch.setattr(ynab_client, "list_categories", lambda token, budget_id: NEW_CATEGORIES)
    match.fetch_categories("token", OLD.id)
    for plugin in plugins:
        assert json.loads(plugin.categories_path.read_text()) == TABLE
