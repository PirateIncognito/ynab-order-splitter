import datetime
import json
import types
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from core import match


def _write_orders(module, orders: list[dict], charges: list[dict] | None = None) -> None:
    module.ORDER_DATA_PATH.write_text(
        json.dumps({"last_scraped_at": None, "orders": orders, "charges": charges or []})
    )


def _saved(module) -> dict:
    return json.loads(module.ORDER_DATA_PATH.read_text())


class _FakeItem:
    def __init__(self, title, price, quantity=1, asin="B000000000"):
        self.title = title
        self.price = price
        self.quantity = quantity
        self.asin = asin


class _FakeOrder:
    def __init__(self, order_number, order_placed_date, grand_total, estimated_tax=None, items=None, whole_foods=False):
        self.order_number = order_number
        self.is_whole_foods = whole_foods
        self.order_placed_date = order_placed_date
        self.grand_total = grand_total
        self.estimated_tax = estimated_tax
        self.items = items or []
        self.shipments = []
        self.cancelled = False
        for field in (
            "subtotal shipping_total free_shipping promotion_applied coupon_savings "
            "subscription_discount multibuy_discount amazon_discount reward_points gift_card "
            "gift_wrap total_before_tax refund_total payment_method payment_method_last_4"
        ).split():
            setattr(self, field, None)


class _FakeCharge:
    def __init__(self, order_number, completed_date, grand_total, payment_method="Visa ****1234", text=None):
        self.order_number = order_number
        self.order_details_link = None
        text = text or f"{payment_method} Order #{order_number} Amazon.com ${abs(grand_total):.2f}"
        self.parsed = types.SimpleNamespace(get_text=lambda *args, **kwargs: text)
        self.completed_date = completed_date
        self.grand_total = grand_total
        self.is_refund = grand_total > 0
        self.payment_method = payment_method
        self.payment_method_last_4 = payment_method[-4:]
        self.seller = "Amazon.com"


class _FakeAmazonSession:
    """Records login/logout calls; `saved_session` controls whether a
    persisted session is reported as present."""

    def __init__(self, saved_session=False):
        self.saved_session = saved_session
        self.is_authenticated = False
        self.calls = []

    def auth_cookies_stored(self):
        return self.saved_session

    def login(self):
        self.calls.append("login")
        self.is_authenticated = True

    def logout(self):
        self.calls.append("logout")


def _fake_amazon_transactions_cls(charges: list, calls: list | None = None):
    class _FakeAmazonTransactions:
        def __init__(self, session):
            pass

        def get_transactions(self, days=365):
            if calls is not None:
                calls.append(days)
            return charges

    return _FakeAmazonTransactions


TODAY = datetime.date(2026, 8, 25)


def _freeze_today(module, monkeypatch, today):
    class _Date(datetime.date):
        @classmethod
        def today(cls):
            return today

    class _Datetime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.datetime.combine(today, datetime.time(12)).astimezone(tz)

    monkeypatch.setattr(
        module, "datetime", types.SimpleNamespace(date=_Date, datetime=_Datetime, timezone=datetime.timezone)
    )


def _stub_scrape(module, monkeypatch, session, orders_by_filter, charges=None, today=TODAY):
    """Wire up fakes for a get_data_from_vendor() call on `today`; returns
    the list the fake AmazonOrders records each requested time_filter into."""
    _freeze_today(module, monkeypatch, today)
    monkeypatch.setattr(module.AmazonPlugin, "_credentials", lambda self: ("user", "pass", None))
    monkeypatch.setattr(module, "AmazonSession", lambda **kwargs: session)
    monkeypatch.setattr(module, "AmazonTransactions", _fake_amazon_transactions_cls(charges or []))
    calls = []
    monkeypatch.setattr(module, "AmazonOrders", _fake_amazon_orders_cls(orders_by_filter, calls))
    return calls


# Orders per Amazon order history page.
PAGE_SIZE = 10


