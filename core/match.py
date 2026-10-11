"""Match cycle for one plugin: pull YNAB data, hand each candidate transaction
to the plugin to find the vendor record behind it, and save the result to
disk for review.

Fetches the budget's categories (core/ynab_categories.json) and, for the
chosen plugin, the YNAB transactions matching that plugin's mapping
settings, each with its match status and the vendor record the plugin
found in the date match window (the plugin's matched_path,
plugins/<plugin>/<plugin>_ynab_matched.json, overwritten on each run of
that plugin).
core/split.py then adds a proposed split to each matched transaction in
that file, core/review.py lets the user categorize and approve them, and
core/push.py writes the approved ones to YNAB.
"""

import collections
import dataclasses
import datetime
import json
import logging
import time
from pathlib import Path
from typing import Any

import ynab
from ynab.exceptions import ApiException

from core import categorize, config, plugin_settings, push, review, split, user_prefs, ynab_client
from plugins.plugin import VendorPlugin, VendorTransaction

CORE_DIR = Path(__file__).resolve().parent
CATEGORIES_PATH = CORE_DIR / "ynab_categories.json"

log = logging.getLogger(__name__)


def fetch_categories(access_token: str, budget_id: str) -> list[ynab_client.Category]:
    """Fetch every selectable category and overwrite ynab_categories.json,
    first moving the plugins' category tables to them if the saved ones are
    another budget's."""
    categories = ynab_client.list_categories(access_token, budget_id)
    if CATEGORIES_PATH.exists():
        saved = json.loads(CATEGORIES_PATH.read_text())
        if saved["budget_id"] != budget_id:
            categorize.follow_budget(saved["categories"], categories)
    data = {
        "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "budget_id": budget_id,
        "categories": [dataclasses.asdict(c) for c in categories],
    }
    CATEGORIES_PATH.write_text(json.dumps(data, indent=2))
    return categories


def _matches_mapping(transaction: ynab.TransactionDetail, prefs: dict[str, Any]) -> bool:
    if prefs.get("mode") == "account":
        return str(transaction.account_id) in prefs.get("account_ids", [])
    if prefs.get("mode") == "payee":
        payee_name = prefs.get("payee_name") or ""
        return (transaction.payee_name or "").strip().lower() == payee_name.strip().lower()
    return False


def _fitting_records(
    plugin: VendorPlugin, transaction: ynab.TransactionDetail, window_days: int, claimed: set[str]
) -> list[VendorTransaction]:
    """Every vendor record not in `claimed` with `transaction`'s signed
    amount, dated within `window_days` days either side of the YNAB date,
    closest date first (the earlier one first at the same distance). Order
    dates usually come before the YNAB date, Amazon charge dates after it.
    """
    skip = set(claimed)
    found = []
    for offset in sorted(range(-window_days, window_days + 1), key=abs):
        # ynab.TransactionDetail's date field is named var_date, not date --
        # the SDK generator renames it to avoid colliding with the `date` type.
        date = transaction.var_date + datetime.timedelta(days=offset)
        while (record := plugin.get_vendor_transaction(date, transaction.amount, skip)) is not None:
            found.append(record)
            skip.add(record.record_id)
    return found


def match_transaction(
    plugin: VendorPlugin, transaction: ynab.TransactionDetail, window_days: int, claimed: set[str]
) -> dict:
    """Match `transaction` to the closest-dated vendor record that fits and
    isn't in `claimed`, and add that record to `claimed`. Returns the
    transaction's match_status ("matched"; "ambiguous" when more than one
    record fits; "unmatched"), match_reason and vendor_match."""
    found = _fitting_records(plugin, transaction, window_days, claimed)
    if not found:
        if _fitting_records(plugin, transaction, window_days, set()):
            reason = "all candidates claimed"
        elif transaction.amount > 0:
            reason = "no refund in window"
        else:
            reason = "no record in window"
        return {"match_status": "unmatched", "match_reason": reason, "vendor_match": None}
    claimed.add(found[0].record_id)
    return {
        "match_status": "matched" if len(found) == 1 else "ambiguous",
        "match_reason": None if len(found) == 1 else f"{len(found)} records fit; took the closest date",
        "vendor_match": _vendor_transaction_dict(found[0]),
    }


