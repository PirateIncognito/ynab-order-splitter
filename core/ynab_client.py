"""Wrapper around the YNAB API (https://api.ynab.com/v1) via the `ynab` SDK.

Note: the installed SDK (PyPI `ynab`) models a budget as a "Plan"
(PlansApi / PlanSummary) rather than "Budget" — that's the SDK's naming,
not a different concept. We expose it here as "budget" to match YNAB's
own product terminology.
"""

import datetime
import json
from dataclasses import dataclass

import ynab
from ynab.exceptions import ApiException, NotFoundException, UnauthorizedException


class InvalidTokenError(Exception):
    """Raised when the YNAB access token is rejected by the API."""


@dataclass
class Budget:
    id: str
    name: str


@dataclass
class Category:
    id: str
    name: str
    group_name: str


@dataclass
class Payee:
    id: str
    name: str


def _client(access_token: str) -> ynab.ApiClient:
    return ynab.ApiClient(ynab.Configuration(access_token=access_token))


def describe_api_error(e: ApiException) -> str:
    """Pull the human-readable detail out of a YNAB API error response."""
    try:
        return json.loads(e.body)["error"]["detail"]
    except (TypeError, ValueError, KeyError):
        return e.reason or str(e)


def validate_token(access_token: str) -> str:
    """Return the YNAB user id if the token is valid. Raises
    InvalidTokenError only when YNAB rejects the token (HTTP 401); any
    other failure (YNAB down, rate limit, offline) propagates unchanged, so
    an outage is never reported as a bad token."""
    try:
        with _client(access_token) as client:
            user = ynab.UserApi(client).get_user()
            return str(user.data.user.id)
    except UnauthorizedException as e:
        raise InvalidTokenError(describe_api_error(e)) from e


def list_budgets(access_token: str) -> list[Budget]:
    with _client(access_token) as client:
        response = ynab.PlansApi(client).get_plans()
    return [Budget(id=str(plan.id), name=plan.name) for plan in response.data.plans]


def list_categories(access_token: str, budget_id: str) -> list[Category]:
    """Flatten every budget/group into a single list of selectable categories.

    Internal groups ("Internal Master Category", "Credit Card Payments") are
    YNAB-managed and not valid categorization targets, so they're excluded.
    Hidden categories, and every category in a hidden group, are left out
    too: hidden is a flag on each category and group, not a group of its own.
    """
    with _client(access_token) as client:
        response = ynab.CategoriesApi(client).get_categories(budget_id)
    return [
        Category(id=str(c.id), name=c.name, group_name=group.name)
        for group in response.data.category_groups
        if not group.deleted and not group.internal and not group.hidden
        for c in group.categories
        if not c.deleted and not c.hidden
    ]


def list_payees(access_token: str, budget_id: str) -> list[Payee]:
    """The budget's payees, leaving out deleted ones and transfer payees
    (giving a transaction one of those would make it a transfer)."""
    with _client(access_token) as client:
        response = ynab.PayeesApi(client).get_payees(budget_id)
    return [
        Payee(id=str(p.id), name=p.name) for p in response.data.payees if not p.deleted and not p.transfer_account_id
    ]


def list_accounts(access_token: str, budget_id: str) -> list[ynab.Account]:
    """Raw SDK objects (not a wrapped dataclass) since callers typically need
    several fields (type, balance, on_budget, closed) beyond id/name --
    same reasoning as list_transactions below."""
    with _client(access_token) as client:
        response = ynab.AccountsApi(client).get_accounts(budget_id)
    return [a for a in response.data.accounts if not a.deleted]


def list_transactions(
    access_token: str,
    budget_id: str,
    since_date: datetime.date | None = None,
    unapproved_only: bool = False,
) -> list[ynab.TransactionDetail]:
    with _client(access_token) as client:
        response = ynab.TransactionsApi(client).get_transactions(
            budget_id,
            since_date=since_date,
            type="unapproved" if unapproved_only else None,
        )
    return [t for t in response.data.transactions if not t.deleted]


def get_transaction(access_token: str, budget_id: str, transaction_id: str) -> ynab.TransactionDetail | None:
    """One transaction by id; None if YNAB has no such transaction or it was deleted."""
    try:
        with _client(access_token) as client:
            t = ynab.TransactionsApi(client).get_transaction_by_id(budget_id, transaction_id).data.transaction
    except NotFoundException:
        return None
    return None if t.deleted else t


def update_transactions(access_token: str, budget_id: str, transactions: list[dict]) -> list[str]:
    """Update `transactions` (each a dict in the API's JSON shape, with its
    "id") in one bulk PATCH; return the ids YNAB says it saved.

    For a split, YNAB requires the subtransaction amounts to sum exactly to
    the transaction's amount; the caller is responsible for that.
    """
    with _client(access_token) as client:
        response = ynab.TransactionsApi(client).update_transactions(
            budget_id, ynab.PatchTransactionsWrapper.from_dict({"transactions": transactions})
        )
    return response.data.transaction_ids
