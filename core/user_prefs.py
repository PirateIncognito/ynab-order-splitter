"""Read/write core/user_prefs.json — non-secret application settings."""

import json
from pathlib import Path
from typing import Any

CORE_DIR = Path(__file__).resolve().parent
USER_PREFS_PATH = CORE_DIR / "user_prefs.json"

DEFAULT_PREFS: dict[str, Any] = {
    "ynab_budget_id": None,
    "ynab_budget_name": None,
    "enabled_plugins": [],
    # How many days before the earliest candidate YNAB transaction's date a
    # scraping plugin should look back when refreshing its vendor order
    # data -- covers the normal lag between a vendor order and the bank
    # posting the matching transaction. Not exposed in the settings menu;
    # edit user_prefs.json directly to change it.
    "vendor_lookback_margin_days": 7,
    # Match window for every plugin: a vendor record matches a YNAB
    # transaction dated up to this many days before or after it.
    "match_window_days": 5,
    # Splits put sales tax on its own line, in this category, instead of
    # spreading it over the items. Not in the settings menu yet.
    "split_sales_tax_line": False,
    "sales_tax_category_id": None,
    # Screen colours: "auto" (Gruvbox or Gruvbox light, to match the
    # terminal's background), "dark" or "light". Settings > Display.
    "colours": "auto",
    # What push writes to YNAB memos: "none", "order" (the order number on
    # the transaction) or "items" (each split line's item name). A memo
    # already in YNAB is never overwritten. Settings > Memos.
    "memos": "order",
    # AI categorization: on/off, the provider (a key of
    # core/agents.PROVIDERS) and, once picked, the model: the keys of an
    # agents.list_<provider>_models() entry ("model" id, "model_name",
    # Claude's "effort"). No model: the provider's default. Settings > AI agent.
    "agent": {
        "enabled": False,
        "provider": None,
    },
}


def load_user_prefs() -> dict[str, Any]:
    if not USER_PREFS_PATH.exists():
        return dict(DEFAULT_PREFS)
    return {**DEFAULT_PREFS, **json.loads(USER_PREFS_PATH.read_text())}


def save_user_prefs(prefs: dict[str, Any]) -> None:
    USER_PREFS_PATH.write_text(json.dumps(prefs, indent=2))


def needs_ynab_setup() -> bool:
    prefs = load_user_prefs()
    return not prefs.get("ynab_budget_id")
