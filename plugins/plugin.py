"""The plugin SDK: the contract every vendor plugin implements.

Individual plugin directories (e.g. plugins/amazon.com/) subclass
VendorPlugin from here. This module is only the contract -- it imports
nothing from core. The settings every plugin shares (YNAB mapping, match
window, run mode) are asked for and checked by core/plugin_settings.py;
finding and loading installed plugins is plugins/orchestrator.py.
"""

import datetime
import json
from abc import ABC, abstractmethod
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class VendorLineItem:
    description: str
    amount: int  # milliunits, positive; for a charge, the whole order's items (M3 allocates per shipment)
    quantity: int | None = None
    # Which of the order's shipments it came in (0-based), when the record may
    # pay for only some of them (a charge per shipment); else None.
    shipment: int | None = None


@dataclass
class VendorTransaction:
    """A vendor record (an order, or one charge for it), standardized so core
    matching/splitting code never needs to know which plugin produced it."""

    date: datetime.date
    amount: int  # signed milliunits, like YNAB: negative for a purchase, positive for a refund
    record_id: str  # unique per vendor record, so core can match each record only once
    line_items: list[VendorLineItem] = field(default_factory=list)
    sales_tax: int | None = None  # milliunits, if the vendor breaks it out
    source_id: str | None = None  # the order this record belongs to, e.g. an order number
    link: str | None = None  # the vendor's web page for this record, if it has one


@dataclass
class Setting:
    """One user-configurable setting, described once: the plugin settings
    menu, the Settings overview and first-run setup are all built from a
    list of these (see core/plugin_settings.all_settings)."""

    label: str  # e.g. "Run mode"
    value: str  # current value, for display, e.g. "nonsplit"
    change: Callable[[], None]  # asks the user for a new value and saves it
    missing: bool = False  # required, but not filled in yet


class VendorPlugin(ABC):
    """Header metadata (name/description/url) plus the three data-access
    methods core uses to get vendor data.

    prefs_path is set by each subclass (like name/description/url): the
    plugin's own <plugin_name>_user_prefs.json, holding both its own prefs
    (defaults in default_prefs) and the shared settings core manages there.
    categories_path is set the same way: the plugin's own
    <plugin_name>_categories.json, the item -> category table that core's
    review reads and writes. matched_path too: the plugin's own
    <plugin_name>_ynab_matched.json, the results of its last match run. And
    approved_path: the plugin's own <plugin_name>_ynab_approved.json, the
    approved transactions queued to be written to YNAB. And pushed_path:
    the plugin's own <plugin_name>_ynab_pushed.json, the ids of the
    transactions pushed to YNAB.
    """

    name: str
    description: str
    url: str
    prefs_path: Path
    categories_path: Path
    matched_path: Path
    approved_path: Path
    pushed_path: Path
    default_prefs: dict[str, Any] = {}  # plugin-specific prefs only
    # Whether get_data_from_vendor() is a real scraper core can call
    # automatically, vs. the default no-op that just raises
    # NotImplementedError. Checked up front instead of relying on that
    # exception, since a plugin's own scraping code could legitimately
    # raise NotImplementedError too (e.g. an unhandled page layout) --
    # catching it to mean "no scraping support" would silently mask that.
    scrapes_vendor_data: bool = False

    def load_own_prefs(self) -> dict[str, Any]:
        if not self.prefs_path.exists():
            return dict(self.default_prefs)
        return {**self.default_prefs, **json.loads(self.prefs_path.read_text())}

    def save_own_prefs(self, prefs: dict[str, Any]) -> None:
        self.prefs_path.write_text(json.dumps(prefs, indent=2))

    def settings(self) -> list[Setting]:
        """This plugin's own settings (credentials, plugin-specific
        options), in menu order. Core shows them after the shared ones.
        Default: none."""
        return []

    def get_data_from_vendor(self, date: datetime.date | None = None) -> None:
        """Refresh the local vendor JSON by scraping the vendor's site.

        Optional: plugins that don't automate scraping should leave this
        raising NotImplementedError and instead document (e.g. in their
        module docstring) how a user obtains an updated JSON file manually.
        """
        raise NotImplementedError(f"{self.name} does not support automated scraping.")

    @abstractmethod
    def get_data_from_JSON(self, date: datetime.date, amount: int) -> list[dict]:
        """Return raw vendor JSON records matching a date and a YNAB amount
        (signed milliunits)."""

    @abstractmethod
    def get_vendor_transaction(
        self, date: datetime.date, amount: int, exclude: Collection[str] = ()
    ) -> VendorTransaction | None:
        """Return a standardized VendorTransaction for a date and a YNAB
        amount (signed milliunits: a purchase only matches a negative
        amount, a refund a positive one), skipping records whose record_id
        is in `exclude`; None if no other vendor record matches."""
