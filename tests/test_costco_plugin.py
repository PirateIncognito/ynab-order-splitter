import datetime
import importlib.util
import json
import logging
import shutil
from pathlib import Path

import pytest

from plugins.plugin import VendorLineItem

PLUGIN_PATH = Path(__file__).resolve().parent.parent / "plugins" / "costco.com" / "plugin.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "costco"
MARCH_2 = datetime.date(2026, 3, 2)


@pytest.fixture
def costco(tmp_path, monkeypatch):
    """The Costco plugin module, loaded fresh, reading exports from tmp_path."""
    spec = importlib.util.spec_from_file_location("costco_com_plugin_test", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "PLUGIN_DIR", tmp_path)
    monkeypatch.setattr(module, "ORDER_DATA_PATH", tmp_path / "costco.com_order_data.json")
    return module


def _export(module, fixture: str, dates: str) -> Path:
    return shutil.copy(FIXTURES / fixture, module.PLUGIN_DIR / f"costco-receipts-{dates}.json")


@pytest.mark.parametrize(
    "older, newer",
    [
        # the newer export starts earlier and sorts first by name: only its TO date says it's newer
        ("2026-01-01_to_2026-03-31", "2025-12-01_to_04_30_2026"),
        ("01_01_2026_to_03_31_2026", "01_01_2025_to_2026-04-30"),
    ],
)
def test_receipt_in_the_export_with_the_later_to_date_wins(costco, older, newer):
    _export(costco, "receipts_a.json", older)
    _export(costco, "receipts_b.json", newer)
    plugin = costco.CostcoPlugin()

    assert plugin.get_vendor_transaction(MARCH_2, -10780).record_id == "00000000000000000000001"
    assert plugin.get_vendor_transaction(MARCH_2, -11320) is None
    barcodes = [r["transactionBarcode"] for r in json.loads(costco.ORDER_DATA_PATH.read_text())]
    assert sorted(barcodes) == [f"0000000000000000000000{n}" for n in range(1, 8)]


def test_merging_twice_changes_nothing_and_leaves_exports_alone(costco):
    exports = [_export(costco, "receipts_a.json", "2026-01-01_to_2026-03-31"),
               _export(costco, "receipts_b.json", "2026-03-01_to_04_30_2026")]
    modes = [e.stat().st_mode for e in exports]

    costco.CostcoPlugin().get_data_from_JSON(MARCH_2, -10780)
    merged = costco.ORDER_DATA_PATH.read_bytes()
    costco.CostcoPlugin().get_data_from_JSON(MARCH_2, -10780)

    assert costco.ORDER_DATA_PATH.read_bytes() == merged
    assert costco.ORDER_DATA_PATH.stat().st_mode & 0o777 == 0o600
    fixtures = [FIXTURES / "receipts_a.json", FIXTURES / "receipts_b.json"]
    assert [e.read_bytes() for e in exports] == [f.read_bytes() for f in fixtures]
    assert [e.stat().st_mode for e in exports] == modes


def test_malformed_export_is_skipped_and_the_others_still_merge(costco, caplog):
    # the malformed export comes first, so a loop that stops at it loses the good one
    (costco.PLUGIN_DIR / "costco-receipts-2025-01-01_to_2025-12-31.json").write_text('[{"transactionBarcode": ')
    _export(costco, "receipts_a.json", "2026-01-01_to_2026-03-31")

    assert costco.CostcoPlugin().get_vendor_transaction(MARCH_2, -11320) is not None
    assert "costco-receipts-2025-01-01_to_2025-12-31.json" in caplog.text


def test_merged_copy_survives_when_no_export_parses(costco):
    shutil.copy(FIXTURES / "receipts_a.json", costco.ORDER_DATA_PATH)
    (costco.PLUGIN_DIR / "costco-receipts-2026-04-01_to_2026-04-30.json").write_text("not json")

    assert costco.CostcoPlugin().get_vendor_transaction(MARCH_2, -11320) is not None
    assert costco.ORDER_DATA_PATH.read_bytes() == (FIXTURES / "receipts_a.json").read_bytes()


def test_purchase_matches_only_an_outflow_and_return_only_an_inflow(costco):
    _export(costco, "receipts_a.json", "2026-01-01_to_2026-03-31")
    plugin = costco.CostcoPlugin()

    assert plugin.get_vendor_transaction(MARCH_2, -11320).record_id == "00000000000000000000001"
    assert plugin.get_vendor_transaction(MARCH_2, 11320) is None
    # booked as "Sales", but its total is negative: a return
    refund = plugin.get_vendor_transaction(MARCH_2, 21590)
    assert refund.record_id == "00000000000000000000002"
    assert refund.line_items == [VendorLineItem("BATH TOWELS", 19990, 1)]
    assert refund.sales_tax == 1600
    assert plugin.get_vendor_transaction(MARCH_2, -21590) is None
    # each fails one criterion only
    assert plugin.get_vendor_transaction(datetime.date(2026, 3, 3), -11320) is None
    assert plugin.get_vendor_transaction(MARCH_2, -11310) is None
    assert plugin.get_vendor_transaction(MARCH_2, -11320, exclude={"00000000000000000000001"}) is None


