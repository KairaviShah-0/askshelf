"""Thin wrapper around the Google Gemini SDK with model fall-back and retries (used by the Data Lab planner, explainer and report writer)."""
from __future__ import annotations

import json
import re
import time

from .config import DEFAULT_MODELS


def _json_loads(text: str) -> dict | None:
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.MULTILINE).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, flags=re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


class GeminiBackend:
    """Thin wrapper around google-genai with model fall-back (handles quota / not-found errors)."""

    _RETRY_HINTS = ("429", "quota", "resource_exhausted", "resource exhausted", "404", "not found", "unavailable", "503", "500",
                    "overloaded", "not supported", "deadline", "timeout", "internal", "502", "504", "server", "temporarily")

    def __init__(self, api_key: str, models: list[str] | None = None):
        from google import genai
        from google.genai import types
        self.genai, self.types = genai, types
        self.client = genai.Client(api_key=api_key)
        self.models = list(models or DEFAULT_MODELS)
        self.active: str | None = None

    # -- low level
    def _generate(self, contents, config):
        last: Exception | None = None
        order = ([self.active] if self.active else []) + [m for m in self.models if m != self.active]
        for m in order:
            try:
                resp = self.client.models.generate_content(model=m, contents=contents, config=config)
                self.active = m
                return resp
            except Exception as e:      # noqa: BLE001
                last = e
                if any(h in str(e).lower() for h in self._RETRY_HINTS):
                    continue
                raise
        raise last or RuntimeError("no model available")

    @staticmethod
    def _text(resp) -> str:
        out = []
        for c in (getattr(resp, "candidates", None) or [])[:1]:
            for part in getattr(getattr(c, "content", None), "parts", None) or []:
                if getattr(part, "text", None):
                    out.append(part.text)
        return "".join(out).strip()

    def complete(self, system: str, prompt: str, json_mode: bool = True, temperature: float = 0.2) -> str:
        """General-purpose single call (used by the Data Lab planner, explainer and report writer)."""
        t = self.types
        cfg = t.GenerateContentConfig(system_instruction=system, temperature=temperature, **({"response_mime_type": "application/json"} if json_mode else {}))
        last: Exception | None = None
        for attempt in range(3):                       # transient 5xx / overload errors are common: retry with a short back-off
            try:
                return self._text(self._generate(prompt, cfg))
            except Exception as e:      # noqa: BLE001
                last = e
                time.sleep(1.2 * (attempt + 1))
        raise last or RuntimeError("AI call failed")
