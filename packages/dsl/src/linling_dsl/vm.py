"""DSL v0 Interpreter / Virtual Machine.

Walks the AST produced by the parser and executes handlers, producing
output segments (text, image, etc.) and interacting with the tool registry
and KV store.
"""

from __future__ import annotations

import json
import operator as _op
import random as _r
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from linling_core.events import Event
from linling_core.segments import (
    AtSegment,
    CardSegment,
    FaceSegment,
    ImageSegment,
    ReplySegment,
    Segment,
    TextSegment,
    VoiceSegment,
    XmlSegment,
)
from linling_core.storage.kv import KVStore
from linling_core.tools import ToolCtx, ToolRegistry

from linling_dsl.ast_nodes import (
    ArithExpr,
    Assign,
    Expr,
    FuncCall,
    FuncCallExpr,
    Handler,
    IfStmt,
    JsonAccess,
    Jump,
    Label,
    Literal,
    OutputFlashImage,
    OutputImage,
    OutputReply,
    OutputText,
    OutputVoice,
    ReturnStmt,
    Stmt,
    VarRef,
)
from linling_dsl.parser import _parse_func_args

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class VMError(Exception):
    """Base class for VM execution errors."""


class SandboxError(VMError):
    """Raised when sandbox limits are exceeded."""


class UndefinedVarError(VMError):
    """Raised when a variable is not found."""


class _JumpSignal(VMError):
    """Internal: bubble a ``$jump :label$`` up through nested IfStmt bodies.

    QRDic / QRSpeed semantics: ``$jump :label$`` jumps to the *nearest
    enclosing label with that name*, regardless of which ``如果:`` body
    the jump statement was nested in. The classic example is
    ``扭蛋口令``::

        :重随机
        S:$随机数 1-3$
        ...
        如果:%S%==%B%|...
        $jump :重随机$
        ...
        如果尾

    The jump lives inside the ``如果`` body but must reach the label
    declared at the enclosing handler scope. We model the jump as an
    exception so each ``_exec_body`` frame can either resolve it (if
    it owns the label) or re-raise to the parent.
    """

    def __init__(self, target: str) -> None:
        self.target = target
        super().__init__(target)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class VMResult:
    """Result of executing a handler."""

    segments: list[Segment] = field(default_factory=list)
    returned: bool = False


# ---------------------------------------------------------------------------
# Safe arithmetic evaluator
# ---------------------------------------------------------------------------

_ARITH_TOKEN_RE = re.compile(r"(\d+\.?\d*|[+\-*/%()])")


def _safe_eval_arith(expr: str) -> str:
    """Evaluate a simple arithmetic expression safely.

    Supports: integers, floats, +, -, *, /, %, parentheses.

    If parsing fails, returns the original expression re-wrapped in
    brackets. That makes ``[键]`` / ``[序号]`` / ``[值]`` survive
    unchanged when they appear as rank-format tokens inside a
    ``$排行榜 ...$`` call (the ``[...]`` shape is ambiguous: in most
    contexts it's an arith block, but inside rank format strings the
    brackets are QRDic's template delimiters). Returning the original
    text is also more informative for malformed arithmetic.
    """
    expr = expr.strip()
    if not expr:
        return "0"
    try:
        result = _ArithParser(expr).parse()
        # Format: integer if no decimal part, else float
        if result == int(result):
            return str(int(result))
        return str(result)
    except Exception:
        return f"[{expr}]"


class _ArithParser:
    """Simple recursive descent parser for arithmetic expressions."""

    def __init__(self, text: str) -> None:
        self._text = text
        self._pos = 0

    def parse(self) -> float:
        result = self._expr()
        self._skip_spaces()
        if self._pos < len(self._text):
            raise ValueError("unexpected character")
        return result

    def _skip_spaces(self) -> None:
        while self._pos < len(self._text) and self._text[self._pos] == " ":
            self._pos += 1

    def _peek(self) -> str | None:
        self._skip_spaces()
        if self._pos >= len(self._text):
            return None
        return self._text[self._pos]

    def _consume(self, ch: str) -> None:
        self._skip_spaces()
        if self._pos < len(self._text) and self._text[self._pos] == ch:
            self._pos += 1
        else:
            raise ValueError(f"expected '{ch}'")

    def _expr(self) -> float:
        return self._add_sub()

    def _add_sub(self) -> float:
        left = self._mul_div()
        while True:
            self._skip_spaces()
            if self._pos >= len(self._text):
                break
            op = self._text[self._pos]
            if op in ("+", "-"):
                self._pos += 1
                right = self._mul_div()
                if op == "+":
                    left += right
                else:
                    left -= right
            else:
                break
        return left

    def _mul_div(self) -> float:
        left = self._unary()
        while True:
            self._skip_spaces()
            if self._pos >= len(self._text):
                break
            op = self._text[self._pos]
            if op in ("*", "/", "%"):
                self._pos += 1
                right = self._unary()
                if op == "*":
                    left *= right
                elif op == "/":
                    if right == 0:
                        raise ValueError("division by zero")
                    left /= right
                else:  # %
                    if right == 0:
                        raise ValueError("modulo by zero")
                    left = float(int(left) % int(right))
            else:
                break
        return left

    def _unary(self) -> float:
        self._skip_spaces()
        if self._pos < len(self._text) and self._text[self._pos] == "-":
            self._pos += 1
            return -self._unary()
        if self._pos < len(self._text) and self._text[self._pos] == "+":
            self._pos += 1
            return self._unary()
        return self._atom()

    def _atom(self) -> float:
        self._skip_spaces()
        if self._pos >= len(self._text):
            raise ValueError("unexpected end")
        ch = self._text[self._pos]
        if ch == "(":
            self._pos += 1
            val = self._expr()
            self._consume(")")
            return val
        # Number
        start = self._pos
        if ch in "0123456789.":
            while self._pos < len(self._text) and self._text[self._pos] in "0123456789.":
                self._pos += 1
            return float(self._text[start : self._pos])
        raise ValueError(f"unexpected char: {ch!r}")


# ---------------------------------------------------------------------------
# Comparison helpers
# ---------------------------------------------------------------------------

_NUM_OPS: dict[str, object] = {
    "==": _op.eq,
    "!=": _op.ne,
    ">": _op.gt,
    "<": _op.lt,
    ">=": _op.ge,
    "<=": _op.le,
}


def _numeric_cmp(left: float, right: float, op: str) -> bool:
    fn = _NUM_OPS.get(op)
    if fn is None:
        return False
    return bool(fn(left, right))  # type: ignore[operator]


