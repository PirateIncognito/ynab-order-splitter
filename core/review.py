"""Review screen: one screen per split transaction in
<plugin>_ynab_matched.json, newest first, where the user categorizes its split
lines (dividing a line into two halves for two categories), optionally picks
another of the budget's YNAB payees for it, and approves or skips it, a
single key press each.

Uncategorized lines are filled from the plugin's category table (item
description -> {"category": id, "by": "user" | "agent"}). Every category
the user sets, and every line of a transaction they approve, is written
to the table as "user", so a repeat purchase needs only [a]. With AI on,
core/categorize.py fills in the rest of the table in the background. The
outcome ("review_status": approved / skipped, and for a transaction with nothing to
split its "review_category_id", and a picked payee's "review_payee_id") is
saved into each transaction, and each approved transaction is queued for
core/push.py to write to YNAB.
"""

import collections
import json
import logging
from collections.abc import Callable
from pathlib import Path

from core import categorize, push, ui
from core.milliunits import milliunits_to_dollars
from core.ynab_client import Category, Payee

# Each split line's label and key on the review screen: 1-9, then letters
# (no a -- all rows, no q -- it backs out). Longer splits are numbered and
# the number typed.
ROW_KEYS = "123456789bcdefghijklmnoprstuvwxyz"
ITEM_WIDTH = 125  # longer descriptions are cut short with "…"

log = logging.getLogger(__name__)


def left_to_review(transactions: list[dict]) -> list[dict]:
    """The split transactions in `transactions` not approved yet: what the review shows."""
    return [t for t in transactions if "split_status" in t and t.get("review_status") != "approved"]


def find_category(text: str, labels: dict[str, str]) -> str | None:
    """The id of the category, ignoring case, whose label ("Group: Name" ->
    id in `labels`) is `text`; else the only one whose name is `text`; else
    the only one whose label contains it. None if there is no such category
    or several fit."""
    text = text.strip().lower()
    found = (
        [label for label in labels if label.lower() == text]
        or [label for label in labels if label.lower().split(": ", 1)[1] == text]
        or [label for label in labels if text in label.lower()]
    )
    return labels[found[0]] if len(found) == 1 else None


def prefill(transaction: dict, table: dict[str, dict], names: dict[str, str]) -> None:
    """Fill each uncategorized split line with the category `table` holds
    for its description, if that category still exists (is in `names`, id -> label)."""
    for line in transaction["split_lines"]:
        category_id = table.get(line["memo"], {}).get("category")
        if line["category_id"] is None and category_id in names:
            line["category_id"] = category_id


def set_category(transaction: dict, line_number: int, category_id: str, table: dict[str, dict]) -> None:
    """Give split line `line_number` (from 1) `category_id` and write it to
    `table` for that line's description (not for the sales tax line or a
    line with none), set by the user. A transaction with nothing to split
    gets the category as a whole."""
    if not transaction["split_lines"]:
        transaction["review_category_id"] = category_id
        return
    line = transaction["split_lines"][line_number - 1]
    line["category_id"] = category_id
    if line["memo"] not in categorize.NOT_ITEMS:
        table[line["memo"]] = {"category": category_id, "by": "user"}


def missing_categories(transaction: dict, names: dict[str, str]) -> str | None:
    """What still needs a category (one of `names`, id -> label) before
    `transaction` can be approved, or None if nothing does."""
    if not transaction["split_lines"]:
        return None if transaction.get("review_category_id") in names else "the whole transaction"
    rows = [_row_label(n, len(transaction["split_lines"]))
            for n, line in enumerate(transaction["split_lines"], 1) if line["category_id"] not in names]
    return f"row(s) {', '.join(rows)}" if rows else None


def _row_label(n: int, count: int) -> str:
    """Split line `n`'s (from 1) label and key, of `count` lines."""
    return ROW_KEYS[n - 1] if count <= len(ROW_KEYS) else str(n)


def _money(milliunits: int) -> str:
    return f"{milliunits_to_dollars(milliunits):,.2f}"


