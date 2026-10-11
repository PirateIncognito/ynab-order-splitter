import io
import itertools
import json
import logging
import re
import threading
import time

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.data_structures import Size
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.vt100 import Vt100_Output
from tabulate import tabulate

from core import categorize, review, ui
from core.ynab_client import Category, Payee

CATEGORIES = [
    Category(id="groceries", name="Groceries", group_name="Food"),
    Category(id="dining", name="Dining", group_name="Food"),
    Category(id="dining-out", name="Dining Out", group_name="Food"),
    Category(id="supplies", name="Supplies", group_name="Home"),
]
LABELS = {f"{c.group_name}: {c.name}": c.id for c in CATEGORIES}
PAYEES = [Payee(id="p-whole-foods", name="Whole Foods"), Payee(id="p-amazon", name="Amazon"),
          Payee(id="p-costco", name="Costco ")]


_ids = itertools.count()


def _transaction(*memos: str) -> dict:
    return {
        "id": f"t{next(_ids)}",
        "date": "2025-01-02",
        "payee_name": "Amazon",
        "amount": -1000 * len(memos),
        "match_status": "matched",
        "vendor_match": {"source_id": "100-0000001-0000000"},
        "split_status": "exact" if memos else "nothing to split",
        "split_reason": None,
        "split_lines": [{"amount": -1000, "memo": m, "category_id": None} for m in memos],
    }


def _review(tmp_path, monkeypatch, transactions: list[dict], keys: list[str], typed: list[str] = (),
            ask=None) -> list[dict]:
    """Run the review with `keys` pressed and `typed` entered as categories,
    and `ask` as the AI agent; return the saved transactions."""
    path = tmp_path / "amazon.com_ynab_matched.json"
    path.write_text(json.dumps({"budget_id": "budget-1", "transactions": transactions}))
    keys, typed = iter(keys), iter(typed)
    monkeypatch.setattr(ui, "key", lambda message, allowed: next(keys))
    monkeypatch.setattr(ui, "autocomplete", lambda message, choices, validate: next(typed))
    monkeypatch.setattr(ui, "clear_screen", lambda: None)
    wait_for = categorize.lock.wait_for

    def bounded_wait_for(predicate):  # an agent that never ends the wait fails the test instead of hanging it
        assert wait_for(predicate, timeout=5), "still waiting for the agent"
        return True
    monkeypatch.setattr(categorize.lock, "wait_for", bounded_wait_for)
    review.review_transactions(path, CATEGORIES, PAYEES, tmp_path / "amazon.com_categories.json",
                               tmp_path / "amazon.com_ynab_approved.json", ask)
    for thread in threading.enumerate():  # let the agent finish its call and log within the test
        if thread.name == "AI agent":
            thread.join(timeout=5)
            assert not thread.is_alive()
    return json.loads(path.read_text())["transactions"]


@pytest.mark.parametrize(
    "text, category_id",
    [
        ("groc", "groceries"),  # middle of a word, other case
        ("HOME: SUPPLIES", "supplies"),
        ("food: dining", "dining"),  # an exact label wins over "Food: Dining Out"
        ("Dining", "dining"),  # so does an exact category name
        ("din", None),  # two categories contain it
        ("Toys", None),  # no such category
        ("", None),
    ],
)
def test_find_category_accepts_only_one_real_category(text, category_id):
    assert review.find_category(text, LABELS) == category_id


def test_a_repeat_purchase_needs_only_approve(tmp_path, monkeypatch):
    [first] = _review(tmp_path, monkeypatch, [_transaction("Coffee beans")], keys=["e", "a"], typed=["groc"])
    assert first["split_lines"][0]["category_id"] == "groceries"
    assert first["review_status"] == "approved"

    [repeat] = _review(tmp_path, monkeypatch, [_transaction("Coffee beans")], keys=["a"])

    assert repeat["split_lines"][0]["category_id"] == "groceries"
    assert repeat["review_status"] == "approved"


def test_lines_are_filled_from_the_table_unless_the_category_was_deleted(tmp_path, monkeypatch):
    (tmp_path / "amazon.com_categories.json").write_text(json.dumps({
        "Coffee beans": {"category": "groceries", "by": "user"},
        "Mop": {"category": "supplies", "by": "agent"},
        "Tea": {"category": "deleted-category", "by": "user"},
    }))

    [t] = _review(tmp_path, monkeypatch, [_transaction("Coffee beans", "Mop", "Tea")], keys=["q"])

    assert [line["category_id"] for line in t["split_lines"]] == ["groceries", "supplies", None]


