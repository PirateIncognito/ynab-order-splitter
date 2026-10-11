"""Provider calls checked offline: each SDK gets a mock HTTP transport that
records the request and replies with a documented sample response."""

import functools
import json

import anthropic
import httpx2
import openai
import pytest
from google import genai

from core import agents, categorize, config, user_prefs

SCHEMA = {
    "type": "object",
    "properties": {"category": {"type": "string"}},
    "required": ["category"],
    "additionalProperties": False,
}
ANSWER = {"category": "Groceries"}


class Server:
    """Records each request and answers with `reply` (a JSON body)."""

    def __init__(self):
        self.reply = None
        self.sent = []

    def __call__(self, request):
        self.sent.append(request)
        return httpx2.Response(200, json=self.reply)


@pytest.fixture
def server(monkeypatch):
    server = Server()
    http = httpx2.Client(transport=httpx2.MockTransport(server))
    monkeypatch.setattr(config, "get_env_var", lambda key: f"key-for-{key}")
    monkeypatch.setattr(anthropic, "Anthropic", functools.partial(anthropic.Anthropic, http_client=http))
    monkeypatch.setattr(openai, "OpenAI", functools.partial(openai.OpenAI, http_client=http))
    monkeypatch.setattr(genai, "Client", functools.partial(
        genai.Client, http_options=genai.types.HttpOptions(httpx_client=http)))
    return server


def claude_reply(stop_reason="end_turn"):
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": json.dumps(ANSWER)}],
        "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 20, "output_tokens": 8},
    }


def openai_reply(content=None, status="completed", incomplete_details=None):
    return {
        "id": "resp_1", "object": "response", "created_at": 1760000000, "model": "gpt-6-astra",
        "status": status, "incomplete_details": incomplete_details, "error": None,
        "output": [{
            "type": "message", "id": "msg_1", "status": status, "role": "assistant",
            "content": content or [{"type": "output_text", "text": json.dumps(ANSWER), "annotations": []}],
        }],
        "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
    }


def gemini_reply(status="completed"):
    return {
        "created": "2025-11-26T12:25:15Z", "id": "v1_abc", "model": "gemini-3.8-flash",
        "object": "interaction", "status": status, "updated": "2025-11-26T12:25:15Z",
        "steps": [{"type": "model_output", "content": [{"type": "text", "text": json.dumps(ANSWER)}]}],
    }


def test_claude_request_and_answer(server):
    server.reply = claude_reply()
    assert agents.ask_claude("sys", "items", SCHEMA) == ANSWER
    body = json.loads(server.sent[0].content)
    assert server.sent[0].headers["x-api-key"] == "key-for-ANTHROPIC_API_KEY"
    assert body["model"] == "claude-opus-5-5"
    assert body["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}}
    assert body["system"] == "sys"
    assert body["messages"] == [{"role": "user", "content": "items"}]


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_claude_refusal_or_cutoff_raises(server, stop_reason):
    server.reply = claude_reply(stop_reason)
    with pytest.raises(RuntimeError, match=stop_reason):
        agents.ask_claude("sys", "items", SCHEMA)


def test_openai_request_and_answer(server):
    server.reply = openai_reply()
    assert agents.ask_openai("sys", "items", SCHEMA) == ANSWER
    body = json.loads(server.sent[0].content)
    assert server.sent[0].headers["authorization"] == "Bearer key-for-OPENAI_API_KEY"
    assert body["model"] == "gpt-6-astra"
    assert body["text"]["format"] == {"type": "json_schema", "name": "answer", "schema": SCHEMA, "strict": True}
    assert body["input"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "items"}]


def test_openai_refusal_raises(server):
    server.reply = openai_reply([{"type": "refusal", "refusal": "I'm sorry, I cannot assist with that request."}])
    with pytest.raises(RuntimeError, match="refused"):
        agents.ask_openai("sys", "items", SCHEMA)


