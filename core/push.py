"""Push approved transactions to YNAB: the only code that writes to YNAB.

The review queues each transaction the user approves in its plugin's
approved_path (<plugin>_ynab_approved.json). push_approved() offers that
queue at the end of each review only (user 2026-10-07: not at start or
exit); a declined queue waits for the next review. Right before writing it
re-reads the queued
transactions and the categories from YNAB, and skips any that changed
meanwhile. An entry leaves the queue only once YNAB confirms it.
"""

import datetime
import json
import logging
from pathlib import Path

from ynab.exceptions import ApiException

from core import ui, ynab_client
from core.milliunits import milliunits_to_dollars

# Half the ~1.1 MB a split PATCH was measured to take before YNAB times out (~30 s).
MAX_PATCH_BYTES = 500_000
MEMO_LENGTH = 50  # characters of an item name written to a YNAB memo

log = logging.getLogger(__name__)


def queue(path: Path, budget_id: str, transaction: dict) -> None:
    """Add approved `transaction` (from <plugin>_ynab_matched.json, in
    budget `budget_id`) to the queue at `path`, replacing any earlier entry
    for it."""
    entries = json.loads(path.read_text()) if path.exists() else []
    entries = [e for e in entries if e["id"] != transaction["id"]]
    entries.append({
        "id": transaction["id"],
        "budget_id": budget_id,
        "date": transaction["date"],
        "amount": transaction["amount"],
        "split_lines": transaction["split_lines"],
        "category_id": transaction.get("review_category_id"),  # with no split lines, the whole transaction's
        "payee_id": transaction.get("review_payee_id"),  # None: the payee is left as it is in YNAB
    })
    path.write_text(json.dumps(entries, indent=2))


def for_budget(queued: list[dict], budget_id: str) -> list[dict]:
    """The entries of `queued` (the queue file's list) for budget `budget_id`."""
    return [e for e in queued if e["budget_id"] == budget_id]


def _payload(entry: dict, current) -> dict:
    """What to PATCH for queued `entry` (`current`: its transaction as just
    re-read from YNAB): approved, and split by its lines, each with the
    picked payee, else the current one in YNAB; one line is no split: that line's
    category and memo; no lines: its one category. The transaction's own
    payee only if the user picked one."""
    payload = {"id": entry["id"], "approved": True}
    if entry.get("payee_id"):  # entries queued before [p] existed have no payee_id
        payload["payee_id"] = entry["payee_id"]
    # Item names stay whole in the queue (the category table's key); YNAB gets the first 50 characters.
    lines = [{**line, "memo": line["memo"][:MEMO_LENGTH]} for line in entry["split_lines"]]
    payee_id = entry.get("payee_id") or (str(current.payee_id) if current.payee_id else None)
    if len(lines) > 1:
        payload["subtransactions"] = [{**line, "payee_id": payee_id} if payee_id else line for line in lines]
    elif lines:
        payload.update(category_id=lines[0]["category_id"], memo=lines[0]["memo"])
    else:
        payload["category_id"] = entry["category_id"]
    return payload


def batches(payloads: list[dict], max_bytes: int = MAX_PATCH_BYTES) -> list[list[dict]]:
    """`payloads`, in order, cut into batches of at most `max_bytes` of JSON
    each (a single larger payload gets a batch of its own)."""
    result, size = [], 0
    for p in payloads:
        p_size = len(json.dumps(p))
        if not result or size + p_size > max_bytes:
            result.append([])
            size = 0
        result[-1].append(p)
        size += p_size
    return result


def _confirm(count: int) -> bool:
    """Ask before writing. Kept to this one function, called once, so it
    can be removed cleanly."""
    return ui.key(f"Push {count} approved transaction(s) to YNAB? (y/n)", "yn") == "y"


def _label(entry: dict) -> str:
    return f"{entry['date']} {milliunits_to_dollars(entry['amount']):,.2f}"


def _reason(e: Exception) -> str:
    return ynab_client.describe_api_error(e) if isinstance(e, ApiException) else str(e)


def push_approved(path: Path, access_token: str, budget_id: str) -> None:
    """Write the approved transactions queued at `path` for budget
    `budget_id` (the one selected in Settings) to YNAB, after asking, and
    remove each from the queue once YNAB confirms it. One that is deleted,
    missing or already split in YNAB, or whose date, amount or categories
    changed, leaves the queue unwritten (a changed one with a message to
    match and approve it again). Entries for another budget stay queued."""
    queued = json.loads(path.read_text()) if path.exists() else []
    entries = for_budget(queued, budget_id)
    if len(entries) < len(queued):
        log.warning(
            f"Not pushing {len(queued) - len(entries)} approved transaction(s) queued for another budget; "
            f"select that budget in Settings to push them."
        )
    if not entries:
        return
    if not _confirm(len(entries)):
        log.info(f"Not pushed: {len(entries)} approved transaction(s) stay queued in {path.name}.")
        return

    log.info(f"Attempting to push {len(entries)} transaction(s) to YNAB...")
    since = datetime.date.fromisoformat(min(e["date"] for e in entries))
    try:
        current = {str(t.id): t for t in ynab_client.list_transactions(access_token, budget_id, since)}
        for e in entries:  # not since `since`: deleted, or its date moved earlier
            if e["id"] not in current:
                current[e["id"]] = ynab_client.get_transaction(access_token, budget_id, e["id"])
        category_ids = {c.id for c in ynab_client.list_categories(access_token, budget_id)}
    except Exception as e:  # YNAB error, offline
        log.error(f"Failed pushing to YNAB: {_reason(e)}; all {len(entries)} stay queued")
        return

    done, to_write = set(), []  # done: ids to remove from the queue
    dropped = flagged = 0
    for e in entries:
        t = current[e["id"]]
        used = {line["category_id"] for line in e["split_lines"]} or {e["category_id"]}
        if t is None or t.subtransactions:
            log.warning(f"Dropped {_label(e)}: {'deleted or missing' if t is None else 'already split'} in YNAB")
            dropped += 1
        elif t.var_date.isoformat() != e["date"] or t.amount != e["amount"] or not used <= category_ids:
            log.warning(f"Not pushing {_label(e)}: changed in YNAB; run Match and Split again")
            flagged += 1
        else:
            to_write.append(e)
            continue
        done.add(e["id"])

    error = None
    todo = batches([_payload(e, current[e["id"]]) for e in to_write])
    for n, batch in enumerate(todo, 1):
        try:
            done.update(ynab_client.update_transactions(access_token, budget_id, batch))
        except Exception as e:  # YNAB error, offline, invalid data
            error = e  # a 500 near 30 s may still have been saved: the next push re-reads first
            break
        log.info(f"Progress on pushing: batch {n} of {len(todo)}, {len(done) - dropped - flagged} written")

    left = [e for e in queued if e["id"] not in done]
    path.write_text(json.dumps(left, indent=2))
    counts = f"{len(done) - dropped - flagged} written, {dropped} dropped, {flagged} flagged"
    if error:
        log.error(
            f"Failed pushing to YNAB: {_reason(error)}; {counts}, "
            f"{len(left)} still queued (checked again before the next push)"
        )
    else:
        log.info(f"Succeeded with pushing: {counts}")