def test_edit_and_approve_set_the_transactions_items_to_user(tmp_path, monkeypatch):
    table_path = tmp_path / "amazon.com_categories.json"
    table_path.write_text(json.dumps({
        "Mop": {"category": "supplies", "by": "agent"},
        "Kettle": {"category": "supplies", "by": "agent"},  # not in the transaction
    }))

    _review(tmp_path, monkeypatch, [_transaction("Mop", "Tea"), _transaction("Soap")],
            keys=["e", "2", "a", "e", "q"], typed=["groc", "home"])  # Soap: edited, not approved

    assert json.loads(table_path.read_text()) == {
        "Mop": {"category": "supplies", "by": "user"},
        "Tea": {"category": "groceries", "by": "user"},
        "Soap": {"category": "supplies", "by": "user"},
        "Kettle": {"category": "supplies", "by": "agent"},
    }


def test_approve_is_refused_until_every_line_has_a_category(tmp_path, monkeypatch, capsys, caplog):
    caplog.set_level(logging.INFO)
    transactions = [_transaction("Coffee beans", "Mop", "Tea", *[f"Item {n}" for n in range(4, 12)]), _transaction()]
    transactions[0]["split_lines"][2]["category_id"] = "deleted-category"

    t, whole = _review(tmp_path, monkeypatch, transactions, keys=["e", "2", "a", "q"], typed=["supplies"])

    assert "no category on row(s) 1, 3, 4, 5, 6, 7, 8, 9, b, c." in capsys.readouterr().out
    assert "review_status" not in t
    assert "Succeeded with review: 0 approved, 0 skipped, 2 left" in caplog.text
    names = {category_id: label for label, category_id in LABELS.items()}
    assert review.missing_categories(whole, names) == "the whole transaction"
    review.set_category(whole, 1, "supplies", {})
    assert review.missing_categories(whole, names) is None


def test_editing_a_line_fills_lines_with_the_same_description(tmp_path, monkeypatch):
    [t] = _review(tmp_path, monkeypatch, [_transaction("Mop", "Tea", "Mop")], keys=["e", "1", "a", "q"], typed=["home"])

    assert [line["category_id"] for line in t["split_lines"]] == ["supplies", None, "supplies"]


def test_lines_past_nine_are_picked_by_letter_without_q(tmp_path, monkeypatch):
    memos = [f"Item {n}" for n in range(1, 27)]
    keys = ["e", "\x1b", "e", "q", "e", "b", "e", "s", "q"]  # Esc and q go back; b is line 10, s line 26
    [t] = _review(tmp_path, monkeypatch, [_transaction(*memos)], keys=keys, typed=["groc", "home"])

    categories = [line["category_id"] for line in t["split_lines"]]
    assert categories == [None] * 9 + ["groceries"] + [None] * 15 + ["supplies"]


def test_table_columns_long_items_and_whole_transaction(monkeypatch, capsys):
    monkeypatch.setattr(ui, "clear_screen", lambda: None)
    review._draw(_transaction("x" * (review.ITEM_WIDTH + 5)), 1, 2, {}, {})
    whole = _transaction()
    whole["amount"] = -5000
    review._draw(whole, 2, 2, {}, {})
    screen = re.sub(r"\033\[[0-9;]*m", "", capsys.readouterr().out).splitlines()

    assert [line.split() for line in screen if "Item" in line] == [["#", "Item", "Category", "Amount"]] * 2
    assert "1  " + "x" * (review.ITEM_WIDTH - 1) + "…  (none)" in "\n".join(screen)
    assert any(line.split() == ["Whole", "transaction", "(none)", "-5.00"] for line in screen)


def test_approving_queues_the_transaction_for_ynab_and_skipping_does_not(tmp_path, monkeypatch):
    approved, skipped = _transaction("Mop"), _transaction("Tea")
    _review(tmp_path, monkeypatch, [approved, skipped], keys=["e", "a", "s"], typed=["home"])

    [entry] = json.loads((tmp_path / "amazon.com_ynab_approved.json").read_text())
    assert entry["id"] == approved["id"]
    assert entry["budget_id"] == "budget-1"
    assert entry["split_lines"] == [{"amount": -1000, "memo": "Mop", "category_id": "supplies"}]


