"""Helpers for pacing multi-message assistant replies."""

from __future__ import annotations

import random

from linling_core.events import ACTION_DELAY_BEFORE_OPTION, Action


def with_random_delay_before(
    action: Action,
    *,
    min_s: float,
    max_s: float,
) -> Action:
    """Return ``action`` tagged with a random pre-send delay, if enabled."""
    delay_s = random_delay_seconds(min_s=min_s, max_s=max_s)
    if delay_s is None:
        return action
    options = dict(action.options)
    options[ACTION_DELAY_BEFORE_OPTION] = delay_s
    return action.model_copy(update={"options": options})


def random_delay_seconds(*, min_s: float, max_s: float) -> float | None:
    """Return a random delay in seconds, or ``None`` when pacing is disabled."""
    lo = max(0.0, float(min_s))
    hi = max(0.0, float(max_s))
    if hi <= 0:
        return None
    if hi < lo:
        lo, hi = hi, lo
    return random.uniform(lo, hi)
