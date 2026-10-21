"""Costco vendor plugin.

No scraping: the user exports their Costco warehouse receipts with
Costco-Receipt-Downloader (https://github.com/harrykhh/Costco-Receipt-Downloader)
and saves each export (costco-receipts-<FROM>_to_<TO>.json) into this
directory. Whenever the exports in the folder change, they are merged into
ORDER_DATA_PATH on top of what it already holds (so deleting an old export
loses nothing), and get_data_from_JSON / get_vendor_transaction read the
result. The exports themselves are never changed.

The exports cover warehouse receipts only: Costco gas and costco.com
charges have no receipt here and won't match.
"""

import datetime
import json
import logging
from collections.abc import Collection
from pathlib import Path

from core.milliunits import dollars_to_milliunits
from plugins.plugin import VendorLineItem, VendorPlugin, VendorTransaction

log = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).resolve().parent
ORDER_DATA_PATH = PLUGIN_DIR / "costco.com_order_data.json"
PREFS_PATH = PLUGIN_DIR / "costco.com_user_prefs.json"
STALE_DAYS = 30

HOW_TO = (
    "To get Costco data, install Costco-Receipt-Downloader "
    "(https://github.com/harrykhh/Costco-Receipt-Downloader), export your receipts "
    "and save the file into plugins/costco.com/. The exports cover warehouse receipts only, "
    "so Costco gas and costco.com charges won't match."
)


def _to_date(export: Path) -> datetime.date:
    """The TO date in an export's name, costco-receipts-<FROM>_to_<TO>.json,
    written YYYY-MM-DD or MM_DD_YYYY; date.min if it doesn't parse."""
    to = export.stem.rpartition("_to_")[2]
    for fmt in ("%Y-%m-%d", "%m_%d_%Y"):
        try:
            return datetime.datetime.strptime(to, fmt).date()
        except ValueError:
            pass
    return datetime.date.min


def _merged_receipts(exports: list[Path]) -> list[dict]:
    """Every receipt in the merged copy and the exports (oldest TO date
    first), once per transactionBarcode -- the last copy wins -- saved to
    ORDER_DATA_PATH. An export that doesn't parse is skipped; if none
    parses, the merged copy is used as it is."""
    receipts = {}
    if ORDER_DATA_PATH.exists():
        try:
            receipts = {r["transactionBarcode"]: r for r in json.loads(ORDER_DATA_PATH.read_text())}
        except ValueError as e:
            log.warning(f"Rebuilding {ORDER_DATA_PATH.name} from the exports: it doesn't parse ({e})")
    parsed = False
    for export in exports:
        try:
            data = json.loads(export.read_text())
        except ValueError as e:
            log.warning(f"Skipped Costco export {export.name}: it doesn't parse ({e})")
            continue
        parsed = True
        receipts.update((r["transactionBarcode"], r) for r in data)
    merged = sorted(receipts.values(), key=lambda r: r["transactionDateTime"])
    if parsed:
        ORDER_DATA_PATH.touch(mode=0o600)
        ORDER_DATA_PATH.chmod(0o600)  # touch leaves an existing file's mode alone
        ORDER_DATA_PATH.write_text(json.dumps(merged, indent=2))
    return merged


def _lines(receipt: dict) -> list[list]:
    """The receipt's items as [description, signed milliunits, unit], each
    with the instant savings / coupon lines ("/..." lines, which follow the
    item they discount) folded in; items that come to 0 are dropped."""
    lines = []
    for item in receipt["itemArray"]:
        amount = dollars_to_milliunits(item["amount"])
        if item["itemDescription01"].startswith("/") and lines:
            lines[-1][1] += amount
        else:
            lines.append([item["itemDescription01"].strip(), amount, item["unit"]])
    return [line for line in lines if line[1]]


class CostcoPlugin(VendorPlugin):
    name = "costco.com"
    description = "Matches Costco warehouse receipts to YNAB transactions."
    url = "https://www.costco.com"
    prefs_path = PREFS_PATH
    categories_path = PLUGIN_DIR / "costco.com_categories.json"
    matched_path = PLUGIN_DIR / "costco.com_ynab_matched.json"
    approved_path = PLUGIN_DIR / "costco.com_ynab_approved.json"
    pushed_path = PLUGIN_DIR / "costco.com_ynab_pushed.json"
    scrapes_vendor_data = False

    # The receipts that can be matched, merged again only when the exports
    # change -- matching looks receipts up once per date in the match window.
    _receipts: list[dict] | None = None
    _exports_key: tuple | None = None

    def _load(self) -> list[dict]:
        exports = sorted(PLUGIN_DIR.glob("costco-receipts-*.json"), key=lambda p: (_to_date(p), p.name))
        key = tuple((p.name, p.stat().st_mtime_ns) for p in exports)
        if key == self._exports_key:
            return self._receipts
        receipts = _merged_receipts(exports)
        if not receipts:
            log.warning(f"No Costco data. {HOW_TO}")
        else:
            newest = max(datetime.date.fromisoformat(r["transactionDate"]) for r in receipts)
            age = (datetime.date.today() - newest).days
            if age > STALE_DAYS:
                log.warning(f"Costco data is {age} days old. {HOW_TO}")
        self._receipts = []
        two_tenders = no_items = mixed_signs = 0
        for receipt in receipts:
            lines = _lines(receipt)
            total = dollars_to_milliunits(receipt["total"])
            if len(receipt["tenderArray"]) > 1:
                two_tenders += 1
            elif not lines:
                no_items += 1
            elif any((amount > 0) != (total > 0) for _, amount, _ in lines):
                # line amounts are made positive, so these couldn't add up to the total
                mixed_signs += 1
            else:
                self._receipts.append(receipt)
        if two_tenders:
            log.info(f"Skipped {two_tenders} Costco receipt(s) paid with more than one tender")
        if no_items:
            log.info(f"Skipped {no_items} Costco receipt(s) with no items")
        if mixed_signs:
            log.info(
                f"Skipped {mixed_signs} Costco receipt(s) with lines of both signs "
                "(an exchange, or a coupon bigger than its item)"
            )
        self._exports_key = key
        return self._receipts

    def get_data_from_JSON(self, date: datetime.date, amount: int) -> list[dict]:
        """Receipts on `date` whose total is `amount`: a purchase (positive
        total) matches an outflow, a return (negative total) an inflow."""
        return [
            r
            for r in self._load()
            if r["transactionDate"] == date.isoformat() and -dollars_to_milliunits(r["total"]) == amount
        ]

    def get_vendor_transaction(
        self, date: datetime.date, amount: int, exclude: Collection[str] = ()
    ) -> VendorTransaction | None:
        receipt = next(
            (r for r in self.get_data_from_JSON(date, amount) if r["transactionBarcode"] not in exclude), None
        )
        if receipt is None:
            return None
        return VendorTransaction(
            date=date,
            amount=amount,
            record_id=receipt["transactionBarcode"],
            line_items=[
                VendorLineItem(description, abs(line_amount), abs(unit))
                for description, line_amount, unit in _lines(receipt)
            ],
            sales_tax=abs(dollars_to_milliunits(receipt["taxes"])),
            source_id=receipt["transactionBarcode"],
        )
