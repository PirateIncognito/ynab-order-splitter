"""Split building (dry run): turn each matched transaction in
<plugin>_ynab_matched.json into split lines that sum exactly to its amount.

Shipping, fees, discounts and rounding are never lines of their own: the
whole charge is spread over the item lines in proportion to their amounts.
Sales tax is spread the same way, unless the split_sales_tax_line pref puts
it on its own line.
"""

import collections
import itertools
import json
import logging
from pathlib import Path

SALES_TAX = "Sales tax"  # memo of the tax line: not an item, so never in a category table or sent to the agent

log = logging.getLogger(__name__)


def _spread(total: int, weights: list[int]) -> list[int]:
    """`total` divided in proportion to `weights` (largest remainder), so
    the parts sum to exactly `total`. Equal parts if every weight is 0."""
    if not any(weights):
        weights = [1] * len(weights)
    weight_sum = sum(weights)
    parts = [total * w // weight_sum for w in weights]
    by_remainder = sorted(range(len(weights)), key=lambda i: total * weights[i] % weight_sum, reverse=True)
    for i in by_remainder[: total - sum(parts)]:
        parts[i] += 1
    return parts


def _charged_items(charge: int, items: list[dict], tax: int, refund: bool) -> tuple[list[dict], int] | None:
    """The items `charge` (positive milliunits) pays for, and their part of
    `tax`, or None if they can't be told. Every item, if they cost no more
    than the charge. Otherwise, for one shipment (or a whole order), every
    item if it is a purchase charged more than the tax: a discount or
    credit. For several shipments -- charged once per shipment -- the
    shipments whose items leave between 0 and `tax` of the charge for tax
    (a cent either way, for rounding), the one closest to their
    proportional share of tax if several do."""
    items_total = sum(i["amount"] for i in items)
    if items_total + tax <= charge:
        return items, tax
    shipments = sorted({i["shipment"] for i in items if i["shipment"] is not None})
    if len(shipments) < 2:
        return (items, tax) if not refund and tax < charge else None
    best = None
    for size in range(1, len(shipments)):
        for picked in itertools.combinations(shipments, size):
            picked_items = [i for i in items if i["shipment"] in picked]
            picked_total = sum(i["amount"] for i in picked_items)
            picked_tax = charge - picked_total
            if -10 <= picked_tax <= tax + 10:
                off = abs(picked_tax - tax * picked_total / items_total)
                if best is None or off < best[0]:
                    best = (off, picked_items, max(picked_tax, 0))
    return best[1:] if best else None


def build_split(amount: int, vendor_match: dict, tax_line: bool = False, tax_category_id: str | None = None) -> dict:
    """Split lines for a YNAB transaction of `amount` (signed milliunits)
    matched to `vendor_match` (a VendorTransaction as saved in
    <plugin>_ynab_matched.json). Lines carry the transaction's sign and sum to
    exactly `amount`; each is {"amount", "memo", "category_id"}.

    split_status is "exact" when the charged items are known (all of them,
    a discounted purchase, the shipments that add up to the charge, or the
    order's only item); "approximate" (with a split_reason) when the charge
    is spread over all of the order's items, with a proportional part of
    the tax, because which items it is for can't be told; or "nothing to
    split" -- no lines -- when the vendor record has no items to describe
    (e.g. a charge with no order on file); that transaction is categorized
    whole.
    """
    items = vendor_match["line_items"]
    if not items:
        return {"split_status": "nothing to split", "split_reason": "no line items", "split_lines": []}
    sign = -1 if amount < 0 else 1
    charge = abs(amount)
    tax = vendor_match["sales_tax"] or 0
    status, reason = "exact", None
    charged = _charged_items(charge, items, tax, refund=amount > 0)
    if charged:
        items, tax_share = charged
    else:
        tax_share = round(tax * charge / (sum(i["amount"] for i in items) + tax))
        if len(items) > 1:
            status = "approximate"
            if amount > 0:
                reason = "partial refund; refunded items unknown"
            elif len({i["shipment"] for i in items}) > 1:
                reason = "no shipment adds up to the charge; spread over the whole order"
            else:
                reason = "charge at or below the order's tax; charged items unknown"

    tax_lines = []
    if tax_line and tax_share:
        tax_lines = [{"amount": sign * tax_share, "memo": SALES_TAX, "category_id": tax_category_id}]
        charge -= tax_share
    parts = _spread(charge, [i["amount"] for i in items])
    lines = [{"amount": sign * part, "memo": i["description"], "category_id": None} for i, part in zip(items, parts)]
    return {"split_status": status, "split_reason": reason, "split_lines": lines + tax_lines}


def split_transactions(results_path: Path, tax_line: bool, tax_category_id: str | None) -> None:
    """Add a proposed split to every matched or ambiguous transaction in
    `results_path` (<plugin>_ynab_matched.json) and save it. A dry run: nothing
    is written to YNAB."""
    data = json.loads(results_path.read_text())
    to_split = [t for t in data["transactions"] if t["match_status"] != "unmatched"]
    log.info(f"Attempting splitting of {len(to_split)} matched transaction(s)...")
    for n, t in enumerate(to_split, 1):
        t.update(build_split(t["amount"], t["vendor_match"], tax_line, tax_category_id))
        if n % 500 == 0:
            log.info(f"Progress on splitting: {n} of {len(to_split)}")
    results_path.write_text(json.dumps(data, indent=2))
    counts = collections.Counter(t["split_status"] for t in to_split)
    log.info(
        f"Succeeded with splitting: {counts['exact']} exact, {counts['approximate']} approximate, "
        f"{counts['nothing to split']} nothing to split (saved to {results_path.name})"
    )
