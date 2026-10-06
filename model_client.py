"""Shared OpenRouter model settings and completion validation."""
import logging
import requests
from collections import deque

MODEL = "openai/gpt-6-luna"
MAX_OUTPUT_TOKENS = 16000
CALL_RECORDS = deque(maxlen=256)


def post_completion(payload, headers, **kwargs):
    payload = dict(payload)
    payload.update(model=MODEL, reasoning={"effort": "low", "exclude": True})
    payload.pop("temperature", None)
    payload["max_tokens"] = max(MAX_OUTPUT_TOKENS, payload.get("max_tokens", 0))
    for attempt in range(3):
        response = requests.post("https://openrouter.ai/api/v1/chat/completions",
                                 headers=headers, json=payload, **kwargs)
        response.raise_for_status()
        data = response.json()
        choice = data["choices"][0]
        reason = choice.get("finish_reason")
        usage = data.get("usage", {})
        record = {"model": data.get("model", MODEL), "finish_reason": reason,
                  "max_tokens": payload["max_tokens"], "usage": usage}
        CALL_RECORDS.append(record)
        logging.info("[MODEL] model=%s finish=%s max_tokens=%s usage=%s",
                     record['model'], reason, payload['max_tokens'], usage)
        if reason == "length" and attempt < 2:
            payload['max_tokens'] *= 2
            continue
        if reason != "stop":
            raise RuntimeError(f"Model did not complete the answer: {reason}")
        if not (choice.get("message", {}).get("content") or "").strip():
            raise RuntimeError("Model returned empty content")
        return data
    raise RuntimeError("Model output limit exhausted")