def test_a_picked_payee_is_shown_and_queued_for_ynab(tmp_path, monkeypatch, capsys):
    [t] = _review(tmp_path, monkeypatch, [_transaction("Mop")], keys=["p", "e", "a"], typed=["whole foods", "home"])

    assert "Whole Foods  -1.00" in re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)
    assert t["review_payee_id"] == "p-whole-foods"
    [entry] = json.loads((tmp_path / "amazon.com_ynab_approved.json").read_text())
    assert entry["payee_id"] == "p-whole-foods"


def test_an_empty_payee_answer_changes_nothing(tmp_path, monkeypatch):
    [t] = _review(tmp_path, monkeypatch, [_transaction("Mop")], keys=["p", "e", "a"], typed=["", "home"])

    assert "review_payee_id" not in t
    [entry] = json.loads((tmp_path / "amazon.com_ynab_approved.json").read_text())
    assert entry["payee_id"] is None


def test_a_payee_name_with_a_trailing_space_can_be_picked(tmp_path, monkeypatch):
    [t] = _review(tmp_path, monkeypatch, [_transaction("Mop")], keys=["p", "s"], typed=["costco"])

    assert t["review_payee_id"] == "p-costco"


def test_a_picked_payee_no_longer_in_ynab_is_dropped(tmp_path, monkeypatch, capsys):
    t = _transaction("Mop")
    t["review_payee_id"] = "p-merged-away"
    [t] = _review(tmp_path, monkeypatch, [t], keys=["e", "a"], typed=["home"])

    assert "Amazon  -1.00" in re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)
    assert "review_payee_id" not in t
    [entry] = json.loads((tmp_path / "amazon.com_ynab_approved.json").read_text())
    assert entry["payee_id"] is None


def test_a_failure_partway_keeps_the_review_so_far(tmp_path, monkeypatch):
    with pytest.raises(StopIteration):  # the keys run out on the second transaction
        _review(tmp_path, monkeypatch, [_transaction("Mop"), _transaction("Tea")], keys=["e", "a"], typed=["home"])

    first, second = json.loads((tmp_path / "amazon.com_ynab_matched.json").read_text())["transactions"]
    assert first["review_status"] == "approved"
    assert "review_status" not in second
    assert json.loads((tmp_path / "amazon.com_categories.json").read_text()) == {
        "Mop": {"category": "supplies", "by": "user"}
    }


@pytest.mark.parametrize("link, opens, message", [
    ("https://www.amazon.com/order", True, None),
    (None, True, "No order page for this transaction."),  # a receipt, or matched before links
    ("https://www.amazon.com/order", False, "Couldn't open a browser."),
])
def test_o_opens_the_order_page_or_says_why_not(tmp_path, monkeypatch, capsys, link, opens, message):
    t = _transaction("Mop")
    if link:
        t["vendor_match"]["link"] = link
    opened = []
    monkeypatch.setattr(ui, "open_in_browser", lambda url: opened.append(url) or opens)

    [t] = _review(tmp_path, monkeypatch, [t], keys=["o", "s"])

    assert opened == ([link] if link else [])
    assert t["review_status"] == "skipped"
    out = capsys.readouterr().out
    for said in ("No order page for this transaction.", "Couldn't open a browser."):
        assert (said in out) == (said == message)


def test_open_in_browser_throws_away_the_browsers_output(monkeypatch):
    started = []
    monkeypatch.setattr(ui.shutil, "which", lambda name: "/usr/bin/xdg-open")
    monkeypatch.setattr(ui.subprocess, "Popen", lambda args, **kwargs: started.append((args, kwargs)))

    assert ui.open_in_browser("https://example.com/order") is True
    assert started == [(["xdg-open", "https://example.com/order"],
                        {"stdout": ui.subprocess.DEVNULL, "stderr": ui.subprocess.DEVNULL, "start_new_session": True})]


def test_open_in_browser_without_xdg_open_falls_back_to_webbrowser(monkeypatch):
    opened = []
    monkeypatch.setattr(ui.shutil, "which", lambda name: None)
    monkeypatch.setattr(ui.webbrowser, "open", lambda url: opened.append(url) or True)

    assert ui.open_in_browser("https://example.com/order") is True
    assert opened == ["https://example.com/order"]


