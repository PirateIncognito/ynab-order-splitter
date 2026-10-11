import logging

import pytest

from core import agents, categorize, config, user_prefs


def _t(*memos: str) -> dict:
    return {"split_lines": [{"memo": m} for m in memos]}


def test_first_ten_transactions_go_singly_then_a_hundred_per_call():
    transactions = [_t(), _t("Known", "Sales tax"), _t("A", "A", "Known"), _t("A")]
    transactions += [_t(f"Item {n}") for n in range(1, 112)]

    calls = categorize.plan_calls(transactions, {"Known": {"category": "groceries", "by": "user"}})

    assert calls[:10] == [["A"]] + [[f"Item {n}"] for n in range(1, 10)]
    assert calls[10:] == [[f"Item {n}" for n in range(10, 110)], ["Item 110", "Item 111"]]


def test_lines_with_no_memo_are_never_sent_to_the_agent():  # the halves of a divided whole transaction
    assert categorize.plan_calls([_t("", ""), _t("Mop", "")], {}) == [["Mop"]]


def test_only_valid_answers_for_items_without_an_entry_are_written():
    items = ["Mop", "Tea", "Soap", "Kettle", "Pan", "Cup"]  # Cup gets no answer
    table = {"Kettle": {"category": "groceries", "by": "user"}}  # typed while the call was out
    answer = {"results": [
        {"item": 1, "category": 2, "confidence": "high"},
        {"item": 2, "category": 1, "confidence": "high"},  # Tea twice
        {"item": 2, "category": 2, "confidence": "low"},
        {"item": 3, "category": 0, "confidence": "medium"},  # none fits
        {"item": 4, "category": 2, "confidence": "high"},
        {"item": 5, "category": 3, "confidence": "high"},  # no category 3
        {"item": 7, "category": 1, "confidence": "high"},  # no item 7
    ]}

    written = categorize.write_answers(answer, items, ["groceries", "supplies"], table)

    assert written == 1
    assert table == {
        "Kettle": {"category": "groceries", "by": "user"},
        "Mop": {"category": "supplies", "by": "agent"},
    }


def test_after_quit_the_agent_writes_nothing_and_stops(tmp_path):
    asked = []

    def ask(system, user, schema):
        asked.append(user)
        agent.stopped = True  # the user quits while this call is out
        return {"results": [{"item": 1, "category": 1, "confidence": "high"}]}

    table = {}
    agent = categorize.Agent(ask, [["Mop"], ["Tea"]], {"Home: Supplies": "supplies"}, table, tmp_path / "t.json")
    agent.run()

    assert asked == ["Items:\n1. Mop"]
    assert table == {}
    assert not (tmp_path / "t.json").exists()


@pytest.mark.parametrize("enabled, key, expected", [
    (False, "sk-claude", None),
    (True, None, None),
    (True, "sk-claude", agents.ask_claude),
])
def test_ai_runs_only_when_on_and_its_key_is_set(monkeypatch, caplog, enabled, key, expected):
    monkeypatch.setattr(user_prefs, "load_user_prefs", lambda: {"agent": {"enabled": enabled, "provider": "claude"}})
    monkeypatch.setattr(config, "get_env_var", {"ANTHROPIC_API_KEY": key}.get)

    assert getattr(categorize.chosen_ask(), "func", None) is expected
    failed = [r.message for r in caplog.records if r.message.startswith("Failed")]
    assert failed == (["Failed AI categorization: no claude key; AI is off for this run"] if enabled and not key else [])


ANSWER = {"results": [{"item": 1, "category": 1, "confidence": "high"}]}


@pytest.mark.parametrize("replies, quit_on_second_call, lines", [
    ([ANSWER, ANSWER], False, ["Succeeded with AI categorization: 2 of 2 item(s) categorized"]),
    ([RuntimeError("overloaded"), ANSWER], False, [
        "Failed AI categorization: overloaded",
        "Failed AI categorization: 1 of 2 call(s) failed; 1 of 2 item(s) categorized",
    ]),
    ([{}, ANSWER], False, [
        "Failed AI categorization: answer has no 'results'",
        "Failed AI categorization: 1 of 2 call(s) failed; 1 of 2 item(s) categorized",
    ]),
    # A call that fails after the user quit is no news to them.
    ([ANSWER, RuntimeError("cancelled")], True, ["Failed AI categorization: stopped by quit; 1 of 2 item(s) categorized"]),
])
def test_the_run_ends_with_one_line_that_says_how_it_went(tmp_path, caplog, replies, quit_on_second_call, lines):
    caplog.set_level(logging.INFO)
    replies = iter(replies)

    def ask(system, user, schema):
        reply = next(replies)
        agent.stopped = quit_on_second_call and user.endswith("Tea")
        if isinstance(reply, Exception):
            raise reply
        return reply

    agent = categorize.Agent(ask, [["Mop"], ["Tea"]], {"Home: Supplies": "supplies"}, {}, tmp_path / "t.json")
    agent.run()

    ending = [r.message for r in caplog.records if r.message.startswith(("Failed", "Succeeded"))]
    assert ending == lines
