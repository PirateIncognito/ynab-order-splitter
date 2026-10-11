"""Amazon.com vendor plugin.

Scrapes order history directly from Amazon via the `amazon-orders` package
(https://pypi.org/project/amazon-orders/) and writes it to a local JSON
file. get_data_from_JSON / get_vendor_transaction then read that file to
match a given YNAB transaction's date and amount against a card charge, or
failing that an order.

Plugin secrets (Amazon login) live in this plugin's own
.env file, asked for by this plugin's configure_secrets() (through
core.ui's prompts) -- core code never reads them.

Login can involve an interactive 2FA/OTP or CAPTCHA prompt from
amazon-orders itself (via its default console IODefault), so
get_data_from_vendor must be run from an interactive terminal.

If Amazon serves a JavaScript/ACIC auth challenge, amazon-orders' default
form chain can't get past it on its own (it just raises, pointing at the
[browser] extra). AUTH_FORMS_CLASSES below registers amazon-orders'
Playwright-based manual WAF solving for that case: PlaywrightAcicForm
handles the ACIC challenge page headlessly, and PlaywrightManualWafForm
opens a **visible** browser window for any embedded WAF puzzle it can't
clear on its own, so a human can solve it -- see
https://amazon-orders.readthedocs.io/browser.html. That visible window
needs a real display, not just a terminal, so this path only works when
run somewhere with one (a local machine, not a headless SSH session).

Session reuse: every full login risks a WAF puzzle, 2FA prompt, or a 503
rate-limit block from Amazon, so the session cookies are persisted to this
plugin's own COOKIE_JAR_PATH and reused on later runs -- a fresh login only
happens when no saved session exists or Amazon rejects the saved one. The
session is deliberately never logged out at the end of a scrape (logging
out clears the saved cookies). Changing credentials discards the saved
session, since it belongs to the old login.

Two kinds of vendor data are scraped into ORDER_DATA_PATH:
  - orders: order history with full details (items, tax, shipping,
    discounts) -- what a YNAB transaction gets split by.
  - charges: Amazon's payment transaction history -- the actual card
    charges, each tagged with its order number. An order shipped in parts
    is charged once per shipment, so a YNAB transaction's amount matches a
    charge even when it doesn't match any order's grand total.

Every charge gets an order: digital (D01-) orders and orders older than
the history scraped are looked up by number, Amazon Pay (P01-) orders are
read from their payments.amazon.com page (merchant and total only). Whole
Foods charges carry a receipt UUID and are linked to their Whole Foods
order by amount and date; Whole Foods purchases aren't split.
"""

import datetime
import json
import logging
import re
import time
from collections.abc import Collection
from pathlib import Path

from amazonorders.conf import AmazonOrdersConfig
from amazonorders.exception import AmazonOrdersAuthRedirectError
from amazonorders.orders import AmazonOrders
from amazonorders.session import AmazonSession
from amazonorders.transactions import AmazonTransactions

from core import config, ui
from core.milliunits import dollars_to_milliunits
from plugins.plugin import Setting, VendorLineItem, VendorPlugin, VendorTransaction

log = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).resolve().parent
ENV_PATH = PLUGIN_DIR / ".env"
ORDER_DATA_PATH = PLUGIN_DIR / "amazon.com_order_data.json"
PREFS_PATH = PLUGIN_DIR / "amazon.com_user_prefs.json"
# Persisted amazon-orders session cookies -- as sensitive as the password
# itself (they're a logged-in session), so gitignored alongside .env.
COOKIE_JAR_PATH = PLUGIN_DIR / ".amazon_cookies.json"
# Deliberately nonexistent: amazon-orders merges its config file over the
# defaults if one exists at config_path, and its default location
# (~/.config/amazonorders/config.yml) is shared with the standalone
# amazon-orders CLI. Pointing at a file that's never written keeps this
# plugin's session config fully determined by _amazon_config() below.
AMAZONORDERS_CONFIG_PATH = PLUGIN_DIR / ".amazonorders_config.yml"
# Where debug-mode HTML/screenshot snapshots go (gitignored).
DEBUG_OUTPUT_DIR = PLUGIN_DIR.parent.parent / "output"
# How long a visible Playwright window waits for a human to solve a WAF
# puzzle -- the amazon-orders default (30s) is too short in practice.
BROWSER_TIMEOUT_SECONDS = 180

