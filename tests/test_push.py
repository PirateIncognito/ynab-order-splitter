import datetime
import json
import logging
import uuid
from types import SimpleNamespace

import pytest
from ynab.exceptions import ApiException

from core import push, ui
from core.ynab_client import Category

BUDGET = "budget-1"


def _matched(id, amount=-3000, lines=((-2000, "Mop", "home"), (-1000, "Tea", "food"))) -> dict:
    """An approved transaction as review.py leaves it in <plugin>_ynab_matched.json."""
    return {
        "id": id,
        "date": "2025-01-02",
        "amount": amount,
        "split_lines": [{"amount": a, "memo": m, "category_id": c} for a, m, c in lines],
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


def _in_ynab(id, date="2025-01-02", amount=-3000, subtransactions=(), payee_id=AMAZON):
    return SimpleNamespace(
        id=id, var_date=datetime.date.fromisoformat(date), amount=amount, subtransactions=list(subtransactions),
        payee_id=payee_id,
    )


@pytest.fixture
def queue_path(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(ui, "key", lambda message, keys: "y")
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
    assert whole == {"id": "whole", "approved": True, "category_id": "food"}
    assert _queued(queue_path) == []


def test_split_memos_are_cut_to_50_characters_but_kept_whole_in_the_queue(queue_path, monkeypatch):
    long = "Organic Fair Trade Whole Bean Coffee, Medium Roast, 2 lb Bag, Pack of 3"
    ynab = FakeYnab(monkeypatch, [_in_ynab("split")], fail=ApiException(status=500))  # failed: stays queued
    push.queue(queue_path, BUDGET, _matched("split", lines=((-2000, long, "food"), (-1000, "Tea", "food"))))

    push.push_approved(queue_path, "token", BUDGET)

    [[split]] = ynab.patches
    assert [s["memo"] for s in split["subtransactions"]] == [long[:50], "Tea"]
    assert json.loads(queue_path.read_text())[0]["split_lines"][0]["memo"] == long


def test_payload_sets_the_payee_only_when_the_user_picked_one(queue_path, monkeypatch):
    ynab = FakeYnab(monkeypatch, [_in_ynab("picked"), _in_ynab("unchanged")])
    push.queue(queue_path, BUDGET, {**_matched("picked"), "review_payee_id": "p-whole-foods"})
    push.queue(queue_path, BUDGET, _matched("unchanged"))

    push.push_approved(queue_path, "token", BUDGET)

    [[picked, unchanged]] = ynab.patches
    assert picked["payee_id"] == "p-whole-foods"
    assert "payee_id" not in unchanged  # an unchanged payee must not be overwritten
    # split lines inherit the parent's payee: the picked one, else YNAB's own
    assert [s["payee_id"] for s in picked["subtransactions"]] == ["p-whole-foods"] * 2
    assert [s["payee_id"] for s in unchanged["subtransactions"]] == [str(AMAZON)] * 2


def test_split_lines_get_the_payee_in_ynab_at_push_time(queue_path, monkeypatch):
    renamed = uuid.UUID("00000000-0000-0000-0000-00000000b002")
    ynab = FakeYnab(monkeypatch, [_in_ynab("t1", payee_id=renamed)])  # changed in YNAB after approval
    push.queue(queue_path, BUDGET, {**_matched("t1"), "payee_id": str(AMAZON)})

    push.push_approved(queue_path, "token", BUDGET)

    [[t1]] = ynab.patches
    assert [s["payee_id"] for s in t1["subtransactions"]] == [str(renamed)] * 2


def test_split_lines_get_no_payee_when_ynab_has_none(queue_path, monkeypatch):
    ynab = FakeYnab(monkeypatch, [_in_ynab("t1", payee_id=None)])
    push.queue(queue_path, BUDGET, _matched("t1"))

    push.push_approved(queue_path, "token", BUDGET)

    [[t1]] = ynab.patches
    assert all("payee_id" not in s for s in t1["subtransactions"])  # not "None", which YNAB would reject


def test_one_line_is_pushed_unsplit_with_its_category_and_memo(queue_path, monkeypatch):
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
