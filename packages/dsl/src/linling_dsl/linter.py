"""Static analysis / linter for linling ``.ling`` files.

Pure AST-based lint rules. No side effects; no file I/O.

Rules:
    L001  syntax-error      — parser raised ``ParseError`` in strict or lenient.
    L002  parse-warning     — lenient parse accepted, strict parse rejected.
    L100  unused-variable   — local assigned but never read in this handler.
    L110  unreachable-code  — statement after a ``ReturnStmt`` or unconditional
                              ``Jump`` at the same body level.
    L200  dangerous-tool    — call to a tool in :data:`DANGEROUS_TOOLS` without
                              a ``&&权限:`` declaration in the trigger block.
                              (MVP: always flagged — permission wiring TBD.)
    L300  trigger-conflict  — two or more handlers whose trigger regexes match
                              the same probe string.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

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
    Literal,
    OutputFlashImage,
    OutputImage,
    OutputReply,
    OutputText,
    OutputVoice,
    ReturnStmt,
    Script,
    Stmt,
    VarRef,
)
from linling_dsl.parser import ParseError, parse

# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------


class Severity(StrEnum):
    """Severity levels for diagnostics."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True)
class Diagnostic:
    """A single lint finding."""

    severity: Severity
    code: str
    message: str
    line: int
    col: int = 0
    handler_trigger: str | None = None


_SEVERITY_ORDER: dict[Severity, int] = {
    Severity.ERROR: 0,
    Severity.WARNING: 1,
    Severity.INFO: 2,
}


@dataclass
class LintReport:
    """Collected diagnostics for a single source."""

    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(d.severity == Severity.ERROR for d in self.diagnostics)

    @property
    def has_warnings(self) -> bool:
        return any(d.severity == Severity.WARNING for d in self.diagnostics)

    def sorted(self) -> list[Diagnostic]:
        """Sort by line, then by severity (errors first), then by code."""
        return sorted(
            self.diagnostics,
            key=lambda d: (d.line, _SEVERITY_ORDER[d.severity], d.code),
        )


# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

#: Tool names that can have destructive side effects when invoked.
DANGEROUS_TOOLS: frozenset[str] = frozenset(
    {
        "删除",
        "撤回",
        "发送",
        "群头衔",
        "禁言",
        "kick",
    }
)

#: Probe strings used to detect trigger regex conflicts. Crafted so that common
#: catch-alls (``(.*)``, ``.*``, empty triggers) will match at least one probe.
_CONFLICT_PROBES: tuple[str, ...] = (
    "",
    "a",
    "123",
    "测试",
    "你好",
    "打卡",
)

#: Built-in variables that are provided by the event context and therefore
#: never count as "unused locals".
_BUILTIN_VARS: frozenset[str] = frozenset(
    {
        # identity
        "QQ",
        "用户",
        "群号",
        "群",
        "会话",
        "昵称",
        "Robot",
        "自己",
        "管理员",
        "主群",
        # message metadata
        "Code",
        "Reqid",
        "Msgbar",
        "Json",
        "Type",
        "参数-1",
    }
)

#: Prefixes for built-in variable families (``AT0``/``AT1``/…, ``时间HH`` …).
_BUILTIN_PREFIXES: tuple[str, ...] = (
    "AT",
    "IMG",
    "括号",
    "时间",
)


def _is_builtin_var(name: str) -> bool:
    """Return True if ``name`` is a built-in event/context variable."""
    if name in _BUILTIN_VARS:
        return True
    return any(name.startswith(prefix) and name != prefix for prefix in _BUILTIN_PREFIXES)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def lint_source(source: str, *, filename: str = "<string>") -> LintReport:
    """Parse ``source`` in lenient mode and run every lint check on it.

    If lenient parsing fails outright, the returned report contains a single
    ``L001`` error and no further checks are attempted. If lenient parsing
    succeeds but strict parsing would have failed, an ``L002`` warning is
    emitted in addition to the usual checks.
    """
    report = LintReport()

    # Lenient parse: always attempt first — it is the source of truth for
    # every downstream rule.
    try:
        script = parse(source, filename=filename, strict=False)
    except ParseError as exc:
        report.diagnostics.append(
            Diagnostic(
                severity=Severity.ERROR,
                code="L001",
                message=f"语法错误: {exc.args[0] if exc.args else exc}",
                line=exc.line,
                col=exc.col,
            )
        )
        return report

    # Strict parse: if this one rejects input the lenient parser accepted,
    # that's a warning ("parser papered over stray 如果尾 or missing 如果尾").
    try:
        parse(source, filename=filename, strict=True)
    except ParseError as exc:
        report.diagnostics.append(
            Diagnostic(
                severity=Severity.WARNING,
                code="L002",
                message=f"解析器自动修复: {exc.args[0] if exc.args else exc}",
                line=exc.line,
                col=exc.col,
            )
        )

    _run_script_checks(script, report)
    return report