@pytest.mark.parametrize("typed", ["\x1b[200~es\x1b[201~E", "\x1b[200~\x1b[201~E"])
def test_key_ignores_pasted_text_and_takes_either_case(typed):
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text(typed)
        assert ui.key("?", "aesq") == "e"


@pytest.mark.parametrize("typed, expected", [
    ("ser\t\r", "Bills: Phone service"),  # Tab takes the first suggestion
    ("e\t\t\r", "Bills: Phone service"),  # a second Tab keeps the filled-in one
    ("zz\t\r", "zz"),  # nothing suggested: Tab changes nothing
    ("gro\r", "Food: Groceries"),  # one suggestion left fills the line
    ("groc\r", "Food: Groceries"),  # typing on goes into what was typed, not the filled line
    ("e\r", "e"),  # two suggestions: the line is what was typed
    ("gro\x7f\r", "gr"),  # Backspace on a filled line: what was typed, less a letter
    ("groceries\r", "Food: Groceries"),  # typed past half the suggestion
    ("food: gr\r", "Food: Groceries"),
    ("grocerie\t\r", "Food: Groceries"),
    ("zzz\x1b[1;2Hx\r", "x"),  # typing replaces a Shift+Home selection
])
def test_autocomplete_fills_in_a_suggestion(typed, expected):
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text(typed)
        assert ui.autocomplete("?", ["Bills: Phone service", "Food: Groceries", "Food: Dining out"], validate=lambda s: True) == expected


def test_menus_long_enough_to_reach_j_and_k_still_work():
    options = [(f"Account {n}", n) for n in range(1, 21)]
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text("k")  # 20th option: 1-9, then a-k
        assert ui.choose("?", options) == 20


def test_an_approved_transaction_is_not_shown_again_but_still_counted(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    approved, unreviewed = _transaction("Mop"), _transaction("Tea")
    approved["date"] = "2025-01-03"  # newest, so it would be shown first
    approved["review_status"] = "approved"

    approved, unreviewed = _review(tmp_path, monkeypatch, [approved, unreviewed], keys=["s"])

    assert approved["review_status"] == "approved"
    assert unreviewed["review_status"] == "skipped"
    assert "Succeeded with review: 1 approved, 1 skipped, 0 left" in caplog.text


def test_rows_past_nine_are_picked_by_letter(tmp_path, monkeypatch, capsys):
    memos = [f"Item {n}" for n in range(1, 13)]

    [t] = _review(tmp_path, monkeypatch, [_transaction(*memos)], keys=["e", "c", "q"], typed=["home"])

    assert [n for n, line in enumerate(t["split_lines"], 1) if line["category_id"]] == [11]
    assert re.search(r"\bc +Item 11 ", re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out))


def test_a_sets_every_item_line_but_not_the_sales_tax_line(tmp_path, monkeypatch, capsys):
    t = _transaction(*[f"Item {n}" for n in range(1, 11)], categorize.SALES_TAX)
    t["split_lines"][-1]["category_id"] = "dining"

    [t] = _review(tmp_path, monkeypatch, [t], keys=["e", "a", "q"], typed=["home"])

    assert [line["category_id"] for line in t["split_lines"]] == ["supplies"] * 10 + ["dining"]
    assert json.loads((tmp_path / "amazon.com_categories.json").read_text()) == {
        f"Item {n}": {"category": "supplies", "by": "user"} for n in range(1, 11)
    }
    assert re.search(r"\bb +Item 10 ", re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out))


def test_a_sets_every_line_past_the_row_keys(tmp_path, monkeypatch):
    memos = [f"Item {n}" for n in range(1, len(review.ROW_KEYS) + 2)]
    monkeypatch.setattr(ui, "text", lambda message, validate: "a" if validate("a") is True else None)

    [t] = _review(tmp_path, monkeypatch, [_transaction(*memos)], keys=["e", "q"], typed=["home"])

    assert [line["category_id"] for line in t["split_lines"]] == ["supplies"] * len(memos)


@pytest.mark.parametrize("keys, typed", [(["e", "q", "q"], []), (["e", "\x1b", "q"], []), (["e", "1", "q"], [""])])
def test_edit_backs_out_without_a_category(tmp_path, monkeypatch, keys, typed):
    [t] = _review(tmp_path, monkeypatch, [_transaction("Mop", "Tea")], keys=keys, typed=typed)

    assert [line["category_id"] for line in t["split_lines"]] == [None, None]


