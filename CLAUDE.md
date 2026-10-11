# YNABuddy

Console app that matches vendor order data to YNAB transactions, splits each
transaction by the items bought, helps categorize the lines (manually, or
with optional AI suggestions) and writes approved splits back to YNAB.

YNABuddy is built on a **plugin architecture** so it can support many
vendors. Each vendor is a plugin: a connector, built from the plugin SDK
(`plugins/plugin.py`), that gets that vendor's order data and hands it to
core. Core does everything vendor-neutral --
matching, splitting, review, categorization -- and is the only part that
talks to YNAB or an AI provider. 

**v1.0.0 works with amazon.com** (the `plugins/amazon.com/` plugin). 

Code diagrams: `DIAGRAM.md`.

## Commands

- Setup: `uv sync`, then `uv run playwright install chromium`
- Run: `uv run python run.py` (from the project root)
- Tests: `uv run pytest -q` -- offline; must pass before any commit
- Deps: `uv add <pkg>` -- only when a change needs it
- amazon-orders (https://github.com/alexdlaird/amazon-orders) changes often:
  before Amazon plugin work, check PyPI for a newer release and read its
  CHANGELOG before upgrading.

## Layout

- `run.py` -- entry point; run from the project root
- `core/` -- the app; vendor-neutral
  - `main.py` -- startup, menus, settings
  - `setup_wizard.py` -- first-run YNAB setup (token, budget)
  - `match.py` -- the Match and Split cycle
  - `split.py` -- split lines from a matched vendor record
  - `review.py` -- the review screen
  - `push.py` -- push approved transactions to YNAB
  - `categorize.py` -- AI categorization
  - `agents.py` -- AI connector (Claude, OpenAI, Gemini)
  - `ynab_client.py` -- the YNAB SDK wrapper
  - `ui.py` -- all console I/O (uses [Questionary](https://github.com/tmbo/questionary) )
  - `plugin_settings.py` -- settings every plugin shares (YNAB mapping, run mode)
  - `config.py` -- loading/writing core secrets (core/.env)
  - `user_prefs.py` -- read/write core/user_prefs.json — non-secret application settings 
  - `milliunits.py` -- YNAB units
- `plugins/` -- vendor plugins
  - `plugin.py` -- the plugin SDK: the contract every plugin implements
  - `orchestrator.py` -- finds and loads installed plugins
  - `amazon.com/plugin.py` -- the Amazon plugin
- `tests/` -- pytest
  - `conftest.py` -- blocks all network requests and browser launches

## Writing a plugin

- Subclass `VendorPlugin` from `plugins/plugin.py` in
  `plugins/<vendor>/plugin.py`; the orchestrator loads only such subclasses.
- Required: `get_vendor_transaction(date, amount, exclude)` (what core calls
  to match a YNAB transaction) and `get_data_from_JSON(date, amount)`.
- Optional: `get_data_from_vendor(date)` to scrape (set
  `scrapes_vendor_data = True`), and `settings()` for the plugin's own
  settings (credentials, options).
- A plugin never calls YNAB or an AI provider; it returns
  `VendorTransaction` / `VendorLineItem` and core does the rest.

## Coding rules

- Plain stdlib code, inline; no helpers for a single call, no defensive
  checks a change didn't ask for.
- JSON files: `json.loads(path.read_text())` / `path.write_text(json.dumps(...))`.
- questionary only in `core/ui.py`; menus and prompts return values, never
  positions.
- `plugins/plugin.py` imports nothing from `core/`; vendor URLs and vendor
  logic live in the plugin.
- Money is int YNAB milliunits end to end, outflows negative; convert only
  at the edges.
- Steps the user waits on log "Attempting ...", "Progress on ...", then
  "Succeeded with ..." or "Failed ...: <reason>".
- A status that claims certainty (e.g. a split marked `exact`) is set only
  where the data checks it.
- Tests guard real failure modes: each fails if a plausible bug is
  introduced; no tests that only restate code or assert on mocks.

## YNAB and secrets

- The app writes only to the budget selected in Settings, and only through
  `core/push.py`, after the user confirms.
- When developing, test writes against a sandbox budget of your own,
  checked by id -- never your real budget.
- YNAB rate limit: 200 requests/hour per token. Prefer bulk endpoints.
  API spec: https://api.ynab.com/papi/open_api_spec.yaml; the installed SDK
  in `.venv` is the truth for model and method names.
- Never read, print or commit `.env` files, `*_cookies.json`, or any
  per-install data (order data, match results, prefs, categories) --
  `.gitignore` lists them.