def _fake_amazon_orders_cls(orders_by_filter: dict, calls: list, start_indexes: list | None = None):
    """Build a fake AmazonOrders class serving canned orders for each
    time_filter one page (keep_paging=False) at a time, like amazon-orders:
    a start_index past the last order gets an empty page. Records each
    time_filter into `calls` once (at its first page), and every
    start_index requested into `start_indexes`."""

    class _FakeAmazonOrders:
        def __init__(self, session):
            pass

        def get_order_history(self, time_filter=None, full_details=True, keep_paging=True, start_index=None):
            assert not keep_paging
            start_index = start_index or 0
            if start_index == 0:
                calls.append(time_filter)
            if start_indexes is not None:
                start_indexes.append(start_index)
            return orders_by_filter.get(time_filter, [])[start_index : start_index + PAGE_SIZE]

    return _FakeAmazonOrders


def test_get_data_from_json_matches_date_and_amount(amazon_module):
    _write_orders(
        amazon_module,
        [
            {
                "order_number": "111-1111111-1111111",
                "order_placed_date": "2026-08-01",
                "grand_total": 42.50,
                "estimated_tax": 3.50,
                "items": [{"title": "Widget", "price": 39.00, "quantity": 1, "asin": "B000000000"}],
            },
            {
                "order_number": "222-2222222-2222222",
                "order_placed_date": "2026-08-02",
                "grand_total": 10.00,
                "estimated_tax": None,
                "items": [{"title": "Gadget", "price": 10.00, "quantity": 1, "asin": "B000000001"}],
            },
        ],
    )

    matches = amazon_module.AmazonPlugin().get_data_from_JSON(
        datetime.date(2026, 8, 1), -42500
    )

    assert len(matches) == 1
    assert matches[0]["order_number"] == "111-1111111-1111111"


def test_get_data_from_json_no_match_returns_empty(amazon_module):
    _write_orders(amazon_module, [])
    assert amazon_module.AmazonPlugin().get_data_from_JSON(datetime.date(2026, 8, 1), 42500) == []


def test_get_vendor_transaction_builds_line_items(amazon_module):
    _write_orders(
        amazon_module,
        [
            {
                "order_number": "111-1111111-1111111",
                "order_placed_date": "2026-08-01",
                "grand_total": 42.50,
                "estimated_tax": 3.50,
                "items": [
                    {"title": "Widget", "price": 39.00, "quantity": 1, "asin": "B000000000"},
                ],
            },
        ],
    )

    vendor_transaction = amazon_module.AmazonPlugin().get_vendor_transaction(
        datetime.date(2026, 8, 1), -42500
    )

    assert vendor_transaction is not None
    assert vendor_transaction.amount == -42500
    assert vendor_transaction.sales_tax == 3500
    assert vendor_transaction.source_id == "111-1111111-1111111"
    assert vendor_transaction.link == "https://www.amazon.com/gp/your-account/order-details?orderID=111-1111111-1111111"
    assert len(vendor_transaction.line_items) == 1
    assert vendor_transaction.line_items[0].description == "Widget"
    assert vendor_transaction.line_items[0].amount == 39000


def test_get_vendor_transaction_no_match_returns_none(amazon_module):
    _write_orders(amazon_module, [])
    assert amazon_module.AmazonPlugin().get_vendor_transaction(datetime.date(2026, 8, 1), 42500) is None


def test_get_data_from_vendor_merges_results_across_multiple_filter_requests(amazon_module, monkeypatch):
    order_2025 = _FakeOrder("111-1111111", datetime.date(2025, 12, 20), 20.00, items=[_FakeItem("Old Widget", 20.00)])
    order_2026 = _FakeOrder("222-2222222", datetime.date(2026, 1, 15), 30.00, items=[_FakeItem("New Widget", 30.00)])
    calls = _stub_scrape(
        amazon_module,
        monkeypatch,
        _FakeAmazonSession(),
        {"year-2025": [order_2025], "year-2026": [order_2026]},
    )

    amazon_module.AmazonPlugin().get_data_from_vendor(datetime.date(2025, 10, 1))

    assert calls == ["year-2026", "year-2025"]
    assert {o["order_number"] for o in _saved(amazon_module)["orders"]} == {"111-1111111", "222-2222222"}


