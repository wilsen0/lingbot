"""Fishing game logic — the ``$钓鱼抽签$`` draw tool.

The fishing minigame keeps a player's *bucket* (``{species: count}``)
and *dex* (``{species: cumulative}``) as JSON objects in the KV store.
DSL ``$JSON$`` has no "increment a map value" primitive, so doing the
bookkeeping in pure DSL is awkward and error-prone. Instead this tool
owns the whole catch transaction:

1. Decide the catch from the time-window + active rod-enchant buff.
2. Update the bucket (real fish only), the bucket's total 灵玉 value,
   and the dex (fish *and* junk — collecting junk is a gag goal).
3. Return a JSON blob the DSL reads field-by-field via ``@结[name]``.

Keeping the species catalogue, sale values and drop weights in one
Python module (shared with :mod:`linling_tools_stdlib.fishing_image`)
guarantees the settlement/bucket/dex images and the economy maths can
never drift apart.

DSL contract::

    现:$时间 %s$
    甩:$读 休闲系/钓鱼/甩杆秒 %QQ% 0$
    过:[%现%-%甩%]
    福:$读 休闲系/钓鱼/附魔 %QQ% 无$
    结:$钓鱼抽签 %QQ% %过% %福%$
    类:@结[result]      # catch / junk / empty / gone
    名:@结[name]
    价:@结[value]
    首:@结[first]
"""

from __future__ import annotations

import json
import random as _random
from typing import Any

from linling_core.tools import ToolCtx, tool

from linling_tools_stdlib.fishing_image import (
    FISH_CATALOGUE,
    Species,
    _counts_from_legacy_emoji,
    species_by_name,
)

# KV layout — mirrors the DSL ``$读/写 休闲系/钓鱼/<file> %QQ%$`` paths.
_SCOPE = "休闲系/钓鱼"
_BUCKET_FILE = "水桶"
_VALUE_FILE = "水桶价值"
_DEX_FILE = "图鉴"

# Rarity tier names (must match fishing_image.Rarity.name values).
_TIER_LEGEND = "传说"
_TIER_RARE = "稀有"
_TIER_COMMON = "普通"
_TIER_JUNK = "杂物"
_TIER_EMPTY = "空"

# Species grouped by tier, in catalogue order.
_BY_TIER: dict[str, list[Species]] = {
    _TIER_LEGEND: [s for s in FISH_CATALOGUE if s.rarity.name == _TIER_LEGEND],
    _TIER_RARE: [s for s in FISH_CATALOGUE if s.rarity.name == _TIER_RARE],
    _TIER_COMMON: [s for s in FISH_CATALOGUE if s.rarity.name == _TIER_COMMON],
    _TIER_JUNK: [s for s in FISH_CATALOGUE if s.rarity.name == _TIER_JUNK],
}

# Tier draw weights per time-window. Order: legend, rare, common, junk, empty.
# Pulling too early ("早") is mostly empty/junk; the golden window
# ("黄金") has the best rare/legend odds; pulling late ("迟") decays.
_WINDOW_WEIGHTS: dict[str, tuple[float, float, float, float, float]] = {
    "早": (0.0, 1.0, 15.0, 34.0, 50.0),
    "普通": (1.0, 14.0, 60.0, 10.0, 15.0),
    "黄金": (5.0, 30.0, 55.0, 4.0, 6.0),
    "迟": (1.0, 8.0, 40.0, 16.0, 35.0),
}

# Active rod-enchant buff names (see the 附魔 handler in main.ling).
BUFF_LUCKY = "幸运"  # legend/rare weight boost
BUFF_CLEAN = "驱垃圾"  # no junk
BUFF_TIMELY = "守时"  # golden window reached sooner
BUFF_PLUMP = "肥美"  # caught fish worth +50%
KNOWN_BUFFS: tuple[str, ...] = (BUFF_LUCKY, BUFF_CLEAN, BUFF_TIMELY, BUFF_PLUMP)

