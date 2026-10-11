import datetime
import json
import logging
import uuid
from types import SimpleNamespace

import pytest
from ynab.exceptions import ApiException

from core import push, ui, user_prefs
from core.ynab_client import Category

BUDGET = "budget-1"
ORDER = "111-1111111-1111111"


def _matched(id, amount=-3000, lines=((-2000, "Mop", "home"), (-1000, "Tea", "food"))) -> dict:
    """An approved transaction as review.py leaves it in <plugin>_ynab_matched.json."""
    return {
        "id": id,
        "date": "2025-01-02",
        "amount": amount,
        "split_lines": [{"amount": a, "memo": m, "category_id": c} for a, m, c in lines],
        "vendor_match": {"source_id": ORDER},
    }


class FakeYnab:
    """Stands in for ynab_client: YNAB's current transactions and
    categories, and the PATCHes sent."""

    def __init__(self, monkeypatch, transactions, categories=("home", "food"), fail=None):
        self.transactions = transactions
        self.categories = [Category(id=c, name=c, group_name="G") for c in categories]
        self.fail = fail
        self.patches = []
        monkeypatch.setattr(push.ynab_client, "list_transactions", self.list_transactions)
        monkeypatch.setattr(push.ynab_client, "list_categories", lambda token, budget_id: self.categories)
        monkeypatch.setattr(push.ynab_client, "update_transactions", self.update_transactions)
        monkeypatch.setattr(push.ynab_client, "get_transaction", self.get_transaction)

    def list_transactions(self, token, budget_id, since_date):
        return [t for t in self.transactions if t.var_date >= since_date]

    def get_transaction(self, token, budget_id, transaction_id):
        return next((t for t in self.transactions if t.id == transaction_id), None)

    def update_transactions(self, token, budget_id, transactions):
        assert budget_id == BUDGET
        self.patches.append(transactions)
        if self.fail:
            raise self.fail
        return [t["id"] for t in transactions]


AMAZON = uuid.UUID("00000000-0000-0000-0000-00000000a001")  # YNAB's TransactionDetail.payee_id is a UUID


def _in_ynab(id, date="2025-01-02", amount=-3000, subtransactions=(), memo=None):
    return SimpleNamespace(
        id=id, var_date=datetime.date.fromisoformat(date), amount=amount, subtransactions=list(subtransactions),
        payee_id=AMAZON, memo=memo,
    )


