"""Cost-per-query estimate. Built-in mode costs nothing; Gemini mode is estimated from text length (about 4 characters per token).

The per-million-token rates are *assumptions* (edit them in config.py to match the current price list of the model you use).
"""
from __future__ import annotations

from .config import LLM_IN_PER_M, LLM_OUT_PER_M

ROUTER_OVERHEAD_CHARS = 9000      # system prompt + 20 tool declarations sent with every routing call


def estimate(mode: str, question: str, payload_chars: int, answer_chars: int, self_review: bool = True) -> float:
    if not mode.startswith("gemini"):
        return 0.0
    route_in = ROUTER_OVERHEAD_CHARS + len(question)
    narrate_in = 3500 + len(question) + payload_chars
    narrate_out = answer_chars
    critic_in = (3000 + len(question) + payload_chars + answer_chars) if self_review else 0
    critic_out = 300 if self_review else 0
    tin = (route_in + narrate_in + critic_in) / 4
    tout = (60 + narrate_out + critic_out) / 4
    return tin / 1e6 * LLM_IN_PER_M + tout / 1e6 * LLM_OUT_PER_M
