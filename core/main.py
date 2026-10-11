import logging
import logging.handlers
import sys
from collections.abc import Callable
from pathlib import Path

from ynab.exceptions import ApiException

from core import agents, config, match, plugin_settings, setup_wizard, ui, user_prefs, ynab_client
from plugins import orchestrator

LOG_PATH = Path(__file__).resolve().parent.parent / "ynabuddy.log"

log = logging.getLogger(__name__)


# Result of the one YNAB token check made at startup (see initialize()),
# shown in Settings instead of calling YNAB on every redraw.
token_status = "not checked"


def _check_token(token: str | None) -> str:
    """Ask YNAB whether `token` works: "valid", "invalid" (rejected),
    "not set", or "unchecked" (YNAB couldn't be reached)."""
    if not token:
        return "not set"
    try:
        ynab_client.validate_token(token)
        return "valid"
    except ynab_client.InvalidTokenError:
        return "invalid"
    except Exception as e:  # outage, rate limit, offline
        log.warning(f"Couldn't reach YNAB to check the access token ({e}); continuing.")
        return "unchecked"


def _change_token() -> None:
    token = setup_wizard.prompt_for_token()
    if token is None:
        return
    config.set_ynab_access_token(token)
    global token_status
    token_status = "valid"  # prompt_for_token only returns a token YNAB accepted
    log.info("Token updated.")


def _change_budget() -> None:
    token = config.get_ynab_access_token()
    if not token:
        log.warning("No YNAB access token is set yet. Change the access token first.")
        return
    if setup_wizard.choose_budget(token, back_label="Back"):
        plugin_settings.clear_accounts()  # first: the fetch below can fail in ways it doesn't catch
        log.info("Attempting categories for the new budget...")
        try:
            categories = match.fetch_categories(token, user_prefs.load_user_prefs()["ynab_budget_id"])
        except ApiException as e:  # Match and Split fetches them again
            log.error(f"Failed categories: {ynab_client.describe_api_error(e)}; "
                      "category tables follow the new budget on the next fetch")
        else:
            log.info(f"Succeeded with categories: {len(categories)} fetched")
        plugin_settings.run_first_run_setup()


def _ynab_settings() -> list[tuple[str, str, Callable[[], None]]]:
    """(label, current value, change function) for each core YNAB setting --
    the same shape as a plugin's settings()."""
    budget = user_prefs.load_user_prefs().get("ynab_budget_name") or "not set"
    return [
        ("YNAB access token", token_status, _change_token),
        ("Budget", budget, _change_budget),
    ]


def _update_ynab_credentials_menu() -> None:
    while True:
        change = ui.menu(
            "=== Update YNAB credentials / budget ===",
            [(f"{label}: {value}", change) for label, value, change in _ynab_settings()],
        )
        if change is None:
            return
        change()


def _configure_plugin_menu(plugin) -> None:
    while True:
        change = ui.menu(
            f"=== Configure {plugin.name} ===",
            [(f"{s.label}: {s.value}", s.change) for s in plugin_settings.all_settings(plugin)],
        )
        if change is None:
            return
        change()


def _plugins_menu() -> None:
    plugins = orchestrator.list_plugins()
    if not plugins:
        log.info("No plugins installed.")
        return
    while True:
        plugin = ui.menu("=== Configure plugins ===", [
            (f"{p.name}{' (setup needed)' if plugin_settings.needs_setup(p) else ''}", p) for p in plugins
        ])
        if plugin is None:
            return
        _configure_plugin_menu(plugin)


def _with_details(heading: str, lines: list[str]) -> str:
    """A menu label with indented detail lines under it."""
    return heading + "".join(f"\n    {line}" for line in lines)


def _colours_label() -> str:
    choice = user_prefs.load_user_prefs()["colours"]
    return f"Colours: {choice}" + (f" ({ui.theme_name})" if choice == "auto" else "")


def _colours_menu() -> None:
    choice = ui.choose("Colours:", [
        ("Auto -- match the terminal's background", "auto"),
        ("Dark (Gruvbox)", "dark"),
        ("Light (Gruvbox light)", "light"),
    ], back_label="Back")
    if choice is None:
        return
    prefs = user_prefs.load_user_prefs()
    prefs["colours"] = choice
    user_prefs.save_user_prefs(prefs)
    ui.set_theme(choice)
    log.info(f"{_colours_label()}.")


MEMO_CHOICES = [
    ("None", "none"),
    ("Order number, on the transaction", "order"),
    ("Item names", "items"),
]


def _memos_label() -> str:
    choice = user_prefs.load_user_prefs()["memos"]
    return "Memos: " + next(label for label, value in MEMO_CHOICES if value == choice)


def _memos_menu() -> None:
    choice = ui.choose("Memos written to YNAB:", MEMO_CHOICES, back_label="Back")
    if choice is None:
        return
    prefs = user_prefs.load_user_prefs()
    prefs["memos"] = choice
    user_prefs.save_user_prefs(prefs)
    log.info(f"{_memos_label()}.")


def _agent_label() -> str:
    agent = user_prefs.load_user_prefs()["agent"]
    if not agent["enabled"]:
        return "Off"
    model = agent.get("model_name", f"default model {agents.DEFAULT_MODELS[agent['provider']]}")
    return f"On, {agents.PROVIDERS[agent['provider']][0]}, {model}"