AMAZON_USERNAME_KEY = "AMAZON_USERNAME"
AMAZON_PASSWORD_KEY = "AMAZON_PASSWORD"
# Optional: the secret key Amazon shows when manually adding a 2FA authenticator
# app. Lets amazon-orders auto-solve OTP challenges instead of prompting
# interactively. Same env var name amazon-orders itself reads, so it's
# recognized even without being passed explicitly -- see AmazonSession.
AMAZON_OTP_SECRET_KEY = "AMAZON_OTP_SECRET_KEY"

# See the module docstring's note on manual WAF solving.
AUTH_FORMS_CLASSES = [
    "amazonorders.contrib.browser.playwright.PlaywrightAcicForm",
    "amazonorders.contrib.browser.playwright.PlaywrightManualWafForm",
]

# Amazon-specific prefs only; the shared ones (YNAB mapping, run mode)
# and their defaults live in core/plugin_settings.py.
DEFAULT_PREFS = {
    # Save an HTML/screenshot snapshot of every page amazon-orders loads to
    # DEBUG_OUTPUT_DIR -- for diagnosing login/WAF failures. Off by default:
    # the snapshots are of a logged-in Amazon session.
    "debug": False,
}


def _item_to_dict(item) -> dict:
    return {"title": item.title, "price": item.price, "quantity": item.quantity, "asin": item.asin}


def _order_to_dict(order) -> dict:
    return {
        "order_number": order.order_number,
        "order_placed_date": order.order_placed_date.isoformat() if order.order_placed_date else None,
        "grand_total": order.grand_total,
        "subtotal": order.subtotal,
        "shipping_total": order.shipping_total,
        "free_shipping": order.free_shipping,
        "promotion_applied": order.promotion_applied,
        "coupon_savings": order.coupon_savings,
        "subscription_discount": order.subscription_discount,
        "multibuy_discount": order.multibuy_discount,
        "amazon_discount": order.amazon_discount,
        "reward_points": order.reward_points,
        "gift_card": order.gift_card,
        "gift_wrap": order.gift_wrap,
        "total_before_tax": order.total_before_tax,
        "estimated_tax": order.estimated_tax,
        "refund_total": order.refund_total,
        "payment_method": order.payment_method,
        "payment_method_last_4": order.payment_method_last_4,
        "cancelled": order.cancelled,
        "is_whole_foods": order.is_whole_foods,
        "items": [_item_to_dict(i) for i in order.items],
        "shipments": [
            {"delivery_status": s.delivery_status, "items": [_item_to_dict(i) for i in s.items]}
            for s in order.shipments
        ],
    }


