import datetime
import json

import pytest

from core import match
from plugins.plugin import VendorLineItem, VendorTransaction


class FakeTransaction:
    """Stands in for ynab.TransactionDetail. Its date field is named
    var_date, not date -- the real SDK's field is a renamed/aliased pydantic
    field (alias="date" for JSON, attribute var_date in Python) -- so this
    stub matches that shape rather than the more obvious `self.date`."""

    def __init__(self, id, date, amount, account_id="acct-1", payee_name="Amazon.com", subtransactions=None):
        self.id = id
        self.var_date = date
        self.amount = amount
        self.account_id = account_id
        self.payee_name = payee_name
        self.subtransactions = subtransactions or []

    def to_json(self) -> str:
        return json.dumps(
            {
                "id": self.id,
                "date": self.var_date.isoformat(),
                "amount": self.amount,
                "account_id": self.account_id,
                "payee_name": self.payee_name,
                "subtransactions": self.subtransactions,
            }
        )


class FakePlugin:
    name = "amazon.com"
    scrapes_vendor_data = False
    matched_path = None  # tests set a tmp path

    def __init__(self, prefs=None, vendor_transactions=None, scrape_error=None, match_error=None):
        self._prefs = prefs or {}
        self._vendor_transactions = vendor_transactions or []
        self.scrape_calls = []
        self.scrape_error = scrape_error
        self.match_error = match_error

    def load_own_prefs(self):
        return self._prefs

    def get_data_from_vendor(self, date=None):
        self.scrape_calls.append(date)
        if self.scrape_error:
            raise self.scrape_error

    def get_vendor_transaction(self, date, amount, exclude=()):
        if self.match_error and amount == self.match_error[0]:
            raise self.match_error[1]
        return next(
            (
                v
                for v in self._vendor_transactions
                if (v.date, v.amount) == (date, amount) and v.record_id not in exclude
            ),
            None,
        )


def test_match_transaction_takes_closest_date_and_flags_ambiguity():
    """Two records fit: the one closest to the YNAB date wins, flagged."""
    far = VendorTransaction(date=datetime.date(2026, 1, 6), amount=-5000, record_id="far")
    near = VendorTransaction(date=datetime.date(2026, 1, 12), amount=-5000, record_id="near")
    plugin = FakePlugin(vendor_transactions=[far, near])
    claimed = set()

    result = match.match_transaction(plugin, FakeTransaction("t1", datetime.date(2026, 1, 10), -5000), 5, claimed)

    assert result["match_status"] == "ambiguous"
    assert result["vendor_match"]["record_id"] == "near"
    assert claimed == {"near"}


def test_matches_mapping_account_mode():
    txn = FakeTransaction("t1", datetime.date(2026, 1, 1), -1000, account_id="acct-1")

    assert match._matches_mapping(txn, {"mode": "account", "account_ids": ["acct-1"]})
    assert not match._matches_mapping(txn, {"mode": "account", "account_ids": ["acct-2"]})


def test_matches_mapping_payee_mode_case_insensitive():
    txn = FakeTransaction("t1", datetime.date(2026, 1, 1), -1000, payee_name="AMAZON.COM")

    assert match._matches_mapping(txn, {"mode": "payee", "payee_name": "amazon.com"})


def test_matches_mapping_unknown_mode_returns_false():
    txn = FakeTransaction("t1", datetime.date(2026, 1, 1), -1000)

    assert not match._matches_mapping(txn, {"mode": None})


def test_vendor_transaction_dict_serializes_date():
    vendor_txn = VendorTransaction(
        date=datetime.date(2026, 1, 1),
        amount=1000,
        record_id="r1",
        line_items=[VendorLineItem(description="Widget", amount=1000)],
        sales_tax=50,
        source_id="abc",
    )

    d = match._vendor_transaction_dict(vendor_txn)

    assert d["date"] == "2026-01-01"
    json.dumps(d)  # must not raise