def _vendor_transaction_dict(vendor_transaction: VendorTransaction) -> dict:
    d = dataclasses.asdict(vendor_transaction)
    d["date"] = vendor_transaction.date.isoformat()
    return d


def _refresh_vendor_data(plugin: VendorPlugin, candidates: list, margin_days: int) -> None:
    """If `plugin` scrapes its own vendor data, refresh it before matching.

    Looks back `margin_days` before the earliest candidate transaction's
    YNAB date -- vendor orders are typically placed before the bank posts
    the matching transaction, so scraping from the transaction date alone
    can miss orders (e.g. a December order behind a January transaction).
    A scrape failure (bad creds, network, site changes) is reported but
    doesn't abort the cycle -- matching just falls back to whatever vendor
    data is already on disk.
    """
    if not plugin.scrapes_vendor_data or not candidates:
        return
    label = plugin.name
    lookback_date = min(t.var_date for t in candidates) - datetime.timedelta(days=margin_days)
    try:
        plugin.get_data_from_vendor(lookback_date)
    except Exception as e:
        log.warning(f"Failed {label} vendor data refresh: {e}; using existing vendor data.")


def match_transactions_for_plugin(
    access_token: str, budget_id: str, plugin: VendorPlugin, vendor_lookback_margin_days: int, window_days: int
) -> Path:
    """Pull the YNAB transactions matching `plugin`'s mapping/run_mode
    settings, refresh the plugin's vendor data if it scrapes one, match
    each transaction to a vendor record (one record per transaction at
    most), and overwrite plugin.matched_path with the results. If the
    plugin fails partway, the results so far are still saved. Returns the
    results file's path."""
    prefs = plugin_settings.load(plugin)
    run_mode = prefs.get("run_mode", "unapproved")

    log.info("Attempting to fetch YNAB transactions...")
    transactions = ynab_client.list_transactions(
        access_token, budget_id, since_date=datetime.date.min, unapproved_only=(run_mode == "unapproved")
    )
    candidates = [t for t in transactions if _matches_mapping(t, prefs)]
    if run_mode == "nonsplit":
        candidates = [t for t in candidates if not t.subtransactions]
    earliest = min(t.var_date for t in candidates).isoformat() if candidates else "none"
    log.info(
        f"Succeeded with YNAB transactions: {len(candidates)} candidate(s) for {plugin.name} "
        f"(run mode {run_mode}), earliest {earliest}"
    )

    _refresh_vendor_data(plugin, candidates, vendor_lookback_margin_days)

    log.info(f"Attempting to match {len(candidates)} transaction(s) to {plugin.name} records...")
    records = []
    claimed = set()
    error = None
    try:
        for t in candidates:
            # to_json() (not to_dict()) so UUID/date fields come out as plain
            # strings -- to_dict() leaves them as native objects, which
            # json.dump can't serialize (see commit 1452e2a).
            record = json.loads(t.to_json())
            record.update(match_transaction(plugin, t, window_days, claimed))
            records.append(record)
            if len(records) % 500 == 0:
                log.info(f"Progress on matching: {len(records)} of {len(candidates)}")
    except Exception as e:  # a plugin bug mustn't lose the results so far
        error = e

    results_path = plugin.matched_path
    data = {
        "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "budget_id": budget_id,
        "plugin": plugin.name,
        "mode": prefs.get("mode"),
        "run_mode": run_mode,
        "transactions": records,
    }
    results_path.write_text(json.dumps(data, indent=2))
    if error:
        log.error(
            f"Failed matching: {error!r}; saved {len(records)} of {len(candidates)} result(s) to {results_path.name}"
        )
        return results_path
    counts = collections.Counter(r["match_status"] for r in records)
    log.info(
        f"Succeeded with matching: {counts['matched']} matched, {counts['ambiguous']} ambiguous, "
        f"{counts['unmatched']} unmatched (saved to {results_path.name})"
    )
    return results_path


