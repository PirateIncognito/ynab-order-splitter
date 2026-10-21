# Changelog

## 1.2.1 -- 2026-10-20

### Fixed
- **All-transactions run mode no longer re-offers pushed transactions** --
  a transaction pushed with one category (no split) looked untouched in
  YNAB, so every later run in that mode matched it and asked to review
  it again. Each plugin now keeps the ids it has pushed in
  `plugins/<plugin>/<plugin>_ynab_pushed.json` and skips them; delete
  that file to have them offered again. Transactions pushed before 1.2.1
  are offered once more, then remembered. Unapproved-only mode was not
  affected.

## 1.2.0 -- 2026-10-13

### Added
- **Review screen: `[p]` previous transaction** -- goes back to the
  previous transaction of this review, approved or skipped, and shows
  its earlier outcome. `[a]` approves it again with any changes, `[s]`
  takes it out of the push queue. Nothing is pushed until the review
  ends, so any of them can still be changed.

### Changed
- **Plugin account mapping lists accounts by number** -- every open
  account is printed as a table (name, type, balance) and picked by
  typing its number; budgets with more than 35 accounts can now pick
  all of them. Accounts already added are marked "(added)".

### Fixed
- Settings menu: detail lines line up under their heading instead of
  under its number.

## 1.1.0 -- 2026-10-11

### Added
- **Costco plugin** (`plugins/costco.com`): matches YNAB transactions to
  Costco warehouse receipts exported with
  [Costco-Receipt-Downloader](https://github.com/harrykhh/Costco-Receipt-Downloader)
  and splits them by item. Exports saved into `plugins/costco.com/` are
  merged into one copy, so deleting an old export loses nothing.
  Receipts mixing purchases and returns are skipped. Gas and costco.com
  charges have no receipt and don't match.
- **Review screen: `[m]` merge identical categories** -- combines sub-transactions
  that share a category into one line. A merged line is never added to the
  item -> category table; its items are still recorded with their final
  category on approve. This helps reduce unnecessary splits with the same category.
- **Review screen: `[u]` undo last change** -- steps back through category
  edits, divides and merges on the transaction on screen, back to the
  original. In memory only; approved transactions are never touched.
- **Settings > Memos** -- choose what push writes to YNAB memos: None,
  Order number on the transaction (default), or Item names. A memo already
  in YNAB is never overwritten, and merged lines get no memo.

### Changed
- Review screen lists one option per line: approve, edit category, skip,
  merge, divide (split a sub-transaction in half), undo, open order in
  browser, quit.
- Match and Split goes straight to matching when a plugin has no last
  results to review, instead of showing a one-option menu.
- Split lines no longer get item names as memos by default; the order
  number goes on the transaction instead (Settings > Memos > Item names
  for the old behaviour). Transactions approved under 1.0.0 and still
  queued push without a memo.
- Push no longer sends a payee: split lines show under the transaction's
  payee in YNAB.

### Removed
- Review screen `[p]` payee option. By default in YNAB sub-transactions inherit their parent transaction payee data (even though the field shows blanks). This program does not support modifying the payee.

### Fixed
- Whole Foods purchases paid in several charges (e.g. points plus card)
  are linked to their Amazon order, so the order number -- not the
  receipt id -- is used. The link survives later re-scrapes.

## 1.0.0 -- 2026-10-10

First public release: Amazon plugin (order history and card charges via
amazon-orders), matching by amount and date, splitting with shipping,
discounts and rounding spread over the items, review screen with
category autocomplete and a per-plugin item -> category table, optional
AI category suggestions (Claude, OpenAI, Gemini), and push to YNAB after
a y/n prompt and a freshness check.
