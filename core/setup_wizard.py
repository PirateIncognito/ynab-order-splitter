"""First-run setup: collect the YNAB token, validate it, and pick a budget."""

import logging

from core import config, ui, user_prefs, ynab_client

log = logging.getLogger(__name__)


def prompt_for_token() -> str | None:
    """Ask for a YNAB token until YNAB accepts one. None if the user
    cancels (Ctrl+C)."""
    print("Enter your YNAB personal access token.")
    print("(Create one at https://app.ynab.com/settings/developer)")
    while True:
        token = ui.secret("YNAB_ACCESS_TOKEN:")
        if token is None:
            return None
        token = token.strip()
        try:
            ynab_client.validate_token(token)
        except ynab_client.InvalidTokenError as e:
            print(f"That token was rejected by YNAB: {e}\n")
            continue
        return token


def choose_budget(token: str, back_label: str | None = None) -> bool:
    """Ask which budget to use and save it (the same one too, to keep its
    name current); True if the budget changed.
    A new budget clears the sales tax category (an id from the old one).
    With `back_label`, the user can back out without changing anything."""
    budgets = ynab_client.list_budgets(token)
    if not budgets:
        raise RuntimeError("No YNAB budgets found on this account.")
    budget = ui.choose(
        "Which budget should YNABuddy use?", [(b.name, b) for b in budgets], back_label=back_label
    )
    if budget is None:
        return False
    prefs = user_prefs.load_user_prefs()
    changed = budget.id != prefs["ynab_budget_id"]
    prefs["ynab_budget_id"] = budget.id
    prefs["ynab_budget_name"] = budget.name
    if changed:
        prefs["sales_tax_category_id"] = None
    user_prefs.save_user_prefs(prefs)
    log.info(f"Using budget: {budget.name}")
    return changed


def run_setup_wizard() -> None:
    ui.clear_screen()
    print("=== YNABuddy first-run setup ===\n")
    token = prompt_for_token()
    if token is None:
        raise SystemExit("Setup cancelled.")
    config.set_ynab_access_token(token)
    log.info("Token verified and saved.")

    choose_budget(token)
    if user_prefs.needs_ynab_setup():  # Ctrl+C at the budget question
        raise SystemExit("Setup cancelled.")
    log.info("Setup complete.")
