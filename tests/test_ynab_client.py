from types import SimpleNamespace

import pytest

from core import ynab_client


def test_list_categories_leaves_out_hidden_categories_and_groups(monkeypatch):
    def category(name, hidden=False):
        return SimpleNamespace(id=name, name=name, hidden=hidden, deleted=False)

    def group(name, categories, hidden=False):
        return SimpleNamespace(name=name, categories=categories, hidden=hidden, deleted=False, internal=False)

    groups = [
        group("Food", [category("Groceries"), category("Old diet", hidden=True)]),
        group("Retired", [category("Gym")], hidden=True),
    ]
    response = SimpleNamespace(data=SimpleNamespace(category_groups=groups))
    monkeypatch.setattr(
        ynab_client.ynab, "CategoriesApi", lambda client: SimpleNamespace(get_categories=lambda budget_id: response)
    )

    categories = ynab_client.list_categories("token", "budget-1")

    assert [c.name for c in categories] == ["Groceries"]


def test_an_unstubbed_ynab_call_is_blocked():
    with pytest.raises(RuntimeError, match="network blocked in tests"):
        ynab_client.list_categories("not-a-token", "budget-1")