@pytest.mark.parametrize(
    "answer, kind",
    [
        ("\x1b]11;rgb:ffff/ffff/ffff\x1b\\", "light"),
        ("\x1b]11;rgb:fbfb/f1f1/c7c7\x07", "light"),  # Gruvbox light
        ("\x1b]11;rgb:28/28/28\x07", "dark"),  # 2 hex digits per channel
        ("\x1b]11;rgb:f/f/f\x07", "light"),  # 1 digit
        ("\x1b]11;rgb:2828/2828/2828\x1b\\", "dark"),
        ("", None),  # no answer
    ],
)
def test_background_kind_reads_the_terminal_reply(answer, kind):
    assert ui.background_kind(answer) == kind


def test_print_table_colours_without_changing_the_text(capsys):
    rows = [["1", "Mop", "(none)", "-1.00"], ["b", "A much longer item name", "Home: Supplies", "-123.45"]]
    align = ("right", "left", "left", "right")

    ui.print_table(["#", "Item", "Category", "Amount"], rows, align, [["muted", "", "missing", "out"]] * 2)

    out = capsys.readouterr().out
    assert re.sub(r"\x1b\[[0-9;]*m", "", out) == tabulate(
        rows, headers=["#", "Item", "Category", "Amount"], tablefmt="simple", colalign=align, disable_numparse=True
    ) + "\n"
    assert "\x1b[38;2;" in out


def test_key_hides_the_cursor_while_it_waits():
    """Nothing is typed at a single-key prompt, so no cursor (the user's
    choice); prompt_toolkit's default puts it on the message's first character."""
    screen = io.StringIO()
    output = Vt100_Output(screen, lambda: Size(rows=24, columns=80), term="xterm")
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
        pipe.send_text("a")
        ui.key("Row (1-4)?", "1234a")

    drawn = screen.getvalue()
    waiting = drawn[drawn.index("Row (1-4)?"):drawn.rindex("\r\n")]
    assert drawn.index("\x1b[?25l") < drawn.index("Row (1-4)?")
    assert "\x1b[?25h" not in waiting


def test_the_sales_tax_line_never_goes_into_the_table_and_skip_keeps_agent(tmp_path, monkeypatch):
    table_path = tmp_path / "amazon.com_categories.json"
    table_path.write_text(json.dumps({
        "Mop": {"category": "supplies", "by": "user"},
        "Tea": {"category": "groceries", "by": "agent"},
    }))

    _review(tmp_path, monkeypatch, [_transaction("Mop", "Sales tax"), _transaction("Tea")],
            keys=["e", "2", "a", "s"], typed=["home"])  # [e] the tax line, [a], then [s] Tea

    assert json.loads(table_path.read_text()) == {
        "Mop": {"category": "supplies", "by": "user"},
        "Tea": {"category": "groceries", "by": "agent"},
    }


# Stub agents.ask_* functions that take a moment, so the review reaches the
# transaction before the answer does.
def _answers_supplies(system, user, schema):
    time.sleep(0.1)
    return {"results": [{"item": 1, "category": 4, "confidence": "high"}]}  # 4: "Home: Supplies"


def _fails(system, user, schema):
    time.sleep(0.1)
    raise RuntimeError("overloaded")


def test_the_review_waits_for_the_agents_answer(tmp_path, monkeypatch, capsys):
    [t] = _review(tmp_path, monkeypatch, [_transaction("Mop")], keys=["q"], ask=_answers_supplies)

    assert "Waiting for Agent suggestions..." in capsys.readouterr().out
    assert t["split_lines"][0]["category_id"] == "supplies"
    assert json.loads((tmp_path / "amazon.com_categories.json").read_text()) == {
        "Mop": {"category": "supplies", "by": "agent"}
    }


def test_a_failed_agent_call_ends_the_wait_with_none(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)

    [t] = _review(tmp_path, monkeypatch, [_transaction("Mop")], keys=["q"], ask=_fails)

    assert t["split_lines"][0]["category_id"] is None
    assert "Failed AI categorization: overloaded" in caplog.text


