"""Player marketplace — atomic listing / purchase / cancel operations.

The marketplace lets players put items up for sale at a per-unit price
in :data:`~linling_core.storage.kv` paths that the existing rules use
(``啊/灵玉系/灵玉`` for the trade currency, ``休闲系/钓鱼/鱼竿`` /
``鱼饵`` and ``休闲系/珍品/蛋壳`` for the MVP-tradable goods). The
DSL can express most of this through ``$读$`` / ``$写$``, but those
are individual statements with no atomicity — two concurrent buyers
could both pass the "left ≥ 1" check and then both decrement, selling
more units than were ever on the listing.

To prevent that, all four mutating operations funnel through this
module and use the underlying :meth:`SqliteKVStore.transaction`
context manager. Each call holds the connection-level
``_tx_lock`` for its duration, so even if the rule authors reach
the DB from a thousand concurrent rule executions only one
"check status → debit buyer → credit seller → decrement left"
sequence is in flight at a time.

Tradable item whitelist
------------------------
Items eligible for the marketplace live in :data:`TRADABLE_ITEMS`.
The :file:`bot/rules/main.ling` 上架 trigger is hard-coded to the
three MVP names (``鱼竿``/``鱼饵``/``蛋壳``); adding a new line here
also requires a corresponding update in the trigger regex. The
``tests/test_trade_ops.py::test_tradable_items_known`` test pins the
whitelist so a future addition shows up there. Items that exist as
group-scoped KV paths (e.g. the ``啊/%群%/禁言卡`` family — the
runtime resolves the ``%群%`` placeholder per-event) are rejected by
:func:`_is_group_scoped_item` because the marketplace operates on the
player's global inventory.

Price model
-----------
The seller's ``price_each`` is what they want to *receive* per unit.
The buyer pays ``ceil(price_each * 1.04)`` — 4% goes to the bot's
admin account ``1707476110`` (same sink as :file:`bot/rules/main.ling`'s
灵玉划转). The ceiling avoids rounding the buyer up by zero on small
listings; rounding down would silently let the seller over-receive.

Expiration
----------
Each listing carries ``expire_at`` (epoch seconds, set at creation).
Buying checks the timestamp inside the same transaction and rejects
expired listings, then lazily flips ``status="expired"`` and refunds
the escrow. No background sweeper is required.
"""

from __future__ import annotations

import base64
import io
import json
import math
import random
import time
from pathlib import Path
from typing import Any

import structlog
from linling_core.storage.kv import KVStore
from linling_core.storage.sqlite_kv import SqliteKVStore
from linling_core.tools import ToolCtx, tool
from PIL import Image, ImageDraw, ImageFont

logger = structlog.get_logger(__name__)

# 4% service fee to the bot admin account, mirroring the 灵玉划转
# commission in bot/rules/main.ling. Buyer pays this on top of the
# seller's listed price; rounding is the buyer's loss, never the
# seller's gain.
_FEE_RATE = 0.04
_BOT_FEE_ACCOUNT = "1707476110"

# Items the marketplace will accept. Each entry maps the user-facing
# name to its KV ``scope/file`` pair (multi-segment scope allowed; the
# KV layer handles ``rpartition("/')`` internally).
#
# Anything not in this dict is rejected at the tool boundary. That's
# deliberately restrictive: boolean items (锦囊 / 节日礼包 / the nine
# 珍品) are bounded to 0/1 so a single sale would empty the seller's
# stash, and structural items (钓鱼图鉴 is a JSON dict) cannot be
# split into discrete tradeable units.
TRADABLE_ITEMS: dict[str, tuple[str, str]] = {
    "鱼竿": ("休闲系/钓鱼", "鱼竿"),
    "鱼饵": ("休闲系/钓鱼", "鱼饵"),
    "蛋壳": ("休闲系/珍品", "蛋壳"),
}

# Default listing duration. The DSL caller can override per-listing
# via ``上架挂单``'s ``duration_hours`` argument; 24h is the same
# window most QRDic-era systems used for "next-day reset" cycles.
DEFAULT_DURATION_S = 24 * 3600

# Anti-spam: a given (buyer, listing) pair may complete at most one
# purchase per hour. Stops the obvious "split your order into 99
# 1-unit buys to dodge price tiers" trick.
_BUY_COOLDOWN_S = 3600

# Per-seller cap on simultaneously active listings. Stops runaway
# automation from filling the store with junk and starving legitimate
# sellers of the global-active index budget.
_MAX_ACTIVE_LISTINGS_PER_SELLER = 10

# Large-trade notification threshold. Mirrors 灵玉划转 in
# bot/rules/main.ling: anything above this rings the admin.
_LARGE_TRADE_THRESHOLD = 1999

# KV layout. Kept under the existing ``啊/`` umbrella so the WebUI
# KV browser picks it up automatically.
_SCOPE_LISTING = "啊/摊位/挂单"  # file = <list_id>, value = JSON blob
_SCOPE_GLOBAL_INDEX = "啊/摊位/索引/全局"
_SCOPE_SELLER_INDEX = "啊/摊位/索引/卖家"
_SCOPE_BUYER_COOLDOWN = "啊/摊位/购买冷却"
_SCOPE_LARGE_TRADE = "啊/摊位/大额"

_FILE_ACTIVE = "active"  # JSON list of list_ids in the global index
_FILE_SELLER = "卖家"  # JSON list of list_ids, key = seller QQ
_FILE_LARGE = "大额记录"  # key = "<list_id>|<buyer>" -> ts


def _inventory_scope(item: str) -> str:
    """Return the KV scope where *item* is stored on a player."""
    return TRADABLE_ITEMS[item][0]


def _inventory_file(item: str) -> str:
    """Return the KV file segment where *item* is stored on a player."""
    return TRADABLE_ITEMS[item][1]