def test_same_day_same_amount_receipts_come_one_at_a_time(costco):
    _export(costco, "receipts_a.json", "2026-01-01_to_2026-03-31")
    plugin = costco.CostcoPlugin()
    day = datetime.date(2026, 3, 10)

    first = plugin.get_vendor_transaction(day, -1990).record_id
    second = plugin.get_vendor_transaction(day, -1990, exclude={first}).record_id

    assert {first, second} == {"00000000000000000000003", "00000000000000000000004"}
    assert plugin.get_vendor_transaction(day, -1990, exclude={first, second}) is None


def test_discount_lines_fold_into_their_item(costco):
    _export(costco, "receipts_a.json", "2026-01-01_to_2026-03-31")

    purchase = costco.CostcoPlugin().get_vendor_transaction(MARCH_2, -11320)

    # FREEBIE and its discount come to 0 and are dropped
    assert purchase.line_items == [VendorLineItem("KS WATER 40PK", 3990, 1), VendorLineItem("EGGS 24CT", 6490, 1)]
    assert sum(i.amount for i in purchase.line_items) + purchase.sales_tax == 11320


def test_two_tender_and_no_item_receipts_are_never_offered(costco):
    _export(costco, "receipts_a.json", "2026-01-01_to_2026-03-31")
    plugin = costco.CostcoPlugin()

    assert plugin.get_vendor_transaction(datetime.date(2026, 3, 15), -25910) is None
    assert plugin.get_vendor_transaction(datetime.date(2026, 3, 20), 0) is None


def test_no_data_logs_the_how_to_tip(costco, caplog):
    assert costco.CostcoPlugin().get_vendor_transaction(MARCH_2, -11320) is None
    assert "Costco-Receipt-Downloader" in caplog.text


@pytest.mark.parametrize("age, stale", [(31, True), (30, False)])
def test_stale_data_logs_the_how_to_tip(costco, caplog, age, stale):
    receipt = json.loads((FIXTURES / "receipts_a.json").read_text())[2]
    day = datetime.date.today() - datetime.timedelta(days=age)
    receipt["transactionDate"], receipt["transactionDateTime"] = day.isoformat(), f"{day}T09:00:00"
    (costco.PLUGIN_DIR / "costco-receipts-2026-01-01_to_2026-12-31.json").write_text(json.dumps([receipt]))

    assert costco.CostcoPlugin().get_vendor_transaction(day, -1990) is not None
    assert (f"Costco data is {age} days old" in caplog.text) is stale
    assert ("Costco-Receipt-Downloader" in caplog.text) is stale


def test_export_added_after_a_first_load_is_matched(costco):
    plugin = costco.CostcoPlugin()
    assert plugin.get_vendor_transaction(MARCH_2, -11320) is None

    _export(costco, "receipts_a.json", "2026-01-01_to_2026-03-31")

    assert plugin.get_vendor_transaction(MARCH_2, -11320).record_id == "00000000000000000000001"


def test_merged_copy_keeps_a_deleted_exports_receipts_and_a_later_export_still_wins(costco):
    plugin = costco.CostcoPlugin()
    old = _export(costco, "receipts_a.json", "2026-01-01_to_2026-03-31")
    assert plugin.get_vendor_transaction(MARCH_2, -11320) is not None

    old.unlink()
    _export(costco, "receipts_b.json", "2026-03-01_to_04_30_2026")

    assert plugin.get_vendor_transaction(MARCH_2, 21590).record_id == "00000000000000000000002"
    assert plugin.get_vendor_transaction(MARCH_2, -10780).record_id == "00000000000000000000001"
    assert plugin.get_vendor_transaction(MARCH_2, -11320) is None


def test_receipts_with_lines_of_both_signs_are_not_offered(costco, caplog):
    caplog.set_level(logging.INFO)
    exchange, _, starts_with_discount, bananas, *_ = json.loads((FIXTURES / "receipts_a.json").read_text())
    # a returned item on a purchase: positive total, one negative line that isn't a "/" line
    towels = {**exchange["itemArray"][0], "itemDescription01": "BATH TOWELS", "amount": -2.0, "unit": -1}
    exchange["itemArray"].append(towels)
    exchange["total"] = 9.32
    # a "/" line with no item before it
    starts_with_discount["itemArray"].insert(0, exchange["itemArray"][1])
    starts_with_discount["total"] = 0.99
    export = costco.PLUGIN_DIR / "costco-receipts-2026-01-01_to_2026-03-31.json"
    export.write_text(json.dumps([exchange, starts_with_discount, bananas]))
    plugin = costco.CostcoPlugin()

    assert plugin.get_vendor_transaction(MARCH_2, -9320) is None
    assert plugin.get_vendor_transaction(datetime.date(2026, 3, 10), -990) is None
    assert plugin.get_vendor_transaction(datetime.date(2026, 3, 10), -1990).record_id == "00000000000000000000004"
    assert "Skipped 2 Costco receipt(s) with lines of both signs" in caplog.text