def test_get_data_from_vendor_appends_to_existing_orders_from_prior_calls(amazon_module, monkeypatch):
    _write_orders(
        amazon_module,
        [
            {
                "order_number": "000-0000000",
                "order_placed_date": "2024-05-01",
                "grand_total": 5.00,
                "estimated_tax": None,
                "items": [],
            }
        ],
    )
    new_order = _FakeOrder("111-1111111", datetime.date(2026, 8, 20), 10.00, items=[])
    _stub_scrape(amazon_module, monkeypatch, _FakeAmazonSession(), {"year-2026": [new_order]})

    amazon_module.AmazonPlugin().get_data_from_vendor()

    saved = _saved(amazon_module)
    assert {o["order_number"] for o in saved["orders"]} == {"000-0000000", "111-1111111"}
    assert saved["last_scraped_at"] is not None


def test_get_data_from_vendor_reuses_saved_session_without_logging_in(amazon_module, monkeypatch):
    session = _FakeAmazonSession(saved_session=True)
    _stub_scrape(amazon_module, monkeypatch, session, {})

    amazon_module.AmazonPlugin().get_data_from_vendor()

    assert session.is_authenticated
    assert session.calls == []


def test_get_data_from_vendor_logs_in_without_saved_session_and_never_logs_out(amazon_module, monkeypatch):
    session = _FakeAmazonSession(saved_session=False)
    _stub_scrape(amazon_module, monkeypatch, session, {})

    amazon_module.AmazonPlugin().get_data_from_vendor()

    assert session.calls == ["login"]


def test_get_data_from_vendor_logs_in_again_when_saved_session_is_rejected(amazon_module, monkeypatch):
    session = _FakeAmazonSession(saved_session=True)
    _stub_scrape(amazon_module, monkeypatch, session, {})
    attempts = []

    class _ExpiringAmazonOrders:
        def __init__(self, session):
            pass

        def get_order_history(self, time_filter=None, full_details=True, keep_paging=True, start_index=None):
            if start_index:
                return []
            attempts.append(time_filter)
            if len(attempts) == 1:
                raise amazon_module.AmazonOrdersAuthRedirectError("redirected to login")
            return [_FakeOrder("111-1111111", datetime.date(2026, 8, 20), 10.00)]

    monkeypatch.setattr(amazon_module, "AmazonOrders", _ExpiringAmazonOrders)

    amazon_module.AmazonPlugin().get_data_from_vendor()

    assert session.calls == ["login"]
    assert attempts == ["year-2026", "year-2026"]
    assert [o["order_number"] for o in _saved(amazon_module)["orders"]] == ["111-1111111"]


def test_get_data_from_vendor_saves_and_dedupes_charges(amazon_module, monkeypatch):
    existing = {
        "order_number": "111-1111111",
        "completed_date": "2026-08-20",
        "grand_total": -10.00,
        "is_refund": False,
        "payment_method": "Visa ****1234",
        "payment_method_last_4": "1234",
        "seller": "Amazon.com",
    }
    _write_orders(amazon_module, [], [existing])
    charges = [
        # Same charge scraped again -- must not duplicate.
        _FakeCharge("111-1111111", datetime.date(2026, 8, 20), -10.00),
        # Second shipment of the same order, charged separately.
        _FakeCharge("111-1111111", datetime.date(2026, 8, 22), -5.00),
    ]
    _stub_scrape(amazon_module, monkeypatch, _FakeAmazonSession(True), {}, charges)

    amazon_module.AmazonPlugin().get_data_from_vendor(datetime.date(2026, 8, 1))

    saved_charges = _saved(amazon_module)["charges"]
    assert sorted((c["completed_date"], c["grand_total"]) for c in saved_charges) == [
        ("2026-08-20", -10.00),
        ("2026-08-22", -5.00),
    ]


def test_get_data_from_vendor_first_run_requests_charges_covering_lookback(amazon_module, monkeypatch):
    _stub_scrape(amazon_module, monkeypatch, _FakeAmazonSession(True), {})
    days_requested = []
    monkeypatch.setattr(amazon_module, "AmazonTransactions", _fake_amazon_transactions_cls([], days_requested))

    amazon_module.AmazonPlugin().get_data_from_vendor(TODAY - datetime.timedelta(days=10))

    assert days_requested == [11]