def test_ctrl_c_while_waiting_quits_the_review_like_q(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)

    def ctrl_c(self, items):
        raise KeyboardInterrupt
    monkeypatch.setattr(categorize.Agent, "wait", ctrl_c)

    [t] = _review(tmp_path, monkeypatch, [_transaction("Mop")], keys=[], ask=_answers_supplies)

    assert "review_status" not in t
    assert "Succeeded with review: 0 approved, 0 skipped, 1 left" in caplog.text


def test_the_review_screen_shows_only_the_agents_messages(monkeypatch, capsys):
    def record(text, thread):
        r = logging.LogRecord("core", logging.INFO, "", 0, text, None, None)
        r.threadName = thread
        return r
    for text, thread in [("Matched", "MainThread"), ("Progress on AI", "Thread-1"), ("Split", "MainThread")]:
        ui.messages.put(record(text, thread))
    monkeypatch.setattr(ui, "clear_screen", lambda: None)

    review._draw(_transaction("Mop"), 1, 1, {}, {})

    assert capsys.readouterr().out.startswith("Progress on AI\n")
    assert [ui.messages.get().msg for _ in range(2)] == ["Matched", "Split"]  # left for the next menu, in order
    assert ui.messages.empty()


@pytest.mark.parametrize("typed, picked", [("2", 2), ("b", 11), ("0", None), ("\x1b[B\r", 2)])
def test_a_menu_key_picks_its_option_without_enter(typed, picked):
    options = [(f"Option {n}", n) for n in range(1, 12)]
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text(typed + "\x03")  # Ctrl+C after: if the key didn't pick, the menu ends (None) instead of hanging
        assert ui.choose("Pick:", options, back_label="Back") == picked


@pytest.mark.parametrize("amount, halves", [(-1010, [-510, -500]), (1010, [510, 500])])
def test_divide_halves_the_line_in_whole_cents_with_its_category(tmp_path, monkeypatch, amount, halves):
    t = _transaction("Mop")
    t["amount"] = amount
    t["split_lines"][0].update(amount=amount, category_id="supplies")

    [t] = _review(tmp_path, monkeypatch, [t], keys=["d", "q"])

    assert t["split_lines"] == [{"amount": a, "memo": "Mop", "category_id": "supplies"} for a in halves]


def test_divide_asks_for_the_row_and_keeps_the_halves_together(tmp_path, monkeypatch):
    keys = ["d", "q", "d", "2", "d", "3", "q"]  # q backs out; then Tea, then Tea's second half
    [t] = _review(tmp_path, monkeypatch, [_transaction("Mop", "Tea")], keys=keys)

    assert [(line["memo"], line["amount"]) for line in t["split_lines"]] == [
        ("Mop", -1000), ("Tea", -500), ("Tea", -250), ("Tea", -250)
    ]


def test_divide_a_halves_every_item_line_but_not_the_sales_tax_line(tmp_path, monkeypatch):
    t = _transaction("Mop", "Tea")
    t["split_lines"].append({"amount": -110, "memo": categorize.SALES_TAX, "category_id": "dining"})

    [t] = _review(tmp_path, monkeypatch, [t], keys=["d", "a", "q"])

    assert [(line["memo"], line["amount"]) for line in t["split_lines"]] == [
        ("Mop", -500), ("Mop", -500), ("Tea", -500), ("Tea", -500), (categorize.SALES_TAX, -110)
    ]


def test_divide_splits_a_whole_transaction_with_its_category(tmp_path, monkeypatch):
    t = _transaction()
    t["amount"] = -2010
    t["review_category_id"] = "groceries"

    [t] = _review(tmp_path, monkeypatch, [t], keys=["d", "e", "1", "a"], typed=["supp"])

    assert t["review_status"] == "approved"
    [entry] = json.loads((tmp_path / "amazon.com_ynab_approved.json").read_text())
    assert entry["split_lines"] == [
        {"amount": -1010, "memo": "", "category_id": "supplies"},
        {"amount": -1000, "memo": "", "category_id": "groceries"},
    ]
    assert json.loads((tmp_path / "amazon.com_categories.json").read_text()) == {}  # no item name to remember


def test_editing_one_half_leaves_the_other_halfs_category(tmp_path, monkeypatch):
    t = _transaction("Mop")
    t["split_lines"][0]["category_id"] = "supplies"

    [t] = _review(tmp_path, monkeypatch, [t], keys=["d", "e", "1", "q"], typed=["groc"])

    assert [line["category_id"] for line in t["split_lines"]] == ["groceries", "supplies"]