def test_refresh_vendor_data_scrapes_with_lookback_date():
    plugin = FakePlugin()
    plugin.scrapes_vendor_data = True
    candidates = [
        FakeTransaction("t1", datetime.date(2026, 1, 2), -1000),
        FakeTransaction("t2", datetime.date(2026, 1, 5), -1000),
    ]

    match._refresh_vendor_data(plugin, candidates, margin_days=7)

    assert plugin.scrape_calls == [datetime.date(2025, 12, 26)]


def test_refresh_vendor_data_skips_when_plugin_does_not_scrape():
    plugin = FakePlugin()
    plugin.scrapes_vendor_data = False
    candidates = [FakeTransaction("t1", datetime.date(2026, 1, 2), -1000)]

    match._refresh_vendor_data(plugin, candidates, margin_days=7)

    assert plugin.scrape_calls == []


def test_refresh_vendor_data_skips_when_no_candidates():
    plugin = FakePlugin()
    plugin.scrapes_vendor_data = True

    match._refresh_vendor_data(plugin, [], margin_days=7)

    assert plugin.scrape_calls == []


def test_refresh_vendor_data_survives_scrape_failure(caplog):
    plugin = FakePlugin(scrape_error=RuntimeError("boom"))
    plugin.scrapes_vendor_data = True
    candidates = [FakeTransaction("t1", datetime.date(2026, 1, 2), -1000)]

    match._refresh_vendor_data(plugin, candidates, margin_days=7)  # must not raise

    assert "boom" in caplog.text


def _stub_ynab(monkeypatch, tmp_path, transactions):
    monkeypatch.setattr(FakePlugin, "matched_path", tmp_path / "amazon.com_ynab_matched.json")
    monkeypatch.setattr(
        match.ynab_client,
        "list_transactions",
        lambda access_token, budget_id, since_date=None, unapproved_only=False: transactions,
    )


def _saved_results(tmp_path) -> dict:
    return {r["id"]: r for r in json.loads((tmp_path / "amazon.com_ynab_matched.json").read_text())["transactions"]}


def test_match_transactions_for_plugin_writes_results_file(monkeypatch, tmp_path):
    matched_txn = FakeTransaction("t1", datetime.date(2026, 1, 10), -5000, account_id="acct-1")
    unmatched_txn = FakeTransaction("t2", datetime.date(2026, 1, 11), -7000, account_id="acct-1")
    other_account_txn = FakeTransaction("t3", datetime.date(2026, 1, 10), -1000, account_id="acct-2")
    _stub_ynab(monkeypatch, tmp_path, [matched_txn, unmatched_txn, other_account_txn])

    vendor_txn = VendorTransaction(
        date=datetime.date(2026, 1, 10),
        amount=-5000,
        record_id="r1",
        line_items=[VendorLineItem(description="Widget", amount=5000)],
    )
    plugin = FakePlugin(
        prefs={"mode": "account", "account_ids": ["acct-1"], "run_mode": "unapproved"},
        vendor_transactions=[vendor_txn],
    )

    match.match_transactions_for_plugin("token", "budget-1", plugin, vendor_lookback_margin_days=7, window_days=5)

    saved = _saved_results(tmp_path)
    assert set(saved) == {"t1", "t2"}  # other_account_txn excluded by mapping
    assert saved["t1"]["match_status"] == "matched"
    assert saved["t1"]["vendor_match"]["line_items"][0]["description"] == "Widget"
    assert saved["t2"]["match_status"] == "unmatched"
    assert saved["t2"]["match_reason"] == "no record in window"