def lint_script(script: Script) -> LintReport:
    """Run all AST-level lint checks on an already-parsed :class:`Script`."""
    report = LintReport()
    _run_script_checks(script, report)
    return report


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _run_script_checks(script: Script, report: LintReport) -> None:
    for handler in script.handlers:
        _check_unused_vars(handler, report)
        _check_unreachable(handler, report)
        _check_dangerous_tools(handler, report)

    _check_trigger_conflicts(script.handlers, report)


# ---------------------------------------------------------------------------
# L100 — unused variables
# ---------------------------------------------------------------------------


def _check_unused_vars(handler: Handler, report: LintReport) -> None:
    """Flag local variables assigned but never read within this handler."""
    # name -> (first assignment line number)
    assigned: dict[str, int] = {}
    read: set[str] = set()

    for stmt in _walk_stmts(handler.body):
        if isinstance(stmt, Assign):
            # Track the first assignment line; also record any reads inside
            # the assigned expression (e.g. ``玉:[%玉%+100]`` reads 玉).
            assigned.setdefault(stmt.name, stmt.line)
            _collect_reads_from_expr(stmt.value, read)
        else:
            _collect_reads_from_stmt(stmt, read)

    for name, line in assigned.items():
        if name in read:
            continue
        if _is_builtin_var(name):
            continue
        report.diagnostics.append(
            Diagnostic(
                severity=Severity.WARNING,
                code="L100",
                message=f"局部变量 %{name}% 被赋值但从未使用",
                line=line,
                handler_trigger=handler.trigger,
            )
        )


def _collect_reads_from_stmt(stmt: Stmt, read: set[str]) -> None:
    """Collect every variable read inside ``stmt`` (recursively)."""
    if isinstance(stmt, OutputText):
        for part in stmt.parts:
            _collect_reads_from_expr(part, read)
    elif isinstance(stmt, OutputImage | OutputVoice | OutputFlashImage):
        _collect_reads_from_expr(stmt.src, read)
    elif isinstance(stmt, OutputReply):
        _collect_reads_from_expr(stmt.msg_id, read)
    elif isinstance(stmt, FuncCall):
        for arg in stmt.args:
            _collect_reads_from_expr(arg, read)
    elif isinstance(stmt, IfStmt):
        _collect_reads_from_text(stmt.condition.text, read)
        # Recursion into the body is handled by the caller via _walk_stmts.
    # Assign.value is handled by the caller (we want to know the name too).
    # ReturnStmt / Jump / Label have no reads.


def _collect_reads_from_expr(expr: Expr, read: set[str]) -> None:
    if isinstance(expr, VarRef):
        read.add(expr.name)
    elif isinstance(expr, ArithExpr):
        _collect_reads_from_text(expr.text, read)
    elif isinstance(expr, JsonAccess):
        read.add(expr.var)
    elif isinstance(expr, FuncCallExpr):
        for arg in expr.args:
            _collect_reads_from_expr(arg, read)
    elif isinstance(expr, Literal):
        # Literals can still contain %var% references when they come from
        # assignment values that weren't simplified (see parser fallback).
        _collect_reads_from_text(expr.value, read)


# Matches %name%, avoiding empty names.
_VARREF_RE = re.compile(r"%([^%\s]+)%")


def _collect_reads_from_text(text: str, read: set[str]) -> None:
    """Extract %var% references from a raw text blob (conditions, arith)."""
    for match in _VARREF_RE.finditer(text):
        read.add(match.group(1))


# ---------------------------------------------------------------------------
# L110 — unreachable code
# ---------------------------------------------------------------------------


def _check_unreachable(handler: Handler, report: LintReport) -> None:
    """Flag the first statement following a terminator at the same body level.

    We inspect the top-level handler body and each ``IfStmt`` body. A terminator
    is a ``ReturnStmt`` or a ``Jump`` (unconditional jumps always transfer
    control). Only the first statement after the terminator is flagged to keep
    the noise down.
    """
    _check_unreachable_in_body(handler.body, handler.trigger, report)
    for stmt in _walk_stmts(handler.body):
        if isinstance(stmt, IfStmt):
            # Inside an ``if`` body, unreachable code after 返回 is still
            # worth flagging — even though the body itself may be skipped,
            # anything after an in-body 返回 definitely cannot run.
            _check_unreachable_in_body(stmt.body, handler.trigger, report)


