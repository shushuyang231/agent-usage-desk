"""Versioned USD list rates per million text tokens."""

from datetime import datetime
import re
from typing import NamedTuple

PRICE_CHECKED_AT = "2026-11-21"
PRICE_SOURCES = {
    "openai": "https://developers.openai.com/api/docs/pricing",
    "google": "https://ai.google.dev/gemini-api/docs/pricing",
    "anthropic": "https://platform.claude.com/docs/en/about-claude/pricing",
}

# fresh input, cached read, cache write (short retention), output
RATES = {
    "gpt-6.2-astra": (8, .4, 10, 40),
    "gpt-6.2-sol": (2, .1, 2.5, 10),
    "gpt-6.1-sol": (2, .1, 2.5, 10),
    "gpt-6.1-terra": (1, .05, 1.25, 5),
    "gpt-6-astra": (10, 1, 12.5, 50),
    "gpt-6-sol": (2, .2, 2.5, 10),
    "gpt-6-luna": (.1, .01, .125, .5),
    "gpt-5.6-sol": (4, .4, 5, 20),
    "gpt-5.6-terra": (2, .2, 2.5, 12),
    "gpt-5.6-luna": (.2, .02, .25, 1.2),
    "gpt-5.5": (5, .5, None, 30),
    "gpt-5.5-pro": (30, None, None, 180),
    "gpt-5.4": (2.5, .25, None, 15),
    "gpt-5.4-mini": (.75, .075, None, 4.5),
    "gpt-5.4-nano": (.2, .02, None, 1.25),
    "gpt-5.3-codex": (1.75, .175, None, 14),
    "gpt-5.2-codex": (1.75, .175, None, 14),
    "gpt-5.2": (1.75, .175, None, 14),
    "gpt-5.1-codex": (1.25, .125, None, 10),
    "gpt-5.1": (1.25, .125, None, 10),
    "gpt-5": (1.25, .125, None, 10),
    "gpt-5-mini": (.25, .025, None, 2),
    "gpt-5-nano": (.05, .005, None, .4),
    "gemini-4-neon": (3, .15, None, 15),
    "gemini-4-hydrogen": (1.5, .075, None, 7.5),
    "gemini-3.7-flash": (1.5, .15, None, 7.5),
    "gemini-3.6-flash": (1.5, .15, None, 7.5),
    "gemini-3.1-pro": (2, .2, None, 12),
    "gemini-3-pro": (2, .2, None, 12),
    "gemini-3-flash-preview": (.5, .05, None, 3),
    "gemini-3.1-flash-lite": (.25, .025, None, 1.5),
    "gemini-2.5-pro": (1.25, .125, None, 10),
    "gemini-2.5-flash": (.3, .03, None, 2.5),
    "gemini-2.5-flash-lite": (.1, .01, None, .4),
    "claude-opus-5-5": (4, .2, 5, 20),
    "claude-sonnet-5-5": (2, .1, 2.5, 10),
    "claude-opus-4-6": (5, .5, 6.25, 25),
    "claude-opus-4-5": (5, .5, 6.25, 25),
    "claude-sonnet-4-6": (3, .3, 3.75, 15),
    "claude-sonnet-4-5": (3, .3, 3.75, 15),
    "claude-sonnet-4": (3, .3, 3.75, 15),
    "claude-opus-4": (15, 1.5, 18.75, 75),
    "claude-haiku-4-5": (1, .1, 1.25, 5),
}
OPENAI_LONG = frozenset({
    "gpt-6.2-astra", "gpt-6.2-sol", "gpt-6.1-sol", "gpt-6.1-terra",
    "gpt-6-astra", "gpt-6-sol", "gpt-6-luna",
})
GEMINI_LONG = {
    "gemini-3.1-pro": (4, .4, None, 18),
    "gemini-3-pro": (4, .4, None, 18),
    "gemini-2.5-pro": (2.5, .25, None, 15),
}


class Price(NamedTuple):
    model: str | None
    rates: tuple[float | None, ...]
    threshold: int | None
    long_context: bool
    effective_from: str | None
    effective_before: str | None

    def estimate(self, amounts):
        cost = sum(amount * rate for amount, rate in zip(amounts, self.rates) if rate is not None)
        unpriced = sum(amount for amount, rate in zip(amounts, self.rates) if rate is None)
        return cost / 1_000_000, unpriced


def base_model(model):
    name = model.lower().strip().replace(" ", "-")
    name = re.sub(r"^(openai|anthropic|google|gemini|vertex_ai)/", "", name)
    name = re.sub(r"-(\d{4}-\d\d-\d\d|\d{8})$", "", name)
    name = re.sub(r"(?<=\d)\.(?=\d(?:-|$))", "-", name) if name.startswith("claude-") else name
    name = re.sub(r"-(?:extra-low|extra-high|xhigh|medium|high|low|thinking|trial)$", "", name)
    return name.replace("-tiered", "").removesuffix("-preview") if name.startswith("gemini-4-") else (
        name.replace("-tiered", "") if name.startswith("gemini-3.8-flash") else name
    )


def price_for(label, day, input_tokens):
    model = base_model(label)
    model = {"gpt-5-codex": "gpt-5", "gpt-5.6": "gpt-5.6-sol"}.get(model, model)
    rates = RATES.get(model)
    cutoff = None
    if model in ("gemini-4-argon", "gemini-4-carbon"):
        cutoff = "2026-12-01"
        rates = (2, .1, None, 10) if day < cutoff else (4, .2, None, 20)
    elif model in ("gemini-3.8-flash", "gemini-3.8-flash-n"):
        cutoff = "2027-01-01"
        rates = (.75, .075, None, 3.75) if day < cutoff else (1.5, .15, None, 7.5)
    if rates is None:
        return Price(None, (None,) * 4, None, False, None, None)
    threshold = None
    if model in OPENAI_LONG:
        threshold = 272_000
        if input_tokens > threshold:
            rates = tuple(value * multiplier if value is not None else None
                          for value, multiplier in zip(rates, (2, 2, 2, 1.5)))
    elif model in GEMINI_LONG:
        threshold = 200_000
        if input_tokens > threshold:
            rates = GEMINI_LONG[model]
    return Price(model, rates, threshold, threshold is not None and input_tokens > threshold,
                 cutoff if cutoff and day >= cutoff else None,
                 cutoff if cutoff and day < cutoff else None)


def estimate(event):
    day = datetime.fromtimestamp(event.timestamp).date().isoformat()
    amounts = (event.fresh, event.read, event.write, event.output)
    return price_for(event.model, day, sum(amounts[:3])).estimate(amounts)