def _string_cmp(left: str, right: str, op: str) -> bool:
    fn = _NUM_OPS.get(op)
    if fn is None:
        return False
    return bool(fn(left, right))  # type: ignore[operator]


# ---------------------------------------------------------------------------
# VM
# ---------------------------------------------------------------------------

# Pattern to match %var% references in text
_VAR_PATTERN = re.compile(r"%([^%]+)%")
# Pattern to match $func args$ calls inside conditional / arith bodies.
_FUNC_PATTERN = re.compile(r"\$([^$]+)\$")

# QRDic ``$JSON 添加/删除 var ...$`` is expected to mutate ``var`` in
# place — the Java VM held the array by reference, so the next read
# of ``var`` saw the updated payload. We mirror this by writing the
# tool's return value back to scope when the sub-command is one of
# these mutating ones. Read-only sub-commands (长度 / 获取 / 包含 /
# 键) leave scope alone.
_JSON_MUTATING_SUBCOMMANDS = frozenset({"添加", "add", "append", "删除", "remove", "delete"})

# DSL tools whose convention is "the n-th argument is a *value*
# obtained by late-binding the bare identifier through scope". This
# mirrors the QRDic Java VM's behaviour where these tools received
# *the value* of a variable rather than its name.
#
# Most tools want their args treated as literal keys/paths/numbers
# instead. The 漂流瓶 module exposes the issue: its ``$写 啊/瓶子 R
# %R%$`` writes value ``%R%`` under literal key ``R`` — late-binding
# would catastrophically rewrite the *key* to whatever ``R`` happens
# to hold (a JSON array text), generating phantom KV rows.
#
# Tools listed here are the historically var-name-as-arg ones from
# the QRDic standard library. Anything not on this list keeps its
# arguments as literals.
_LATE_BINDING_TOOL_DSL_NAMES = frozenset({
    "JSON",  # $JSON 长度|获取|添加|删除|包含|键 var ...$
    "替换",  # $替换 SEP TEXT PATTERN$
    "正则",  # $正则 SEP TEXT PATTERN$
    "取中间",  # $取中间 SEP BLOB$
})

# DSL tools whose return value should be *emitted* as user-facing
# text when invoked as a standalone ``$tool args$`` line. Most tools
# either return ``""`` (pure side effects: ``$写$`` / ``$发送$`` /
# ``$JSON 添加$`` / ``$全局变量$`` / ``$调用$``) or return a value
# the rule was meant to capture into a variable (``a:$取中间 ...$``,
# ``r:$排行榜 ...$``); emitting those would leak internals.
#
# Tools listed here are the QRDic / QRSpeed convention's "renderers":
# ``$URLDecoder %x%$`` on its own line means "emit the decoded text".
# ``$输出为 %x%$`` is the explicit emit primitive. ``$访问 url$``
# fetches HTTP and renders the body.
_EMIT_OUTPUT_TOOL_DSL_NAMES = frozenset({
    "URLEncoder",
    "URLDecoder",
    "Base64Encoder",
    "Base64Decoder",
    "HexEncoder",
    "HexDecoder",
    "UnicodeDecoder",
    "MD5",
    "输出为",
    "访问",  # HTTP fetch — body is the user-facing text
    "时间",  # $时间 fmt$ when standalone — formatted timestamp
    "图文",  # rendered image path; rare standalone but matches QRSpeed
})


# Single-name context-variable resolvers. Looking up here is a single
# dict access on the hot path instead of a linear if-chain across ~10
# names. The closures capture only ``Event`` so they're cheap.
_CtxResolver = Callable[[Event], str]
_CTX_RESOLVERS: dict[str, _CtxResolver] = {
    "QQ": lambda e: e.sender.id,
    "用户": lambda e: e.sender.id,
    "群号": lambda e: e.scope.id,
    "群": lambda e: e.scope.id,
    "会话": lambda e: e.scope.id,
    "昵称": lambda e: e.sender.display_name or e.sender.id,
    "Robot": lambda e: e.bot_id,
    "自己": lambda e: e.bot_id,
    "参数-1": lambda e: e.text,
    # Wall-clock time-stamp in milliseconds. QRSpeed's ``%NDTime%`` is
    # the canonical "now" for cooldown / rate-limit logic in community
    # rule sets (see the ziyii01 random-image / 留言板 samples).
    "NDTime": lambda _e: str(int(time.time() * 1000)),
    # Bot startup time-stamp in milliseconds. Set once at module
    # import time — not strictly accurate (the bot might have been
    # constructed earlier) but matches the QRSpeed convention of
    # "milliseconds since the bot first ran" closely enough for
    # uptime-display rules. Tests can patch it for determinism.
    "RobotRunTime": lambda _e: str(_BOT_START_MS[0]),
    # QRDic adapter-injected blobs. Resolve from ``event.raw`` when
    # the platform populates it (OneBot does, CLI/WebUI don't),
    # falling back to empty string so rules that reference them on
    # adapter-less platforms degrade silently rather than crash with
    # an UndefinedVarError. Names match the raw OneBot v11 / QRDic
    # field names.
    "Code": lambda e: _raw_str(e, "operator_id"),
    "Msgbar": lambda e: _raw_str(e, "message_id"),
    "Time": lambda e: _raw_str(e, "time"),
    "Type": lambda e: _raw_str(e, "sub_type"),
    "Value": lambda e: _raw_str(e, "value"),
    "Status": lambda e: _raw_str(e, "status"),
    "Reqid": lambda e: _raw_str(e, "request_id"),
    # OneBot group-event names — the ``[系统]`` trigger flow uses
    # these to format welcome / leave messages. Resolved from
    # ``event.raw`` so adapter-less callers get empty.
    "UinName": lambda e: _raw_str(e, "user_name"),
    "Inviteename": lambda e: _raw_str(e, "operator_name"),
    # Auth blobs — these never travel in a public payload, so they
    # always read empty in our world. Listed here so rules that
    # reference them get a clean miss instead of an error.
    "Json": lambda _e: "",
    "Skey": lambda _e: "",
}


# Bot start timestamp in ms. Captured at module import — the bot
# bootstrap can override via :func:`set_bot_start_time_ms` for an
# exact value; this default keeps unit tests deterministic without
# forcing every test to plumb the flag.
#
# Held inside a one-element list so the setter doesn't need ``global``
# (ruff PLW0603); :data:`_CTX_RESOLVERS` reaches into ``[0]`` and the
# setter overwrites ``[0]``.
_BOT_START_MS: list[int] = [int(time.time() * 1000)]