def _check_unreachable_in_body(body: list[Stmt], trigger: str, report: LintReport) -> None:
    for idx, stmt in enumerate(body):
        if not _is_terminator(stmt):
            continue
        if idx + 1 >= len(body):
            return
        nxt = body[idx + 1]
        report.diagnostics.append(
            Diagnostic(
                severity=Severity.WARNING,
                code="L110",
                message="此语句位于 返回/完成/$jump$ 之后, 永远不会执行",
                line=_stmt_line(nxt),
                handler_trigger=trigger,
            )
        )
        return  # only flag the first one


def _is_terminator(stmt: Stmt) -> bool:
    return isinstance(stmt, ReturnStmt | Jump)


def _stmt_line(stmt: Stmt) -> int:
    return stmt.line


# ---------------------------------------------------------------------------
# L200 — dangerous tool usage
# ---------------------------------------------------------------------------


def _check_dangerous_tools(handler: Handler, report: LintReport) -> None:
    """Walk every :class:`FuncCall` inside the handler and flag dangerous ones.

    MVP behaviour: always emit a warning. The ``&&权限:`` declaration
    machinery will be wired in a follow-up task.
    """
    for stmt in _walk_stmts(handler.body):
        if isinstance(stmt, FuncCall) and stmt.name in DANGEROUS_TOOLS:
            report.diagnostics.append(
                Diagnostic(
                    severity=Severity.WARNING,
                    code="L200",
                    message=(f"危险工具 ${stmt.name}$ 调用未声明权限 (缺少 &&权限: 声明)"),
                    line=stmt.line,
                    handler_trigger=handler.trigger,
                )
            )


# ---------------------------------------------------------------------------
# L300 — trigger regex conflicts
# ---------------------------------------------------------------------------


def _check_trigger_conflicts(handlers: list[Handler], report: LintReport) -> None:
    """Flag any two handlers whose triggers both match the same probe string.

    Each handler's trigger is compiled once (``re.fullmatch`` semantics). A
    single conflict probe covers catch-all handlers (empty trigger, ``.*``,
    ``(.*)``); specific CJK probes catch Chinese keyword overlap.
    """
    compiled: list[tuple[Handler, re.Pattern[str] | None]] = []
    for handler in handlers:
        try:
            pattern: re.Pattern[str] | None = re.compile(handler.trigger)
        except re.error:
            pattern = None
        compiled.append((handler, pattern))

    # Collect (handler_index, probe) pairs that matched, grouped by probe.
    conflicts: dict[tuple[int, int], set[str]] = {}
    for probe in _CONFLICT_PROBES:
        matchers: list[int] = []
        for i, (_handler, pattern) in enumerate(compiled):
            if pattern is None:
                continue
            if pattern.fullmatch(probe) is not None:
                matchers.append(i)
        if len(matchers) < 2:
            continue
        for a_idx in range(len(matchers)):
            for b_idx in range(a_idx + 1, len(matchers)):
                key = (matchers[a_idx], matchers[b_idx])
                conflicts.setdefault(key, set()).add(probe)

    emitted: set[tuple[int, str]] = set()  # (handler_index, other trigger)
    for (i, j), probes in conflicts.items():
        h_i, h_j = handlers[i], handlers[j]
        probe_str = ", ".join(repr(p) for p in sorted(probes))
        for src_idx, src_handler, other in (
            (i, h_i, h_j),
            (j, h_j, h_i),
        ):
            emit_key: tuple[int, str] = (src_idx, other.trigger)
            if emit_key in emitted:
                continue
            emitted.add(emit_key)
            report.diagnostics.append(
                Diagnostic(
                    severity=Severity.WARNING,
                    code="L300",
                    message=(
                        f"触发器 {src_handler.trigger!r} 与 "
                        f"{other.trigger!r} (行 {other.line}) 同时匹配 "
                        f"探测字符串 {probe_str}"
                    ),
                    line=src_handler.line,
                    handler_trigger=src_handler.trigger,
                )
            )


# ---------------------------------------------------------------------------
# AST walking helpers
# ---------------------------------------------------------------------------


def _walk_stmts(body: list[Stmt]) -> list[Stmt]:
    """Yield every statement in ``body``, descending into ``IfStmt`` bodies.

    Order is DFS pre-order so the caller sees an ``IfStmt`` before the
    statements it contains.
    """
    out: list[Stmt] = []

    def _walk(stmts: list[Stmt]) -> None:
        for stmt in stmts:
            out.append(stmt)
            if isinstance(stmt, IfStmt):
                _walk(stmt.body)

    _walk(body)
    return out


__all__ = [
    "DANGEROUS_TOOLS",
    "Diagnostic",
    "LintReport",
    "Severity",
    "lint_script",
    "lint_source",
]
