"""هوش مصنوعی (Gemini): تبدیل جمله کاربر به برنامه زمان‌بندی‌شده برای ربات."""
from typing import NamedTuple

import requests

from automation import compile_plan, extract_json

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODELS = ["gemini-flash-latest", "gemini-2.5-flash"]
TIMEOUT_SEC = 25
MAX_PROMPT_CHARS = 500

SYSTEM_PROMPT = """You convert a user's request (in any language) into a JSON plan for a small tank robot.
Reply with JSON only, no explanations: {"steps": [ ... ]}

Allowed steps:
{"do":"drive","dir":"forward|back|left|right|forward_left|forward_right|back_left|back_right","sec":0.1-10}
{"do":"wait","sec":0.1-10}
{"do":"light","value":true|false}
{"do":"horn"}
{"do":"spin"}   (one full 360-degree spin in place, takes about 2 seconds)
{"do":"stop"}
{"do":"repeat","times":1-50,"steps":[ ...steps above, but no nested repeat... ]}

Rules:
- "left" and "right" turn the robot in place; "forward_left" etc. move while turning.
- Use "wait" for pauses. A drive step stops the robot by itself when its time ends.
- Keep the whole plan under 60 seconds. Never invent other step types.
- If the request is unrelated, impossible, or unsafe, return {"steps": [], "note": "<short reason in English>"}.
Example: "blink the light every second 3 times" ->
{"steps":[{"do":"repeat","times":3,"steps":[{"do":"light","value":true},{"do":"wait","sec":1},{"do":"light","value":false},{"do":"wait","sec":1}]}]}"""


class AIError(Exception):
    """پیام کوتاه انگلیسی برای نمایش در برنامه."""


class PlanResult(NamedTuple):
    events: list
    warnings: list
    note: str


def _error_text(resp):
    try:
        return str(resp.json()["error"]["message"])
    except Exception:
        return resp.text[:120]


def _extract_text(data):
    candidates = data.get("candidates") or []
    if not candidates:
        raise AIError("AI: empty answer (blocked?)")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    if not text.strip():
        raise AIError("AI: empty answer")
    return text


def request_plan(api_key, text, models=None, timeout=TIMEOUT_SEC):
    """درخواست به Gemini؛ برنامه (dict) را برمی‌گرداند یا AIError می‌دهد."""
    if not api_key:
        raise AIError("AI: enter API key")
    text = text.strip()[:MAX_PROMPT_CHARS]
    if not text:
        raise AIError("AI: empty request")

    body = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": text}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}

    last = "AI: model not found"
    for model in (models or DEFAULT_MODELS):
        try:
            resp = requests.post(API_URL.format(model=model), headers=headers, json=body, timeout=timeout)
        except requests.exceptions.Timeout:
            raise AIError("AI: timeout")
        except requests.exceptions.RequestException:
            raise AIError("AI: no internet connection")

        if resp.status_code == 404:
            last = f"AI: model '{model}' not found"
            continue
        if resp.status_code in (400, 401, 403):
            message = _error_text(resp)
            if resp.status_code == 403 and "model" in message.lower():
                last = f"AI: model '{model}' not allowed"
                continue
            if "api key" in message.lower() or resp.status_code in (401, 403):
                raise AIError("AI: API key rejected")
            raise AIError(f"AI: {message[:80]}")
        if resp.status_code == 429:
            raise AIError("AI: rate limit, wait a bit")
        if resp.status_code >= 500:
            raise AIError("AI: server busy, retry")
        if resp.status_code != 200:
            raise AIError(f"AI: error {resp.status_code}")

        try:
            return extract_json(_extract_text(resp.json()))
        except AIError:
            raise
        except Exception:
            raise AIError("AI: could not read the answer")
    raise AIError(last)


def make_plan(api_key, text, models=None):
    plan = request_plan(api_key, text, models)
    note = str(plan.get("note", ""))[:120] if isinstance(plan, dict) else ""
    events, warnings = compile_plan(plan)
    return PlanResult(events, warnings, note)
