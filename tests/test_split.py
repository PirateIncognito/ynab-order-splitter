import dataclasses
import datetime
import json
import logging
from pathlib import Path

import pytest

from core import split

# Anonymized real orders (amounts, dates, signs and shipment structure kept):
# 100-0000001 came in 4 shipments, charged 3 times; 100-0000002 in 3
# shipments, charged once and refunded twice; 100-0000003 is a 2-shipment
# order charged once for its grand total, less than its items and tax;
# 100-0000004 had shipping.
ORDER_DATA = json.loads((Path(__file__).parent / "fixtures" / "amazon_split.json").read_text())


def _vendor_match(module, date: str, amount: int, order_data=ORDER_DATA) -> dict:
    module.ORDER_DATA_PATH.write_text(json.dumps(order_data))
    vendor_transaction = module.AmazonPlugin().get_vendor_transaction(datetime.date.fromisoformat(date), amount)
    return dataclasses.asdict(vendor_transaction)


def _split(module, date: str, amount: int, **prefs) -> dict:
    return split.build_split(amount, _vendor_match(module, date, amount), **prefs)


@pytest.mark.parametrize(
    "date, amount, memos",
    [
        ("2023-11-30", -42780, ["Item 1", "Item 4"]),
        ("2023-12-01", -33970, ["Item 2", "Item 5"]),
        ("2023-12-02", -26740, ["Item 3"]),
        ("2024-03-29", 6130, ["Item 12"]),  # refunds pick their shipment too
        ("2024-03-30", 16040, ["Item 11"]),
    ],
)
def test_charge_for_one_shipment_splits_only_its_items(amazon_module, date, amount, memos):
    result = _split(amazon_module, date, amount)

    assert result["split_status"] == "exact"
    assert [line["memo"] for line in result["split_lines"]] == memos
    assert sum(line["amount"] for line in result["split_lines"]) == amount


def test_uneven_charge_sums_exactly_with_the_transaction_sign(amazon_module):
    """27.58 over items of 14.99, 5.73 and 5.06 leaves 1 milliunit after
    rounding down; it goes to the largest remainder."""
    result = _split(amazon_module, "2024-02-21", -27580)

    assert [line["amount"] for line in result["split_lines"]] == [-16037, -6130, -5413]


def test_shipping_is_spread_over_the_items(amazon_module):
    result = _split(amazon_module, "2025-07-05", -34670)

    assert result["split_status"] == "exact"
    assert [line["memo"] for line in result["split_lines"]] == ["Item 21", "Item 22"]
    assert sum(line["amount"] for line in result["split_lines"]) == -34670


@pytest.mark.parametrize(
    "date, amount, item_amounts, tax_amount",
    [
        ("2024-02-21", -27580, [-14990, -5730, -5060], -1800),
        ("2025-07-05", -34670, [-14011, -18689], -1970),  # shipping stays on the items
        ("2024-03-30", 16040, [14990], 1050),
    ],
)
def test_tax_line_pref_puts_tax_on_its_own_line(amazon_module, date, amount, item_amounts, tax_amount):
    result = _split(amazon_module, date, amount, tax_line=True, tax_category_id="tax-cat")

    *items, tax = result["split_lines"]
    assert [line["amount"] for line in items] == item_amounts
    assert tax == {"amount": tax_amount, "memo": "Sales tax", "category_id": "tax-cat"}


def test_charge_for_the_grand_total_pays_for_the_whole_order(amazon_module):
    """Both shipments' items and tax come to more than the charge (a
    discount), so no shipment fits; the grand total says it is all of them."""
    result = _split(amazon_module, "2022-07-29", -65780)

    assert result["split_status"] == "exact"
    assert [line["memo"] for line in result["split_lines"]] == ["Item 17", "Item 18"]
    assert sum(line["amount"] for line in result["split_lines"]) == -65780