def _is_group_scoped_item(item: str) -> bool:
    """True if *item*'s canonical path includes the ``%群%`` placeholder.

    Group-scoped items (e.g. 御妖符 = ``啊/%群%/禁言卡``) make no sense
    in a cross-group marketplace — buying in group A and using in
    group B would let players launder items across group economies.
    None of the three tradable items are group-scoped today, but the
    guard is here so a future item addition can't slip through.
    """
    return "%群%" in _inventory_scope(item)


def _parse_int(s: str | None, default: int = 0) -> int:
    if s is None:
        return default
    try:
        return int(s)
    except (TypeError, ValueError):
        return default


def _read_json(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _dump_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _new_listing_id() -> str:
    """Generate a short, unique listing id.

    4 characters drawn from ``[0-9A-Z]`` (36^4 = ~1.7M options) — short
    enough for users to type back when buying or cancelling, big enough
    for small-group use. The sqlite write is keyed on the id so a
    collision would overwrite the existing listing; with 36^4 space
    and a 1.7M-row active index that stays well under 50% load, the
    birthday-collision rate is acceptable for the current scale. We
    don't try to be cryptographically unique — uniqueness only needs
    to hold long enough for the durability of the global active
    index.
    """
    r = random.SystemRandom()
    return "".join(r.choice(_ID_ALPHABET) for _ in range(4))


_ID_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


async def _credit_inventory(
    tx: Any, seller: str, item: str, qty: int
) -> None:
    """Add *qty* of *item* back to *seller*'s inventory.

    Listing creation debits the seller's row by ``total``; cancel /
    expire / buy-failure paths call this helper to put the remaining
    units back. The "escrow" terminology is just shorthand for the
    delta between the listing's ``left`` field and the seller's row.
    """
    scope = _inventory_scope(item)
    file = _inventory_file(item)
    current_raw = await tx.read(scope, file, seller, "0")
    current = _parse_int(current_raw)
    await tx.write(scope, file, seller, str(current + qty))


async def _debit_inventory(
    tx: Any, seller: str, item: str, qty: int
) -> bool:
    """Subtract *qty* of *item* from *seller*'s inventory.

    Returns False (without writing) if the seller doesn't have enough.
    The caller is responsible for failing the listing creation when
    this returns False.
    """
    scope = _inventory_scope(item)
    file = _inventory_file(item)
    current_raw = await tx.read(scope, file, seller, "0")
    current = _parse_int(current_raw)
    if current < qty:
        return False
    await tx.write(scope, file, seller, str(current - qty))
    return True


def _require_sqlite_kv(kv: KVStore) -> SqliteKVStore:
    """Narrow the Protocol reference to the SQLite impl.

    The marketplace needs :meth:`SqliteKVStore.transaction` for
    atomicity. If a non-SQLite backend is wired (Postgres, Redis —
    per the Protocol's :file:`linling_core/storage/kv.py` docstring),
    the marketplace can't run safely and we fail closed rather than
    silently allowing oversells.
    """
    if isinstance(kv, SqliteKVStore):
        return kv
    raise RuntimeError(
        "marketplace requires SqliteKVStore (got "
        f"{type(kv).__name__}); non-SQLite backends need a "
        "transaction-aware trade_ops implementation"
    )


# ---------------------------------------------------------------------------
# $上架挂单 item qty price_each [duration_hours]$
# ---------------------------------------------------------------------------


@tool(
    name="marketplace_list",
    dsl_name="上架挂单",
    description=(
        "List items for sale on the player marketplace. "
        "$上架挂单 item qty price_each [duration_hours]$ — "
        "qty and price_each are positive integers; duration_hours "
        "defaults to 24. Returns the listing id on success, or an "
        "error message string starting with 'error:' on failure."
    ),
    schema={
        "item": "string",
        "qty": "string",
        "price_each": "string",
        "duration_hours": "string?",
    },
    safe=False,
    llm_visible=False,
)
async def marketplace_list(
    ctx: ToolCtx,
    item: str = "",
    qty: str = "",
    price_each: str = "",
    duration_hours: str = "",
) -> str:
    """Put *qty* units of *item* up for sale at *price_each* per unit.

    Pricing: *price_each* is what the seller wants to *receive* per
    unit; the buyer pays ``ceil(price_each * 1.04)`` to cover the
    4% service fee.

    Returns the new listing id (e.g. ``142301-MXQR``) on success.
    On failure returns ``error:<reason>`` so the DSL layer can decide
    whether to surface the message to the user.
    """
    if not item or item not in TRADABLE_ITEMS:
        return (
            f"error:嗯…{item} 现在还摆不出来哦"
            f"（苏苏能上架的：{'/'.join(TRADABLE_ITEMS)}）"
        )
    if _is_group_scoped_item(item):
        return f"error:{item} 是全群才有的东西，不能挂单哦"
    qty_i = _parse_int(qty)
    price_i = _parse_int(price_each)
    if qty_i <= 0 or price_i <= 0:
        return "error:数量和单价得是正整数才能上架呀"
    duration_h = _parse_int(duration_hours, 24)
    if duration_h < 1 or duration_h > 24 * 7:
        return "error:挂单时长挑个 1 到 168 小时之间的吧"

    seller = ctx.event.sender.id if ctx.event is not None else ""
    if not seller:
        return "error:无法识别发送者"

    sq = _require_sqlite_kv(ctx.kv)
    list_id = _new_listing_id()
    now = int(time.time())
    expire_at = now + min(duration_h, 168) * 3600

    async with sq.transaction() as tx:
        # 1) Anti-spam: cap active listings per seller.
        seller_index_raw = await tx.read(_SCOPE_SELLER_INDEX, _FILE_SELLER, seller, "[]")
        seller_index: list[str] = list(_read_json(seller_index_raw) or [])
        active_for_seller = 0
        for lid in seller_index:
            blob = await tx.read(_SCOPE_LISTING, "挂单", lid, "")
            if blob and _parse_json_field(blob, "status") == "active":
                active_for_seller += 1
        if active_for_seller >= _MAX_ACTIVE_LISTINGS_PER_SELLER:
            return f"error:手上还挂着{_MAX_ACTIVE_LISTINGS_PER_SELLER}单呢，先撤一些再来吧"

        # 2) Debit seller inventory (escrow).
        ok = await _debit_inventory(tx, seller, item, qty_i)
        if not ok:
            return f"error:你身上 {item} 不够啦"

        # 3) Write the listing record.
        listing = {
            "seller": seller,
            "item": item,
            "total": qty_i,
            "left": qty_i,
            "price_each": price_i,
            "created_at": now,
            "expire_at": expire_at,
            "status": "active",
        }
        await tx.write(_SCOPE_LISTING, "挂单", list_id, _dump_json(listing))

        # 4) Update indexes.
        seller_index.append(list_id)
        await tx.write(
            _SCOPE_SELLER_INDEX,
            _FILE_SELLER,
            seller,
            _dump_json(seller_index),
        )
        global_raw = await tx.read(_SCOPE_GLOBAL_INDEX, _FILE_ACTIVE, "all", "[]")
        global_list: list[str] = list(_read_json(global_raw) or [])
        global_list.append(list_id)
        await tx.write(
            _SCOPE_GLOBAL_INDEX,
            _FILE_ACTIVE,
            "all",
            _dump_json(global_list),
        )

    return list_id


# ---------------------------------------------------------------------------
# $购买挂单 list_id qty$
# ---------------------------------------------------------------------------


@tool(
    name="marketplace_buy",
    dsl_name="购买挂单",
    description=(
        "Buy qty units from listing list_id. "
        "$购买挂单 list_id qty$. Returns 'ok:paid=...,received=...,item=...' "
        "on success, or 'error:<reason>' on failure (insufficient "
        "funds, expired, already sold, self-purchase blocked, …)."
    ),
    schema={"list_id": "string", "qty": "string"},
    safe=False,
    llm_visible=False,
)
async def marketplace_buy(
    ctx: ToolCtx, list_id: str = "", qty: str = ""
) -> str:
    """Atomically execute a marketplace purchase.

    Side effects (all inside one SQLite transaction):
      1. Verify listing is ``active`` and not expired.
      2. Verify ``buyer != seller``.
      3. Debit buyer 灵玉 by ``ceil(qty * price_each * 1.04)``.
      4. Credit seller 灵玉 by ``qty * price_each``.
      5. Credit bot admin 灵玉 by the 4% fee.
      6. Decrement ``left`` (or mark ``sold_out`` if it hits zero).
      7. Enforce 1h per-(buyer, listing) cooldown.
      8. Drop the listing from the global active index when fully
         consumed or expired.
      9. Log a 啊/摊位/大额 record when the trade exceeds
         :data:`_LARGE_TRADE_THRESHOLD` 灵玉.
    """
    if not list_id:
        return "error:挂单号不能为空"
    qty_i = _parse_int(qty)
    if qty_i <= 0:
        return "error:购买数量必须是正整数"

    buyer = ctx.event.sender.id if ctx.event is not None else ""
    if not buyer:
        return "error:无法识别购买者"

    sq = _require_sqlite_kv(ctx.kv)
    now = int(time.time())
    cooldown_key = f"{list_id}|{buyer}"

    async with sq.transaction() as tx:
        # 1) Read & validate the listing.
        blob = await tx.read(_SCOPE_LISTING, "挂单", list_id, "")
        if not blob:
            return f"error:没找到挂单 {list_id}，可能已经撤掉或卖完啦"
        listing = _read_json(blob)
        if not isinstance(listing, dict):
            return f"error:挂单 {list_id} 数据损坏"
        status = listing.get("status")
        seller = listing.get("seller", "")
        item = listing.get("item", "")
        left = _parse_int(str(listing.get("left", 0)))
        price_each = _parse_int(str(listing.get("price_each", 0)))
        expire_at = _parse_int(str(listing.get("expire_at", 0)))

        if status == "expired" or (expire_at and now > expire_at):
            # Lazy expire + refund.
            await _expire_listing_locked(tx, list_id, listing, seller, item)
            return f"error:挂单 {list_id} 过久了，苏苏已经帮你撤掉啦"
        if status != "active":
            return f"error:挂单 {list_id} 已经下架啦"
        if buyer == seller:
            return "error:自己挂的东西不能自己买哦，留给别人嘛"
        if item not in TRADABLE_ITEMS:
            return f"error:这单 {item} 现在不能交易了"
        if left < qty_i:
            return f"error:这单只剩 {left} 个啦，不够你要的"

        # 2) Cooldown check.
        last_buy_raw = await tx.read(_SCOPE_BUYER_COOLDOWN, "购买冷却", cooldown_key, "0")
        last_buy = _parse_int(last_buy_raw)
        if last_buy and now - last_buy < _BUY_COOLDOWN_S:
            return "error:这单一小时内已经买过啦，过会儿再来看看"

        # 3) Funds check + transfers.
        gross_pay = math.ceil(qty_i * price_each * (1 + _FEE_RATE))
        fee = gross_pay - qty_i * price_each
        buyer_balance_raw = await tx.read("啊/灵玉系", "灵玉", buyer, "0")
        buyer_balance = _parse_int(buyer_balance_raw)
        if buyer_balance < gross_pay:
            return f"error:灵玉不太够呢…需要 {gross_pay}，你身上只有 {buyer_balance}"
        seller_balance_raw = await tx.read("啊/灵玉系", "灵玉", seller, "0")
        seller_balance = _parse_int(seller_balance_raw)
        fee_balance_raw = await tx.read("啊/灵玉系", "灵玉", _BOT_FEE_ACCOUNT, "0")
        fee_balance = _parse_int(fee_balance_raw)

        await tx.write("啊/灵玉系", "灵玉", buyer, str(buyer_balance - gross_pay))
        await tx.write("啊/灵玉系", "灵玉", seller, str(seller_balance + qty_i * price_each))
        if fee > 0:
            await tx.write(
                "啊/灵玉系",
                "灵玉",
                _BOT_FEE_ACCOUNT,
                str(fee_balance + fee),
            )

        # 4) Move goods to buyer.
        await _credit_inventory(tx, buyer, item, qty_i)

        # 5) Update listing left / status.
        new_left = left - qty_i
        listing["left"] = new_left
        if new_left <= 0:
            listing["status"] = "sold_out"
            await _drop_from_global_index_locked(tx, list_id)
        await tx.write(_SCOPE_LISTING, "挂单", list_id, _dump_json(listing))

        # 6) Cooldown + large-trade record.
        await tx.write(_SCOPE_BUYER_COOLDOWN, "购买冷却", cooldown_key, str(now))
        if gross_pay > _LARGE_TRADE_THRESHOLD:
            await tx.write(
                _SCOPE_LARGE_TRADE,
                _FILE_LARGE,
                f"{list_id}|{now}",
                _dump_json(
                    {
                        "seller": seller,
                        "buyer": buyer,
                        "item": item,
                        "qty": qty_i,
                        "gross": gross_pay,
                        "fee": fee,
                    }
                ),
            )

    return (
        f"ok:付了 {gross_pay} 灵玉"
        f",到手 {qty_i * price_each} 灵玉的 {item}"
    )


# ---------------------------------------------------------------------------
# $撤单 list_id$
# ---------------------------------------------------------------------------


@tool(
    name="marketplace_cancel",
    dsl_name="撤单",
    description=(
        "Cancel one of your own active marketplace listings and "
        "refund the remaining inventory. Returns 'ok' on success, "
        "or 'error:<reason>' (not your listing, already sold out, …)."
    ),
    schema={"list_id": "string"},
    safe=False,
    llm_visible=False,
)
async def marketplace_cancel(ctx: ToolCtx, list_id: str = "") -> str:
    """Cancel *list_id* if owned by the current sender."""
    if not list_id:
        return "error:挂单号不能为空"
    seller = ctx.event.sender.id if ctx.event is not None else ""
    if not seller:
        return "error:无法识别发送者"

    sq = _require_sqlite_kv(ctx.kv)
    async with sq.transaction() as tx:
        blob = await tx.read(_SCOPE_LISTING, "挂单", list_id, "")
        if not blob:
            return f"error:没找到挂单 {list_id}，可能已经撤掉或卖完啦"
        listing = _read_json(blob)
        if not isinstance(listing, dict):
            return f"error:挂单 {list_id} 数据损坏"
        if listing.get("seller") != seller:
            return "error:这单是别人挂的，苏苏不能帮你撤哦"
        status = listing.get("status")
        if status in ("sold_out", "cancelled", "expired"):
            return f"error:挂单 {list_id} 已经下架啦"
        item = listing.get("item", "")
        left = _parse_int(str(listing.get("left", 0)))
        await _credit_inventory(tx, seller, item, left)
        listing["status"] = "cancelled"
        listing["left"] = 0
        await tx.write(_SCOPE_LISTING, "挂单", list_id, _dump_json(listing))
        await _drop_from_global_index_locked(tx, list_id)
    return "ok"


# ---------------------------------------------------------------------------
# $列出挂单 [item]$
# ---------------------------------------------------------------------------


@tool(
    name="marketplace_list_active",
    dsl_name="列出挂单",
    description=(
        "List active marketplace listings. Filters: item=name "
        "(single-item category), seller=QQ (one player's listings), "
        "agg=1 (returns the by-item summary block used by the main "
        "menu). item and seller are mutually exclusive; agg takes "
        "precedence. Returns a text block, or empty string if no "
        "matches."
    ),
    schema={"item": "string?", "seller": "string?", "agg": "string?"},
    safe=True,
    llm_visible=False,
)
async def marketplace_list_active(
    ctx: ToolCtx, item: str = "", seller: str = "", agg: str = ""
) -> str:
    """Render active listings as a text block, or as a by-item summary.

    Three modes driven by argument shape:

    Three modes driven by argument shape:

    * ``agg == "1"`` — return the main-menu summary block: total
      active listing count, total units on offer, and a per-item
      line for each tradable category that has at least one
      listing. Used by the ``摊位`` first screen.
    * ``item`` set — return up to 20 lines, one per active listing
      of that item, in the same ``id item left/total price seller``
      shape as before. Used by ``摊位鱼竿`` etc.
    * ``seller`` set — return every active listing owned by that
      QQ. No 20-row cap here (per product decision: player stalls
      are usually <10 rows, and the 摊位@某人 caller wants the
      whole stall visible). Used by ``摊位@QQ``.

    Both filters skip and lazily refund any listing that's
    ``active`` but past its ``expire_at`` — the same lazy-cleanup
    pass the original code did.
    """
    sq = _require_sqlite_kv(ctx.kv)
    raw = await sq.read(_SCOPE_GLOBAL_INDEX, _FILE_ACTIVE, "all", "[]")
    ids = list(_read_json(raw) or [])
    # Normalize the DSL pass-through placeholders for "no filter".
    # The DSL tokeniser drops empty tokens between spaces, so rule
    # authors write ``$列出挂单 - %QQ% $`` (or historically ``$…$ …$``
    # whose second ``$`` is the closing delimiter and the inner slot
    # collapses to a single ``$``) to leave the item slot empty.
    # Treat all three as "no item filter" so the rule doesn't
    # accidentally match a literal item named ``$`` / ``-``.
    if item in ("", "-", "$"):
        item = ""
    if seller in ("", "-", "$"):
        seller = ""
    if agg in ("", "-", "$"):
        agg = ""
    # Distinguish "global index empty" from "filter matched nothing".
    # The seller / item caller (e.g. 摊位@某人 / 摊位鱼竿) wants the
    # friendly "no listings for X" message even when no one has
    # listed anything at all — the bare empty return would otherwise
    # render an awkward blank between the rule's title and footer.
    if not ids:
        if agg == "1":
            return "全服摊位空空如也~"
        if seller:
            return f"{seller} 还没来摆过摊呢"
        if item:
            return f"摊位上还没人挂 {item} 哦"
        return ""

    now = int(time.time())
    # Per-item accumulator for the agg branch.
    by_item_counts: dict[str, int] = {name: 0 for name in TRADABLE_ITEMS}
    by_item_units: dict[str, int] = {name: 0 for name in TRADABLE_ITEMS}
    total_active = 0
    total_units = 0
    # Per-listing row builder for the item / seller branches.
    lines: list[str] = []
    refund_jobs: list[tuple[str, dict[str, Any]]] = []

    for lid in ids:
        blob = await sq.read(_SCOPE_LISTING, "挂单", lid, "")
        if not blob:
            continue
        listing = _read_json(blob)
        if not isinstance(listing, dict):
            continue
        status = listing.get("status")
        expire_at = _parse_int(str(listing.get("expire_at", 0)))
        if status == "active" and expire_at and now > expire_at:
            refund_jobs.append((lid, listing))
            continue
        if status != "active":
            continue

        # Filter: item and seller are mutually exclusive at the
        # caller layer (the DSL only sets one of the two). agg
        # bypasses both — it wants every active listing.
        listing_item = str(listing.get("item", ""))
        listing_seller = str(listing.get("seller", ""))
        left = _parse_int(str(listing.get("left", 0)))
        total = _parse_int(str(listing.get("total", 0)))
        price = _parse_int(str(listing.get("price_each", 0)))

        if agg == "1":
            total_active += 1
            total_units += left
            if listing_item in by_item_counts:
                by_item_counts[listing_item] += 1
                by_item_units[listing_item] += left
        else:
            if item and listing_item != item:
                continue
            if seller and listing_seller != seller:
                continue
            lines.append(f"{lid} {listing_item} {left}/{total} {price} {listing_seller}")

    if agg == "1":
        # Agg builds its own output buffer and falls through to the
        # shared refund branch — early-returning here would skip
        # lazy expiration and leak the expired listing.
        if total_active == 0:
            out: list[str] = ["全服摊位空空如也~"]
        else:
            out = [f"全服在售 {total_active} 条 · 共 {total_units} 件"]
            for name in TRADABLE_ITEMS:
                count = by_item_counts[name]
                units = by_item_units[name]
                if count == 0:
                    continue
                out.append(f"  {name} {count} 条 · {units} 件")

    if refund_jobs:
        async with sq.transaction() as tx:
            for lid, _snapshot in refund_jobs:
                # Re-read inside the transaction so a concurrent
                # ``marketplace_buy`` that already expired this listing
                # (and credited the seller) doesn't get its refund
                # doubled. The snapshot we collected is just a hint;
                # the transaction is the source of truth.
                blob = await tx.read(_SCOPE_LISTING, "挂单", lid, "")
                if not blob:
                    continue
                fresh = _read_json(blob)
                if not isinstance(fresh, dict):
                    continue
                if fresh.get("status") != "active":
                    # Already processed by a buyer or another sweeper.
                    # ``_drop_from_global_index_locked`` is idempotent
                    # so re-running it is safe.
                    await _drop_from_global_index_locked(tx, lid)
                    continue
                seller_id = str(fresh.get("seller", ""))
                item_name = str(fresh.get("item", ""))
                left_q = _parse_int(str(fresh.get("left", 0)))
                if seller_id and item_name and left_q > 0:
                    await _credit_inventory(tx, seller_id, item_name, left_q)
                fresh["status"] = "expired"
                fresh["left"] = 0
                await tx.write(_SCOPE_LISTING, "挂单", lid, _dump_json(fresh))
                await _drop_from_global_index_locked(tx, lid)

    if agg == "1":
        return "\n".join(out)

    if not lines:
        if seller:
            return "TA 还没在摊位上卖东西哦"
        if item:
            return f"摊位上还没人挂 {item} 哦"
        return ""

    # 20-row cap only applies to the unfiltered (item-only) branch.
    # Seller branch shows the full stall.
    if not seller:
        lines = lines[:20]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# $查挂单 list_id$
# ---------------------------------------------------------------------------


@tool(
    name="marketplace_inspect",
    dsl_name="查挂单",
    description=(
        "Read the full JSON record for a single marketplace listing. "
        "Returns the raw JSON, or empty string if not found."
    ),
    schema={"list_id": "string"},
    safe=True,
    llm_visible=False,
)
async def marketplace_inspect(ctx: ToolCtx, list_id: str = "") -> str:
    """Return the raw JSON listing record (or empty)."""
    if not list_id:
        return ""
    return await ctx.kv.read(_SCOPE_LISTING, "挂单", list_id, "") or ""


# ---------------------------------------------------------------------------
# $市场图卡 mode [seller]$
# ---------------------------------------------------------------------------
#
# Renders a small (150-px wide) PNG card for the marketplace menu
# and the per-player stall. The DSL caller captures the returned
# ``base64://`` URL into a variable and emits it via
# ``±img=%var%±``. Pillow renders directly to bytes — no on-disk
# temp file, no shared-filesystem dependency between this process
# and the LLBot container.

# Card dimensions. 150 px wide matches the QQ mobile chat preview
# (one-third of the standard 450-px full-width image), keeping the
# menu glanceable. Height is computed from row count so the card
# shrinks or grows to fit the active item list.
_CARD_WIDTH = 150
_CARD_PADDING = 8  # inner margin around the title + per-item rows
_CARD_LINE_HEIGHT = 18
_CARD_TITLE_HEIGHT = 22
_CARD_MIN_HEIGHT = 60

# Per-item accent colors. Picked to be visually distinct at 150 px
# without screaming — pastels, not neons. The order in this dict
# is the on-card order; the agg branch uses the same order so the
# menu and the per-item branch read the same way.
_CARD_ITEM_COLORS: dict[str, tuple[int, int, int]] = {
    "鱼竿": (255, 217, 102),  # warm yellow — 钓竿
    "鱼饵": (156, 204, 101),  # soft green  — 鱼饵
    "蛋壳": (255, 245, 230),  # off-white   — 蛋壳
}

# Card chrome. Body text in near-black so the color blocks pop.
_CARD_TEXT_COLOR = (38, 38, 38)
_CARD_TITLE_COLOR = (78, 52, 46)
_CARD_BG_COLOR = (252, 248, 240)
_CARD_BORDER_COLOR = (210, 195, 170)

# Default bitmap font is intentionally tiny (Pillow's load_default
# is ~10 px). Without a TTF this looks OK at 150 px wide; with a
# TTF in ctx.extras["market_card_font"] the same dimensions render
# Chinese cleanly. The text wrapping is font-agnostic.
_CARD_FONT_SIZE = 12


def _card_load_font(ctx: ToolCtx) -> Any:
    """Resolve the card's TTF path, scanning three sources in order.

    Returns ``Any`` because ``ImageFont.truetype`` returns
    ``FreeTypeFont`` on Pillow ≥10 while ``load_default`` returns
    ``ImageFont`` — they're compatible at the call sites (both
    expose ``getbbox``) but a strict union annotation chokes
    mypy.

    Discovery order:

    1. ``ctx.extras["market_card_font"]`` — explicit override, set
       by the linling bootstrap if a deployment wants to pin a
       specific font (e.g. on hosts where the bundled font isn't
       desirable).
    2. ``data/fonts/*.otf|ttf`` — bundled font shipped with the
       repo. The marketplace card relies on this so operators
       don't need to install ``fonts-noto-cjk`` on the host.
    3. Pillow's ``load_default()`` — last-resort 10-px bitmap that
       renders Chinese as tofu. Logs a warning so an operator
       notices the regression.
    """
    explicit = ctx.extras.get("market_card_font")
    if isinstance(explicit, str):
        try:
            return ImageFont.truetype(explicit, size=_CARD_FONT_SIZE)
        except OSError:
            pass

    bundled = _find_bundled_font()
    if bundled is not None:
        try:
            return ImageFont.truetype(bundled, size=_CARD_FONT_SIZE)
        except OSError:
            pass

    logger.warning(
        "market_card.no_bundled_font",
        hint=(
            "drop a TTF/OTF into data/fonts/ (e.g. NotoSansSC-Regular.otf) "
            "or set ctx.extras['market_card_font']; falling back to "
            "Pillow's default bitmap which renders Chinese as tofu"
        ),
    )
    return ImageFont.load_default()


def _find_bundled_font() -> str | None:
    """Return a usable CJK-capable TTF/OTF/TTC path, or None.

    Search order:

    1. **Bundled** at ``data/fonts/`` (next to the repo root, both
       cwd-relative and ``__file__``-relative lookups). This is the
       preferred path: operators can drop a font in once and every
       deployment (containers, CI, dev boxes) renders correctly
       without depending on host packages.
    2. **System** fonts at the well-known Noto / WenQuanYi / Arphic
       locations. The bundled font is the canonical source of truth
       — this branch is a safety net for development environments
       that ship a CJK font via the OS package manager (e.g.
       ``fonts-noto-cjk`` on Debian/Ubuntu, the standard package on
       NapCat / LLBot containers).
    3. **Not found** — return None so the caller can fall back to
       Pillow's default bitmap (which renders CJK as tofu) and
       log a single warning.

    The function is called once per card render; the search is a
    few ``Path.is_file()`` checks — not worth caching.
    """
    bundled_roots = [
        Path("data/fonts"),
        Path(__file__).resolve().parents[3] / "data" / "fonts",
    ]
    bundled_preferred = (
        "NotoSansSC-Regular.otf", "NotoSansSC-Regular.ttf",
        "NotoSansCJKsc-Regular.otf", "SourceHanSansSC-Regular.otf",
    )
    for root in bundled_roots:
        if not root.is_dir():
            continue
        for name in bundled_preferred:
            p = root / name
            if p.is_file():
                return str(p)
        for p in sorted(root.iterdir()):
            if p.suffix.lower() in (".otf", ".ttf", ".ttc"):
                return str(p)

    system_candidates = [
        # Debian / Ubuntu: fonts-noto-cjk package.
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
        # Older Debian: fonts-noto-cjk-extra / fonts-noto-cjk-pure.
        "/usr/share/fonts/opentype/noto/NotoSansCJK.ttc",
        # Arch / Manjaro.
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        # Fedora / RHEL.
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.otf",
        # WenQuanYi fallback (a few lightweight distros still ship this).
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        # macOS.
        "/System/Library/Fonts/PingFang.ttc",
        "/Library/Fonts/Songti.ttc",
    ]
    for p in system_candidates:
        if Path(p).is_file():
            return p
    return None


def _card_wrap(text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    """Greedy wrap *text* into lines no wider than *max_width* pixels."""
    out: list[str] = []
    cur = ""
    for ch in text:
        candidate = cur + ch
        bbox = font.getbbox(candidate)
        if bbox[2] - bbox[0] <= max_width:
            cur = candidate
        else:
            if cur:
                out.append(cur)
            cur = ch
    if cur:
        out.append(cur)
    return out


def _render_card(
    title: str,
    rows: list[tuple[str, str, tuple[int, int, int]]],
    font: ImageFont.ImageFont,
) -> bytes:
    """Compose the PNG card to bytes.

    *rows* is a list of ``(label, value, bg_color)`` tuples drawn
    top-to-bottom inside their own colored block. The title sits
    above in a slightly darker band. Width is fixed at
    :data:`_CARD_WIDTH`; height grows to fit the rows.
    """
    inner_w = _CARD_WIDTH - 2 * _CARD_PADDING
    height = (
        _CARD_PADDING
        + _CARD_TITLE_HEIGHT
        + _CARD_PADDING
        + len(rows) * (_CARD_LINE_HEIGHT + 4)
        + _CARD_PADDING
    )
    height = max(height, _CARD_MIN_HEIGHT)

    img = Image.new("RGB", (_CARD_WIDTH, height), _CARD_BG_COLOR)
    draw = ImageDraw.Draw(img)
    # 1-px border so the card reads as a "card" in chat previews
    # that downsample aggressively.
    draw.rectangle(
        [(0, 0), (_CARD_WIDTH - 1, height - 1)],
        outline=_CARD_BORDER_COLOR,
    )

    # Title band.
    y = _CARD_PADDING
    title_lines = _card_wrap(title, font, inner_w)
    for line in title_lines:
        draw.text((_CARD_PADDING, y), line, fill=_CARD_TITLE_COLOR, font=font)
        y += _CARD_LINE_HEIGHT
    y += 2  # spacer

    # Per-item rows, each on its own colored block.
    for label, value, color in rows:
        # The row's "block" is just a filled rounded-rect — Pillow
        # has no rounded-rect on older versions, so we use plain
        # rect (the 1-px border already separates rows visually).
        block_top = y
        block_bottom = y + _CARD_LINE_HEIGHT + 2
        draw.rectangle(
            [
                (_CARD_PADDING, block_top),
                (_CARD_WIDTH - _CARD_PADDING, block_bottom),
            ],
            fill=color,
        )
        # Label left, value right.
        draw.text(
            (_CARD_PADDING + 4, y + 2),
            label,
            fill=_CARD_TEXT_COLOR,
            font=font,
        )
        # Right-aligned value.
        v_bbox = font.getbbox(value)
        v_w = v_bbox[2] - v_bbox[0]
        draw.text(
            (_CARD_WIDTH - _CARD_PADDING - 4 - v_w, y + 2),
            value,
            fill=_CARD_TEXT_COLOR,
            font=font,
        )
        y = block_bottom + 2

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _empty_card_b64(font: ImageFont.ImageFont) -> str:
    """A tiny 「空」card returned when there's nothing to render."""
    b = _render_card(
        title="苏苏的摊位",
        rows=[("空", "", (240, 235, 220))],
        font=font,
    )
    return "base64://" + base64.b64encode(b).decode("ascii")


@tool(
    name="market_card",
    dsl_name="市场图卡",
    description=(
        "Render a 150-px wide marketplace card (PNG, base64-inlined). "
        "$市场图卡 [seller=QQ]$ — omit seller for the global agg "
        "menu; pass seller=QQ for one player's stall. Returns a "
        "base64:// URL that the DSL emits via ±img=%var%±."
    ),
    schema={"seller": "string?"},
    safe=True,
    llm_visible=False,
)
async def market_card(ctx: ToolCtx, seller: str = "") -> str:
    """Render a marketplace summary card and return its base64 URL.

    Two modes:
    * **agg** (no ``seller``) — title "苏苏的摊位"; one row per
      tradable category that has at least one active listing, with
      the per-item pastel color and a "N 条 / M 件" label.
    * **seller** (``seller=QQ``) — title "QQ 的摊位"; one row per
      active listing owned by that QQ, showing the listing's
      remaining ``left/total`` and the unit price.

    The card is always returned (even when empty — the menu rule
    wants a "空" placeholder rather than a broken image), so the
    DSL layer can unconditionally emit it.
    """
    sq = _require_sqlite_kv(ctx.kv)
    raw = await sq.read(_SCOPE_GLOBAL_INDEX, _FILE_ACTIVE, "all", "[]")
    ids = list(_read_json(raw) or [])
    font = _card_load_font(ctx)
    now = int(time.time())

    by_item: dict[str, int] = {name: 0 for name in TRADABLE_ITEMS}
    by_units: dict[str, int] = {name: 0 for name in TRADABLE_ITEMS}
    seller_rows: list[tuple[str, int, int, int]] = []  # (item, left, total, price)
    refund_jobs: list[tuple[str, dict[str, Any]]] = []

    for lid in ids:
        blob = await sq.read(_SCOPE_LISTING, "挂单", lid, "")
        if not blob:
            continue
        listing = _read_json(blob)
        if not isinstance(listing, dict):
            continue
        status = listing.get("status")
        expire_at = _parse_int(str(listing.get("expire_at", 0)))
        if status == "active" and expire_at and now > expire_at:
            refund_jobs.append((lid, listing))
            continue
        if status != "active":
            continue

        listing_item = str(listing.get("item", ""))
        listing_seller = str(listing.get("seller", ""))
        left = _parse_int(str(listing.get("left", 0)))
        total = _parse_int(str(listing.get("total", 0)))
        price = _parse_int(str(listing.get("price_each", 0)))

        if seller:
            if listing_seller != seller:
                continue
            if listing_item:
                seller_rows.append((listing_item, left, total, price))
        elif listing_item in by_item:
            by_item[listing_item] += 1
            by_units[listing_item] += left

    # Lazy expiry cleanup. The agg path is the user's primary
    # entry point so we want this to be the canonical sweeper.
    if refund_jobs:
        async with sq.transaction() as tx:
            for lid, _snapshot in refund_jobs:
                # Re-read inside the transaction so a concurrent
                # ``marketplace_buy`` that already swept this listing
                # doesn't trigger a double refund. See the matching
                # block in :func:`marketplace_list_active` for the
                # full rationale.
                blob = await tx.read(_SCOPE_LISTING, "挂单", lid, "")
                if not blob:
                    continue
                fresh = _read_json(blob)
                if not isinstance(fresh, dict):
                    continue
                if fresh.get("status") != "active":
                    await _drop_from_global_index_locked(tx, lid)
                    continue
                seller_id = str(fresh.get("seller", ""))
                item_name = str(fresh.get("item", ""))
                left_q = _parse_int(str(fresh.get("left", 0)))
                if seller_id and item_name and left_q > 0:
                    await _credit_inventory(tx, seller_id, item_name, left_q)
                fresh["status"] = "expired"
                fresh["left"] = 0
                await tx.write(_SCOPE_LISTING, "挂单", lid, _dump_json(fresh))
                await _drop_from_global_index_locked(tx, lid)

    if seller:
        if not seller_rows:
            return _empty_card_b64(font)
        rows: list[tuple[str, str, tuple[int, int, int]]] = []
        for item, left, total, price in seller_rows:
            color = _CARD_ITEM_COLORS.get(item, _CARD_BG_COLOR)
            rows.append((item, f"{left}/{total}  {price}灵玉", color))
        png = _render_card(title=f"{seller} 的摊位", rows=rows, font=font)
    else:
        total_active = sum(1 for c in by_item.values() if c)
        if total_active == 0:
            return _empty_card_b64(font)
        rows = []
        for name in TRADABLE_ITEMS:
            count = by_item[name]
            if count == 0:
                continue
            units = by_units[name]
            rows.append(
                (name, f"{count}条·{units}件", _CARD_ITEM_COLORS[name])
            )
        png = _render_card(title="苏苏的摊位", rows=rows, font=font)

    return "base64://" + base64.b64encode(png).decode("ascii")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _drop_from_global_index_locked(tx: Any, list_id: str) -> None:
    """Remove *list_id* from the global active index. Idempotent."""
    raw = await tx.read(_SCOPE_GLOBAL_INDEX, _FILE_ACTIVE, "all", "[]")
    ids: list[str] = list(_read_json(raw) or [])
    if list_id in ids:
        ids.remove(list_id)
        await tx.write(
            _SCOPE_GLOBAL_INDEX,
            _FILE_ACTIVE,
            "all",
            _dump_json(ids),
        )


async def _expire_listing_locked(
    tx: Any, list_id: str, listing: dict[str, Any], seller: str, item: str
) -> None:
    """Mark *list_id* expired and refund remaining inventory."""
    left = _parse_int(str(listing.get("left", 0)))
    if seller and item and left > 0:
        await _credit_inventory(tx, seller, item, left)
    listing["status"] = "expired"
    listing["left"] = 0
    await tx.write(_SCOPE_LISTING, "挂单", list_id, _dump_json(listing))
    await _drop_from_global_index_locked(tx, list_id)


def _parse_json_field(blob: str, field: str) -> str | None:
    """Tiny helper for status checks inside the active-count loop."""
    parsed = _read_json(blob)
    if isinstance(parsed, dict):
        value = parsed.get(field)
        return str(value) if value is not None else None
    return None


# ---------------------------------------------------------------------------
# $剥离前缀 result prefix
# ---------------------------------------------------------------------------


@tool(
    name="strip_prefix",
    dsl_name="剥离前缀",
    description=(
        "Strip a known prefix from a tool result. "
        "$剥离前缀 result prefix$ — returns *result* with *prefix* removed "
        "(if present) and a leading colon/whitespace trimmed, so the "
        "DSL can show the marketplace's ``ok:...`` or ``error:...`` "
        "strings without leaking the protocol prefix to the end user. "
        "Empty inputs return empty; missing "
        "prefix returns *result* unchanged."
    ),
    schema={"result": "string", "prefix": "string"},
    safe=True,
    llm_visible=False,
)
async def strip_prefix(
    ctx: ToolCtx, result: str = "", prefix: str = ""
) -> str:
    """Return *result* with *prefix* (and a following ':' or space) removed."""
    if not result or not prefix:
        return result
    if not result.startswith(prefix):
        return result
    rest = result[len(prefix):]
    if rest.startswith(":"):
        rest = rest[1:]
    return rest.lstrip()


# ---------------------------------------------------------------------------
# $格式化挂单 list_id
# ---------------------------------------------------------------------------


@tool(
    name="format_listing",
    dsl_name="格式化挂单",
    description=(
        "Format a marketplace listing for end-user display. "
        "$格式化挂单 list_id$ — returns a multi-line Chinese summary "
        "(卖家 / 物品 / 库存 / 单价 / 状态 / 过期) or an empty "
        "string if the listing doesn't exist. Used by 查挂单 / 查单 "
        "so the user sees readable text instead of the raw JSON blob "
        "that ``$查挂单$`` returns."
    ),
    schema={"list_id": "string"},
    safe=True,
    llm_visible=False,
)
async def format_listing(ctx: ToolCtx, list_id: str = "") -> str:
    """Render the listing for *list_id* as a user-friendly summary.

    Fields are taken from the canonical listing JSON; missing fields
    are silently omitted so older rows (which lack ``created_at`` /
    ``expire_at``) still render rather than crashing.
    """
    if not list_id:
        return ""
    blob = await ctx.kv.read(_SCOPE_LISTING, "挂单", list_id, "")
    if not blob:
        return ""
    data = _read_json(blob)
    if not isinstance(data, dict):
        return ""
    item = str(data.get("item", "?"))
    seller = str(data.get("seller", "?"))
    left = _parse_int(str(data.get("left", 0)))
    total = _parse_int(str(data.get("total", 0)))
    price = _parse_int(str(data.get("price_each", 0)))
    status = str(data.get("status", "?"))
    expire_at = _parse_int(str(data.get("expire_at", 0)))
    lines = [
        f"物品：{item}",
        f"卖家：{seller}",
        f"库存：{left}/{total}",
        f"单价：{price} 灵玉/个",
        f"状态：{status}",
    ]
    if expire_at:
        lines.append(f"过期：{_fmt_expire(expire_at)}")
    return "\n".join(lines)


def _fmt_expire(epoch_s: int) -> str:
    """Render a UTC epoch second as a ``MM-dd HH:mm`` wall-clock string."""
    import datetime as _dt

    dt = _dt.datetime.fromtimestamp(epoch_s)
    return dt.strftime("%m-%d %H:%M")
