"""The settings every vendor plugin shares: how its transactions are found
in YNAB (mapping) and the run mode, plus the date match window, which is
one core setting for all plugins (core/user_prefs.json).

Mapping and run mode are stored in each plugin's own
<plugin_name>_user_prefs.json, but asked for, checked and read here --
they're the same for every plugin and need YNAB (accounts, token, budget),
so they belong to core rather than the plugin SDK. A plugin's own settings (credentials, plugin-specific options)
come from its settings() and are shown after these.
"""

import logging
from typing import Any

from core import config, ui, user_prefs, ynab_client
from core.milliunits import milliunits_to_dollars
from plugins.orchestrator import list_plugins
from plugins.plugin import Setting, VendorPlugin

log = logging.getLogger(__name__)

SHARED_DEFAULTS: dict[str, Any] = {
    "mode": None,  # "account" or "payee" -- see configure_vendor_mapping()
    "account_ids": [],
    "account_names": [],
    "payee_name": None,
    "run_mode": "unapproved",  # "unapproved" or "nonsplit"
}


def load(plugin: VendorPlugin) -> dict[str, Any]:
    """The plugin's prefs, with the shared settings' defaults filled in."""
    return {**SHARED_DEFAULTS, **plugin.load_own_prefs()}


def _save(plugin: VendorPlugin, changes: dict[str, Any]) -> None:
    plugin.save_own_prefs({**load(plugin), **changes})


def has_mapping(prefs: dict[str, Any]) -> bool:
    if prefs["mode"] == "account":
        return bool(prefs["account_ids"])
    if prefs["mode"] == "payee":
        return bool(prefs["payee_name"])
    return False


def _mapping_text(prefs: dict[str, Any]) -> str:
    if prefs["mode"] == "account":
        names = prefs["account_names"]
        return f"dedicated account(s) -- {', '.join(names) if names else 'not set'}"
    if prefs["mode"] == "payee":
        return f"payee search -- '{prefs['payee_name']}'"
    return "not set"


def all_settings(plugin: VendorPlugin) -> list[Setting]:
    """Every setting for `plugin`, in menu order: the shared ones, then the
    plugin's own."""
    prefs = load(plugin)
    return [
        Setting(
            "Mapping",
            _mapping_text(prefs),
            lambda: configure_vendor_mapping(plugin),
            missing=not has_mapping(prefs),
        ),
        Setting(
            "Match window",
            f"{user_prefs.load_user_prefs()['match_window_days']} day(s), all plugins",
            configure_match_window,
        ),
        Setting("Run mode", prefs["run_mode"], lambda: configure_run_mode(plugin)),
        *plugin.settings(),
    ]


def needs_setup(plugin: VendorPlugin) -> bool:
    return any(s.missing for s in all_settings(plugin))


def clear_accounts() -> None:
    """After a budget change: clear every account-mode plugin's accounts,
    which belong to the old budget. Payee mappings work in any budget."""
    for plugin in list_plugins():
        if load(plugin)["mode"] == "account":
            _save(plugin, {"account_ids": [], "account_names": []})
            log.info(f"{plugin.name}: accounts cleared for the new budget")


def run_first_run_setup() -> None:
    """For every installed plugin, ask for each required setting that's
    still missing, in menu order. Settings with defaults (match window, run
    mode, ...) aren't asked; they can be changed from the settings menu."""
    for plugin in list_plugins():
        if needs_setup(plugin):
            print(f"\n=== Setup needed: {plugin.name} ===")
            for setting in all_settings(plugin):
                if setting.missing:
                    setting.change()


def configure_vendor_mapping(plugin: VendorPlugin) -> None:
    """Ask how to locate this vendor's transactions in YNAB -- dedicated
    account(s) or payee-name search -- and save the answer into the
    plugin's prefs file. Does nothing if core YNAB setup hasn't happened
    yet, or if the user cancels."""
    token = config.get_ynab_access_token()
    budget_id = user_prefs.load_user_prefs().get("ynab_budget_id")
    if not token or not budget_id:
        log.warning("YNAB isn't set up yet -- finish core setup before configuring plugins.")
        return

    vendor_name = plugin.name
    mode = ui.choose(
        f"How are {vendor_name} transactions located in YNAB?",
        [
            (f"Dedicated account(s) -- all transactions in the account(s) belong to {vendor_name}", "account"),
            (f"Search by payee name (e.g. '{vendor_name}')", "payee"),
        ],
    )
    if mode is None:
        return
    mapping = {
        "mode": mode,
        "account_ids": [],
        "account_names": [],
        "payee_name": vendor_name,
    }
    if mapping["mode"] == "account":
        accounts = ynab_client.list_accounts(token, budget_id)
        open_accounts = [a for a in accounts if a.on_budget and not a.closed]
        if not open_accounts:
            log.warning("No open budget accounts found; falling back to payee search.")
            mapping["mode"] = "payee"
        else:
            options = [
                (f"{a.name} ({a.type.value}) -- balance: {milliunits_to_dollars(a.balance):,.2f}", a)
                for a in open_accounts
            ]
            while True:
                account = ui.choose(f"Select an account for {vendor_name}", options)
                if account is None:
                    return
                if str(account.id) not in mapping["account_ids"]:
                    mapping["account_ids"].append(str(account.id))
                    mapping["account_names"].append(account.name)
                if not ui.confirm("Add another account for this vendor?"):
                    break
    if mapping["mode"] == "payee":
        payee_name = ui.text("Payee name to search for:", default=vendor_name)
        if payee_name is None:
            return
        mapping["payee_name"] = payee_name.strip() or vendor_name

    _save(plugin, mapping)
    log.info(f"{vendor_name} preferences saved.")


def configure_match_window() -> None:
    """Ask for the date match window (how many days a YNAB transaction's
    date may differ from its vendor record's) and save it into core prefs:
    one window for every plugin."""
    print(
        "\nDate match window, for all plugins: how many days a YNAB transaction's "
        "date may be before or after the vendor's order or charge date."
        "\n0 days is the minimum."
        "\n5 days is recommended."
        "\n9 days is the maximum."
    )
    prefs = user_prefs.load_user_prefs()
    days = ui.text(
        "Days [0 - 9]:",
        default=str(prefs["match_window_days"]),
        validate=lambda s: s.strip() in set("0123456789") or "Enter a single digit, 0-9.",
    )
    if days is None:
        return
    prefs["match_window_days"] = int(days)
    user_prefs.save_user_prefs(prefs)
    log.info("Match window saved.")


def configure_run_mode(plugin: VendorPlugin) -> None:
    """Ask which transactions this plugin should process, and save the
    answer into the plugin's prefs file."""
    run_mode = ui.choose(
        "Which transactions should this plugin process?",
        [("Unapproved only (default)", "unapproved"), ("All transactions (--nonsplit)", "nonsplit")],
    )
    if run_mode is None:
        return
    _save(plugin, {"run_mode": run_mode})
    log.info(f"{plugin.name} run mode saved.")