@pytest.mark.parametrize(
    "subtotal, other_charges",
    [
        (96.72, True),  # the scraped grand total is just one of the order's charges
        (38.77, False),  # the only charge, but the order lists items it didn't pay for
    ],
)
def test_grand_total_charge_still_picks_its_shipment_unless_it_paid_for_every_item(
    amazon_module, subtotal, other_charges
):
    order_number = "100-0000001-0000000"
    orders = [
        {**o, "grand_total": 42.78, "subtotal": subtotal} if o["order_number"] == order_number else o
        for o in ORDER_DATA["orders"]
    ]
    charges = [
        c for c in ORDER_DATA["charges"]
        if other_charges or c["order_number"] != order_number or c["completed_date"] == "2023-11-30"
    ]
    order_data = {**ORDER_DATA, "orders": orders, "charges": charges}
    vendor_match = _vendor_match(amazon_module, "2023-11-30", -42780, order_data)

    result = split.build_split(-42780, vendor_match)

    assert result["split_status"] == "exact"
    assert [line["memo"] for line in result["split_lines"]] == ["Item 1", "Item 4"]


def test_no_fitting_shipment_spreads_over_the_whole_order_as_approximate():
    items = [
        {"description": "Item 1", "amount": 29990, "shipment": 0},
        {"description": "Item 2", "amount": 34990, "shipment": 1},
    ]
    result = split.build_split(-50000, {"line_items": items, "sales_tax": 4300})

    assert result["split_status"] == "approximate"
    assert result["split_reason"]
    assert [line["memo"] for line in result["split_lines"]] == ["Item 1", "Item 2"]
    assert sum(line["amount"] for line in result["split_lines"]) == -50000


def _one_shipment_order(*item_amounts: int, tax: int) -> dict:
    items = [{"description": f"Item {n}", "amount": a, "shipment": 0} for n, a in enumerate(item_amounts, 1)]
    return {"line_items": items, "sales_tax": tax}


def test_partial_refund_of_one_shipment_is_approximate():
    """Really the first item (6.24 + 0.44 tax), but nothing says so."""
    result = split.build_split(6680, _one_shipment_order(6240, 5990, tax=860))

    assert result["split_status"] == "approximate"
    assert sum(line["amount"] for line in result["split_lines"]) == 6680


def test_charge_below_the_tax_keeps_item_lines():
    """Tax pref on, 2.53 of a $48 order with $2.88 tax: the tax line is its
    proportional part, not the whole charge."""
    result = split.build_split(-2530, _one_shipment_order(30000, 18000, tax=2880), tax_line=True)

    assert result["split_status"] == "approximate"
    *items, tax = result["split_lines"]
    assert all(line["amount"] < 0 for line in items)
    assert tax["amount"] == -143
    assert sum(line["amount"] for line in result["split_lines"]) == -2530


def test_whole_order_record_is_never_split_by_shipment(amazon_module):
    """Matched as an order (no charge on file), the record pays for every
    shipment, so items worth more than it are a discount, not a part-charge."""
    order_data = {**ORDER_DATA, "charges": []}

    vendor_match = _vendor_match(amazon_module, "2022-07-26", -65780, order_data)

    assert split.build_split(-65780, vendor_match)["split_status"] == "exact"


def test_split_transactions_splits_matched_and_skips_unmatched(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    vendor_match = {"line_items": [{"description": "Widget", "amount": 9000, "shipment": None}], "sales_tax": 1000}
    path = tmp_path / "amazon.com_ynab_matched.json"
    path.write_text(
        json.dumps(
            {
                "transactions": [
                    {"id": "t1", "amount": -10000, "match_status": "ambiguous", "vendor_match": vendor_match},
                    {"id": "t2", "amount": -5000, "match_status": "matched", "vendor_match": {"line_items": []}},
                    {"id": "t3", "amount": -7000, "match_status": "unmatched", "vendor_match": None},
                ]
            }
        )
    )

    split.split_transactions(path, tax_line=False, tax_category_id=None)

    t1, t2, t3 = json.loads(path.read_text())["transactions"]
    assert t1["split_lines"] == [{"amount": -10000, "memo": "Widget", "category_id": None}]
    assert (t2["split_status"], t2["split_lines"]) == ("nothing to split", [])
    assert "split_status" not in t3
    assert "Succeeded with splitting: 1 exact, 0 approximate, 1 nothing to split" in caplog.text