# A Whole Foods charge's reference: a receipt UUID, e.g. "Order
# #00000000-0000-0000-0000-000000000000 Whole Foods SPP 00000", which
# amazon-orders doesn't recognize as an order number.
RECEIPT_RE = re.compile(r"Order #([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def _charge_to_dict(transaction) -> dict:
    """A card charge (or refund) from Amazon's payment transaction history.
    grand_total keeps Amazon's sign: negative for a charge, positive for a
    refund (same as YNAB's outflow/inflow convention).

    Subscription charges (Prime Video add-ons, "amzn.com/pmts" billing, ...)
    aren't tied to an order, and amazon-orders reports their order number
    as "" -- normalized to None so they can't be mistaken for one shared
    order. A Whole Foods charge's order number starts as its receipt UUID,
    until _link_whole_foods finds its order. amazon-orders has no pending
    flag; the charge's text says "Pending"."""
    text = transaction.parsed.get_text(" ", strip=True)
    match = None if transaction.order_number else RECEIPT_RE.search(text)
    receipt = match.group(1) if match else None
    return {
        "order_number": transaction.order_number or receipt,
        "receipt": receipt,
        "is_whole_foods": "Whole Foods" in text,
        "pending": "Pending" in text,
        "completed_date": transaction.completed_date.isoformat(),
        "grand_total": transaction.grand_total,
        "is_refund": transaction.is_refund,
        "payment_method": transaction.payment_method,
        "payment_method_last_4": transaction.payment_method_last_4,
        "seller": transaction.seller,
        "order_details_link": transaction.order_details_link,
    }


def _link_whole_foods(charges: list[dict], orders: list[dict]) -> None:
    """Give each Whole Foods charge still carrying only its receipt UUID the
    number of its order: a Whole Foods order for the same amount, placed 0-3
    days before the charge, that no other charge has. So the order and its
    charge are one purchase (an order with a charge is left out of matching).
    Oldest charge first, each taking the oldest order that fits, so
    same-amount orders a few days apart all get their charge."""
    used = {c["order_number"] for c in charges}
    for charge in sorted(charges, key=lambda c: c["completed_date"]):
        if not charge.get("receipt") or charge["order_number"] != charge["receipt"]:
            continue
        paid = datetime.date.fromisoformat(charge["completed_date"])
        order = min(
            (
                o
                for o in orders
                if o.get("is_whole_foods")
                and o["order_number"] not in used
                and o["grand_total"] is not None
                and dollars_to_milliunits(o["grand_total"]) == -dollars_to_milliunits(charge["grand_total"])
                and 0 <= (paid - datetime.date.fromisoformat(o["order_placed_date"])).days <= 3
            ),
            key=lambda o: o["order_placed_date"],
            default=None,
        )
        if order:
            charge["order_number"] = order["order_number"]
            used.add(order["order_number"])


def _amazon_pay_order(charge: dict, page) -> dict:
    """An Amazon Pay (P01-) order from its payments.amazon.com page, which
    lists no items -- only the merchant ("Your <merchant> purchase details")
    and "Total paid: $<total>" -- so the merchant is its one item."""
    merchant = page.h1 and re.fullmatch(r"Your (.+) purchase details", page.h1.get_text(strip=True))
    total = re.search(r"Total paid: \$([\d,.]+)", page.get_text(" ", strip=True))
    if not merchant or not total:
        raise ValueError("Amazon Pay page without merchant/total")
    merchant, total = merchant.group(1), float(total.group(1).replace(",", ""))
    return {
        "order_number": charge["order_number"],
        "order_placed_date": charge["completed_date"],
        "grand_total": total,
        "is_amazon_pay": True,
        "items": [{"title": f"Amazon Pay: {merchant}", "price": total, "quantity": 1, "asin": None}],
        "shipments": [],
    }


def _charge_key(charge: dict) -> tuple:
    """Charges have no id of their own; an order can be charged several
    times (once per shipment), but never twice on the same day for the same
    amount by the same payment method. A Whole Foods charge is keyed by its
    receipt, which stays the same once it is linked to its order."""
    return (
        charge.get("receipt") or charge["order_number"],
        charge["completed_date"],
        charge["grand_total"],
        charge["payment_method"],
    )


def _record_id(record: dict) -> str:
    """A charge's or an order's id for VendorTransaction.record_id."""
    if "completed_date" in record:
        return "|".join(map(str, _charge_key(record)))
    return record["order_number"]


def _order_link(record: dict) -> str | None:
    """The amazon.com page of the order a charge or order belongs to; Amazon
    Pay (P01-) orders are on payments.amazon.com. None for a charge with no
    order (a subscription) or only a Whole Foods receipt."""
    number = record["order_number"]
    if not number or number == record.get("receipt"):
        return None
    if number.startswith("P01-"):
        return f"https://payments.amazon.com/jr/your-account/orders/{number}"
    return f"https://www.amazon.com/gp/your-account/order-details?orderID={number}"


def _empty_order_data() -> dict:
    return {"last_scraped_at": None, "scraped_years": [], "charges_from": None, "orders": [], "charges": []}


def _amazon_config() -> AmazonOrdersConfig:
    return AmazonOrdersConfig(
        config_path=str(AMAZONORDERS_CONFIG_PATH),
        data={
            "auth_forms_classes": AUTH_FORMS_CLASSES,
            "cookie_jar_path": str(COOKIE_JAR_PATH),
            "browser_timeout": BROWSER_TIMEOUT_SECONDS,
            "output_dir": str(DEBUG_OUTPUT_DIR),
        },
    )


class AmazonPlugin(VendorPlugin):
    name = "amazon.com"
    description = "Matches Amazon.com orders to YNAB transactions."
    url = "https://www.amazon.com"
    prefs_path = PREFS_PATH
    categories_path = PLUGIN_DIR / "amazon.com_categories.json"
    matched_path = PLUGIN_DIR / "amazon.com_ynab_matched.json"
    approved_path = PLUGIN_DIR / "amazon.com_ynab_approved.json"
    default_prefs = DEFAULT_PREFS
    scrapes_vendor_data = True

    # ORDER_DATA_PATH, parsed, and the file modification time it was read
    # at -- matching looks orders up once per date in the match window, so
    # the file is only re-read after it changes.
    _order_data: dict | None = None
    _order_data_mtime: int | None = None

    def settings(self) -> list[Setting]:
        username, password, _ = self._credentials()
        debug = "on" if self.load_own_prefs()["debug"] else "off"
        return [
            Setting(
                "Credentials", username or "not set", self.configure_secrets, missing=not (username and password)
            ),
            Setting("Debug snapshots", debug, self.configure_debug),
        ]

    def configure_secrets(self) -> None:
        print("Enter your Amazon.com login.")
        username = ui.text("Amazon email:", validate=lambda s: bool(s.strip()) or "Required.")
        if username is None:
            return
        password = ui.secret("Amazon password:")
        if password is None:
            return

        print(
            "\nOptional: 2FA authenticator app secret key, to auto-solve OTP "
            "prompts during login (shown by Amazon when you set up an\n"
            "authenticator app under Login & Security -> 2-Step Verification "
            "-> Authenticator app -> \"Can't scan the QR code?\").\n"
            "Leave blank to skip -- amazon-orders will just prompt for the "
            "OTP code interactively during login instead."
        )
        otp_secret_key = ui.secret("OTP secret key (optional):", required=False)
        if otp_secret_key is None:
            return

        config.set_env_var(AMAZON_USERNAME_KEY, username.strip(), ENV_PATH)
        config.set_env_var(AMAZON_PASSWORD_KEY, password.strip(), ENV_PATH)
        if otp_secret_key.strip():
            config.set_env_var(AMAZON_OTP_SECRET_KEY, otp_secret_key.strip(), ENV_PATH)

        # Any saved session belongs to the previous login.
        COOKIE_JAR_PATH.unlink(missing_ok=True)
        log.info("Amazon credentials saved.")

    def configure_debug(self) -> None:
        prefs = self.load_own_prefs()
        debug = ui.confirm(
            "Save debug snapshots (HTML/screenshots of each Amazon page) to output/? "
            "Only for diagnosing login/challenge failures.",
            default=prefs["debug"],
        )
        if debug is None:
            return
        prefs["debug"] = debug
        self.save_own_prefs(prefs)
        log.info(f"Amazon debug snapshots {'on' if debug else 'off'}.")

    def _credentials(self) -> tuple[str | None, str | None, str | None]:
        """(username, password, OTP secret key) from this plugin's .env;
        None for any that aren't set."""
        return (
            config.get_env_var(AMAZON_USERNAME_KEY, ENV_PATH),
            config.get_env_var(AMAZON_PASSWORD_KEY, ENV_PATH),
            config.get_env_var(AMAZON_OTP_SECRET_KEY, ENV_PATH),
        )

    def _open_session(self) -> AmazonSession:
        """An authenticated session: the saved one if there is one, else a
        fresh login (see the module docstring's note on session reuse)."""
        username, password, otp_secret_key = self._credentials()
        if not username or not password:
            raise RuntimeError("Amazon credentials not set. Set them under Settings -> Plugins -> amazon.com.")
        debug = self.load_own_prefs()["debug"]
        # amazon-orders rewrites the cookie file in place, which keeps its
        # permissions, but creates it with default (world-readable) ones.
        # Create it first, readable by this user only.
        if not COOKIE_JAR_PATH.exists():
            COOKIE_JAR_PATH.write_text("{}")
        COOKIE_JAR_PATH.chmod(0o600)
        session = AmazonSession(
            username=username,
            password=password,
            otp_secret_key=otp_secret_key,
            config=_amazon_config(),
            debug=debug,
        )
        if session.auth_cookies_stored():
            log.info("Reusing saved Amazon session.")
            session.is_authenticated = True
        else:
            log.info("Logging in to Amazon...")
            session.login()
        return session

    @staticmethod
    def _with_reauth(session: AmazonSession, fetch):
        """Run `fetch()`, logging in again and retrying once if Amazon
        rejects the (saved) session partway through."""
        try:
            return fetch()
        except AmazonOrdersAuthRedirectError:
            log.info("Saved Amazon session expired; logging in again...")
            session.login()
            return fetch()

    def get_data_from_vendor(self, date: datetime.date | None = None) -> None:
        """Scrape order history and card charges covering `date` through
        today (or just today, if no date is given) into ORDER_DATA_PATH,
        fetching only what earlier scrapes haven't already covered.

        Orders are fetched one calendar year at a time, newest first: the
        current year, then each missing past year back to `date`'s year. The
        file is saved after each year -- so a failure partway keeps the
        years already fetched, and an interrupted first run still leaves
        the most recent orders usable. A past year, once fetched, is
        recorded in "scraped_years" and never fetched again. The current
        year is always refreshed, with the cheapest amazon-orders
        time_filter that reaches back to the newest order on file:
        "last30", "months-3", or the whole year.

        Charges are fetched from `date` when it is earlier than any charges
        scraped so far ("charges_from"), otherwise from the previous scrape.
        "last_scraped_at" is only set once every year and the charges are
        saved. Orders merge by order number, charges by _charge_key; pending
        charges on file are always re-read and replaced.

        Then every charge's order not on file is looked up one by one (see
        "missing" below); a failed lookup is logged and tried again on the
        next scrape.
        """
        today = datetime.date.today()
        start = date or today
        data = {**self._load_order_data()}
        scraped_years = set(data["scraped_years"])
        orders_by_number = {o["order_number"]: o for o in data["orders"]}
        charges_by_key = {_charge_key(c): c for c in data["charges"]}
        last_scraped = (
            datetime.datetime.fromisoformat(data["last_scraped_at"]).astimezone().date()
            if data["last_scraped_at"]
            else None
        )
        charges_from = datetime.date.fromisoformat(data["charges_from"]) if data["charges_from"] else None

        def save(**fields):
            data.update(
                fields,
                scraped_years=sorted(scraped_years),
                orders=list(orders_by_number.values()),
                charges=list(charges_by_key.values()),
            )
            ORDER_DATA_PATH.write_text(json.dumps(data, indent=2))

        older_years = range(today.year - 1, start.year - 1, -1)
        past_years = [y for y in older_years if y not in scraped_years]
        skipped_years = [y for y in older_years if y in scraped_years]
        years = [today.year] + past_years
        # Only orders from order history: digital (D01-) and Amazon Pay
        # (P01-) orders are looked up by charge and aren't in it.
        history = [o for o in data["orders"] if o["order_number"][0].isdigit()]
        newest_order = max((o["order_placed_date"] for o in history if o["order_placed_date"]), default=None)
        age_days = (today - datetime.date.fromisoformat(newest_order)).days if newest_order else None
        if age_days is None or age_days >= 90:
            current_filter, window = f"year-{today.year}", "whole year"
        elif age_days >= 30:
            current_filter, window = "months-3", "last 3 months"
        else:
            current_filter, window = "last30", "last 30 days"
        if last_scraped and charges_from and start >= charges_from:
            charge_start = last_scraped
        else:
            charge_start = start
        # A pending charge's date and amount can change when it completes:
        # re-read it, and drop it below, so its completed version replaces it.
        pending = [c for c in data["charges"] if c.get("pending")]
        if pending:
            charge_start = min(charge_start, datetime.date.fromisoformat(min(c["completed_date"] for c in pending)))
        charge_days = (today - charge_start).days + 1
        skipped = f" (already saved, skipped: {', '.join(map(str, skipped_years))})" if skipped_years else ""
        log.info(
            f"Attempting to fetch Amazon data: orders for {', '.join(map(str, years))}{skipped}, "
            f"then {charge_days} day(s) of charges..."
        )

        session = self._open_session()

        def scrape_orders(year, time_filter, window):
            """Fetch `time_filter`'s orders one history page at a time (so
            progress shows during a long year), until Amazon returns an
            empty page -- which amazon-orders reports for a start_index past
            the window's last order."""
            log.info(f"Attempting to fetch {year} orders ({window}, {years.index(year) + 1} of {len(years)})...")
            started = time.monotonic()
            count = 0
            try:
                while True:
                    page = self._with_reauth(
                        session,
                        lambda: AmazonOrders(session).get_order_history(
                            time_filter=time_filter, full_details=True, keep_paging=False, start_index=count
                        ),
                    )
                    if not page:
                        break
                    orders_by_number.update({o.order_number: _order_to_dict(o) for o in page})
                    count += len(page)
                    elapsed = int(time.monotonic() - started)
                    log.info(
                        f"Progress on {year} orders ({window}): {count} so far ({elapsed // 60}m{elapsed % 60:02d}s)"
                    )
            except Exception as e:
                log.error(f"Failed {year} orders ({window}): {e}")
                raise
            return count

        count = scrape_orders(today.year, current_filter, window)
        save()
        log.info(f"Succeeded with {today.year} orders ({window}): {count} fetched")

        for year in past_years:
            count = scrape_orders(year, f"year-{year}", "whole year")
            scraped_years.add(year)
            save()
            log.info(f"Succeeded with {year} orders (whole year): {count} fetched")

        log.info(f"Attempting to fetch charges (last {charge_days} day(s))...")
        try:
            charges = self._with_reauth(
                session, lambda: AmazonTransactions(session).get_transactions(days=charge_days)
            )
        except Exception as e:
            log.error(f"Failed charges: {e}")
            raise
        # The charges just read start no later than any pending charge on file.
        for charge in pending:
            del charges_by_key[_charge_key(charge)]
        charges_by_key.update({_charge_key(c): c for c in map(_charge_to_dict, charges)})
        _link_whole_foods(list(charges_by_key.values()), list(orders_by_number.values()))
        save(
            last_scraped_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            charges_from=min(start, charges_from or start).isoformat(),
        )
        log.info(f"Succeeded with charges: {len(charges)} saved")

        # Charges whose order isn't in the order history scraped: digital
        # (D01-) orders, orders older than it, and Amazon Pay (P01-) orders,
        # whose page on payments.amazon.com amazon-orders doesn't read.
        missing = {
            c["order_number"]: c
            for c in charges_by_key.values()
            if c["order_number"] and not c.get("receipt") and c["order_number"] not in orders_by_number
        }
        failed = 0
        if missing:
            log.info(f"Attempting to look up {len(missing)} order(s) charged but not in order history...")
            for n, (number, charge) in enumerate(missing.items(), 1):
                try:
                    if number.startswith("P01-"):
                        order = _amazon_pay_order(charge, session.get(charge["order_details_link"]).parsed)
                    else:
                        order = _order_to_dict(
                            self._with_reauth(session, lambda: AmazonOrders(session).get_order(number))
                        )
                    orders_by_number[number] = order
                    save()
                except Exception as e:
                    failed += 1
                    log.warning(f"Failed order {number}: {e}")
                log.info(f"Progress on order lookups: {n} of {len(missing)}")
        if failed:
            log.error(f"Failed order lookups: {failed} of {len(missing)} failed, tried again next scrape")
            log.error(f"Failed Amazon data: {failed} order lookup(s) failed, the rest saved to {ORDER_DATA_PATH.name}")
            return
        if missing:
            log.info(f"Succeeded with order lookups: {len(missing)} saved")
        log.info(f"Succeeded with Amazon data: saved to {ORDER_DATA_PATH.name}")

    def _load_order_data(self) -> dict:
        if not ORDER_DATA_PATH.exists():
            return _empty_order_data()
        mtime = ORDER_DATA_PATH.stat().st_mtime_ns
        if mtime != self._order_data_mtime:
            data = json.loads(ORDER_DATA_PATH.read_text())
            # Files written before charges were scraped are a bare list of orders.
            if isinstance(data, list):
                data = {"orders": data}
            self._order_data = {**_empty_order_data(), **data}
            self._order_data_mtime = mtime
        return self._order_data

    def get_data_from_JSON(self, date: datetime.date, amount: int) -> list[dict]:
        """Charges completed on `date` for `amount` (Amazon's sign matches
        YNAB's), then orders placed on `date` whose grand_total is the
        outflow `amount`. An order with any charge on file is left out: its
        charges are what the card was billed, and the same purchase must
        not fit twice."""
        data = self._load_order_data()
        charges = [
            c
            for c in data["charges"]
            if c["completed_date"] == date.isoformat() and dollars_to_milliunits(c["grand_total"]) == amount
        ]
        orders = [
            o
            for o in data["orders"]
            if o["order_placed_date"] == date.isoformat()
            and o["grand_total"] is not None
            and -dollars_to_milliunits(o["grand_total"]) == amount
        ]
        if orders:
            charged = {c["order_number"] for c in data["charges"]}
            orders = [o for o in orders if o["order_number"] not in charged]
        return charges + orders

    def get_vendor_transaction(
        self, date: datetime.date, amount: int, exclude: Collection[str] = ()
    ) -> VendorTransaction | None:
        record = next((r for r in self.get_data_from_JSON(date, amount) if _record_id(r) not in exclude), None)
        if record is None:
            return None
        order = record
        slots = []  # (asin, shipment index) of each shipped item
        whole_order = False
        if "completed_date" in record:  # a charge: split by the order it paid for
            data = self._load_order_data()
            order = next((o for o in data["orders"] if o["order_number"] == record["order_number"]), {"items": []})
            purchases = [
                c for c in data["charges"] if c["order_number"] == record["order_number"] and c["grand_total"] < 0
            ]
            whole_order = len(purchases) == 1 and abs(amount) == dollars_to_milliunits(order.get("grand_total") or 0)
            # A charge may pay for only some shipments, so say which shipment
            # each item came in. Shipment items carry no price: tie each to
            # an order item by ASIN, first unclaimed one first (an order can
            # list one ASIN twice).
            slots = [(item.get("asin"), n) for n, s in enumerate(order.get("shipments", [])) for item in s["items"]]
        # Whole Foods purchases are categorized as a whole, not split.
        if record.get("is_whole_foods") or order.get("is_whole_foods"):
            return VendorTransaction(
                date=date,
                amount=amount,
                record_id=_record_id(record),
                line_items=[VendorLineItem(description="Whole Foods Market", amount=abs(amount))],
                source_id=record["order_number"],
                link=_order_link(record),
            )
        line_items = []
        for item in order["items"]:
            if item.get("price") is None:
                continue
            slot = next((s for s in slots if item.get("asin") and s[0] == item["asin"]), None)
            if slot:
                slots.remove(slot)
            line_items.append(
                VendorLineItem(
                    description=item["title"],
                    amount=dollars_to_milliunits(item["price"]) * (item.get("quantity") or 1),
                    quantity=item.get("quantity"),
                    shipment=slot[1] if slot else None,
                )
            )
        # The order's only purchase charge, for its grand total, pays for
        # every item if they add up to the order's subtotal: no shipment to
        # pick. (A scraped grand total can be just one of several charges,
        # and some orders list items that were never charged.)
        if whole_order and sum(i.amount for i in line_items) == dollars_to_milliunits(order.get("subtotal") or 0):
            for line_item in line_items:
                line_item.shipment = None
        tax = (
            dollars_to_milliunits(order["estimated_tax"])
            if order.get("estimated_tax") is not None
            else None
        )
        return VendorTransaction(
            date=date,
            amount=amount,
            record_id=_record_id(record),
            line_items=line_items,
            sales_tax=tax,
            source_id=record["order_number"],
            link=_order_link(record),
        )
