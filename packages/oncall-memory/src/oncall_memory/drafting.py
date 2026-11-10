"""An optional model drafter for a proposed lesson's one sentence.

SEPARATE FROM `reflexion` ON PURPOSE. `reflexion.propose` takes a drafter as an
argument and works without one, so the whole proposal path is testable with no
key, no network and no nondeterminism -- which is why the tests for it do not
mock an HTTP call. This module is the only place that talks to a provider, and
nothing in the package imports it unless a caller asks for a model.

`urllib`, not an SDK, following `oncall_eval.judge.core`: `oncall-memory` may
import only contracts and knowledge, so a model client here has to be stdlib or
a new dependency, and the judge already settled that question.

WHAT THE MODEL IS TOLD IT MAY WRITE is one sentence. It is not asked for
preconditions, a status, a service or an id, because it is not given the chance
to supply them -- `reflexion.propose` ignores everything but the returned
string. The prompt says so anyway, since a model that believes it is filling a
form writes worse prose than one that knows it is writing a sentence.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
API_KEY_ENV = "OPENROUTER_API_KEY"
MODEL_ENV = "REFLEXION_MODEL"
#: The same small model the single-agent topology uses, so a reflexion note
#: costs what an investigation step costs.
DEFAULT_MODEL = "openai/gpt-4o-mini"
_TIMEOUT = 30

_SYSTEM = (
    "You write one sentence of operational guidance for an on-call engineer. "
    "Return the sentence and nothing else: no preamble, no quotes, no bullet, "
    "no JSON, no status, no preconditions. State what to do or what to distrust "
    "next time, not what happened. Under 300 characters."
)


class DraftingUnavailable(Exception):
    """No model is configured, or the provider could not be reached.

    Raised rather than falling back to the deterministic sentence: a caller that
    asked for a model and silently got a template would report a reflexion loop
    it did not run. `reflexion.propose` handles its own fallback when it is
    given no drafter at all, which is a different and honest state.
    """


def _prompt(context: dict[str, Any]) -> str:
    reading = {
        "held": "the action cleared the alert and telemetry confirmed it",
        "unconfirmed": (
            "the system reported recovery that telemetry never confirmed -- the "
            "interesting case: say what to distrust"
        ),
        "absent": "no recovery was observed",
    }.get(str(context.get("recovery")), "the outcome is unknown")
    return (
        f"Incident on service {context.get('service') or 'unknown'}.\n"
        f"What paged: {context.get('symptom')}\n"
        f"Outcome: {reading}\n\n"
        "A deterministic draft of the sentence, which you should improve on but "
        f"not contradict:\n{context.get('deterministic')}"
    )


def openrouter_drafter(model: str | None = None) -> Callable[[dict[str, Any]], str]:
    """A drafter calling OpenRouter. Raises if no key is set, rather than later."""
    key = os.environ.get(API_KEY_ENV)
    if not key:
        raise DraftingUnavailable(
            f"{API_KEY_ENV} is not set, so no model can draft the statement. Propose "
            "without --draft-with-model to record the deterministic sentence instead."
        )
    chosen = model or os.environ.get(MODEL_ENV) or DEFAULT_MODEL

    def draft(context: dict[str, Any]) -> str:
        body = json.dumps(
            {
                "model": chosen,
                "messages": [
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": _prompt(context)},
                ],
                "temperature": 0,
                "max_tokens": 160,
            }
        ).encode()
        request = Request(
            f"{OPENROUTER_BASE_URL}/chat/completions",
            data=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=_TIMEOUT) as response:
                payload = json.load(response)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            raise DraftingUnavailable(f"OpenRouter call failed: {exc}") from exc
        try:
            return str(payload["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise DraftingUnavailable(f"unexpected OpenRouter response shape: {exc}") from exc

    return draft


def model_id(model: str | None = None) -> str:
    """The model a drafter would use, for recording provenance in the log."""
    return model or os.environ.get(MODEL_ENV) or DEFAULT_MODEL