def test_plugin_error_keeps_results_so_far(monkeypatch, tmp_path, caplog):
    first = FakeTransaction("t1", datetime.date(2026, 1, 10), -5000)
    second = FakeTransaction("t2", datetime.date(2026, 1, 11), -7000)
    _stub_ynab(monkeypatch, tmp_path, [first, second])
    plugin = FakePlugin(
        prefs={"mode": "payee", "payee_name": "Amazon.com", "run_mode": "unapproved"},
        match_error=(-7000, KeyError("grand_total")),
    )

    match.match_transactions_for_plugin("token", "budget-1", plugin, vendor_lookback_margin_days=7, window_days=5)

    assert set(_saved_results(tmp_path)) == {"t1"}
    assert "Failed matching: KeyError('grand_total')" in caplog.text
    assert "Succeeded with matching" not in caplog.text


def test_match_transactions_for_plugin_filters_nonsplit_run_mode(monkeypatch, tmp_path):
    split_txn = FakeTransaction("t1", datetime.date(2026, 1, 10), -5000, subtransactions=[{"amount": -5000}])
    plain_txn = FakeTransaction("t2", datetime.date(2026, 1, 10), -3000, subtransactions=[])
    _stub_ynab(monkeypatch, tmp_path, [split_txn, plain_txn])

    plugin = FakePlugin(prefs={"mode": "payee", "payee_name": "Amazon.com", "run_mode": "nonsplit"})

    match.match_transactions_for_plugin("token", "budget-1", plugin, vendor_lookback_margin_days=7, window_days=0)

    assert set(_saved_results(tmp_path)) == {"t2"}  # split_txn excluded by nonsplit run mode


def test_match_transactions_for_plugin_triggers_scrape_for_scraping_plugins(monkeypatch, tmp_path):
    _stub_ynab(monkeypatch, tmp_path, [FakeTransaction("t1", datetime.date(2026, 1, 10), -5000)])

    plugin = FakePlugin(prefs={"mode": "payee", "payee_name": "Amazon.com", "run_mode": "unapproved"})
    plugin.scrapes_vendor_data = True

    match.match_transactions_for_plugin("token", "budget-1", plugin, vendor_lookback_margin_days=7, window_days=5)

    assert plugin.scrape_calls == [datetime.date(2026, 1, 3)]


def test_run_cycle_for_plugin_with_incomplete_setup_runs_nothing(monkeypatch, caplog):
    def fail(*args, **kwargs):
        raise AssertionError("must not call YNAB")

    monkeypatch.setattr(match.plugin_settings, "needs_setup", lambda plugin: True)
    monkeypatch.setattr(match.ynab_client, "list_categories", fail)
    monkeypatch.setattr(match.ynab_client, "list_transactions", fail)

    match.run_cycle_for_plugin(FakePlugin())

    assert "setup incomplete" in caplog.text


@pytest.mark.parametrize(
    "results_budget, categories_budget, review_statuses, queued_budget, expected",
    [
        (None, "budget-1", ["skipped"], None, False),  # no matched file
        ("other-budget", "budget-1", ["skipped"], None, False),
        ("budget-1", "other-budget", ["skipped"], None, False),
        ("budget-1", "budget-1", ["approved", "approved"], None, False),
        ("budget-1", "budget-1", ["approved", None], None, True),
        ("budget-1", "budget-1", ["approved", "skipped"], None, True),
        ("budget-1", "budget-1", ["approved"], "budget-1", True),  # all approved, push still waiting
        (None, "budget-1", [], "budget-1", True),
        ("budget-1", "budget-1", ["approved"], "other-budget", False),
    ],
)
def test_has_last_results_only_with_this_budgets_unapproved_splits_or_queue(
    monkeypatch, tmp_path, results_budget, categories_budget, review_statuses, queued_budget, expected
):
    monkeypatch.setattr(FakePlugin, "matched_path", tmp_path / "amazon.com_ynab_matched.json")
    monkeypatch.setattr(match, "CATEGORIES_PATH", tmp_path / "ynab_categories.json")
    monkeypatch.setattr(match.user_prefs, "load_user_prefs", lambda: {"ynab_budget_id": "budget-1"})
    # an unsplit transaction (no split_status) is never reviewed, so it never counts
    transactions = [{"id": "unsplit"}] + [
        {"id": f"t{i}", "split_status": "exact", "review_status": status} for i, status in enumerate(review_statuses)
    ]
    if results_budget:  # None: no results file
        FakePlugin.matched_path.write_text(json.dumps({"budget_id": results_budget, "transactions": transactions}))
    match.CATEGORIES_PATH.write_text(json.dumps({"budget_id": categories_budget, "categories": []}))
    plugin = FakePlugin()
    plugin.approved_path = tmp_path / "amazon.com_ynab_approved.json"
    if queued_budget:  # None: no queue file
        plugin.approved_path.write_text(json.dumps([{"id": "q1", "budget_id": queued_budget}]))

    assert match.has_last_results(plugin) is expected


