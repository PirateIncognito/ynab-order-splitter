"""AI categorization (with AI on in Settings): while the review runs, a
background thread asks the chosen provider for the category of every item
the plugin's category table has no entry for, and writes each valid
answer to the table as "agent".

The table is one dict shared with the review; `lock` guards it, and the
agent's pending items, so neither side's writes are lost.
"""

import collections
import functools
import json
import logging
import threading
from collections.abc import Callable
from pathlib import Path

from core import agents, config, user_prefs, ynab_client
from core.split import SALES_TAX
from plugins.orchestrator import list_plugins

# Lines that are not items: never sent to the agent or written to a category table
# ("": the halves of a whole transaction divided with [d]).
NOT_ITEMS = (SALES_TAX, "")

INSTRUCTIONS = (
    "You categorize purchased items for a personal YNAB budget.\n"
    "For each numbered item, choose the single best category by its number, or 0 if none fits. "
    "Give your confidence as high, medium or low. Answer every item exactly once."
)
SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item": {"type": "integer"},
                    "category": {"type": "integer"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
                "required": ["item", "category", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}
SINGLE_CALLS = 10  # the first transactions needing the agent get a call each, so the first screens wait little
BATCH_SIZE = 100  # then this many transactions' items per call

lock = threading.Condition()

log = logging.getLogger(__name__)


def chosen_ask() -> Callable[[str, str, dict], dict] | None:
    """The agents.ask_* function of the provider chosen in Settings, set to
    the chosen model (if one was picked), or None if AI is off or that
    provider's key isn't in core/.env (the SDKs would otherwise use other
    credentials found on the machine)."""
    agent = user_prefs.load_user_prefs()["agent"]
    if not agent["enabled"]:
        return None
    if not config.get_env_var(agents.PROVIDERS[agent["provider"]][1]):
        log.error(f"Failed AI categorization: no {agent['provider']} key; AI is off for this run")
        return None
    ask = getattr(agents, f"ask_{agent['provider']}")
    return functools.partial(ask, **{k: v for k, v in agent.items() if k in ("model", "effort")})


def plan_calls(transactions: list[dict], table: dict[str, dict]) -> list[list[str]]:
    """The items to ask about, one list per call, in the order of
    `transactions`: each item name once, if `table` has no entry for it
    (never the "Sales tax" line split.py may add, nor a line with no memo
    from a divided whole transaction). The first SINGLE_CALLS
    transactions with such items get a call each, then BATCH_SIZE per call."""
    per_transaction, sent = [], set()
    for t in transactions:
        items = list(dict.fromkeys(
            line["memo"] for line in t["split_lines"]
            if line["memo"] not in NOT_ITEMS and line["memo"] not in table and line["memo"] not in sent
        ))
        if items:
            sent.update(items)
            per_transaction.append(items)
    return per_transaction[:SINGLE_CALLS] + [
        [item for items in per_transaction[i:i + BATCH_SIZE] for item in items]
        for i in range(SINGLE_CALLS, len(per_transaction), BATCH_SIZE)
    ]


def prompt(labels: list[str], items: list[str]) -> tuple[str, str]:
    """(system, user) prompt asking for the category (numbered `labels`) of each numbered item."""
    system = INSTRUCTIONS + "\n\nCategories:\n0. (none fits)\n" + "\n".join(
        f"{n}. {label}" for n, label in enumerate(labels, 1)
    )
    return system, "Items:\n" + "\n".join(f"{n}. {item}" for n, item in enumerate(items, 1))


def write_answers(answer: dict, items: list[str], category_ids: list[str], table: dict[str, dict]) -> int:
    """Write to `table`, as "agent", each item of `items` that `answer`
    gives exactly once a real category (by number, from 1, in
    `category_ids`), unless it has an entry by now. Returns how many."""
    counts = collections.Counter(a["item"] for a in answer["results"])
    written = 0
    for a in answer["results"]:
        if counts[a["item"]] == 1 and 1 <= a["item"] <= len(items) and 1 <= a["category"] <= len(category_ids):
            item = items[a["item"] - 1]
            if item not in table:
                table[item] = {"category": category_ids[a["category"] - 1], "by": "agent"}
                written += 1
    return written


def follow_budget(old_categories: list[dict], categories: list[ynab_client.Category]) -> None:
    """After a budget change: point each plugin's table at `categories`, the
    new budget's. An entry whose category isn't one of them moves to the one
    with the same "Group: Name" label it had in `old_categories` (another
    budget's, as saved in ynab_categories.json), keeping
    "by"; with no such category it is removed, so the agent asks again."""
    old_labels = {c["id"]: f"{c['group_name']}: {c['name']}" for c in old_categories}
    new_ids = {f"{c.group_name}: {c.name}": c.id for c in categories}
    ids = {c.id for c in categories}
    for plugin in list_plugins():
        if not plugin.categories_path.exists():
            continue
        table = json.loads(plugin.categories_path.read_text())
        kept = {}
        for item, entry in table.items():
            category = entry["category"]
            if category not in ids:
                category = new_ids.get(old_labels.get(category))
            if category:
                kept[item] = {**entry, "category": category}
        plugin.categories_path.write_text(json.dumps(kept, indent=2))
        log.info(f"{plugin.name}: category table updated for the new budget: "
                 f"{len(kept)} kept, {len(table) - len(kept)} removed")


class Agent:
    """Asks `ask` about each of `calls` in turn, on `thread`, and writes the
    answers to `table` (`labels`: category label -> id), saving it to
    `table_path` after each answered call. Set `stopped` to stop it after
    its current call, which then writes nothing."""

    def __init__(self, ask: Callable[[str, str, dict], dict], calls: list[list[str]], labels: dict[str, str],
                 table: dict[str, dict], table_path: Path):
        self.ask, self.calls, self.labels, self.table, self.table_path = ask, calls, labels, table, table_path
        self.pending = {item for call in calls for item in call}  # sent or queued
        self.stopped = False
        self.thread = threading.Thread(target=self.run, name="AI agent", daemon=True)

    def wait(self, items: list[str]) -> None:
        """Return once none of `items` is pending, saying so if any is."""
        with lock:
            if not self.pending.isdisjoint(items):
                print("Waiting for Agent suggestions...")
                lock.wait_for(lambda: self.pending.isdisjoint(items))

    def run(self) -> None:
        total = len(self.pending)
        log.info(f"Attempting AI categorization of {total} item(s)...")
        done = written = failed = 0
        for items in self.calls:
            if self.stopped:
                break
            try:
                answer = self.ask(*prompt(list(self.labels), items), SCHEMA)
                with lock:
                    if self.stopped:
                        break
                    written += write_answers(answer, items, list(self.labels.values()), self.table)
                    self.table_path.write_text(json.dumps(self.table, indent=2))
            except Exception as e:  # the review carries on; these items stay "(none)"
                if self.stopped:
                    break
                failed += 1
                log.error(f"Failed AI categorization: {f'answer has no {e}' if isinstance(e, KeyError) else e}")
            finally:
                with lock:
                    self.pending.difference_update(items)
                    lock.notify_all()
            done += len(items)
            log.info(f"Progress on AI categorization: {done} of {total} item(s)")
        result = f"{written} of {total} item(s) categorized"
        if done < total:
            log.error(f"Failed AI categorization: stopped by quit; {result}")
        elif failed:
            log.error(f"Failed AI categorization: {failed} of {len(self.calls)} call(s) failed; {result}")
        else:
            log.info(f"Succeeded with AI categorization: {result}")