@pytest.fixture
def queue_path(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(ui, "key", lambda message, keys: "y")
    monkeypatch.setattr(user_prefs, "USER_PREFS_PATH", tmp_path / "user_prefs.json")
    return tmp_path / "amazon.com_ynab_approved.json"


def _queued(path) -> list[str]:
    return [e["id"] for e in json.loads(path.read_text())]


def test_approving_again_replaces_the_queued_entry(queue_path):
    push.queue(queue_path, BUDGET, _matched("t1"))
    push.queue(queue_path, BUDGET, _matched("t2"))
    push.queue(queue_path, BUDGET, _matched("t1", lines=((-3000, "Mop", "home"),)))

    entries = json.loads(queue_path.read_text())
    assert [e["id"] for e in entries] == ["t2", "t1"]
    assert entries[1]["split_lines"] == [{"amount": -3000, "memo": "Mop", "category_id": "home"}]


def test_entries_for_another_budget_stay_queued_unpushed(queue_path, monkeypatch, caplog):
    ynab = FakeYnab(monkeypatch, [_in_ynab("t1")])
    push.queue(queue_path, BUDGET, _matched("t1"))
    push.queue(queue_path, "other-budget", _matched("t2"))

    push.push_approved(queue_path, "token", BUDGET)

    assert [[t["id"] for t in batch] for batch in ynab.patches] == [["t1"]]
    assert _queued(queue_path) == ["t2"]
    assert "queued for another budget" in caplog.text


def test_declined_writes_nothing_and_is_offered_again(queue_path, monkeypatch):
    ynab = FakeYnab(monkeypatch, [_in_ynab("t1")])
    push.queue(queue_path, BUDGET, _matched("t1"))
    asked = []
    monkeypatch.setattr(ui, "key", lambda message, keys: asked.append(message) or "n")

    push.push_approved(queue_path, "token", BUDGET)
    push.push_approved(queue_path, "token", BUDGET)  # e.g. at the next start

    assert asked == ["Push 1 approved transaction(s) to YNAB? (y/n)"] * 2
    assert ynab.patches == []
    assert _queued(queue_path) == ["t1"]


def test_safeguard_writes_only_unchanged_transactions(queue_path, monkeypatch, caplog):
    ynab = FakeYnab(monkeypatch, [
        _in_ynab("ok"),
        _in_ynab("new-date", date="2025-01-03"),
        _in_ynab("earlier-date", date="2025-01-01"),  # before every queued date: not in the list, fetched by id
        _in_ynab("new-amount", amount=-3500),
        _in_ynab("split", subtransactions=[object()]),
        _in_ynab("gone-category"),
    ])
    for id in ("ok", "new-date", "earlier-date", "new-amount", "split", "deleted"):
        push.queue(queue_path, BUDGET, _matched(id))
    push.queue(queue_path, BUDGET, _matched("gone-category", lines=((-3000, "Mop", "retired"),)))

    push.push_approved(queue_path, "token", BUDGET)

    assert [[t["id"] for t in batch] for batch in ynab.patches] == [["ok"]]
    assert _queued(queue_path) == []
    assert "Succeeded with pushing: 1 written, 2 dropped, 4 flagged" in caplog.text


def test_payload_approves_and_splits_by_the_reviewed_lines(queue_path, monkeypatch):
    ynab = FakeYnab(monkeypatch, [_in_ynab("split"), _in_ynab("whole", amount=-500)])
    push.queue(queue_path, BUDGET, _matched("split"))
    push.queue(queue_path, BUDGET, {**_matched("whole", amount=-500, lines=()), "review_category_id": "food"})

    push.push_approved(queue_path, "token", BUDGET)

    [[split, whole]] = ynab.patches
    assert split["approved"] is True
    assert sum(s["amount"] for s in split["subtransactions"]) == -3000
    assert [s["category_id"] for s in split["subtransactions"]] == ["home", "food"]
    assert whole == {"id": "whole", "approved": True, "category_id": "food", "memo": ORDER}
    assert _queued(queue_path) == []


@pytest.mark.parametrize("memos, parent, line_memos, one_line", [
    ("none", None, [None, None], None),
    ("order", ORDER, [None, None], ORDER),
    ("items", None, ["Mop", "Tea"], "Mop"),
])
def test_memos_follow_the_setting(queue_path, monkeypatch, memos, parent, line_memos, one_line):
    user_prefs.save_user_prefs({"memos": memos})
    ynab = FakeYnab(monkeypatch, [_in_ynab("split"), _in_ynab("one")])
    push.queue(queue_path, BUDGET, _matched("split"))
    push.queue(queue_path, BUDGET, _matched("one", lines=((-3000, "Mop", "home"),)))

    push.push_approved(queue_path, "token", BUDGET)

    [[split, one]] = ynab.patches
    assert split.get("memo") == parent
    assert [s.get("memo") for s in split["subtransactions"]] == line_memos
    assert one.get("memo") == one_line


@pytest.mark.parametrize("memos", ["order", "items"])
def test_a_memo_already_in_ynab_is_never_overwritten(queue_path, monkeypatch, memos):
    user_prefs.save_user_prefs({"memos": memos})
    ynab = FakeYnab(monkeypatch, [_in_ynab("split", memo="Gift for Sam"), _in_ynab("one", memo="Gift for Sam")])
    push.queue(queue_path, BUDGET, _matched("split"))
    push.queue(queue_path, BUDGET, _matched("one", lines=((-3000, "Mop", "home"),)))

    push.push_approved(queue_path, "token", BUDGET)

    [[split, one]] = ynab.patches
    assert "memo" not in split and "memo" not in one


def test_a_merged_line_gets_no_item_memo(queue_path, monkeypatch):
    user_prefs.save_user_prefs({"memos": "items"})
    merged = {"amount": -2000, "memo": "Mop, Soap", "category_id": "home", "items": ["Mop", "Soap"]}
    ynab = FakeYnab(monkeypatch, [_in_ynab("split"), _in_ynab("one", amount=-2000)])
    push.queue(queue_path, BUDGET, {**_matched("split"), "split_lines": [
        merged, {"amount": -1000, "memo": "Tea", "category_id": "food"},
    ]})
    push.queue(queue_path, BUDGET, {**_matched("one", amount=-2000), "split_lines": [merged]})

    push.push_approved(queue_path, "token", BUDGET)

    [[split, one]] = ynab.patches
    assert [s.get("memo") for s in split["subtransactions"]] == [None, "Tea"]
    assert "items" not in split["subtransactions"][0]
    assert one == {"id": "one", "approved": True, "category_id": "home"}


def test_an_entry_queued_before_memos_pushes_without_one(queue_path, monkeypatch):
    ynab = FakeYnab(monkeypatch, [_in_ynab("t1")])
    push.queue(queue_path, BUDGET, _matched("t1"))
    entries = json.loads(queue_path.read_text())
    del entries[0]["order_number"]
    queue_path.write_text(json.dumps(entries))

    push.push_approved(queue_path, "token", BUDGET)

    [[t1]] = ynab.patches
    assert "memo" not in t1
    assert _queued(queue_path) == []


def test_split_memos_are_cut_to_50_characters_but_kept_whole_in_the_queue(queue_path, monkeypatch):
    user_prefs.save_user_prefs({"memos": "items"})
    long = "Organic Fair Trade Whole Bean Coffee, Medium Roast, 2 lb Bag, Pack of 3"
    ynab = FakeYnab(monkeypatch, [_in_ynab("split")], fail=ApiException(status=500))  # failed: stays queued
    push.queue(queue_path, BUDGET, _matched("split", lines=((-2000, long, "food"), (-1000, "Tea", "food"))))

    push.push_approved(queue_path, "token", BUDGET)

    [[split]] = ynab.patches
    assert [s["memo"] for s in split["subtransactions"]] == [long[:50], "Tea"]
    assert json.loads(queue_path.read_text())[0]["split_lines"][0]["memo"] == long


def test_push_sends_no_payee(queue_path, monkeypatch):
    ynab = FakeYnab(monkeypatch, [_in_ynab("old"), _in_ynab("new")])  # both have a payee in YNAB
    push.queue(queue_path, BUDGET, _matched("old"))
    push.queue(queue_path, BUDGET, _matched("new"))
    entries = json.loads(queue_path.read_text())
    entries[0]["payee_id"] = "p-whole-foods"  # queued while the review still had [p]ayee
    queue_path.write_text(json.dumps(entries))

    push.push_approved(queue_path, "token", BUDGET)

    [[old, new]] = ynab.patches
    assert "subtransactions" in old and "subtransactions" in new
    assert "payee_id" not in json.dumps(ynab.patches)  # split lines show under the transaction's payee
    assert _queued(queue_path) == []


def test_one_line_is_pushed_unsplit_with_its_category_and_memo(queue_path, monkeypatch):
    user_prefs.save_user_prefs({"memos": "items"})
    long = "Organic Fair Trade Whole Bean Coffee, Medium Roast, 2 lb Bag, Pack of 3"
    ynab = FakeYnab(monkeypatch, [_in_ynab("one")])
    push.queue(queue_path, BUDGET, _matched("one", lines=((-3000, long, "food"),)))

    push.push_approved(queue_path, "token", BUDGET)

    [[one]] = ynab.patches
    assert one == {"id": "one", "approved": True, "category_id": "food", "memo": long[:50]}


def test_a_failed_write_keeps_its_entries_queued(queue_path, monkeypatch, caplog):
    FakeYnab(monkeypatch, [_in_ynab("t1")], fail=ApiException(status=500, reason="Internal Server Error"))
    push.queue(queue_path, BUDGET, _matched("t1"))

    push.push_approved(queue_path, "token", BUDGET)

    assert _queued(queue_path) == ["t1"]
    assert "Failed pushing to YNAB" in caplog.text
    assert "Succeeded" not in caplog.text


def test_offline_keeps_the_queue_without_crashing(queue_path, monkeypatch, caplog):
    FakeYnab(monkeypatch, [_in_ynab("t1")], fail=ConnectionError("network is unreachable"))
    push.queue(queue_path, BUDGET, _matched("t1"))

    push.push_approved(queue_path, "token", BUDGET)

    assert _queued(queue_path) == ["t1"]
    assert "Failed pushing to YNAB: network is unreachable" in caplog.text


def test_only_ids_ynab_confirms_leave_the_queue(queue_path, monkeypatch):
    FakeYnab(monkeypatch, [_in_ynab("t1"), _in_ynab("t2")])
    monkeypatch.setattr(push.ynab_client, "update_transactions", lambda token, budget_id, transactions: ["t2"])
    push.queue(queue_path, BUDGET, _matched("t1"))
    push.queue(queue_path, BUDGET, _matched("t2"))

    push.push_approved(queue_path, "token", BUDGET)

    assert _queued(queue_path) == ["t1"]


def test_batches_are_cut_by_payload_size():
    payloads = [{"id": str(n), "memo": "x" * 100} for n in range(5)]
    size = len(json.dumps(payloads[0]))

    assert [len(b) for b in push.batches(payloads, max_bytes=2 * size)] == [2, 2, 1]
    assert [len(b) for b in push.batches(payloads, max_bytes=size - 1)] == [1] * 5
