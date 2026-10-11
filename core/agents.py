"""Two functions per AI provider: ask(system, user, schema, model) -> the
answer as a dict matching the JSON `schema`, using the provider's structured
output; and list_<provider>_models() -> the models to choose from in Settings, as
{"model": id, "model_name": display name} (Claude adds "effort": whether the
model takes an effort setting). Each raises on failure (API error, refusal,
cut-off, unparseable reply)."""

import json
import re

from core import config

# provider -> (menu label, key name in core/.env)
PROVIDERS = {
    "claude": ("Claude", "ANTHROPIC_API_KEY"),
    "openai": ("OpenAI (untested)", "OPENAI_API_KEY"),
    "gemini": ("Gemini (untested)", "GEMINI_API_KEY"),
}

# Used until the user picks a model in Settings.
DEFAULT_MODELS = {"claude": "claude-opus-5-5", "openai": "gpt-6-astra", "gemini": "gemini-3.8-flash"}

# Gemini lists no capabilities: these image, speech and video models also
# answer generateContent, but are not text models with structured output.
GEMINI_NOT_TEXT = ("image", "banana", "tts", "transcribe", "omni")


def list_claude_models() -> list[dict]:
    import anthropic  # here, not at the top: the AI SDKs are slow to import

    client = anthropic.Anthropic(api_key=config.get_env_var("ANTHROPIC_API_KEY"))
    return [
        {"model": m.id, "model_name": m.display_name, "effort": m.capabilities.effort.low.supported}
        for m in client.models.list()
        if m.capabilities and m.capabilities.structured_outputs.supported
    ]


def ask_claude(system: str, user: str, schema: dict, model: str = DEFAULT_MODELS["claude"], effort: bool = True) -> dict:
    import anthropic

    client = anthropic.Anthropic(api_key=config.get_env_var("ANTHROPIC_API_KEY"))
    output_config = {"format": {"type": "json_schema", "schema": schema}}
    if effort:
        output_config["effort"] = "low"
    response = client.messages.create(
        model=model,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config=output_config,
    )
    if response.stop_reason != "end_turn":
        raise RuntimeError(f"Claude stopped early: {response.stop_reason}")
    text = next((block.text for block in response.content if block.type == "text"), None)
    if text is None:
        raise RuntimeError("Claude replied with no text")
    return json.loads(text)


def list_openai_models() -> list[dict]:
    """Untested. OpenAI's models list has no capabilities: this keeps GPT-5
    and later (Structured Outputs need GPT-4o or later) to keep the list short."""
    import openai

    client = openai.OpenAI(api_key=config.get_env_var("OPENAI_API_KEY"))
    return [
        {"model": m.id, "model_name": m.id}
        for m in client.models.list()
        if re.match(r"gpt-([5-9]|\d\d)", m.id)
    ]


def ask_openai(system: str, user: str, schema: dict, model: str = DEFAULT_MODELS["openai"]) -> dict:
    """Untested: written from OpenAI's docs, never run with a real key.
    Strict mode needs every property required and additionalProperties false."""
    import openai

    client = openai.OpenAI(api_key=config.get_env_var("OPENAI_API_KEY"))
    response = client.responses.create(
        model=model,
        input=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        text={"format": {"type": "json_schema", "name": "answer", "schema": schema, "strict": True}},
    )
    for output in response.output:
        if output.type == "message":
            for part in output.content:
                if part.type == "refusal":
                    raise RuntimeError(f"OpenAI refused: {part.refusal}")
    if response.status != "completed":
        raise RuntimeError(f"OpenAI stopped early: {response.status} {response.incomplete_details or response.error}")
    return json.loads(response.output_text)


def list_gemini_models() -> list[dict]:
    """Untested. Gemini's models list says which models generate content,
    not which take a JSON schema: this keeps the Gemini text models."""
    from google import genai

    client = genai.Client(api_key=config.get_env_var("GEMINI_API_KEY"))
    return [
        {"model": m.name.removeprefix("models/"), "model_name": m.display_name}
        for m in client.models.list()
        if m.name.startswith("models/gemini") and "generateContent" in (m.supported_actions or [])
        and not any(word in m.name for word in GEMINI_NOT_TEXT)
    ]


def ask_gemini(system: str, user: str, schema: dict, model: str = DEFAULT_MODELS["gemini"]) -> dict:
    """Untested: written from Google's docs, never run with a real key."""
    from google import genai

    client = genai.Client(api_key=config.get_env_var("GEMINI_API_KEY"))
    interaction = client.interactions.create(
        model=model,
        system_instruction=system,
        input=user,
        response_format={"type": "text", "mime_type": "application/json", "schema": schema},
    )
    if interaction.status != "completed":
        raise RuntimeError(f"Gemini stopped early: {interaction.status}")
    return json.loads(interaction.output_text)