def test_openai_cutoff_raises(server):
    server.reply = openai_reply(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
    with pytest.raises(RuntimeError, match="incomplete"):
        agents.ask_openai("sys", "items", SCHEMA)


def test_gemini_request_and_answer(server):
    server.reply = gemini_reply()
    assert agents.ask_gemini("sys", "items", SCHEMA) == ANSWER
    body = json.loads(server.sent[0].content)
    assert server.sent[0].headers["x-goog-api-key"] == "key-for-GEMINI_API_KEY"
    assert server.sent[0].url.path.endswith("/interactions")
    assert body["model"] == "gemini-3.8-flash"
    assert body["response_format"] == {"type": "text", "mime_type": "application/json", "schema": SCHEMA}
    assert body["system_instruction"] == "sys"
    assert body["input"] == "items"


@pytest.mark.parametrize("status", ["failed", "incomplete"])
def test_gemini_not_completed_raises(server, status):
    server.reply = gemini_reply(status)
    with pytest.raises(RuntimeError, match=status):
        agents.ask_gemini("sys", "items", SCHEMA)


def claude_model(model_id, structured=True, effort=True):
    yes, no = {"supported": True}, {"supported": False}
    return {
        "type": "model", "id": model_id, "display_name": f"Name of {model_id}", "created_at": "2026-01-01T00:00:00Z",
        "capabilities": {
            "batch": yes, "citations": yes, "code_execution": yes, "image_input": yes, "pdf_input": yes,
            "context_management": {"supported": True},
            "effort": {"supported": True, "low": {"supported": effort}, "medium": yes, "high": yes, "max": yes},
            "structured_outputs": yes if structured else no,
            "thinking": {"supported": True, "types": {"adaptive": yes, "enabled": yes}},
        },
    }


def test_claude_models_listed_only_with_structured_outputs(server):
    server.reply = {
        "data": [claude_model("claude-haiku-5-5"), claude_model("claude-old", structured=False),
                 claude_model("claude-haiku-4-5", effort=False)],
        "has_more": False, "first_id": "claude-haiku-5-5", "last_id": "claude-haiku-4-5",
    }
    assert agents.list_claude_models() == [
        {"model": "claude-haiku-5-5", "model_name": "Name of claude-haiku-5-5", "effort": True},
        {"model": "claude-haiku-4-5", "model_name": "Name of claude-haiku-4-5", "effort": False},
    ]


def test_openai_and_gemini_list_text_models_only(server):
    server.reply = {"object": "list", "data": [
        {"id": model_id, "object": "model", "created": 1760000000, "owned_by": "openai"}
        for model_id in ["gpt-6-astra", "gpt-4-turbo", "gpt-image-2.5-flare", "gpt-realtime-2", "gpt-5.6-cyber", "dall-e-3"]
    ]}
    assert [m["model"] for m in agents.list_openai_models()] == ["gpt-6-astra", "gpt-5.6-cyber"]

    server.reply = {"models": [
        {"name": "models/gemini-3.8-flash", "displayName": "Gemini 3.8 Flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-3.8-flash-tts", "displayName": "TTS", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-embedding-001", "displayName": "Embedding", "supportedGenerationMethods": ["embedContent"]},
        {"name": "models/veo-3.1-generate-preview", "displayName": "Veo", "supportedGenerationMethods": ["predictLongRunning"]},
    ]}
    assert agents.list_gemini_models() == [{"model": "gemini-3.8-flash", "model_name": "Gemini 3.8 Flash"}]


@pytest.mark.parametrize("agent, model, output_config", [
    ({}, "claude-opus-5-5", {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}}),
    ({"model": "claude-haiku-5-5", "model_name": "Claude Haiku 5.5", "effort": True},
     "claude-haiku-5-5", {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}}),
    ({"model": "claude-haiku-4-5", "model_name": "Claude Haiku 4.5", "effort": False},
     "claude-haiku-4-5", {"format": {"type": "json_schema", "schema": SCHEMA}}),
])
def test_the_chosen_model_reaches_the_request(server, monkeypatch, agent, model, output_config):
    monkeypatch.setattr(user_prefs, "load_user_prefs", lambda: {"agent": {"enabled": True, "provider": "claude", **agent}})
    server.reply = claude_reply()
    categorize.chosen_ask()("sys", "items", SCHEMA)
    body = json.loads(server.sent[0].content)
    assert (body["model"], body["output_config"]) == (model, output_config)