def run_cycle_for_plugin(plugin: VendorPlugin) -> None:
    """Fetch categories, match `plugin`'s YNAB transactions to its vendor
    data, split them, review the splits and push the approved ones to YNAB.
    Runs nothing if the plugin's setup is incomplete."""
    label = plugin.name
    if plugin_settings.needs_setup(plugin):
        log.warning(f"{label}: setup incomplete. Finish it under Settings > Plugins first.")
        return
    token = config.get_ynab_access_token()
    core_prefs = user_prefs.load_user_prefs()
    budget_id = core_prefs.get("ynab_budget_id")
    lookback_margin_days = core_prefs.get("vendor_lookback_margin_days", 7)
    window_days = core_prefs["match_window_days"]

    try:
        categories = fetch_categories(token, budget_id)
        log.info(f"Saved {len(categories)} categories to {CATEGORIES_PATH.name}.")
        payees = ynab_client.list_payees(token, budget_id)
        results_path = match_transactions_for_plugin(token, budget_id, plugin, lookback_margin_days, window_days)
        split.split_transactions(results_path, core_prefs["split_sales_tax_line"], core_prefs["sales_tax_category_id"])
        time.sleep(1)  # let the summaries be read before the review screen clears them
        review.review_transactions(
            results_path, categories, payees, plugin.categories_path, plugin.approved_path, categorize.chosen_ask()
        )
        push.push_approved(plugin.approved_path, token, budget_id)
    except ApiException as e:
        log.error(f"Failed {label} match and split: YNAB API error: {ynab_client.describe_api_error(e)}")


def _has_results_to_review(plugin: VendorPlugin, budget_id: str) -> bool:
    """Whether `plugin`'s last Match and Split run saved results and
    categories for budget `budget_id` with a split transaction still to
    approve."""
    if not plugin.matched_path.exists() or not CATEGORIES_PATH.exists():
        return False
    results = json.loads(plugin.matched_path.read_text())
    if results["budget_id"] != budget_id or json.loads(CATEGORIES_PATH.read_text())["budget_id"] != budget_id:
        return False
    return bool(review.left_to_review(results["transactions"]))


def has_last_results(plugin: VendorPlugin) -> bool:
    """Whether Review last results has anything for the selected budget: a
    split transaction still to approve, or an approved one queued to push.
    Local files only."""
    budget_id = user_prefs.load_user_prefs().get("ynab_budget_id")
    queued = json.loads(plugin.approved_path.read_text()) if plugin.approved_path.exists() else []
    return bool(push.for_budget(queued, budget_id)) or _has_results_to_review(plugin, budget_id)


def review_last_results(plugin: VendorPlugin) -> None:
    """Review the results saved by `plugin`'s last Match and Split run again,
    with the categories saved then and the budget's payees fetched now (no
    vendor calls, no re-matching or re-splitting), if any are left to
    approve, then offer to push the approved ones to YNAB. The menu offers
    it only when has_last_results()."""
    budget_id = user_prefs.load_user_prefs().get("ynab_budget_id")
    token = config.get_ynab_access_token()
    if _has_results_to_review(plugin, budget_id):
        categories = [ynab_client.Category(**c) for c in json.loads(CATEGORIES_PATH.read_text())["categories"]]
        try:
            payees = ynab_client.list_payees(token, budget_id)
        except ApiException as e:
            log.error(f"Failed review of last results: YNAB API error: {ynab_client.describe_api_error(e)}")
            return
        review.review_transactions(
            plugin.matched_path, categories, payees, plugin.categories_path, plugin.approved_path,
            categorize.chosen_ask(),
        )
    push.push_approved(plugin.approved_path, token, budget_id)
