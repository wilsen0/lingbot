"""AST node definitions for the linling DSL.

All nodes are frozen dataclasses for immutability and hashability.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Literal:
    """A plain text literal."""

    value: str


@dataclass(frozen=True)
class VarRef:
    """A variable reference: %name%."""

    name: str


@dataclass(frozen=True)
class ArithExpr:
    """An arithmetic expression: [expr]. Raw text, evaluated at runtime."""

    text: str


@dataclass(frozen=True)
class FuncCallExpr:
    """An inline function call expression: $func arg1 arg2$."""

    name: str
    args: list[Expr]


@dataclass(frozen=True)
class JsonAccess:
    """JSON field access: @var[field1][field2]."""

    var: str
    path: list[str]


Expr = Literal | VarRef | ArithExpr | FuncCallExpr | JsonAccess


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Condition:
    """A condition expression. Raw text for v0, parsed at runtime.

    ``is_regex=True`` marks conditions that came from a ``正则:`` line
    rather than a plain ``如果:``. Operators ``==`` / ``!=`` then
    compare via :func:`re.search` rather than literal equality;
    other operators (``>`` / ``<`` / ``>=`` / ``<=``) keep their
    numeric semantics.
    """

    text: str
    line: int
    is_regex: bool = False


# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Assign:
    """Variable assignment: name:value."""

    name: str
    value: Expr
    line: int


@dataclass(frozen=True)
class IfStmt:
    """If statement: 如果:condition ... 如果尾."""

    condition: Condition
    body: list[Stmt]
    line: int


@dataclass(frozen=True)
class ReturnStmt:
    """Return statement: 返回 or 完成."""

    line: int


@dataclass(frozen=True)
class Label:
    """A label: :name."""

    name: str
    line: int


@dataclass(frozen=True)
class Jump:
    """A jump to a label: $jump :label$ or $跳 :label$."""

    target: str
    line: int


@dataclass(frozen=True)
class FuncCall:
    """A standalone function call statement: $func args...$."""

    name: str
    args: list[Expr]
    line: int


@dataclass(frozen=True)
class OutputText:
    """Text output with interpolation."""

    parts: list[Expr]
    line: int


@dataclass(frozen=True)
class OutputImage:
    """Image output: ±img=src±."""

    src: Expr
    line: int


@dataclass(frozen=True)
class OutputVoice:
    """Voice / PTT output: ``±ptt=src±``.

    QRSpeed's voice-message sigil. Lands as a
    :class:`linling_core.segments.VoiceSegment` at runtime;
    adapters that don't render audio fall through to the
    description text on their side.
    """

    src: Expr
    line: int


@dataclass(frozen=True)
class OutputFlashImage:
    """Flash-image output: ``±fimg=src±``.

    QQ's "flash photo" mode — the image is an :class:`ImageSegment`
    with an ``extras={"flash": True}`` hint so the OneBot adapter can
    flag the outgoing message accordingly. Non-QQ adapters should
    render it the same as a regular image.
    """

    src: Expr
    line: int


@dataclass(frozen=True)
class OutputReply:
    """Reply marker: ``±rep msg_id±``.

    Constructs a :class:`linling_core.segments.ReplySegment` so the
    outgoing message is shown as a reply to the indicated message id.
    """

    msg_id: Expr
    line: int


Stmt = (
    Assign
    | IfStmt
    | ReturnStmt
    | Label
    | Jump
    | FuncCall
    | OutputText
    | OutputImage
    | OutputVoice
    | OutputFlashImage
    | OutputReply
)


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Handler:
    """A single handler: trigger regex + body statements.

    ``expose_to_llm`` and ``summary_mode`` are optional metadata for the
    DSL Action Ledger feature. Defaults of ``None`` mean "not declared",
    so legacy ``Handler(trigger=..., is_internal=..., body=..., line=...)``
    construction keeps working unchanged. The ``LedgerWriter`` falls back
    to ``[内部]`` prefix detection / ``Global_Default_Expose`` and to
    ``"with_result"`` mode when these fields are ``None``.
    """

    trigger: str
    is_internal: bool
    body: list[Stmt]
    line: int
    expose_to_llm: bool | None = None
    summary_mode: str | None = None


@dataclass(frozen=True)
class Script:
    """A complete .ling script file."""

    handlers: list[Handler] = field(default_factory=list)