def set_bot_start_time_ms(ms: int) -> None:
    """Set the value returned by ``%RobotRunTime%`` interpolation.

    Bootstrapping code calls this with the actual ``time.time()*1000``
    captured when ``RunningBot`` was constructed, so rule files that
    compute uptime against the live bot rather than the import event
    get the right value. Tests can also call it to pin the value.
    """
    _BOT_START_MS[0] = ms


def _raw_str(event: Event, key: str) -> str:
    """Pull ``key`` out of ``event.raw`` and stringify it.

    Returns the empty string for missing keys so QRDic rules that
    optimistically interpolate adapter-side fields don't blow up
    on platforms that don't surface them.
    """
    value = event.raw.get(key)
    if value is None:
        return ""
    return str(value)


# ---------------------------------------------------------------------------
# Segment-indexed context vars
# ---------------------------------------------------------------------------
#
# QRSpeed-era rule files use ``%IMG0%`` / ``%FACE0%`` / ``%XML0%`` etc.
# to address segments by position within the inbound message. Each of
# those families resolves to either:
#
# * an N-th matching segment's salient field (``%IMGN%`` → url), or
# * a count-of-matching ("NUM" suffix; ``%IMGNUM%`` → integer count).
#
# We dispatch through a single declarative table so adding a new
# family doesn't sprawl into more if-chains. ``predicate`` lets us
# carve out subsets (e.g. flash-only images) without subclassing
# ``ImageSegment``.

_T_SegmentExtractor = Callable[[Segment], str]
_T_SegmentPredicate = Callable[[Segment], bool]


@dataclass(frozen=True)
class _SegmentVarFamily:
    """One ``%PREFIX*%`` family of segment-indexed context vars.

    ``prefix`` is the ``%PREFIX0%`` / ``%PREFIXNUM%`` shared name
    (e.g. ``"IMG"``). ``predicate`` filters which segments match.
    ``extract`` returns the field for ``%PREFIXN%``. The family also
    governs ``%PREFIXNUM%`` (count of matching segments).
    """

    prefix: str
    predicate: _T_SegmentPredicate
    extract: _T_SegmentExtractor


def _image_url(seg: Segment) -> str:
    """url → path → b64 fallback, used by IMG / FIMG."""
    assert isinstance(seg, ImageSegment)
    return seg.url or seg.path or seg.b64 or ""


def _is_image(seg: Segment) -> bool:
    return isinstance(seg, ImageSegment)


def _is_flash_image(seg: Segment) -> bool:
    return isinstance(seg, ImageSegment) and bool(seg.extras.get("flash"))


# Order matters: longer prefixes must come first so ``FACENEW0`` is
# matched as the FACENEW family, not the FACE family with name ``NEW0``.
# (Same shape as the original prefix-cascade in ``_get_event_context_var``.)
_SEGMENT_VAR_FAMILIES: tuple[_SegmentVarFamily, ...] = (
    _SegmentVarFamily("FIMG", _is_flash_image, _image_url),
    _SegmentVarFamily("IMG", _is_image, _image_url),
    _SegmentVarFamily(
        "FACEPRO",
        lambda s: isinstance(s, FaceSegment),
        lambda s: s.face_id,  # type: ignore[union-attr]
    ),
    _SegmentVarFamily(
        "FACENEW",
        lambda s: isinstance(s, FaceSegment),
        lambda s: s.face_id,  # type: ignore[union-attr]
    ),
    _SegmentVarFamily(
        "FACE",
        lambda s: isinstance(s, FaceSegment),
        lambda s: s.face_id,  # type: ignore[union-attr]
    ),
    _SegmentVarFamily(
        "XML",
        lambda s: isinstance(s, XmlSegment),
        lambda s: s.xml,  # type: ignore[union-attr]
    ),
    _SegmentVarFamily(
        "JSON",
        lambda s: isinstance(s, CardSegment),
        lambda s: s.payload,  # type: ignore[union-attr]
    ),
    _SegmentVarFamily(
        "AT",
        lambda s: isinstance(s, AtSegment),
        lambda s: s.user_id,  # type: ignore[union-attr]
    ),
)


def _resolve_segment_var(name: str, segments: list[Segment]) -> str | None:
    """Look up a segment-indexed context variable.

    Walks :data:`_SEGMENT_VAR_FAMILIES` in declaration order. For each
    family, accepts either ``<PREFIX>NUM`` (returns count) or
    ``<PREFIX><digits>`` (returns extracted field of nth match).
    Returns ``None`` when the name doesn't match any family — the
    caller falls back to the next resolution rung.
    """
    for fam in _SEGMENT_VAR_FAMILIES:
        if name == f"{fam.prefix}NUM":
            return str(sum(1 for s in segments if fam.predicate(s)))
        if name.startswith(fam.prefix):
            tail = name[len(fam.prefix) :]
            if tail.isdigit():
                matches = [s for s in segments if fam.predicate(s)]
                idx = int(tail)
                if 0 <= idx < len(matches):
                    return fam.extract(matches[idx])
                return ""
    return None


# QRDic-compat time-format suffixes (after the ``时间`` prefix). Built
# once at module load — building this dict per call would dominate the
# var-resolution profile in any handler that prints timestamps.
_TIME_FMT_MAP: dict[str, str] = {
    "": "%H:%M:%S",  # bare ``%时间%`` → HH:MM:SS, used in a couple of templates
    "HH": "%H",
    "mm": "%M",
    "dd": "%d",
    "HHmm": "%H%M",
    "MMdd": "%m%d",
    "MMddHH": "%m%d%H",
    "yyyyMM": "%Y%m",
    "yyyyMMdd": "%Y%m%d",
    "yyMMdd": "%y%m%d",
    "ddHH": "%d%H",
    "HH:mm": "%H:%M",
    "hh:mm": "%I:%M",
    "hh:mm:dd": "%I:%M:%S",
    "dd日HH:mm": "%d日%H:%M",
}


def _stringify(result: object) -> str:
    """Coerce a tool's return value into the DSL's string-only world."""
    if result is None:
        return ""
    return str(result)