def _scrape(module, monkeypatch, start, today=TODAY, orders_by_filter=None):
    """Run get_data_from_vendor(start) on `today`; returns (time_filters
    requested, charge days requested)."""
    calls = _stub_scrape(module, monkeypatch, _FakeAmazonSession(True), orders_by_filter or {}, today=today)
    days_requested = []
    monkeypatch.setattr(module, "AmazonTransactions", _fake_amazon_transactions_cls([], days_requested))
    module.AmazonPlugin().get_data_from_vendor(start)
    return calls, days_requested


def test_immediate_rerun_fetches_only_new_data(amazon_module, monkeypatch):
    start = datetime.date(2024, 3, 1)
    recent = {"year-2026": [_FakeOrder("111-1111111", TODAY - datetime.timedelta(days=3), 10.00)]}
    calls, _ = _scrape(amazon_module, monkeypatch, start, orders_by_filter=recent)
    assert calls == ["year-2026", "year-2025", "year-2024"]

    calls, days_requested = _scrape(amazon_module, monkeypatch, start)

    assert calls == ["last30"]
    assert days_requested == [1]  # the previous scrape's day through today


def test_earlier_start_fetches_only_new_past_years_and_charges_from_it(amazon_module, monkeypatch):
    recent = {"year-2026": [_FakeOrder("111-1111111", TODAY, 10.00)]}
    _scrape(amazon_module, monkeypatch, datetime.date(2025, 3, 1), orders_by_filter=recent)

    start = datetime.date(2023, 6, 1)
    calls, days_requested = _scrape(amazon_module, monkeypatch, start)

    assert calls == ["last30", "year-2024", "year-2023"]
    assert days_requested == [(TODAY - start).days + 1]


def test_failed_year_keeps_newer_years_and_they_are_skipped_next_run(amazon_module, monkeypatch):
    _stub_scrape(amazon_module, monkeypatch, _FakeAmazonSession(True), {})

    class _FailingIn2024:
        def __init__(self, session):
            pass

        def get_order_history(self, time_filter=None, full_details=True, keep_paging=True, start_index=None):
            if time_filter == "year-2024":
                raise RuntimeError("Amazon is down")
            year = int(time_filter.removeprefix("year-"))
            return [] if start_index else [_FakeOrder(f"111-{year}", datetime.date(year, 5, 1), 20.00)]

    monkeypatch.setattr(amazon_module, "AmazonOrders", _FailingIn2024)

    with pytest.raises(RuntimeError):
        amazon_module.AmazonPlugin().get_data_from_vendor(datetime.date(2023, 1, 1))

    saved = _saved(amazon_module)
    assert saved["scraped_years"] == [2025]
    assert sorted(o["order_number"] for o in saved["orders"]) == ["111-2025", "111-2026"]
    assert saved["last_scraped_at"] is None

    calls, _ = _scrape(amazon_module, monkeypatch, datetime.date(2023, 1, 1))
    assert calls == ["year-2026", "year-2024", "year-2023"]


# Scraped just now: the filter must follow the orders, not the scrape time.
SCRAPED_NOW = datetime.datetime.combine(TODAY, datetime.time(12)).astimezone().isoformat()


@pytest.mark.parametrize(
    ("days_ago", "time_filter"), [(29, "last30"), (30, "months-3"), (89, "months-3"), (90, "year-2026")]
)
def test_current_year_filter_depends_on_age_of_newest_order(amazon_module, monkeypatch, days_ago, time_filter):
    orders = [
        {"order_number": "111", "order_placed_date": (TODAY - datetime.timedelta(days=200)).isoformat()},
        {"order_number": "222", "order_placed_date": (TODAY - datetime.timedelta(days=days_ago)).isoformat()},
        {"order_number": "333", "order_placed_date": None},
    ]
    amazon_module.ORDER_DATA_PATH.write_text(json.dumps({"last_scraped_at": SCRAPED_NOW, "orders": orders}))

    assert _scrape(amazon_module, monkeypatch, TODAY)[0] == [time_filter]


