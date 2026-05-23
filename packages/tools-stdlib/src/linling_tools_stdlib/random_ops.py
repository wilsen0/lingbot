"""Probabilistic operations beyond the simple ``random_int`` built-in.

QRDic's ``$概率随机$`` takes two parallel JSON arrays — weights and
values — and returns one of the values proportionally. We accept the
arrays as JSON strings because the DSL only transports strings.
"""

from __future__ import annotations

import json
import random as _random
from typing import Any

from linling_core.tools import ToolCtx, tool


def _parse_json_list(text: str) -> list[Any]:
    """Parse *text* as a JSON list; raise ``ValueError`` otherwise.

    Accepts both ``"[a,b,c]"`` (canonical JSON) and a single scalar
    that we wrap into a one-element list. The scalar fallback exists
    because the DSL's ``[arith]`` evaluator collapses ``[1]`` (a
    one-element JSON array authored in the rule source) into the
    numeric string ``"1"`` *before* it reaches us. Without this
    tolerance, a rule like ``$概率随机 [1] ["only"]$`` would crash
    even though it's semantically a valid one-bucket dice roll.

    Truly non-JSON strings (``"a"`` / ``"hello"``) also pass through
    via the scalar fallback rather than raising — operators typo'ing
    a literal arg shouldn't crash the dispatcher.
    """
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        # Treat as bare string — wrap into a single-element list.
        return [text]
    if isinstance(value, list):
        return value
    # Scalar fallback — preserve the value as a single-element list
    # so the caller's parallel ``values`` list still aligns.
    if isinstance(value, (str, int, float, bool)) or value is None:
        return [value]
    raise ValueError(f"expected JSON array, got {type(value).__name__}")


@tool(
    name="weighted_random",
    dsl_name="概率随机",
    description="Pick one value from 'values' weighted by 'weights' (both JSON arrays)",
    schema={"weights": "string", "values": "string"},
    safe=True,
)
async def weighted_random(ctx: ToolCtx, weights: str = "", values: str = "") -> str:
    """Return one of *values* chosen proportionally to *weights*.

    Both arguments are JSON array strings. Weights should be non-negative
    numbers; values are rendered back to strings (JSON-encoded if they
    aren't strings already).

    The implementation uses :func:`random.choices`. To make tests
    deterministic, seed via :class:`random.Random` before invoking; an
    optional seeded ``random`` instance may be supplied through
    ``ctx.extras["random"]``.
    """
    if not weights or not values:
        return ""
    weight_list = _parse_json_list(weights)
    value_list = _parse_json_list(values)

    if not value_list:
        return ""
    if len(weight_list) != len(value_list):
        # Length mismatch → align by truncating to the shorter list.
        # QRDic's tolerance: shape-mismatched calls degrade rather
        # than raise; the result is best-effort.
        n = min(len(weight_list), len(value_list))
        weight_list = weight_list[:n]
        value_list = value_list[:n]
        if not value_list:
            return ""
    try:
        float_weights = [float(w) for w in weight_list]
    except (TypeError, ValueError):
        # Non-numeric weights → fall back to uniform. Better than
        # crashing the rule on a typo'd weight literal.
        float_weights = [1.0] * len(weight_list)
    if any(w < 0 for w in float_weights):
        # Negative weights → clamp to 0 rather than raise. Same
        # spirit as the all-zero fallback below: degrade gracefully.
        float_weights = [max(0.0, w) for w in float_weights]
    if sum(float_weights) <= 0:
        # All zero → uniform fallback matches QRDic's graceful behaviour.
        float_weights = [1.0] * len(float_weights)

    rng = ctx.extras.get("random")
    choices = rng.choices if isinstance(rng, _random.Random) else _random.choices
    picked = choices(value_list, weights=float_weights, k=1)[0]

    if isinstance(picked, str):
        return picked
    return json.dumps(picked, ensure_ascii=False)