# Window boundaries in *effective* seconds since cast.
_W_EARLY = 25  # < this: 早
_W_NORMAL = 80  # < this: 普通
_W_GOLDEN = 200  # < this: 黄金
_W_LATE = 420  # < this: 迟; >= this: gone (鱼跑了)
# Hard ceiling on real elapsed regardless of buff — after this the fish
# has definitely escaped.
_W_HARD_GONE = 900


def _window_for(elapsed: float, buff: str) -> str:
    """Map elapsed seconds → time-window name, applying the 守时 buff.

    守时 makes the golden window arrive sooner by inflating the
    effective elapsed time. A negative elapsed (clock skew / stale
    cast timestamp) is treated as "早".
    """
    if elapsed < 0:
        return "早"
    if elapsed >= _W_HARD_GONE:
        return "gone"
    eff = elapsed * (1.6 if buff == BUFF_TIMELY else 1.0)
    if eff < _W_EARLY:
        return "早"
    if eff < _W_NORMAL:
        return "普通"
    if eff < _W_GOLDEN:
        return "黄金"
    if eff < _W_LATE:
        return "迟"
    return "gone"


def _apply_buff_to_weights(
    weights: tuple[float, float, float, float, float], buff: str
) -> list[float]:
    """Return buff-adjusted tier weights [legend, rare, common, junk, empty]."""
    w = list(weights)
    if buff == BUFF_LUCKY:
        w[0] *= 3.0  # legend
        w[1] *= 2.5  # rare
    if buff == BUFF_CLEAN:
        w[3] = 0.0  # junk
    return w


def _rng(ctx: ToolCtx) -> Any:
    rng = ctx.extras.get("random")
    return rng if isinstance(rng, _random.Random) else _random


def _pick_tier(ctx: ToolCtx, weights: list[float]) -> str:
    tiers = [_TIER_LEGEND, _TIER_RARE, _TIER_COMMON, _TIER_JUNK, _TIER_EMPTY]
    if sum(weights) <= 0:
        return _TIER_EMPTY
    picked: str = _rng(ctx).choices(tiers, weights=weights, k=1)[0]
    return picked


def _pick_species(ctx: ToolCtx, tier: str) -> Species | None:
    pool = _BY_TIER.get(tier) or []
    if not pool:
        return None
    chosen: Species = _rng(ctx).choice(pool)
    return chosen


def _load_obj(raw: str | None) -> dict[str, int]:
    try:
        data = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, int] = {}
    for k, v in data.items():
        try:
            out[str(k)] = int(v)
        except (TypeError, ValueError):
            continue
    return out


