"""json_chat.py - One JSON-object chat completion against an OpenAI client.

Shared by the shopping conversation engine (domains/shopping/conversation.py)
and long-term memory (services/memory_service.py). Reasoning-model families
(gpt-5*, o*) reject `temperature` and take `max_completion_tokens` instead of
`max_tokens`, so the model can be swapped from .env without touching callers.
"""

import json


def _is_reasoning_model(model: str) -> bool:
    model = (model or "").lower()
    return model.startswith(("gpt-5", "o1", "o3", "o4"))


def chat_json(client, model: str, system: str, user: str, max_tokens: int = 600, temperature: float = 0.2) -> dict:
    """Returns the parsed JSON object. Raises on API errors or invalid JSON —
    callers decide their own fallback."""
    kwargs = {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if _is_reasoning_model(model):
        # Reasoning tokens count against this budget too.
        kwargs["max_completion_tokens"] = max_tokens * 4
    else:
        kwargs["max_tokens"] = max_tokens
        kwargs["temperature"] = temperature

    response = client.chat.completions.create(**kwargs)
    result = json.loads(response.choices[0].message.content)
    if not isinstance(result, dict):
        raise ValueError(f"Expected a JSON object, got {type(result).__name__}")
    return result
