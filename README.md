# YNABuddy

Console app that matches vendor order data (i.e. Amazon, Costco) to
[YNAB](https://www.ynab.com) transactions, splits each transaction by the items bought, helps you categorize the lines, and writes the approved splits back to YNAB.

What it does today (v1.2.0; changes in `CHANGELOG.md`):

- **Amazon data**: scrapes your Amazon order history and card charges with
  [amazon-orders](https://github.com/alexdlaird/amazon-orders). Whole Foods
  purchases are linked to their order, even when paid in several charges.
- **Costco data**: reads warehouse receipts you export with
  [Costco-Receipt-Downloader](https://github.com/harrykhh/Costco-Receipt-Downloader)
  into `plugins/costco.com/` (no scraping; gas and costco.com charges have
  no receipt there and won't match).
- **Matching**: each YNAB transaction is matched to an Amazon charge or
  order, or a Costco receipt, by exact amount and date.
- **Splitting**: the charged amount is split across the items; shipping,
  discounts and rounding are spread over the items (sales tax on its own
  line is an option, off by default).
- **Review screen**, one key per action:
  `[a]` approve, `[e]` edit category, `[s]` skip, `[p]` previous transaction,
  `[m]` merge identical categories, `[d]` divide, `[u]` undo last change,
  `[o]` open order in browser, `[q]` quit
  -- edit categories with autocomplete; a per-plugin item -> category
  table remembers your choices. Merge folds lines that share a category
  into one; undo steps back through edits, divides and merges on the
  transaction on screen; previous revisits the transactions approved or
  skipped earlier in this review (nothing is pushed until it ends).
- **AI suggestions (optional)**: Claude, OpenAI or Gemini suggest categories
  for new items in the background; you approve every one.
- **Memos**: Settings > Memos picks what goes into YNAB memos: none, the
  order number on the transaction (default), or item names. A memo already
  in YNAB is never overwritten.
- **Push**: approved splits are written to the budget selected in Settings,
  after a y/n prompt and a check that nothing changed in YNAB meanwhile.
  Split lines get no payee of their own; YNAB shows them under the
  transaction's.

Docs: code diagrams `DIAGRAM.md`; release notes `CHANGELOG.md`.

## Running

Needs [uv](https://docs.astral.sh/uv/) (it installs the right Python and the
dependencies), a YNAB personal access token, and an Amazon account and/or
Costco receipt exports.

```
uv sync
uv run playwright install chromium   # amazon-orders' browser login fallback
uv run python run.py
```

Run `run.py` from the project root -- don't run `core/main.py` directly;
its imports (`from core import ...`) only resolve with the project root on
`sys.path`.

The first run asks for the YNAB token and a budget, then each plugin's
required settings (for Amazon: which YNAB account(s) hold Amazon
transactions, and the Amazon login). For Costco, save each
Costco-Receipt-Downloader export (`costco-receipts-<FROM>_to_<TO>.json`)
into `plugins/costco.com/`; new exports are merged into what's already
there. Changing the budget later clears the
plugin accounts and asks for the new budget's.

## Screenshots

The review screen: one transaction, its split lines and the keys.

![The review screen](docs/images/review-screen.png)

Editing a category: autocomplete over the budget's categories.

![Editing a category with autocomplete](docs/images/edit-category.png)

## Tests

```
uv run pytest -q
```

Offline: a guard in `tests/conftest.py` makes any real YNAB, Amazon or AI
request -- or browser launch -- fail the test.

## Where things are stored

All per-install and gitignored:

- Secrets: `core/.env` (YNAB token, AI keys), `plugins/<plugin>/.env` (vendor
  login), the Amazon session cookies -- readable by your user only.
- Settings: `core/user_prefs.json`, `plugins/<plugin>/<plugin>_user_prefs.json`.
- Data: `core/ynab_categories.json` (the budget's categories),
  `plugins/amazon.com/amazon.com_order_data.json` (scraped orders and
  charges), `plugins/costco.com/costco.com_order_data.json` (merged Costco
  receipts), `plugins/<plugin>/<plugin>_ynab_matched.json` (latest match
  results and review progress), `plugins/<plugin>/<plugin>_ynab_approved.json`
  (approved, waiting to be pushed), `plugins/<plugin>/<plugin>_categories.json`
  (item -> category table).
- Status log: `ynabuddy.log`; Amazon debug snapshots (off by default): `output/`.