def test_current_year_is_fetched_whole_when_no_orders_on_file(amazon_module, monkeypatch):
    amazon_module.ORDER_DATA_PATH.write_text(json.dumps({"last_scraped_at": SCRAPED_NOW, "orders": []}))

    assert _scrape(amazon_module, monkeypatch, TODAY)[0] == ["year-2026"]


def test_new_year_fetches_the_finished_year_in_full(amazon_module, monkeypatch):
    start = datetime.date(2025, 6, 1)
    recent = {"year-2025": [_FakeOrder("111-1111111", datetime.date(2025, 12, 18), 10.00)]}
    _scrape(amazon_module, monkeypatch, start, today=datetime.date(2025, 12, 20), orders_by_filter=recent)

    calls, days_requested = _scrape(amazon_module, monkeypatch, start, today=datetime.date(2026, 1, 5))

    assert calls == ["last30", "year-2025"]
    assert days_requested == [17]  # Dec 20 through Jan 5
    assert _saved(amazon_module)["scraped_years"] == [2025]


@pytest.mark.parametrize(
    ("order_count", "expected_start_indexes"),
    [(0, [0]), (7, [0, 7]), (20, [0, 10, 20]), (25, [0, 10, 20, 25])],
)
def test_year_is_paged_to_the_end_without_skipping_or_repeating(
    amazon_module, monkeypatch, order_count, expected_start_indexes
):
    orders = [_FakeOrder(f"111-{i:07}", datetime.date(2025, 3, 1), 1.00) for i in range(order_count)]
    _stub_scrape(amazon_module, monkeypatch, _FakeAmazonSession(True), {})
    start_indexes = []
    monkeypatch.setattr(
        amazon_module, "AmazonOrders", _fake_amazon_orders_cls({"year-2025": orders}, [], start_indexes)
    )

    amazon_module.AmazonPlugin().get_data_from_vendor(datetime.date(2025, 1, 1))

    # 2026's single empty page comes first, then 2025 is paged.
    assert start_indexes == [0] + expected_start_indexes
    assert sorted(o["order_number"] for o in _saved(amazon_module)["orders"]) == [o.order_number for o in orders]


def test_load_order_data_reads_legacy_bare_order_list(amazon_module):
    amazon_module.ORDER_DATA_PATH.write_text(json.dumps([{"order_number": "111"}]))

    data = amazon_module.AmazonPlugin()._load_order_data()

    assert data["orders"] == [{"order_number": "111"}]
    assert data["charges"] == []


def test_configure_secrets_discards_saved_session(amazon_module, monkeypatch, tmp_path):
    monkeypatch.setattr(amazon_module, "ENV_PATH", tmp_path / ".env")
    amazon_module.COOKIE_JAR_PATH.write_text("{}")
    monkeypatch.setattr(amazon_module.ui, "text", lambda *a, **k: "me@example.com")
    secrets = iter(["hunter2", ""])
    monkeypatch.setattr(amazon_module.ui, "secret", lambda *a, **k: next(secrets))

    amazon_module.AmazonPlugin().configure_secrets()

    assert not amazon_module.COOKIE_JAR_PATH.exists()


def test_charge_without_order_number_is_stored_as_none(amazon_module):
    charge = amazon_module._charge_to_dict(_FakeCharge("", datetime.date(2026, 8, 10), -4.99))
    assert charge["order_number"] is None


RECEIPTS = [f"00000000-0000-0000-0000-00000000000{n}" for n in range(6)]


def _whole_foods_charge(receipt, completed_date, grand_total):
    text = f"Visa ****1234 Order #{receipt} Whole Foods SPP 00000 -${abs(grand_total):.2f}"
    return _FakeCharge("", completed_date, grand_total, text=text)