@tool(
    name="fishing_draw",
    dsl_name="钓鱼抽签",
    description=(
        "Resolve one fishing catch: pick by time-window + buff, update "
        "bucket/dex/value KV, return a JSON result the DSL reads via @结[...]."
    ),
    schema={
        "qq": "string",
        "elapsed": "string",
        "buff": "string?",
    },
    safe=False,
)
async def fishing_draw(
    ctx: ToolCtx,
    qq: str = "",
    elapsed: str = "0",
    buff: str = "",
) -> str:
    """``$钓鱼抽签 %QQ% %过秒% %附魔%$`` → catch result JSON.

    Returns a compact JSON object::

        {"result":"catch|junk|empty|gone",
         "name":"草鱼","emoji":"🐟","rarity":"普通",
         "value":59,"first":0,"buff":"幸运"}

    Side effects (only on a real ``catch``/``junk``):
    * ``catch`` — bucket[name]++ , 水桶价值 += value, 图鉴[name]++
    * ``junk``  — 图鉴[name]++ only (junk isn't sellable inventory)

    ``first`` is ``1`` when this catch is the species' debut in the dex.
    """
    buff = (buff or "").strip()
    if buff in ("无", "0"):
        buff = ""

    try:
        elapsed_s = float(elapsed)
    except (TypeError, ValueError):
        elapsed_s = 0.0

    window = _window_for(elapsed_s, buff)

    # Gone / empty short-circuit — no draw, no KV mutation.
    if window == "gone":
        return json.dumps(
            {
                "result": "gone",
                "name": "",
                "emoji": "",
                "rarity": "",
                "value": 0,
                "first": 0,
                "buff": buff,
            },
            ensure_ascii=False,
        )

    weights = _apply_buff_to_weights(_WINDOW_WEIGHTS[window], buff)
    tier = _pick_tier(ctx, weights)

    if tier == _TIER_EMPTY:
        return json.dumps(
            {
                "result": "empty",
                "name": "",
                "emoji": "",
                "rarity": "",
                "value": 0,
                "first": 0,
                "buff": buff,
            },
            ensure_ascii=False,
        )

    species = _pick_species(ctx, tier)
    if species is None:
        return json.dumps(
            {
                "result": "empty",
                "name": "",
                "emoji": "",
                "rarity": "",
                "value": 0,
                "first": 0,
                "buff": buff,
            },
            ensure_ascii=False,
        )

    is_junk = tier == _TIER_JUNK
    value = 0 if is_junk else species.value
    if not is_junk and buff == BUFF_PLUMP:
        value = round(value * 1.5)

    if not qq:
        # No player id — return the roll without persisting. Keeps the
        # tool usable for previews/tests that don't supply %QQ%.
        return json.dumps(
            {
                "result": "junk" if is_junk else "catch",
                "name": species.name,
                "emoji": species.emoji,
                "rarity": species.rarity.name,
                "value": value,
                "first": 0,
                "buff": buff,
            },
            ensure_ascii=False,
        )

    # --- persist the catch ------------------------------------------------
    dex = _load_obj(await ctx.kv.read(_SCOPE, _DEX_FILE, qq, "{}"))
    first = 1 if dex.get(species.name, 0) <= 0 else 0
    dex[species.name] = dex.get(species.name, 0) + 1
    await ctx.kv.write(_SCOPE, _DEX_FILE, qq, json.dumps(dex, ensure_ascii=False))

    if not is_junk:
        bucket = _load_obj(await ctx.kv.read(_SCOPE, _BUCKET_FILE, qq, "{}"))
        bucket[species.name] = bucket.get(species.name, 0) + 1
        await ctx.kv.write(_SCOPE, _BUCKET_FILE, qq, json.dumps(bucket, ensure_ascii=False))

        try:
            total = int(float(await ctx.kv.read(_SCOPE, _VALUE_FILE, qq, "0") or "0"))
        except (TypeError, ValueError):
            total = 0
        await ctx.kv.write(_SCOPE, _VALUE_FILE, qq, str(total + value))

    return json.dumps(
        {
            "result": "junk" if is_junk else "catch",
            "name": species.name,
            "emoji": species.emoji,
            "rarity": species.rarity.name,
            "value": value,
            "first": first,
            "buff": buff,
        },
        ensure_ascii=False,
    )


# Buff flavour text — shown when a rod-enchant is rolled.
_BUFF_DESC: dict[str, str] = {
    BUFF_LUCKY: "稀有鱼上钩概率大增",
    BUFF_CLEAN: "本段时间不会钓上杂物",
    BUFF_TIMELY: "更快进入黄金咬钩期",
    BUFF_PLUMP: "钓上的鱼额外肥美，售价+50%",
}

# Roll weights for each buff (uniform-ish; 幸运 slightly rarer).
_BUFF_WEIGHTS: dict[str, float] = {
    BUFF_LUCKY: 1.0,
    BUFF_CLEAN: 2.0,
    BUFF_TIMELY: 2.0,
    BUFF_PLUMP: 1.5,
}


@tool(
    name="fishing_enchant_roll",
    dsl_name="附魔抽取",
    description="Roll a random rod-enchant buff; return JSON {buff,charges,desc}.",
    schema={"charges": "string?"},
    safe=True,
)
async def fishing_enchant_roll(ctx: ToolCtx, charges: str = "") -> str:
    """``$附魔抽取 次数$`` → ``{"buff":"幸运","charges":3,"desc":"..."}``.

    Pure (no KV writes) so the rule stays in control of *when* the buff
    is stored and how charges decrement. *charges* defaults to 3 when
    blank/invalid.
    """
    try:
        n = int(charges)
    except (TypeError, ValueError):
        n = 3
    if n <= 0:
        n = 3
    buffs = list(_BUFF_WEIGHTS.keys())
    weights = [_BUFF_WEIGHTS[b] for b in buffs]
    picked = _rng(ctx).choices(buffs, weights=weights, k=1)[0]
    return json.dumps(
        {"buff": picked, "charges": n, "desc": _BUFF_DESC.get(picked, "")},
        ensure_ascii=False,
    )