def _draw(t: dict, position: int, total: int, names: dict[str, str], payee_names: dict[str, str]) -> None:
    ui.clear_screen()
    ui.show_background_messages()
    amount = t["amount"]
    payee = payee_names.get(t.get("review_payee_id"), t["payee_name"])
    print(ui.styled([
        ("title", f"[{position}/{total}]  {t['date']}  {payee}  "),
        ("out" if amount < 0 else "in", _money(amount)),
        ("muted", f"  order {t['vendor_match']['source_id'] or '(none)'}"),
    ]))
    if t["match_status"] == "ambiguous":
        print(ui.styled([("warn", f"  ! AMBIGUOUS MATCH: {t['match_reason']}")]))
    if t["split_status"] != "exact" and not (t["split_status"] == "nothing to split" and t["split_lines"]):  # [d]
        print(ui.styled([("warn", f"  ! {t['split_status'].upper()}: {t['split_reason']}")]))
    print()
    lines = t["split_lines"] or [
        {"amount": amount, "memo": "Whole transaction", "category_id": t.get("review_category_id")}
    ]
    rows, roles = [], []
    for n, line in enumerate(lines, 1):
        memo = line["memo"] if len(line["memo"]) <= ITEM_WIDTH else line["memo"][:ITEM_WIDTH - 1] + "…"
        category = names.get(line["category_id"])
        rows.append([_row_label(n, len(lines)) if t["split_lines"] else "", memo, category or "(none)",
                     _money(line["amount"])])
        roles.append(["muted", "", "" if category else "missing", "out" if line["amount"] < 0 else "in"])
    ui.print_table(["#", "Item", "Category", "Amount"], rows, ("right", "left", "left", "right"), roles)
    print()


def _ask_row(count: int, with_all: bool) -> int | str | None:
    """Ask which of `count` split lines (from 1) to act on, or "a" for all
    if `with_all`; with one line (or none), 1. None if q, Esc or Enter backs out."""
    for_all = ", a for all" if with_all else ""
    if count > len(ROW_KEYS):
        answer = ui.text(
            f"Row (1-{count}){for_all}, or Enter to go back:",
            validate=lambda s: not s or with_all and s == "a" or s.isdigit() and 1 <= int(s) <= count,
        )
        return None if not answer else "a" if answer == "a" else int(answer)
    if count > 1:
        keys = ROW_KEYS[:count]
        shown = f"1-{keys[-1]}" if count <= 9 else "1-9, b" if count == 10 else f"1-9, b-{keys[-1]}"
        answer = ui.key(f"Row ({shown}){for_all}, or q / Esc to go back?", keys + ("a" if with_all else "") + "q\x1b")
        return None if answer in (None, "q", "\x1b") else "a" if answer == "a" else keys.index(answer) + 1
    return 1


def _edit(transaction: dict, labels: dict[str, str], names: dict[str, str], table: dict[str, dict]) -> None:
    """Ask which line to categorize (if there is a choice), or a for every
    item line (not the sales tax line), then its category; other lines with
    the same description get it too. q or Esc at the line, or an empty
    category, backs out."""
    row = _ask_row(len(transaction["split_lines"]), with_all=True)
    if row is None:
        return
    items = [n for n, line in enumerate(transaction["split_lines"], 1) if line["memo"] != categorize.SALES_TAX]
    line_numbers = items if row == "a" else [row]
    answer = ui.autocomplete(
        "Category (Enter on empty to go back):",
        list(labels),
        validate=lambda s: not s.strip() or find_category(s, labels) is not None
        or "Type more, or pick a category from the list.",
    )
    if answer and answer.strip():
        category_id = find_category(answer, labels)
        with categorize.lock:
            for line_number in line_numbers:
                set_category(transaction, line_number, category_id, table)
        prefill(transaction, table, names)


def _divide(transaction: dict) -> None:
    """Ask which line to divide (if there is a choice), or a for every line
    but the sales tax line, and replace each with two lines next to each
    other, each half its amount in whole cents (the first takes an odd
    cent), with its memo and category. A transaction with nothing to split
    becomes two lines of its whole amount, with its category and no memo.
    q or Esc backs out."""
    lines = transaction["split_lines"]
    if not lines:
        lines.append(
            {"amount": transaction["amount"], "memo": "", "category_id": transaction.get("review_category_id")}
        )
    row = _ask_row(len(lines), with_all=True)
    if row is None:
        return
    rows = [n for n, line in enumerate(lines, 1) if line["memo"] != categorize.SALES_TAX] if row == "a" else [row]
    for n in reversed(rows):  # from the end, so the rows before keep their numbers
        line = lines[n - 1]
        half = abs(line["amount"]) // 20 * 10
        second = -half if line["amount"] < 0 else half
        lines[n - 1:n] = [{**line, "amount": line["amount"] - second}, {**line, "amount": second}]