def test_whole_foods_charges_link_to_their_orders_and_match_once(amazon_module, monkeypatch):
    def aug(day):
        return datetime.date(2026, 8, day)

    orders = [  # newest first, like order history
        _FakeOrder("113-0000009", aug(20), None, whole_foods=True),  # cancelled: no grand total
        _FakeOrder("113-0000005", aug(19), 31.00, whole_foods=True),  # in the window, another amount
        _FakeOrder("113-0000001", aug(19), 50.00, whole_foods=True),
        _FakeOrder("113-0000002", aug(17), 50.00, whole_foods=True),
        _FakeOrder("113-0000006", aug(20), 40.00, whole_foods=True),
        _FakeOrder("113-0000007", aug(17), 40.00, whole_foods=True),
        _FakeOrder("113-0000003", aug(15), 30.00, whole_foods=True),  # 5 days before
        _FakeOrder("113-0000004", aug(22), 20.00, whole_foods=True),  # the day after
    ]
    charges = [
        # Aug 20 could take either $50 order, Aug 17 only the Aug 17 one.
        _whole_foods_charge(RECEIPTS[0], aug(20), -50.00),
        _whole_foods_charge(RECEIPTS[1], aug(17), -50.00),
        _whole_foods_charge(RECEIPTS[2], aug(20), -30.00),
        _whole_foods_charge(RECEIPTS[3], aug(21), -20.00),
        # Aug 20 must take the Aug 17 order (exactly 3 days), so Aug 21 gets one too.
        _whole_foods_charge(RECEIPTS[4], aug(20), -40.00),
        _whole_foods_charge(RECEIPTS[5], aug(21), -40.00),
    ]
    plugin = amazon_module.AmazonPlugin()
    saved = []
    for _ in range(2):  # a re-scrape changes nothing
        _stub_scrape(amazon_module, monkeypatch, _FakeAmazonSession(True), {"year-2026": orders}, charges)
        plugin.get_data_from_vendor(aug(1))
        saved.append(sorted((c["receipt"], c["order_number"]) for c in _saved(amazon_module)["charges"]))

    assert saved[0] == saved[1] == [
        (RECEIPTS[0], "113-0000001"),
        (RECEIPTS[1], "113-0000002"),
        (RECEIPTS[2], RECEIPTS[2]),
        (RECEIPTS[3], RECEIPTS[3]),
        (RECEIPTS[4], "113-0000007"),
        (RECEIPTS[5], "113-0000006"),
    ]
    # The orders' charges stand for them: a transaction matches the charge only.
    assert plugin.get_data_from_JSON(aug(19), -50000) == []
    assert len(plugin.get_data_from_JSON(aug(17), -50000)) == 1
    assert plugin.get_vendor_transaction(aug(17), -50000).link.endswith("orderID=113-0000002")
    # A Whole Foods charge with no order on file isn't split either.
    unlinked = plugin.get_vendor_transaction(aug(20), -30000)
    assert [(i.description, i.amount) for i in unlinked.line_items] == [("Whole Foods Market", 30000)]


@pytest.mark.parametrize("charge, link", [
    (_FakeCharge("112-0000000-0000001", TODAY, -10.00),
     "https://www.amazon.com/gp/your-account/order-details?orderID=112-0000000-0000001"),
    (_FakeCharge("D01-0000000-0000002", TODAY, -10.00),
     "https://www.amazon.com/gp/your-account/order-details?orderID=D01-0000000-0000002"),
    (_FakeCharge("P01-0000000-0000003", TODAY, -10.00),
     "https://payments.amazon.com/jr/your-account/orders/P01-0000000-0000003"),
    (_whole_foods_charge(RECEIPTS[0], TODAY, -10.00), None),  # a receipt has no page
    (_FakeCharge("", TODAY, -10.00), None),  # a subscription: no order
])
def test_a_charge_links_to_its_order_page(amazon_module, charge, link):
    _write_orders(amazon_module, [], [amazon_module._charge_to_dict(charge)])

    assert amazon_module.AmazonPlugin().get_vendor_transaction(TODAY, -10000).link == link


def test_whole_foods_order_is_one_line_without_tax(amazon_module):
    order = {
        "order_number": "113-0000001",
        "order_placed_date": "2026-08-01",
        "grand_total": 42.50,
        "subtotal": 39.00,
        "estimated_tax": 3.50,
        "is_whole_foods": True,
        "items": [{"title": "Bananas", "price": 9.00, "quantity": 1}, {"title": "Bread", "price": 30.0, "quantity": 1}],
    }
    _write_orders(amazon_module, [order])

    vendor_transaction = amazon_module.AmazonPlugin().get_vendor_transaction(datetime.date(2026, 8, 1), -42500)

    assert [(i.description, i.amount, i.shipment) for i in vendor_transaction.line_items] == [
        ("Whole Foods Market", 42500, None)
    ]
    assert vendor_transaction.sales_tax is None