# Legacy bucket file (pre-rewrite) — a concatenated emoji run.
_LEGACY_BUCKET_FILE = "水桶里有"


def _species_value(name: str) -> int:
    sp = species_by_name(name)
    return sp.value if sp else 0


@tool(
    name="fishing_sell_bucket",
    dsl_name="钓鱼卖鱼",
    description="Sell the whole bucket: migrate legacy data, clear it, return {count,value} JSON.",
    schema={"qq": "string"},
    safe=False,
)
async def fishing_sell_bucket(ctx: ToolCtx, qq: str = "") -> str:
    """``$钓鱼卖鱼 %QQ%$`` → ``{"count":N,"value":V}`` and empties the bucket.

    Migrates any legacy ``水桶里有`` emoji run into the JSON bucket
    first, so old players' fish are counted. Clears the bucket, the
    legacy field and the cached total value; the dex is left intact.
    """
    if not qq:
        return json.dumps({"count": 0, "value": 0}, ensure_ascii=False)

    counts = _load_obj(await ctx.kv.read(_SCOPE, _BUCKET_FILE, qq, "{}"))

    # Fold any legacy emoji-run into the counts (idempotent: we clear it after).
    legacy = await ctx.kv.read(_SCOPE, _LEGACY_BUCKET_FILE, qq, "")
    if legacy:
        for name, n in _counts_from_legacy_emoji(legacy).items():
            counts[name] = counts.get(name, 0) + n

    total = 0
    count = 0
    for name, n in counts.items():
        total += _species_value(name) * n
        count += n

    await ctx.kv.write(_SCOPE, _BUCKET_FILE, qq, "{}")
    await ctx.kv.write(_SCOPE, _VALUE_FILE, qq, "0")
    if legacy:
        await ctx.kv.write(_SCOPE, _LEGACY_BUCKET_FILE, qq, "")

    return json.dumps({"count": count, "value": total}, ensure_ascii=False)


@tool(
    name="fishing_bucket_sync",
    dsl_name="钓鱼背包同步",
    description="Migrate legacy emoji-run bucket → JSON bucket; return the JSON bucket text.",
    schema={"qq": "string"},
    safe=False,
)
async def fishing_bucket_sync(ctx: ToolCtx, qq: str = "") -> str:
    """``$钓鱼背包同步 %QQ%$`` → current bucket JSON (after legacy merge).

    Used by the view handlers (``查看水桶`` / ``我的鱼``) so they can
    render old + new data together. Persists the merged JSON and clears
    the legacy field so the merge happens at most once.
    """
    if not qq:
        return "{}"
    counts = _load_obj(await ctx.kv.read(_SCOPE, _BUCKET_FILE, qq, "{}"))
    legacy = await ctx.kv.read(_SCOPE, _LEGACY_BUCKET_FILE, qq, "")
    if legacy:
        for name, n in _counts_from_legacy_emoji(legacy).items():
            counts[name] = counts.get(name, 0) + n
        merged = json.dumps(counts, ensure_ascii=False)
        await ctx.kv.write(_SCOPE, _BUCKET_FILE, qq, merged)
        await ctx.kv.write(_SCOPE, _LEGACY_BUCKET_FILE, qq, "")
        # Recompute the cached total so 卖鱼 stays consistent.
        total = sum(_species_value(n) * c for n, c in counts.items())
        await ctx.kv.write(_SCOPE, _VALUE_FILE, qq, str(total))
        return merged
    return json.dumps(counts, ensure_ascii=False)