# Mapping of QRDic-source escape sequences to the characters they
# stand for at *output* time. The DSL's source convention is to write
# ``\n`` literally in handler bodies (and inside KV values) and have
# the runtime turn it into a real newline when the text is rendered
# to the user. ``%0A`` / ``%0a`` is the same idea url-encoded — some
# rules use it to slip a newline past the whitespace-sensitive
# argument tokenizer. ``\\`` carries an authored backslash (rare).
#
# We decode at output time, *not* at KV-write time, so values stored
# in the DB keep their original representation; the next read +
# emit roundtrip will decode them. That matches QRDic's behaviour
# where "\n" is purely a presentation artifact.
_OUTPUT_ESCAPE_MAP: tuple[tuple[str, str], ...] = (
    ("\\\\", "\u0001"),  # placeholder so a literal backslash isn't double-decoded
    ("\\n", "\n"),
    ("\\r", "\r"),
    ("\\t", "\t"),
    ("\u0001", "\\"),
)


def _decode_qrdic_escapes(text: str) -> str:
    """Translate QRDic source escape sequences to their runtime forms.

    The order matters — we first stash any literal ``\\\\`` behind a
    placeholder so the subsequent ``\\n`` rewrite doesn't eat the
    second backslash of an authored ``\\\\n`` pair. The placeholder
    swap-back at the end produces the single literal backslash the
    author intended.

    We deliberately do *not* decode ``%0A`` here even though some
    rules use it as an inline newline — those occurrences are always
    inside *tool argument* slots (``$写文件$`` / ``$排行榜$ sep``)
    and are decoded by the relevant tool, not by the output stream.
    Touching them here would also make ``%0A`` collide with the
    ``%var%`` interpolation pass since ``%0A...%`` would be eaten as
    an attempted variable lookup.
    """
    if "\\" not in text:
        return text
    for src, dst in _OUTPUT_ESCAPE_MAP:
        if src in text:
            text = text.replace(src, dst)
    return text


class VM:
    """DSL v0 interpreter that walks the AST and produces output."""

    def __init__(
        self,
        tool_registry: ToolRegistry,
        kv: KVStore,
        *,
        bot_id: str = "linling",
        max_steps: int = 10000,
        max_output_segments: int = 20,
        timeout_ms: int = 2000,
        extras: dict[str, Any] | None = None,
    ) -> None:
        self._registry = tool_registry
        self._kv = kv
        self._bot_id = bot_id
        self._max_steps = max_steps
        self._max_output_segments = max_output_segments
        self._timeout_ms = timeout_ms
        # ``extras`` is forwarded to every ``ToolCtx``. The DSL
        # dispatcher uses this to plug in things like the bot's
        # :class:`Scheduler` (so ``$调用$`` can enqueue handler calls)
        # and the active adapter (so ``$发送$`` can route an outbound
        # message). Stateless tools that never touch ``ctx.extras``
        # are unaffected.
        self._extras: dict[str, Any] = extras or {}

    async def execute_handler(
        self,
        handler: Handler,
        event: Event,
        captures: list[str] | None = None,
    ) -> VMResult:
        """Execute a handler against an event, returning the result."""
        ctx = _ExecContext(
            vm=self,
            handler=handler,
            event=event,
            captures=captures or [],
        )
        await ctx.run()
        return VMResult(segments=ctx.segments, returned=ctx.returned)


# ---------------------------------------------------------------------------
# Execution context (internal)
# ---------------------------------------------------------------------------