def test_missing_orders_are_looked_up_once_and_failures_retried(amazon_module, monkeypatch):
    lookups = []

    class _AmazonOrders:
        def __init__(self, session):
            pass

        def get_order_history(self, **kwargs):
            return []

        def get_order(self, order_id):
            lookups.append(order_id)
            if order_id == "111-0000009":
                raise RuntimeError("Amazon is down")
            return _FakeOrder(order_id, TODAY, 11.99, items=[_FakeItem("Ebook", 11.99)])

    charges = [
        _FakeCharge("D01-0000001-0000001", TODAY, -11.99),
        _FakeCharge("111-0000009", TODAY, -5.00),
    ]
    for _ in range(2):
        _stub_scrape(amazon_module, monkeypatch, _FakeAmazonSession(True), {}, charges)
        monkeypatch.setattr(amazon_module, "AmazonOrders", _AmazonOrders)
        amazon_module.AmazonPlugin().get_data_from_vendor()

    assert lookups == ["D01-0000001-0000001", "111-0000009", "111-0000009"]
    saved = _saved(amazon_module)
    assert saved["last_scraped_at"] is not None
    assert [o["items"][0]["title"] for o in saved["orders"]] == ["Ebook"]


def test_amazon_pay_page_becomes_an_order_with_the_merchant_as_its_item(amazon_module, monkeypatch):
    page = "<html><body><h1>Your EXAMPLE NEWS purchase details</h1><p>Total paid: $1,039.99</p></body></html>"
    session = _FakeAmazonSession(True)
    urls = []
    session.get = lambda url: urls.append(url) or types.SimpleNamespace(parsed=BeautifulSoup(page, "html.parser"))
    charge = _FakeCharge("P01-0000001-0000001", TODAY, -1039.99)
    charge.order_details_link = "https://payments.amazon.com/jr/your-account/orders/P01-0000001-0000001"
    _stub_scrape(amazon_module, monkeypatch, session, {}, [charge])

    plugin = amazon_module.AmazonPlugin()
    plugin.get_data_from_vendor()

    assert urls == [charge.order_details_link]
    vendor_transaction = plugin.get_vendor_transaction(TODAY, -1039990)
    assert [(i.description, i.amount) for i in vendor_transaction.line_items] == [
        ("Amazon Pay: EXAMPLE NEWS", 1039990)
    ]


def test_pending_charge_is_replaced_by_its_completed_version(amazon_module, monkeypatch):
    pending = _FakeCharge("111-0000001", datetime.date(2026, 8, 20), -10.00, text="Pending Visa ****1234 -$10.00")
    _stub_scrape(amazon_module, monkeypatch, _FakeAmazonSession(True), {}, [pending])
    amazon_module.AmazonPlugin().get_data_from_vendor(datetime.date(2026, 8, 1))

    completed = _FakeCharge("111-0000001", datetime.date(2026, 8, 22), -10.00)
    _stub_scrape(amazon_module, monkeypatch, _FakeAmazonSession(True), {}, today=datetime.date(2026, 8, 28))
    days_requested = []
    monkeypatch.setattr(amazon_module, "AmazonTransactions", _fake_amazon_transactions_cls([completed], days_requested))
    amazon_module.AmazonPlugin().get_data_from_vendor(datetime.date(2026, 8, 1))

    assert days_requested == [9]  # from the pending charge (Aug 20), not the last scrape (Aug 25)
    assert [(c["completed_date"], c["pending"]) for c in _saved(amazon_module)["charges"]] == [("2026-08-22", False)]


def test_looked_up_orders_do_not_narrow_the_order_history_window(amazon_module, monkeypatch):
    orders = [
        {"order_number": "111-0000001", "order_placed_date": (TODAY - datetime.timedelta(days=60)).isoformat()},
        {"order_number": "D01-0000001-0000001", "order_placed_date": (TODAY - datetime.timedelta(days=5)).isoformat()},
    ]
    amazon_module.ORDER_DATA_PATH.write_text(json.dumps({"last_scraped_at": SCRAPED_NOW, "orders": orders}))

    assert _scrape(amazon_module, monkeypatch, TODAY)[0] == ["months-3"]


