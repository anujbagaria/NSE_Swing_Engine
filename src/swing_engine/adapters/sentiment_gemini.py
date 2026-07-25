"""Gemini implementation of SentimentPort.

Hardening applied here (not in the domain):
- model string PINNED (a version bump can silently change outputs)
- temperature 0 for determinism
- structured Pydantic output, so we parse a bounded enum, never free text
- the score is treated as a COARSE regime tag, not a precise multiplier
- a weekly EWMA over the persisted history smooths the Saturday tag; the
  Monday delta run asks for the RAW tag (smoothed=False) so a weekend shock
  is reflected immediately.
"""
from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel

from ..domain.config import StrategyConfig
from ..domain.types import Regime

PINNED_MODEL = "gemini-3.5-flash"  # pin explicitly; bump deliberately, never implicitly

_REGIME_TO_SCORE = {Regime.RISK_OFF: -1.0, Regime.NEUTRAL: 0.0, Regime.RISK_ON: 1.0}
_SCORE_TO_REGIME = [  # (upper_bound, regime); first match wins
    (-0.34, Regime.RISK_OFF),
    (0.34, Regime.NEUTRAL),
    (1.01, Regime.RISK_ON),
]


class _RegimeResponse(BaseModel):
    regime: Literal["risk_off", "neutral", "risk_on"]
    confidence: float


def _score_to_regime(score: float) -> Regime:
    for upper, regime in _SCORE_TO_REGIME:
        if score <= upper:
            return regime
    return Regime.RISK_ON


class GeminiSentiment:
    def __init__(self, config: StrategyConfig, history: list[float] | None = None):
        self.config = config
        # history = past weekly numeric regime scores, oldest first. Persisted
        # in state so the EWMA is stable across serverless runs.
        self._history = list(history or [])

    @property
    def history(self) -> list[float]:
        return list(self._history)

    def _raw_tag(self, news_text: str) -> Regime:
        from google import genai
        from google.genai import types as gtypes

        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        prompt = (
            "You are a macro risk classifier for Indian equity ETFs. "
            "Given the following recent headlines, classify the near-term market "
            "regime as exactly one of risk_off, neutral, or risk_on, and give a "
            "confidence 0-1. Base it only on the text.\n\nHEADLINES:\n" + news_text
        )
        resp = client.models.generate_content(
            model=PINNED_MODEL,
            contents=prompt,
            config=gtypes.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json",
                response_schema=_RegimeResponse,
            ),
        )
        parsed = _RegimeResponse.model_validate_json(resp.text)
        return Regime(parsed.regime)

    def regime_tag(self, news_text: str, smoothed: bool) -> Regime:
        raw = self._raw_tag(news_text) if news_text.strip() else Regime.NEUTRAL

        if not smoothed:
            return raw  # Monday: react immediately, no dilution

        # Saturday: append raw score, take a weekly EWMA over history.
        self._history.append(_REGIME_TO_SCORE[raw])
        span = self.config.ewma_span_weeks
        alpha = 2.0 / (span + 1.0)
        ewma = self._history[0]
        for s in self._history[1:]:
            ewma = alpha * s + (1 - alpha) * ewma
        return _score_to_regime(ewma)