def _ensure_key(provider: str) -> bool:
    """Whether `provider`'s API key is in core/.env, asking for it if not."""
    label, key_name = agents.PROVIDERS[provider]
    if config.get_env_var(key_name):
        return True
    key = ui.secret(f"{label} API key:")
    if key is None:
        return False
    config.set_env_var(key_name, key.strip())
    return True


def _choose_model(provider: str) -> dict:
    """The model picked from the provider's list (an agents.list_*_models()
    entry), or {} if listing failed or the user went back."""
    log.info("Attempting to list models...")
    try:
        models = getattr(agents, f"list_{provider}_models")()
    except Exception as e:  # offline, bad key
        log.error(f"Failed listing models: {e}")
        return {}
    log.info(f"Succeeded with listing models: {len(models)} found")
    return ui.choose("Model:", [(m["model_name"], m) for m in models], back_label="Back") or {}


def _ai_agent_menu() -> None:
    """Off, or on with one provider, then a pick from its models (Back keeps
    the current one). Without that provider's key, AI stays off."""
    choice = ui.choose("AI agent:", [("Off", "off")] + [
        (label, provider) for provider, (label, _) in agents.PROVIDERS.items()
    ], back_label="Back")
    if choice is None:
        return
    prefs = user_prefs.load_user_prefs()
    agent = prefs["agent"]
    if choice not in ("off", agent["provider"]):  # a new provider: its default model until one is picked
        agent = {"provider": choice}
    agent = {**agent, "enabled": choice != "off" and _ensure_key(choice)}
    if agent["enabled"]:
        agent.update(_choose_model(choice))
    prefs["agent"] = agent
    user_prefs.save_user_prefs(prefs)
    log.info(f"AI agent: {_agent_label()}.")


def settings_menu() -> None:
    while True:
        ynab_lines = [f"{label}: {value}" for label, value, _ in _ynab_settings()]
        plugin_lines = []
        for plugin in orchestrator.list_plugins():
            plugin_lines.append(plugin.name)
            plugin_lines += [f"    {s.label}: {s.value}" for s in plugin_settings.all_settings(plugin)]
        action = ui.menu("=== Settings ===", [
            (_with_details("YNAB", ynab_lines), _update_ynab_credentials_menu),
            (_with_details("Plugins", plugin_lines or ["(none installed)"]), _plugins_menu),
            (_with_details("Display", [_colours_label()]), _colours_menu),
            (_with_details("AI agent", [_agent_label()]), _ai_agent_menu),
            (_with_details("Memos", [_memos_label()]), _memos_menu),
        ])
        if action is None:
            return
        action()


def _match_and_split_menu() -> None:
    budget = user_prefs.load_user_prefs().get("ynab_budget_name")
    plugins = orchestrator.list_plugins()
    while True:
        plugin = ui.menu(f"=== Match and Split (budget: {budget}) ===", [
            (f"{p.name}{' (setup needed)' if plugin_settings.needs_setup(p) else ''}", p) for p in plugins
        ])
        if plugin is None:
            return
        if not match.has_last_results(plugin):
            match.run_cycle_for_plugin(plugin)
            continue
        action = ui.menu(f"=== {plugin.name} ===", [
            ("Match and Split", match.run_cycle_for_plugin),
            ("Review last results", match.review_last_results),
        ])
        if action is not None:
            action(plugin)


def main_menu() -> None:
    while True:
        action = ui.menu("=== YNABuddy ===", [
            ("Match and Split YNAB Transactions", _match_and_split_menu),
            ("Settings", settings_menu),
        ], back_label="Exit")
        if action is None:
            print("Goodbye.")
            return
        action()


def _setup_logging() -> None:
    """Status messages go to three places: the screen as they happen, a
    queue that the next menu or review screen shows again after clearing
    the screen, and ynabuddy.log with timestamps. Only the app's own
    messages reach the screen; library logs (amazon-orders etc.) go to the
    file only, and background threads' (the AI agent's) to the queue and
    file, so they don't print over the screen the user is on."""
    to_screen = logging.StreamHandler(sys.stdout)
    to_next_screen = logging.handlers.QueueHandler(ui.messages)
    for handler in (to_screen, to_next_screen):
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler.addFilter(lambda r: r.name.startswith(("__main__", "core", "plugins", "plugin_")))
    to_screen.addFilter(lambda r: r.threadName == "MainThread")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(LOG_PATH), to_screen, to_next_screen],
    )


def initialize() -> None:
    """Get the app ready to use: YNAB set up with a working token, and
    every installed plugin set up. Only interrupts the user when something
    needs their input."""
    global token_status
    if user_prefs.needs_ynab_setup():
        setup_wizard.run_setup_wizard()  # validates the token it collects
        token_status = "valid"
    else:
        token_status = _check_token(config.get_ynab_access_token())
        if token_status == "valid":
            log.info("YNAB access token is valid.")
        elif token_status in ("invalid", "not set"):
            log.warning(f"YNAB access token is {token_status}. Please enter a new one.")
            _change_token()
    plugin_settings.run_first_run_setup()


def main() -> None:
    _setup_logging()
    ui.set_theme(user_prefs.load_user_prefs()["colours"])
    initialize()
    main_menu()


if __name__ == "__main__":
    main()