# Anonymized real cases (dates, amounts, signs and charge/shipment structure
# kept), with every same-amount record and YNAB transaction near them.
MATCH_FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "amazon_match.json").read_text())


def _match_fixture(module, transactions=None) -> dict:
    """Match `transactions` (default: all the fixture's, in order) against
    the fixture's order data in one run; results keyed by case."""
    module.ORDER_DATA_PATH.write_text(json.dumps(MATCH_FIXTURE["order_data"]))
    plugin = module.AmazonPlugin()
    claimed = set()
    return {
        t["case"]: match.match_transaction(
            plugin, types.SimpleNamespace(var_date=datetime.date.fromisoformat(t["date"]), amount=t["amount"]), 5, claimed
        )
        for t in transactions or MATCH_FIXTURE["ynab_transactions"]
    }


def _fixture_transaction(case: str, days: int = 0, sign: int = 1) -> dict:
    t = next(t for t in MATCH_FIXTURE["ynab_transactions"] if t["case"] == case)
    date = datetime.date.fromisoformat(t["date"]) + datetime.timedelta(days=days)
    return {**t, "date": date.isoformat(), "amount": t["amount"] * sign}


def test_real_cases_match_as_on_the_full_data(amazon_module):
    results = _match_fixture(amazon_module)

    assert {case: (r["match_status"], r["match_reason"]) for case, r in results.items()} == {
        "nothing in window": ("unmatched", "no record in window"),
        "lag 0": ("matched", None),
        "same-amount orders 1": ("ambiguous", "3 records fit; took the closest date"),
        "same-amount orders 2": ("ambiguous", "2 records fit; took the closest date"),
        "same-amount orders 3": ("matched", None),
        "refund": ("matched", None),
        "charge 5 days after": ("matched", None),
        "multi-shipment charge 1": ("matched", None),
        "multi-shipment charge 2": ("matched", None),
        "order 5 days before": ("matched", None),
    }


def test_same_amount_orders_are_each_matched_once(amazon_module):
    results = _match_fixture(amazon_module)

    record_ids = [results[f"same-amount orders {n}"]["vendor_match"]["record_id"] for n in (1, 2, 3)]
    assert len(set(record_ids)) == 3


def test_multi_shipment_charges_match_their_one_order(amazon_module):
    results = _match_fixture(amazon_module)

    first, second = (results[f"multi-shipment charge {n}"]["vendor_match"] for n in (1, 2))
    assert first["record_id"] != second["record_id"]
    assert first["source_id"] == second["source_id"] == "100-0000002-0000000"


def test_charge_is_used_instead_of_its_closer_order_date(amazon_module):
    """The order was placed 3 days before the YNAB date, its charge came 5
    days after: only the charge fits, so the match isn't ambiguous."""
    result = _match_fixture(amazon_module, [_fixture_transaction("charge 5 days after")])["charge 5 days after"]

    assert result["match_status"] == "matched"
    assert result["vendor_match"]["date"] == "2020-08-08"


@pytest.mark.parametrize(
    "case, expected_reason",
    [("refund", "no record in window"), ("same-amount orders 3", "no refund in window")],
)
def test_opposite_sign_never_matches(amazon_module, case, expected_reason):
    result = _match_fixture(amazon_module, [_fixture_transaction(case, sign=-1)])[case]

    assert result["match_status"] == "unmatched"
    assert result["match_reason"] == expected_reason


@pytest.mark.parametrize(
    "case, days, status",
    [
        ("order 5 days before", 0, "matched"),
        ("order 5 days before", 1, "unmatched"),  # order 6 days before
        ("charge 5 days after", 0, "matched"),
        ("charge 5 days after", -1, "unmatched"),  # charge 6 days after
    ],
)
def test_window_is_five_days_either_side(amazon_module, case, days, status):
    result = _match_fixture(amazon_module, [_fixture_transaction(case, days=days)])[case]

    assert result["match_status"] == status