def _pick_payee(transaction: dict, payee_ids: dict[str, str]) -> None:
    """Ask for one of the budget's payees (name -> id in `payee_ids`) and
    give `transaction` that payee; an empty answer backs out."""
    ids = {name.strip().lower(): payee_id for name, payee_id in payee_ids.items()}
    answer = ui.autocomplete(
        "Payee (Enter on empty to go back):",
        list(payee_ids),
        validate=lambda s: not s.strip() or s.strip().lower() in ids or "Pick a payee from the list.",
    )
    if answer and answer.strip():
        transaction["review_payee_id"] = ids[answer.strip().lower()]


def _review_one(transaction: dict, position: int, total: int, labels: dict[str, str], names: dict[str, str],
                table: dict[str, dict], payee_ids: dict[str, str], payee_names: dict[str, str]) -> str | None:
    """Show `transaction` until the user approves or skips it ("approved" /
    "skipped"); None if they quit."""
    _draw(transaction, position, total, names, payee_names)
    while True:
        key = ui.key("[a]pprove  [e]dit  [d]ivide  [p]ayee  [o]pen order in browser  [s]kip  [q]uit", "aedposq")
        if key == "a":
            missing = missing_categories(transaction, names)
            if not missing:
                return "approved"
            print(f"Can't approve yet, no category on {missing}.")
        elif key == "e":
            _edit(transaction, labels, names, table)
            _draw(transaction, position, total, names, payee_names)
        elif key == "d":
            _divide(transaction)
            _draw(transaction, position, total, names, payee_names)
        elif key == "p":
            _pick_payee(transaction, payee_ids)
            _draw(transaction, position, total, names, payee_names)
        elif key == "o":
            link = transaction["vendor_match"].get("link")
            if not link:
                print("No order page for this transaction.")
            elif ui.open_in_browser(link):
                _draw(transaction, position, total, names, payee_names)
            else:
                print("Couldn't open a browser.")
        elif key == "s":
            return "skipped"
        else:  # q, or Ctrl+C
            return None


def review_transactions(results_path: Path, categories: list[Category], payees: list[Payee], table_path: Path,
                        approved_path: Path, ask: Callable[[str, str, dict], dict] | None) -> None:
    """Review every split transaction in `results_path` (<plugin>_ynab_matched.json)
    not approved yet, newest first, until the user quits, with `payees` to
    pick from (kept in memory only); save the outcomes
    and the plugin's category table (`table_path`), and queue each approved
    transaction in `approved_path` as it is approved. With `ask` (an
    agents.ask_* function; None for AI off), the agent categorizes the
    table's unknown items meanwhile. Nothing is written to YNAB here."""
    data = json.loads(results_path.read_text())
    table = json.loads(table_path.read_text()) if table_path.exists() else {}
    labels = {f"{c.group_name}: {c.name}": c.id for c in categories}
    names = {category_id: label for label, category_id in labels.items()}
    payee_ids = {p.name: p.id for p in payees}
    payee_names = {p.id: p.name for p in payees}
    split = [t for t in data["transactions"] if "split_status" in t]
    to_review = sorted(left_to_review(data["transactions"]), key=lambda t: t["date"], reverse=True)

    log.info(f"Attempting review of {len(to_review)} transaction(s)...")
    calls = categorize.plan_calls(to_review, table) if ask else []
    agent = categorize.Agent(ask, calls, labels, table, table_path) if calls else None
    if agent:
        agent.thread.start()
    try:
        for position, t in enumerate(to_review, 1):
            if agent:
                try:
                    agent.wait([line["memo"] for line in t["split_lines"]])
                except KeyboardInterrupt:  # Ctrl+C while waiting: quit, as at the prompt
                    break
            prefill(t, table, names)
            if t.get("review_payee_id") not in payee_names:  # deleted or merged in YNAB since it was picked
                t.pop("review_payee_id", None)
            outcome = _review_one(t, position, len(to_review), labels, names, table, payee_ids, payee_names)
            if outcome is None:
                break
            t["review_status"] = outcome
            if outcome == "approved":
                push.queue(approved_path, data["budget_id"], t)
                with categorize.lock:
                    for line in t["split_lines"]:
                        if line["memo"] not in categorize.NOT_ITEMS:
                            table[line["memo"]] = {"category": line["category_id"], "by": "user"}
    finally:  # keep what was done, even if the review fails partway
        if agent:
            agent.stopped = True
        results_path.write_text(json.dumps(data, indent=2))
        with categorize.lock:
            table_path.write_text(json.dumps(table, indent=2))
    counts = collections.Counter(t.get("review_status") for t in split)
    log.info(
        f"Succeeded with review: {counts['approved']} approved, {counts['skipped']} skipped, "
        f"{counts[None]} left (saved to {results_path.name})"
    )