def test_review_last_results_with_only_a_queue_pushes_without_reviewing(monkeypatch, tmp_path):
    # the matched file is another budget's (a later run overwrote it): it must not be reviewed
    monkeypatch.setattr(FakePlugin, "matched_path", tmp_path / "amazon.com_ynab_matched.json")
    monkeypatch.setattr(match, "CATEGORIES_PATH", tmp_path / "ynab_categories.json")
    monkeypatch.setattr(match.user_prefs, "load_user_prefs", lambda: {"ynab_budget_id": "budget-1"})
    monkeypatch.setattr(match.config, "get_ynab_access_token", lambda: "token")
    monkeypatch.setattr(match.review, "review_transactions", lambda *args: pytest.fail("must not review"))
    pushed = []
    monkeypatch.setattr(match.push, "push_approved", lambda path, token, budget_id: pushed.append((path, budget_id)))
    FakePlugin.matched_path.write_text(
        json.dumps({"budget_id": "other-budget", "transactions": [{"id": "t1", "split_status": "exact"}]})
    )
    match.CATEGORIES_PATH.write_text(json.dumps({"budget_id": "other-budget", "categories": []}))
    plugin = FakePlugin()
    plugin.approved_path = tmp_path / "amazon.com_ynab_approved.json"
    plugin.approved_path.write_text(json.dumps([{"id": "q1", "budget_id": "budget-1"}]))

    match.review_last_results(plugin)

    assert pushed == [(plugin.approved_path, "budget-1")]


def test_review_last_results_reviews_the_plugins_matched_file(monkeypatch, tmp_path):
    monkeypatch.setattr(FakePlugin, "matched_path", tmp_path / "amazon.com_ynab_matched.json")
    monkeypatch.setattr(match, "CATEGORIES_PATH", tmp_path / "ynab_categories.json")
    monkeypatch.setattr(match.user_prefs, "load_user_prefs", lambda: {"ynab_budget_id": "budget-1"})
    monkeypatch.setattr(match.categorize, "chosen_ask", lambda: None)
    reviewed, pushed = [], []
    monkeypatch.setattr(match.review, "review_transactions", lambda path, *args: reviewed.append(path))
    monkeypatch.setattr(match.push, "push_approved", lambda path, token, budget_id: pushed.append((path, budget_id)))
    monkeypatch.setattr(match.config, "get_ynab_access_token", lambda: "token")
    monkeypatch.setattr(match.ynab_client, "list_payees", lambda token, budget_id: [])
    FakePlugin.matched_path.write_text(
        json.dumps({"budget_id": "budget-1", "transactions": [{"id": "t1", "split_status": "exact"}]})
    )
    match.CATEGORIES_PATH.write_text(json.dumps({"budget_id": "budget-1", "categories": []}))

    plugin = FakePlugin()
    plugin.categories_path = tmp_path / "amazon.com_categories.json"
    plugin.approved_path = tmp_path / "amazon.com_ynab_approved.json"

    match.review_last_results(plugin)

    assert reviewed == [FakePlugin.matched_path]
    assert pushed == [(plugin.approved_path, "budget-1")]