class _ExecContext:
    """Mutable state for a single handler execution."""

    def __init__(
        self,
        vm: VM,
        handler: Handler,
        event: Event,
        captures: list[str],
    ) -> None:
        self._vm = vm
        self._handler = handler
        self._event = event
        self._captures = captures
        self._scope: dict[str, str] = {}
        self.segments: list[Segment] = []
        self.returned: bool = False
        self._steps: int = 0
        self._start_time: float = time.monotonic()

    # --- public ---

    async def run(self) -> None:
        try:
            await self._exec_body(self._handler.body, 0)
        except _JumpSignal as jmp:
            # Jump target not found anywhere up to the handler root —
            # match QRDic's lenient behaviour and silently stop. Real
            # rule files occasionally typo a label; the original
            # interpreter would just halt the handler on a bad jump.
            structlog_logger = None
            try:
                import structlog  # noqa: PLC0415

                structlog_logger = structlog.get_logger(__name__)
            except ImportError:
                pass
            if structlog_logger is not None:
                structlog_logger.warning(
                    "vm.unresolved_jump",
                    handler=self._handler.trigger,
                    target=jmp.target,
                )

    # --- sandbox checks ---

    def _tick(self) -> None:
        self._steps += 1
        if self._steps > self._vm._max_steps:
            raise SandboxError(f"max_steps exceeded ({self._vm._max_steps})")
        if self._steps % 100 == 0:
            elapsed_ms = (time.monotonic() - self._start_time) * 1000
            if elapsed_ms > self._vm._timeout_ms:
                raise SandboxError(f"timeout exceeded ({self._vm._timeout_ms}ms)")

    def _check_output_limit(self) -> None:
        if len(self.segments) > self._vm._max_output_segments:
            raise SandboxError(f"max_output_segments exceeded ({self._vm._max_output_segments})")

    def _inline_emit_segments(self, segments: list[Segment]) -> None:
        """Append ``segments`` to this handler's output buffer.

        Wired into the per-call :class:`ToolCtx` as
        ``extras["_inline_emit"]`` *only* when the VM was constructed
        with ``inline_zero_delay_calls=True`` in its extras. That flag
        is set by the WebUI's chat dispatcher and unset for QQ-side
        deployments — keeping the legacy fire-and-forget semantics on
        the IM transports while making the WebUI's single-bubble
        surface hear ``[内部]我在`` etc. on the same turn.

        The only tool wired to this path today is
        :func:`linling_tools_stdlib.scheduler_ops.schedule_handler`,
        which uses it to run ``$调用 0 handler$`` synchronously and
        promote the inner handler's segments. ``ms > 0`` keeps the
        scheduler path even when the hook is published.

        Each appended segment respects the same
        ``max_output_segments`` budget as ``$输出$`` / ``OutputText``
        so a runaway inline call can't blow the cap.
        """
        for seg in segments:
            self.segments.append(seg)
            self._check_output_limit()

    def _build_tool_extras(self) -> dict[str, Any]:
        """Build the per-call ``extras`` dict for a :class:`ToolCtx`.

        Always shallow-copies ``self._vm._extras`` so a tool that
        accidentally mutates its ``ctx.extras`` can't poison the
        shared VM-wide dict. The optional ``_inline_emit`` hook is
        published when *either* the VM-wide
        ``inline_zero_delay_calls`` flag is set *or* the inbound
        event came from a single-bubble transport (currently
        ``platform == "webui"``). QQ-side OneBot dispatches keep the
        scheduler-driven fan-out semantics — see
        :meth:`_inline_emit_segments`.
        """
        extras = dict(self._vm._extras)
        if extras.get("inline_zero_delay_calls") or self._wants_inline_calls():
            extras["_inline_emit"] = self._inline_emit_segments
        return extras

    def _wants_inline_calls(self) -> bool:
        """Decide per-event whether ``$调用 0$`` should run inline.

        Centralised so the policy stays in one place. Today only the
        WebUI surface needs this — its single-bubble UI cannot render
        a scheduler-fired follow-up. IM platforms (OneBot, …) keep
        the legacy next-tick fan-out so existing rules, especially
        the ``[内部]X 加牌``-style chained handlers in dicpro.txt,
        keep their proven behaviour.
        """
        return self._event is not None and self._event.platform == "webui"

    # --- body execution ---

    async def _exec_body(self, body: list[Stmt], start_idx: int) -> None:
        idx = start_idx
        while idx < len(body):
            if self.returned:
                return
            stmt = body[idx]
            self._tick()

            # Handle Jump specially — it changes idx. If the label
            # isn't in this body, propagate via :class:`_JumpSignal`
            # so an outer ``_exec_body`` frame (containing e.g. a
            # handler-level ``:重随机`` label that the jump-inside-
            # ``如果`` wants to reach) can catch and resume.
            if isinstance(stmt, Jump):
                target_idx = self._find_label(body, stmt.target)
                if target_idx is not None:
                    idx = target_idx
                    continue
                raise _JumpSignal(stmt.target)

            try:
                await self._exec_stmt(stmt, body)
            except _JumpSignal as jmp:
                # An inner ``如果`` body raised. Try to resolve at this
                # level; if not, let it propagate further out.
                target_idx = self._find_label(body, jmp.target)
                if target_idx is None:
                    raise
                idx = target_idx
                continue
            idx += 1

    # --- statement dispatch ---

    async def _exec_stmt(self, stmt: Stmt, body: list[Stmt]) -> None:
        if isinstance(stmt, Assign):
            value = await self._eval_expr(stmt.value)
            self._scope[stmt.name] = value

        elif isinstance(stmt, IfStmt):
            cond_result = await self._eval_condition(
                stmt.condition.text, is_regex=stmt.condition.is_regex
            )
            if cond_result:
                await self._exec_body(stmt.body, 0)

        elif isinstance(stmt, ReturnStmt):
            self.returned = True

        elif isinstance(stmt, Label):
            pass  # no-op marker

        elif isinstance(stmt, FuncCall):
            # QRSpeed convention: only a small set of "renderer"
            # tools (codecs, ``$输出为$``, ``$访问$``, ``$时间$``)
            # emit their return value as user-facing text when used
            # as a standalone line. Side-effect tools (``$写$``
            # ``$发送$`` ``$JSON 添加$`` ``$全局变量$`` ``$调用$``
            # ``$删除$``) keep silent — their work is done in KV /
            # the scheduler / the adapter sink.
            #
            # Tools whose result the rule wants to *capture* into a
            # variable use the assignment form (``a:$取中间 ...$``);
            # emitting their standalone return would leak internals.
            result = await self._call_tool(stmt.name, stmt.args)
            if result and self._tool_emits_output(stmt.name):
                self.segments.append(
                    TextSegment(text=_decode_qrdic_escapes(result))
                )
                self._check_output_limit()

        elif isinstance(stmt, OutputText):
            text_parts: list[str] = []
            for part in stmt.parts:
                text_parts.append(await self._eval_expr(part))
            text = _decode_qrdic_escapes("".join(text_parts))
            self.segments.append(TextSegment(text=text))
            self._check_output_limit()

        elif isinstance(stmt, OutputImage):
            src = await self._eval_expr(stmt.src)
            self.segments.append(ImageSegment(url=src))
            self._check_output_limit()

        elif isinstance(stmt, OutputVoice):
            src = await self._eval_expr(stmt.src)
            self.segments.append(VoiceSegment(url=src))
            self._check_output_limit()

        elif isinstance(stmt, OutputFlashImage):
            src = await self._eval_expr(stmt.src)
            # Flash photo == regular image with a hint in extras.
            # Adapters that don't know how to render the flash bit
            # silently render the image normally — same downgrade as
            # other QQ-specific extras.
            self.segments.append(
                ImageSegment(url=src, extras={"flash": True})
            )
            self._check_output_limit()

        elif isinstance(stmt, OutputReply):
            msg_id = await self._eval_expr(stmt.msg_id)
            if msg_id:
                self.segments.append(ReplySegment(message_id=msg_id))
            self._check_output_limit()

    # --- expression evaluation ---

    async def _eval_expr(self, expr: Expr) -> str:
        if isinstance(expr, Literal):
            # Literal may contain %var% references that need interpolation
            return await self._interpolate_text(expr.value)

        elif isinstance(expr, VarRef):
            # QRDic / QRSpeed treats undefined ``%var%`` as the literal
            # placeholder text. Real-world rule files (see
            # ``[内部]十扭蛋记录`` / ``[内部]五十扭蛋记录`` in main.ling)
            # rely on this — they reference ``%蛋%`` which is never
            # assigned, expecting it to render as a literal ``%蛋%``
            # the user can ignore. Raising here would crash the entire
            # gacha-record handler. Mirror the same lenient fallback
            # the ``_interpolate_text`` path already uses.
            try:
                return self._lookup_var(expr.name)
            except UndefinedVarError:
                return f"%{expr.name}%"

        elif isinstance(expr, ArithExpr):
            return await self._eval_arith(expr.text)

        elif isinstance(expr, FuncCallExpr):
            return await self._call_tool(expr.name, expr.args)

        elif isinstance(expr, JsonAccess):
            return self._eval_json_access(expr.var, expr.path)

        return ""

    # --- variable lookup ---

    def _lookup_var(self, name: str) -> str:
        # 1. Local scope
        if name in self._scope:
            return self._scope[name]

        # 2. Event context variables
        ctx_val = self._get_event_context_var(name)
        if ctx_val is not None:
            return ctx_val

        # 3. Raise if not found
        raise UndefinedVarError(f"undefined variable: %{name}%")

    def _get_event_context_var(self, name: str) -> str | None:
        """Resolve built-in event context variables.

        Single-name vars (``QQ``, ``群号``, ``昵称`` …) are dispatched
        through :data:`_CTX_RESOLVERS` — a tiny lookup table built once
        at import time. Prefixed forms (``AT[0-9]+``, ``括号[0-9]+``,
        ``时间...``, ``随机数N-M``, segment-indexed ``IMG[0-9]+``,
        ``FACE[0-9]+`` etc.) keep dedicated branches because they
        compute on the fly.
        """
        resolver = _CTX_RESOLVERS.get(name)
        if resolver is not None:
            return resolver(self._event)

        # Bot-level identity vars exposed via ``extras``. The bootstrap
        # pushes ``admin_users`` (tuple) into the dispatcher extras so
        # the migrator-emitted ``%管理员%`` / ``%主人%`` placeholders
        # resolve at runtime. ``%管理员%`` returns the *first* admin
        # (matching QRSpeed's single-owner convention); ``%主人%`` is
        # an alias.
        if name in ("管理员", "主人"):
            admins = self._vm._extras.get("admin_users") or ()
            return str(admins[0]) if admins else ""

        # Capture groups: 括号1..括号9 → 1-based index into regex captures.
        if name.startswith("括号") and name[2:].isdigit():
            idx = int(name[2:]) - 1
            return self._captures[idx] if 0 <= idx < len(self._captures) else ""

        # QRDic ``%参数N%`` (N >= 1) — N-th whitespace-separated token
        # in the original message text, 1-indexed. Differs from
        # ``%参数-1%`` (already handled in the resolver table) which
        # is the *full* original text. Real rule files use this for
        # admin commands like ``苏苏减好感12345 50`` (``%参数2%`` ==
        # ``"50"``) or ``禁言@xxx 30`` (``%参数1%`` after the prefix
        # is consumed by the trigger regex). Returns empty when the
        # token doesn't exist — matches QRDic's silent-default.
        if name.startswith("参数") and name[2:].isdigit():
            n = int(name[2:])
            if n >= 1:
                tokens = self._event.text.split()
                # 1-indexed; out-of-range → empty
                return tokens[n - 1] if 0 < n <= len(tokens) else ""

        if name.startswith("时间"):
            return self._get_time_var(name)

        # Inline random: %随机数N-M%
        if name.startswith("随机数"):
            return self._get_inline_random(name)

        # Segment-indexed prefixes — ``%AT0%`` ``%IMG0%`` ``%FIMG0%``
        # ``%FACE0%`` ``%XML0%`` ``%JSON0%`` etc. Dispatch table keyed
        # on prefix so the longest match wins (FACEPRO before FACE,
        # FIMG before IMG). Each family also recognises the ``NUM``
        # suffix for a count query.
        return _resolve_segment_var(name, self._event.segments)

    def _get_inline_random(self, name: str) -> str:
        """Resolve ``%随机数N-M%`` shorthand to a random integer in [N, M].

        Returns the empty string on malformed input rather than raising
        — keeping QRDic's tolerance: a typo'd ``%随机数abc%`` shouldn't
        crash the dispatch, just degrade.
        """
        spec = name[len("随机数") :]
        if "-" not in spec:
            return ""
        lo_s, hi_s = spec.split("-", 1)
        try:
            lo = int(lo_s)
            hi = int(hi_s)
        except (TypeError, ValueError):
            return ""
        if lo > hi:
            lo, hi = hi, lo
        return str(_r.randint(lo, hi))

    def _get_time_var(self, name: str) -> str:
        """Resolve ``%时间...%`` time-format variables using a module-level dict.

        Building the format map per call would be wasteful — we look
        it up once and ``strftime`` for the matched suffix. Unknown
        suffixes return empty (matching QRDic's silent-default
        behaviour rather than raising for typo'd templates).
        """
        py_fmt = _TIME_FMT_MAP.get(name[2:])
        return datetime.now().strftime(py_fmt) if py_fmt is not None else ""

    # --- text interpolation ---

    async def _interpolate_text(self, text: str) -> str:
        """Substitute ``%var%`` references in a literal text string.

        ``\\%XX`` URL-encoded escapes are already decoded at parse
        time (see :func:`linling_dsl.parser._decode_url_escapes_for_parsing`),
        so this stage only needs to scan for ``%var%`` boundaries.
        """
        if "%" not in text:
            return text

        result: list[str] = []
        i = 0
        while i < len(text):
            if text[i] == "%" and i + 1 < len(text):
                end = text.find("%", i + 1)
                if end > i:
                    var_name = text[i + 1 : end]
                    try:
                        result.append(self._lookup_var(var_name))
                    except UndefinedVarError:
                        result.append(f"%{var_name}%")
                    i = end + 1
                    continue
            result.append(text[i])
            i += 1
        return "".join(result)

    # --- condition evaluation ---

    async def _eval_condition(self, text: str, *, is_regex: bool = False) -> bool:
        """Evaluate a condition expression.

        ``is_regex=True`` flips ``==`` / ``!=`` semantics from literal
        equality to ``re.search`` containment match. Used by ``正则:``
        guard lines (e.g. ``正则:%L%!=.*%绊%.*``). ``|`` and ``&``
        combinators recurse with the same flag so a top-level ``正则``
        propagates all the way down.
        """
        # OR operator (|)
        if "|" in text:
            parts = text.split("|")
            for part in parts:
                if await self._eval_condition(part.strip(), is_regex=is_regex):
                    return True
            return False

        # AND operator (&)
        if "&" in text:
            parts = text.split("&")
            for part in parts:
                if not await self._eval_condition(part.strip(), is_regex=is_regex):
                    return False
            return True

        # Single comparison
        return await self._eval_single_condition(text.strip(), is_regex=is_regex)

    async def _eval_single_condition(self, text: str, *, is_regex: bool = False) -> bool:
        """Evaluate a single comparison condition.

        ``is_regex=True`` makes ``==`` / ``!=`` use :func:`re.search`
        containment instead of literal equality. Numeric/string-cmp
        operators (``>`` ``<`` ``>=`` ``<=``) are unaffected — the
        ``正则:`` guard only applies to equality, matching QRDic's
        own behaviour where regex form was historically tied to
        ``=`` checks.
        """
        # Try operators in order of length (longest first)
        for op in (">=", "<=", "!=", "==", ">", "<"):
            idx = text.find(op)
            if idx >= 0:
                left_raw = text[:idx].strip()
                right_raw = text[idx + len(op) :].strip()
                left = await self._substitute_vars_in_text(left_raw)
                right = await self._substitute_vars_in_text(right_raw)
                if is_regex and op in ("==", "!="):
                    return self._regex_compare(left, right, op)
                return self._compare(left, right, op)

        # No operator found — treat non-empty as truthy
        resolved = await self._substitute_vars_in_text(text)
        return bool(resolved.strip()) and resolved.strip() != "0"

    def _regex_compare(self, left: str, right: str, op: str) -> bool:
        """``==`` / ``!=`` under ``正则:`` semantics — regex search.

        ``正则:%L%==.*hello.*`` is true if pattern ``.*hello.*``
        matches anywhere in ``%L%``. Invalid regex syntax falls back
        to the string-equality path so a malformed pattern doesn't
        crash a handler.
        """
        try:
            matched = re.search(right, left) is not None
        except re.error:
            return self._compare(left, right, op)
        if op == "==":
            return matched
        return not matched

    async def _substitute_vars_in_text(self, text: str) -> str:
        """Substitute %var%, [arith] and $func$ in condition text.

        Order matters: variables first (so ``[%x%+1]`` becomes
        ``[5+1]`` before the arithmetic pass), then arithmetic
        blocks (so the condition machinery sees a plain number),
        then any inline ``$func$`` calls.

        ``[arith]`` is critical here because real-world rules
        compute the comparison's right-hand side inline:
        ``如果:%玉%<[%括号1%*66]`` — without arith resolution the
        condition would compare ``"2000"`` against the literal
        ``"[10*66]"`` and the string-comparison fallback gives
        nonsense answers (``'2'`` < ``'['`` in ASCII is True).
        """
        # First substitute %var%
        result = text
        var_matches = list(_VAR_PATTERN.finditer(result))
        for m in reversed(var_matches):
            var_name = m.group(1)
            try:
                val = self._lookup_var(var_name)
            except UndefinedVarError:
                val = ""
            result = result[: m.start()] + val + result[m.end() :]

        # Resolve [arith] blocks. We walk from right to left so
        # nested brackets in user-authored expressions don't get
        # eaten before the inner one is computed. Balanced-bracket
        # detection mirrors the parser's own scanner.
        result = self._resolve_arith_blocks(result)

        # Then substitute $func args$ calls
        func_matches = list(_FUNC_PATTERN.finditer(result))
        for m in reversed(func_matches):
            inner = m.group(1)
            parts = inner.split(" ", 1)
            func_name = parts[0]
            args_text = parts[1] if len(parts) > 1 else ""
            func_args = _parse_func_args(args_text) if args_text else []
            val = await self._call_tool(func_name, func_args)
            result = result[: m.start()] + val + result[m.end() :]

        return result

    def _resolve_arith_blocks(self, text: str) -> str:
        """Replace every ``[expr]`` with its evaluated value.

        Only well-balanced ``[...]`` pairs are replaced; we leave
        stray brackets alone so rank-format strings (which use
        ``[键]`` / ``[值]`` as templating tokens, not arithmetic)
        survive when they happen to land in a condition body.
        """
        out: list[str] = []
        i = 0
        n = len(text)
        while i < n:
            ch = text[i]
            if ch != "[":
                out.append(ch)
                i += 1
                continue
            # Find balanced ]
            depth = 1
            j = i + 1
            while j < n and depth > 0:
                if text[j] == "[":
                    depth += 1
                elif text[j] == "]":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if j >= n or depth != 0:
                # Unbalanced — keep literal.
                out.append(ch)
                i += 1
                continue
            inner = text[i + 1 : j]
            out.append(_safe_eval_arith(inner))
            i = j + 1
        return "".join(out)

    def _compare(self, left: str, right: str, op: str) -> bool:
        """Compare two values with the given operator."""
        # Try numeric comparison
        try:
            left_num = float(left)
            right_num = float(right)
            return _numeric_cmp(left_num, right_num, op)
        except ValueError:
            pass

        # String comparison
        return _string_cmp(left, right, op)

    # --- arithmetic evaluation ---

    async def _eval_arith(self, text: str) -> str:
        """Evaluate an arithmetic expression with variable substitution."""
        # Substitute %var% references
        resolved = text
        var_matches = list(_VAR_PATTERN.finditer(resolved))
        for m in reversed(var_matches):
            var_name = m.group(1)
            try:
                val = self._lookup_var(var_name)
            except UndefinedVarError:
                val = "0"
            resolved = resolved[: m.start()] + val + resolved[m.end() :]

        return _safe_eval_arith(resolved)

    # --- JSON access ---

    def _eval_json_access(self, var: str, path: list[str]) -> str:
        """Evaluate ``@var[key1][key2]...`` access.

        Path elements are interpolated through ``%var%`` first — real
        rule files use shapes like ``@X[%i%]`` (黑杰克 跟注) where ``i``
        holds the runtime index. Without this pass the literal text
        ``"%i%"`` would reach the ``int(...)`` cast and fail to a miss.

        Mixed path shapes are supported: a path like ``["data", "%i%"]``
        descends one dict step, then one list step, after the
        ``%i%`` resolves to a digit.
        """
        try:
            raw = self._lookup_var(var)
        except UndefinedVarError:
            return ""

        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return ""

        current = data
        for raw_key in path:
            # Interpolate %var% references in the path element. Done
            # synchronously through the substitution pass — async work
            # in the path resolver isn't needed since segment vars are
            # the only async-resolvable shape and they're not used as
            # JSON path keys in any rule we've seen.
            key = self._interpolate_path_key(raw_key)
            if isinstance(current, dict):
                current = current.get(key, "")
            elif isinstance(current, list):
                try:
                    current = current[int(key)]
                except (ValueError, IndexError):
                    return ""
            else:
                return ""

        if current is None:
            return ""
        if isinstance(current, (dict, list)):
            return json.dumps(current, ensure_ascii=False)
        return str(current)

    def _interpolate_path_key(self, raw_key: str) -> str:
        """Sync-resolve ``%var%`` references inside a JSON-access path key.

        JSON paths only carry simple identifiers in production rules;
        no segment vars / no async lookups. We therefore short-circuit
        through :meth:`_lookup_var` directly without going through the
        async ``_interpolate_text`` machinery.
        """
        if "%" not in raw_key:
            return raw_key
        result: list[str] = []
        i = 0
        while i < len(raw_key):
            if raw_key[i] == "%" and i + 1 < len(raw_key):
                end = raw_key.find("%", i + 1)
                if end > i:
                    var_name = raw_key[i + 1 : end]
                    try:
                        result.append(self._lookup_var(var_name))
                    except UndefinedVarError:
                        # Match the OutputText fallback: undefined
                        # vars render as the literal placeholder text.
                        result.append(f"%{var_name}%")
                    i = end + 1
                    continue
            result.append(raw_key[i])
            i += 1
        return "".join(result)

    # --- tool calling ---

    async def _call_tool(self, name: str, args: list[Expr]) -> str:
        """Call a tool through the registry.

        QRDic semantics for tool args: a bare identifier (e.g. ``l`` in
        ``$JSON 长度 l$``) is *late-bound* against the local scope —
        whatever value was assigned to ``l`` flows in. This matches the
        original Java VM where tools received the runtime value of a
        named variable, not the variable's name. Explicit ``%var%``
        refs and other expressions go through their normal eval path
        unchanged.

        In-place mutation: ``$JSON 添加 var ...$`` / ``$JSON 删除 var
        idx$`` are expected to *update* ``var`` in scope, mirroring
        the original Java VM where the JSON object reference lived
        in the variable. We detect mutating ``JSON`` sub-commands and
        write the tool's return value back into scope under the
        original identifier. Non-mutating sub-commands (``长度``,
        ``获取``, ``包含``, ``键``) leave scope alone.

        Performance: signature info (param names, int-coerced params,
        ``*args`` support) is cached on :class:`ToolDef` at registration
        time, so this hot path doesn't reflect on the function on every
        call. Profiling 453-handler dicpro.txt showed ``inspect.signature``
        was the single biggest VM hot spot before this cache.
        """
        registry = self._vm._registry
        tool_def = registry.get_by_dsl_name(name) or registry.get(name)
        if tool_def is None:
            return ""

        sig = tool_def.signature
        scope = self._scope
        # Only the small handful of QRDic tools listed in
        # :data:`_LATE_BINDING_TOOL_DSL_NAMES` actually want bare-
        # identifier args silently swapped to their scope values.
        # Other tools (``$读$`` / ``$写$`` / ``$删除$`` / ``$排行榜$``
        # / ``$时间$`` / ``$随机数$`` etc.) treat their args as
        # literals and would be miscompiled by the swap — see the
        # 漂流瓶 module, which writes ``$写 path R %R%$`` and depends
        # on ``R`` *being the literal key*, not the JSON value of
        # the local ``R``.
        late_binding_enabled = tool_def.dsl_name in _LATE_BINDING_TOOL_DSL_NAMES

        # Evaluate args, late-binding bare identifiers to scope values.
        # ``late_bound_names[i]`` records the original scope key the
        # i-th arg was rewritten from (or ``None`` if no rewrite); the
        # mutating-JSON path below uses this to push the result back.
        eval_args: list[str] = []
        late_bound_names: list[str | None] = []
        for arg in args:
            value = await self._eval_expr(arg)
            late_name: str | None = None
            if (
                late_binding_enabled
                and isinstance(arg, Literal)
                and value
                and value == arg.value
            ):
                stripped = value.strip()
                if stripped and stripped in scope:
                    value = scope[stripped]
                    late_name = stripped
            eval_args.append(value)
            late_bound_names.append(late_name)

        tool_ctx = ToolCtx(
            kv=self._vm._kv,
            event=self._event,
            bot_id=self._vm._bot_id,
            # Shallow-merge so tools can opt into per-call hooks
            # without mutating the VM-wide extras dict. Today the
            # only hook is ``_inline_emit`` for ``$调用 0 ...$``,
            # which is only published when the bootstrap explicitly
            # asked for synchronous fan-out via
            # ``inline_zero_delay_calls=True`` in ``_extras`` (the
            # WebUI sets this; QQ deployments do not, so QQ-side
            # ``$调用 0$`` keeps its scheduler-driven fan-out).
            #
            # The values in ``self._vm._extras`` stay shared by
            # reference, which is what lets adapters / scheduler /
            # handler_lookup plug-ins keep working.
            extras=self._build_tool_extras(),
        )

        # Variadic tools (e.g. ``schedule_handler``) must be called
        # positionally because ``*args`` cannot accept keyword bindings.
        if sig.accepts_var_args:
            result = _stringify(await tool_def.fn(tool_ctx, *eval_args))
        else:
            # Non-variadic: bind by keyword so missing trailing args fall
            # back to the function's defaults rather than raising.
            param_names = sig.param_names
            int_params = sig.int_params
            kwargs: dict[str, object] = {}
            for i, val in enumerate(eval_args):
                if i >= len(param_names):
                    break
                pname = param_names[i]
                if pname in int_params:
                    try:
                        kwargs[pname] = int(val)
                    except (TypeError, ValueError):
                        kwargs[pname] = val
                else:
                    kwargs[pname] = val
            result = _stringify(await tool_def.fn(tool_ctx, **kwargs))

        # In-place mutation for ``$JSON 添加/删除 var ...$``. The
        # original Java VM mutated the array variable through its
        # reference; we replicate that by writing the new payload
        # back into scope under the bare-identifier name we
        # late-bound from. Other tools / sub-commands are unaffected.
        #
        # The function still *returns* the new payload so the
        # assignment form ``A:$JSON 添加 A foo$`` keeps working —
        # both paths see the updated array. The standalone-emit
        # path doesn't need a special case because ``JSON`` is not
        # in :data:`_EMIT_OUTPUT_TOOL_DSL_NAMES`, so a plain
        # ``$JSON 添加 R foo$`` line stays silent.
        if (
            tool_def.dsl_name == "JSON"
            and len(eval_args) >= 2
            and len(late_bound_names) >= 2
            and late_bound_names[1] is not None
        ):
            sub = eval_args[0].strip()
            if sub in _JSON_MUTATING_SUBCOMMANDS:
                scope[late_bound_names[1]] = result

        return result

    # --- output emission ---

    def _tool_emits_output(self, tool_name: str) -> bool:
        """Decide whether a standalone ``$tool$`` line should render text.

        Walks both the Python name and the DSL name through the
        registry; either match in :data:`_EMIT_OUTPUT_TOOL_DSL_NAMES`
        promotes the call's return value into a TextSegment.
        Defaults to *not* emitting so misconfigured / unknown tools
        stay quiet.
        """
        if tool_name in _EMIT_OUTPUT_TOOL_DSL_NAMES:
            return True
        registry = self._vm._registry
        tool_def = registry.get_by_dsl_name(tool_name) or registry.get(tool_name)
        if tool_def is None:
            return False
        return tool_def.dsl_name in _EMIT_OUTPUT_TOOL_DSL_NAMES

    # --- label lookup ---

    def _find_label(self, body: list[Stmt], name: str) -> int | None:
        """Find the index of a label in the body."""
        for i, stmt in enumerate(body):
            if isinstance(stmt, Label) and stmt.name == name:
                return i
        return None
