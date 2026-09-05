"""Hand-written recursive descent parser for the linling DSL.

Parses .ling source files into an AST. Does NOT use lark or any parser
generator — full control over the quirky QRDic syntax is required.
"""

from __future__ import annotations

import re

from linling_dsl.ast_nodes import (
    ArithExpr,
    Assign,
    Condition,
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
    Script,
    Stmt,
    VarRef,
)


class ParseError(Exception):
    """Raised when the parser encounters invalid syntax."""

    def __init__(self, message: str, line: int, col: int = 0) -> None:
        self.line = line
        self.col = col
        super().__init__(f"line {line}: {message}")


# QRSpeed allows ``\\%XX`` URL-encoded escapes in regular text — the
# canonical examples are ``\\%0A`` (newline), ``\\%20`` (space) and
# ``\\%25`` (literal percent). They originate from rules that need
# control characters inside a single-line ``.ling`` body where the
# author can't directly type them.
#
# The decode happens at *parse* time so the ``%var%`` scan never
# sees a stray ``%`` from the encoded sequence. ``\\%25`` decodes to
# a private-use sentinel during the scan and gets swapped back to
# ``%`` by ``_parse_interpolated_text`` before the resulting Literal
# escapes the parser.
_URL_ESCAPE_RE = re.compile(r"\\%([0-9A-Fa-f]{2})")
_PERCENT_SENTINEL = "\ue000"


def _decode_url_escapes_for_parsing(text: str) -> str:
    """Replace ``\\%XX`` escapes with their characters.

    Encoded ``%`` (``\\%25``) is held as a PUA sentinel so the
    interpolation pass can finish without confusing it for a real
    ``%var%`` boundary; the sentinel is unconditionally swapped back
    to ``%`` after parsing finishes.
    """
    if "\\%" not in text:
        return text

    def _sub(match: re.Match[str]) -> str:
        code = int(match.group(1), 16)
        if code == 0x25:
            return _PERCENT_SENTINEL
        return chr(code)

    return _URL_ESCAPE_RE.sub(_sub, text)


def parse(source: str, *, filename: str = "<string>", strict: bool = True) -> Script:
    """Parse a .ling source string into an AST.

    When ``strict`` is ``False`` the parser tolerates two QRDic-era
    irregularities that QRSpeed silently accepted:

    * a stray ``如果尾`` with no matching ``如果:`` is skipped, and
    * a handler body that reaches its end with an unclosed ``如果:``
      is auto-closed using the statements collected so far.
    """
    lines = source.split("\n")
    handler_blocks = _split_into_handler_blocks(lines)
    handlers: list[Handler] = []
    for block_lines, start_line in handler_blocks:
        handler = _parse_handler(block_lines, start_line, filename, strict=strict)
        if handler is not None:
            handlers.append(handler)
    return Script(handlers=handlers)


# ---------------------------------------------------------------------------
# Block splitting
# ---------------------------------------------------------------------------


def _split_into_handler_blocks(
    lines: list[str],
) -> list[tuple[list[str], int]]:
    """Split source lines into handler blocks separated by blank lines.

    Returns list of (block_lines, 1-based start line number).

    QRDic authoring conventions are loose: a single blank line —
    sometimes even just a line containing nothing but a space — can
    appear inside a handler body for visual grouping, while real
    handler boundaries usually use two-or-more consecutive blanks.

    We treat a blank line as a *real* boundary only when:

    * the next non-blank line *cannot* be a continuation (i.e. it is
      not a control-flow keyword like ``如果:``, ``如果尾``, ``返回``,
      ``完成``, ``正则:``, an ``$func$`` body call, an output line),
      **or**
    * we see two-or-more consecutive blanks (the more idiomatic
      handler separator).

    This fixes a real-world bug where ``L:$排行榜 ...$`` was followed
    by a space-only cosmetic line, splitting one handler into two
    and turning the inner ``如果:%QQ%==%L%`` body into a bogus second
    "trigger".
    """
    blocks: list[tuple[list[str], int]] = []
    current_block: list[str] = []
    block_start = 1

    def _flush() -> None:
        if current_block:
            blocks.append((list(current_block), block_start))
            current_block.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        lineno = i + 1
        if line.strip() == "":
            j = i + 1
            blank_run = 1
            while j < len(lines) and lines[j].strip() == "":
                blank_run += 1
                j += 1

            if not current_block:
                # Leading or inter-block blanks — just skip.
                i = j
                continue

            # Decide whether to close the current block. A run of two
            # or more blanks is always a boundary. A single blank is
            # only a boundary when the next non-blank line cannot be
            # a body continuation.
            if blank_run >= 2 or (j < len(lines) and not _looks_like_body_continuation(lines[j])):
                _flush()
                i = j
                continue
            # Single blank that's followed by a body line — absorb it.
            i = j
            continue

        if not current_block:
            block_start = lineno
        current_block.append(line)
        i += 1

    _flush()
    return blocks


# Tokens that unambiguously mark a body-continuation line. A line
# starting with one of these *cannot* be a fresh handler trigger,
# so a single blank before it is cosmetic, not a block boundary.
_BODY_PREFIXES: tuple[str, ...] = (
    "如果:",
    "如果尾",
    "正则:",
    "返回",
    "完成",
    "结束",
    ":",  # label definition (e.g. ``:形象标记``)
)


def _looks_like_body_continuation(line: str) -> bool:
    """True when ``line`` is unambiguously a handler-body line."""
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith("//") or stripped.startswith("##"):
        return True  # comments stay attached to the surrounding handler
    return any(stripped.startswith(p) for p in _BODY_PREFIXES)


# ---------------------------------------------------------------------------
# Handler parsing
# ---------------------------------------------------------------------------


def _parse_handler(
    block_lines: list[str],
    start_line: int,
    filename: str,
    *,
    strict: bool = True,
) -> Handler | None:
    """Parse a single handler block into a Handler AST node."""
    if not block_lines:
        return None

    trigger_line = block_lines[0]

    # Skip && config/comment lines
    if trigger_line.startswith("&&"):
        return None
    # Skip ``//`` and ``##`` comments. Both styles appear in QRSpeed
    # rule files: ``//`` was the canonical form in the QRDic Pro
    # documentation, while ``##`` shows up in the system-event
    # samples (see ziyii01's group-event guide). dicpro.txt uses
    # ``//`` to comment out whole handlers in-place — a top-level
    # ``//[戳一戳]`` block must disappear, not register as a trigger
    # of its own. The body of a commented-out block is also dropped.
    if trigger_line.lstrip().startswith("//") or trigger_line.lstrip().startswith("##"):
        return None

    # Detect [内部] prefix
    is_internal = False
    trigger = trigger_line
    if trigger.startswith("[内部]"):
        is_internal = True
        trigger = trigger[len("[内部]") :]

    # DSL Action Ledger handler-level metadata directives
    # (Requirement 3.1 / 3.2 / 5.1 / 5.6). Lines that immediately
    # follow the trigger and start with ``^`` carry handler metadata;
    # we consume them here so the body parser doesn't see them. The
    # ``^`` sigil is unused elsewhere in the DSL so it cannot collide
    # with any pre-existing syntax. Invalid *values* for a recognised
    # key fall through with ``None`` rather than aborting handler
    # load (Requirement 3.6 / 5.6) — this keeps Phase 4 strictly
    # additive on top of the legacy parser.
    expose_to_llm: bool | None = None
    summary_mode: str | None = None
    body_start = 1
    _LEDGER_KEYS = ("expose_to_llm", "summary_mode")
    while body_start < len(block_lines):
        meta_line = block_lines[body_start].strip()
        if not meta_line.startswith("^"):
            break
        key, sep, value = meta_line[1:].partition(":")
        if not sep:
            # Not a ``^key: value`` shape — leave it to the body
            # parser, which will treat it as output text.
            break
        normalised_key = key.strip().lower()
        # Conservative consumption:only recognised ledger keys are
        # treated as directives and removed from the body. Anything
        # else (unknown directive, accidental leading ``^`` in output
        # text) falls through so the user's content isn't silently
        # discarded. Future ledger metadata additions extend the
        # ``_LEDGER_KEYS`` tuple above.
        if normalised_key not in _LEDGER_KEYS:
            break
        value_str = value.strip()
        if normalised_key == "expose_to_llm":
            # Accept bool literals in any case form. ``True``/``False``
            # win; everything else (typos, numbers, ``None``) leaves
            # the field as ``None`` and the LedgerWriter fallback
            # kicks in.
            lowered = value_str.lower()
            if lowered in ("true", "1", "yes", "on"):
                expose_to_llm = True
            elif lowered in ("false", "0", "no", "off"):
                expose_to_llm = False
            # else: leave ``None``, fall through to next directive.
        # ``summary_mode`` — only the two valid string values are
        # accepted; everything else falls back to ``None`` and the
        # LedgerWriter defaults to ``"with_result"``.
        elif value_str in ("trigger_only", "with_result"):
            summary_mode = value_str
        body_start += 1

    # Parse body (lines after the trigger and any metadata lines)
    body_lines = block_lines[body_start:]
    body = _parse_body(body_lines, start_line + body_start, filename, strict=strict)

    return Handler(
        trigger=trigger,
        is_internal=is_internal,
        body=body,
        line=start_line,
        expose_to_llm=expose_to_llm,
        summary_mode=summary_mode,
    )


# ---------------------------------------------------------------------------
# Body parsing
# ---------------------------------------------------------------------------


def _parse_body(
    lines: list[str],
    start_line: int,
    filename: str,
    *,
    strict: bool = True,
) -> list[Stmt]:
    """Parse a sequence of body lines into statements."""
    stmts: list[Stmt] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        lineno = start_line + i
        stripped = line.strip()

        # Skip empty lines within body (shouldn't happen after split, but be safe)
        if stripped == "":
            i += 1
            continue

        # Skip comments — both ``//`` (QRDic canonical) and ``##``
        # (used in the ziyii01 community samples). Body lines starting
        # with either are dropped.
        if stripped.startswith("//") or stripped.startswith("##"):
            i += 1
            continue

        # Skip && config lines
        if stripped.startswith("&&"):
            i += 1
            continue

        # 如果: or 正则: — if statement. ``正则:`` flips ``is_regex``
        # so the VM compares ``==`` / ``!=`` via regex.search rather
        # than literal equality (real handlers like ``正则:%T%==.*%QQ%.*``
        # depend on this).
        if stripped.startswith("如果:") or stripped.startswith("正则:"):
            # "如果:" is 3 chars, "正则:" is 3 chars
            is_regex_cond = stripped.startswith("正则:")
            cond_text = stripped[3:]  # skip "如果:" or "正则:"
            # Collect body until 如果尾
            if_body_lines: list[str] = []
            if_start = lineno
            i += 1
            depth = 1
            closed = False
            while i < len(lines):
                inner = lines[i].strip()
                if inner.startswith("如果:") or inner.startswith("正则:"):
                    depth += 1
                    if_body_lines.append(lines[i])
                elif inner == "如果尾":
                    depth -= 1
                    if depth == 0:
                        closed = True
                        break
                    if_body_lines.append(lines[i])
                else:
                    if_body_lines.append(lines[i])
                i += 1
            if not closed:
                if strict:
                    raise ParseError("未找到匹配的 如果尾", if_start)
                # Lenient: auto-close the 如果 using lines collected so far.
                inner_body = _parse_body(if_body_lines, if_start + 1, filename, strict=strict)
                stmts.append(
                    IfStmt(
                        condition=Condition(text=cond_text, line=if_start, is_regex=is_regex_cond),
                        body=inner_body,
                        line=if_start,
                    )
                )
                # We ran past the end of ``lines``; nothing more to consume.
                break

            # Parse the if body
            inner_body = _parse_body(if_body_lines, if_start + 1, filename, strict=strict)
            stmt: Stmt = IfStmt(
                condition=Condition(text=cond_text, line=if_start, is_regex=is_regex_cond),
                body=inner_body,
                line=if_start,
            )
            stmts.append(stmt)
            i += 1  # skip 如果尾
            continue

        # 如果尾 without matching 如果 — error (strict) or skip (lenient)
        if stripped == "如果尾":
            if strict:
                raise ParseError("意外的 如果尾 (没有匹配的 如果:)", lineno)
            i += 1
            continue

        # 返回 or 完成
        if stripped in {"返回", "完成"}:
            stmts.append(ReturnStmt(line=lineno))
            i += 1
            continue

        # :label — label definition
        if stripped.startswith(":") and not stripped.startswith(":"):
            pass  # fall through — this won't match
        if stripped.startswith(":") and len(stripped) > 1 and stripped[1] != " ":
            # It's a label like :形象标记
            label_name = stripped[1:]
            stmts.append(Label(name=label_name, line=lineno))
            i += 1
            continue

        # $jump :label$ or $跳 :label$
        if _is_jump(stripped):
            target = _parse_jump_target(stripped, lineno)
            stmts.append(Jump(target=target, line=lineno))
            i += 1
            continue

        # ±img=...± — image output
        if stripped.startswith("±img=") and stripped.endswith("±"):
            src_text = stripped[len("±img=") : -1]  # strip trailing ±
            src_expr = _parse_expr_text(src_text)
            stmts.append(OutputImage(src=src_expr, line=lineno))
            i += 1
            continue

        # ±ptt=src± — voice / PTT output (QQ "录音" 消息).
        if stripped.startswith("±ptt=") and stripped.endswith("±"):
            src_text = stripped[len("±ptt=") : -1]
            stmts.append(OutputVoice(src=_parse_expr_text(src_text), line=lineno))
            i += 1
            continue

        # ±fimg=src± — QQ flash image (ImageSegment with extras.flash=True).
        if stripped.startswith("±fimg=") and stripped.endswith("±"):
            src_text = stripped[len("±fimg=") : -1]
            stmts.append(OutputFlashImage(src=_parse_expr_text(src_text), line=lineno))
            i += 1
            continue

        # ±rep ...± — reply marker. The body is a single message id
        # (literal or interpolation). QQ-style ``±rep @[msg]±`` style
        # JSON-path lookups are accepted as the literal string and
        # resolved at runtime via the ``%var%`` machinery — we don't
        # parse the ``@[…]`` shorthand here.
        if stripped.startswith("±rep ") and stripped.endswith("±"):
            body = stripped[len("±rep ") : -1].strip()
            stmts.append(OutputReply(msg_id=_parse_expr_text(body), line=lineno))
            i += 1
            continue

        # ±bub N±, ±strmsg ...± and other QQ-only decorative sigils
        # — skip silently for now. They land in dicpro.txt-derived
        # rules occasionally; future adapters that care about them
        # can subscribe to the raw line through a side channel.
        if stripped.startswith(("±bub ", "±strmsg ", "±bub=", "±strmsg=")) and stripped.endswith(
            "±"
        ):
            i += 1
            continue

        # $func args...$ — standalone function call (entire line is $...$)
        if _is_standalone_func_call(stripped):
            func_call = _parse_func_call_stmt(stripped, lineno)
            stmts.append(func_call)
            i += 1
            continue

        # name:value — assignment (name is a short identifier before first colon)
        assign = _try_parse_assign(stripped, lineno)
        if assign is not None:
            stmts.append(assign)
            i += 1
            continue

        # Anything else — output text with interpolation
        parts = _parse_interpolated_text(stripped)
        stmts.append(OutputText(parts=parts, line=lineno))
        i += 1

    return stmts


# ---------------------------------------------------------------------------
# Jump detection and parsing
# ---------------------------------------------------------------------------


def _is_jump(text: str) -> bool:
    """Check if a line is a jump statement: $jump :label$ or $跳 :label$."""
    if not text.startswith("$"):
        return False
    if not text.endswith("$"):
        return False
    inner = text[1:-1].strip()
    return inner.startswith("jump :") or inner.startswith("jump:") or inner.startswith("跳 :")


def _parse_jump_target(text: str, lineno: int) -> str:
    """Extract the target label from a jump statement."""
    inner = text[1:-1].strip()
    # Handle "jump :label" or "jump:label" or "跳 :label"
    if inner.startswith("jump :"):
        return inner[len("jump :") :]
    if inner.startswith("jump:"):
        return inner[len("jump:") :]
    if inner.startswith("跳 :"):
        return inner[len("跳 :") :]
    raise ParseError(f"无法解析跳转目标: {text}", lineno)


# ---------------------------------------------------------------------------
# Function call detection and parsing
# ---------------------------------------------------------------------------


def _is_standalone_func_call(text: str) -> bool:
    """Check if a line is a standalone function call: $func args...$."""
    if not text.startswith("$"):
        return False
    if not text.endswith("$"):
        return False
    # Must not be a jump
    return not _is_jump(text)


def _parse_func_call_stmt(text: str, lineno: int) -> FuncCall:
    """Parse a standalone function call: $func arg1 arg2$."""
    inner = text[1:-1]  # strip $ delimiters
    parts = inner.split(" ", 1)
    name = parts[0]
    args: list[Expr] = []
    if len(parts) > 1:
        args = _parse_func_args(parts[1])
    return FuncCall(name=name, args=args, line=lineno)


def _parse_func_args(args_text: str) -> list[Expr]:
    """Parse function arguments as space-separated expressions."""
    args: list[Expr] = []
    # Arguments are space-separated, but %var% and [expr] can contain spaces
    # For v0, we do a simple split respecting %...% and [...] boundaries
    tokens = _tokenize_func_args(args_text)
    for token in tokens:
        args.append(_parse_expr_text(token))
    return args


def _tokenize_func_args(text: str) -> list[str]:
    """Tokenize function arguments respecting %...%, [...], and $...$ boundaries."""
    tokens: list[str] = []
    current = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == " " and not _in_delimited(current):
            if current:
                tokens.append(current)
                current = ""
            i += 1
            continue
        current += ch
        i += 1

    if current:
        tokens.append(current)
    return tokens


def _in_delimited(text: str) -> bool:
    """Check if we're inside an unclosed delimiter."""
    # Count unclosed % pairs
    pct_count = text.count("%")
    if pct_count % 2 == 1:
        return True
    # Count unclosed [ brackets
    bracket_depth = 0
    for ch in text:
        if ch == "[":
            bracket_depth += 1
        elif ch == "]":
            bracket_depth -= 1
    return bracket_depth > 0


# ---------------------------------------------------------------------------
# Assignment parsing
# ---------------------------------------------------------------------------


def _try_parse_assign(text: str, lineno: int) -> Assign | None:
    """Try to parse a line as an assignment (``name:value``).

    QRDic mixes assignments (``玉:$读 ...$``) with prefix-style output
    text (``tip:只有双方...``) on lines that look syntactically
    identical. We disambiguate with two rules:

    * **Name shape**: the part before the first colon must be a short
      identifier (≤ 2 ASCII/CJK chars, no spaces, not a control
      keyword). Output prefixes like ``tip``, ``Ps``, ``例如`` are
      mostly 3+ chars and fall out here.
    * **Value shape**: the part after the colon must look like an
      *expression* — a tool call (``$...$``), an arith block
      (``[...]``), a var ref (``%...%``), JSON access (``@var[...]``),
      a numeric literal, or a reference to another short identifier.
      Free-form Chinese text is rejected as output.

    Either rule failing makes us return ``None`` and the caller falls
    through to the OutputText branch. The combined rule matches the
    full ``dicpro.txt`` corpus: every real assignment passes, every
    output-prefix line (``tip:``, ``例如:``, ``Ps:``) is rejected.
    """
    colon_idx = text.find(":")
    if colon_idx <= 0:
        return None

    name = text[:colon_idx]
    if " " in name:
        return None
    # Short-identifier rule. ``如果`` / ``正则`` are keywords handled
    # earlier; the assignment cap of two chars covers the long-tail of
    # real names (single CJK, single ASCII letter, ``Q1`` style two-
    # char ids) without absorbing prefix-style output text.
    if len(name) > 2:
        return None
    if name.startswith(("$", "%", "[", "±")):
        return None
    if name in ("如果", "正则"):
        return None

    value_text = text[colon_idx + 1 :]
    if not _looks_like_assignment_value(value_text):
        return None

    value = _parse_expr_text(value_text)
    return Assign(name=name, value=value, line=lineno)


def _looks_like_assignment_value(value: str) -> bool:
    """True iff ``value`` is something a QRDic author would assign.

    Acceptable shapes:

    * Empty string — ``$写 ... %x%$`` then ``x:`` clears x.
    * ``$func ...$`` — tool call result.
    * ``[arith]`` — arithmetic block.
    * ``%var%`` — variable copy.
    * ``@var[k]`` — JSON access.
    * ``{...}`` — literal JSON object (some rules seed dicts).
    * Pure numeric / scalar literal.
    * Any single-token value (no internal whitespace) — covers
      identifier-style copies (``B:true``, ``K:bold``), the rank
      tool's dash-encoded rows (``r:1-12345-1``), URLs, etc.

    Free-form text containing whitespace or Chinese sentence
    punctuation is treated as output. The whitespace check alone
    catches the bug we wanted to fix (``tip:只有双方互相申请才能``)
    while accepting every real assignment in the corpus.
    """
    stripped = value.strip()
    if stripped == "":
        return True
    first = stripped[0]
    if first in ("$", "[", "%", "@", "{"):
        return True
    # Single-token values (no internal whitespace, no Chinese sentence
    # punctuation) — these are unambiguously assignments. The Chinese
    # punctuation check is what stops ``tip:只有双方……\\n``-style output
    # from getting absorbed.
    return not any(ch.isspace() for ch in stripped) and not _has_output_punct(stripped)


# Punctuation that strongly signals "this is user-facing text, not a
# bare variable copy". Notably we do *not* include ``-``, ``_``, or
# the slash since those appear in identifiers, paths, and rank rows
# the rules legitimately assign. We also exclude ASCII period
# because URL-style values like ``https://x.com`` carry it; the
# leading ``$``/``[``/``%`` checks will accept those before this
# fallback anyway, so the period-allowance only matters for bare
# identifier copies which by definition shouldn't contain prose.
_OUTPUT_PUNCT_CHARS = "，。！？；：、（）【】「」《》…—"


def _has_output_punct(text: str) -> bool:
    return any(ch in _OUTPUT_PUNCT_CHARS for ch in text)


# ---------------------------------------------------------------------------
# Expression parsing
# ---------------------------------------------------------------------------


def _parse_expr_text(text: str) -> Expr:
    """Parse a text string as a single expression.

    If it contains multiple parts (interpolation), wraps in a composite.
    For simple cases, returns the appropriate Expr type directly.
    """
    # If the entire text is a single %var%
    if text.startswith("%") and text.endswith("%") and text.count("%") == 2:
        return VarRef(name=text[1:-1])

    # If the entire text is a single [expr]
    if text.startswith("[") and text.endswith("]") and _is_balanced_brackets(text):
        return ArithExpr(text=text[1:-1])

    # If the entire text is a single @var[field]...
    if text.startswith("@"):
        json_access = _try_parse_json_access(text)
        if json_access is not None:
            return json_access

    # If the entire text is a $func ...$
    if text.startswith("$") and text.endswith("$") and len(text) > 2:
        inner = text[1:-1]
        func_parts = inner.split(" ", 1)
        name = func_parts[0]
        func_args: list[Expr] = []
        if len(func_parts) > 1:
            func_args = _parse_func_args(func_parts[1])
        return FuncCallExpr(name=name, args=func_args)

    # Otherwise it's a literal (may contain interpolation, but for assignment
    # values we parse the whole thing as interpolated text and return
    # a single Literal if no interpolation found)
    interp_parts = _parse_interpolated_text(text)
    if len(interp_parts) == 1:
        return interp_parts[0]
    # Multiple parts in an assignment value — return as Literal for now
    # The runtime will handle interpolation of assignment values
    return Literal(value=text)


def _is_balanced_brackets(text: str) -> bool:
    """Check if brackets in text are balanced (for [expr] detection)."""
    depth = 0
    for ch in text:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                # If we close at depth 0 and there's more text, not a single [expr]
                return True
    return depth == 0


def _try_parse_json_access(text: str) -> JsonAccess | None:
    """Try to parse @var[field1][field2]... syntax."""
    if not text.startswith("@"):
        return None

    # Find the variable name (up to first [)
    bracket_start = text.find("[")
    if bracket_start < 0:
        return None

    var_name = text[1:bracket_start]
    if not var_name:
        return None

    # Parse the path segments [field1][field2]...
    path: list[str] = []
    rest = text[bracket_start:]
    while rest.startswith("["):
        close = rest.find("]")
        if close < 0:
            return None
        field = rest[1:close]
        path.append(field)
        rest = rest[close + 1 :]

    if rest:
        # There's trailing text after the last ], not a pure JsonAccess
        return None

    if not path:
        return None

    return JsonAccess(var=var_name, path=path)


# ---------------------------------------------------------------------------
# Interpolated text parsing
# ---------------------------------------------------------------------------


def _parse_interpolated_text(text: str) -> list[Expr]:
    """Parse text with interpolation markers into a list of Expr parts.

    Handles:
    - %var% → VarRef
    - $func args...$ → FuncCallExpr
    - [expr] → ArithExpr
    - @var[field] → JsonAccess
    - plain text → Literal

    QRSpeed-style ``\\%XX`` URL-encoded escapes (``\\%0A`` newline,
    ``\\%20`` space, ``\\%25`` percent, etc.) are decoded *before*
    the ``%`` scan so they don't get mistaken for ``%var%`` starts.
    The literal ``%`` (``\\%25``) is held as a PUA sentinel during
    the scan and swapped back to ``%`` in the resulting :class:`Literal`
    text so output stays correct.
    """
    text = _decode_url_escapes_for_parsing(text)
    parts: list[Expr] = []
    i = 0
    current_literal = ""

    while i < len(text):
        ch = text[i]

        # %var% interpolation
        if ch == "%" and i + 1 < len(text):
            end = text.find("%", i + 1)
            if end > i:
                if current_literal:
                    parts.append(Literal(value=current_literal))
                    current_literal = ""
                var_name = text[i + 1 : end]
                parts.append(VarRef(name=var_name))
                i = end + 1
                continue

        # $func args...$ interpolation
        if ch == "$" and i + 1 < len(text):
            end = text.find("$", i + 1)
            if end > i:
                if current_literal:
                    parts.append(Literal(value=current_literal))
                    current_literal = ""
                inner = text[i + 1 : end]
                func_parts = inner.split(" ", 1)
                name = func_parts[0]
                args: list[Expr] = []
                if len(func_parts) > 1:
                    args = _parse_func_args(func_parts[1])
                parts.append(FuncCallExpr(name=name, args=args))
                i = end + 1
                continue

        # [expr] arithmetic
        if ch == "[":
            close = _find_matching_bracket(text, i)
            if close > i:
                if current_literal:
                    parts.append(Literal(value=current_literal))
                    current_literal = ""
                expr_text = text[i + 1 : close]
                parts.append(ArithExpr(text=expr_text))
                i = close + 1
                continue

        # @var[field] JSON access
        if ch == "@" and i + 1 < len(text):
            json_end = _find_json_access_end(text, i)
            if json_end > i:
                if current_literal:
                    parts.append(Literal(value=current_literal))
                    current_literal = ""
                json_text = text[i:json_end]
                json_expr = _try_parse_json_access(json_text)
                if json_expr is not None:
                    parts.append(json_expr)
                    i = json_end
                    continue

        current_literal += ch
        i += 1

    if current_literal:
        parts.append(Literal(value=current_literal))

    # Swap any encoded ``%`` sentinels back to literal ``%`` in the
    # resulting Literal nodes — the sentinel was only there to keep
    # the ``%var%`` scan honest.
    if _PERCENT_SENTINEL in text:
        parts = [
            Literal(value=p.value.replace(_PERCENT_SENTINEL, "%")) if isinstance(p, Literal) else p
            for p in parts
        ]

    return parts if parts else [Literal(value="")]


def _find_matching_bracket(text: str, start: int) -> int:
    """Find the matching ] for a [ at position start."""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _find_json_access_end(text: str, start: int) -> int:
    """Find the end of a @var[field1][field2]... expression."""
    if not text[start:].startswith("@"):
        return -1

    i = start + 1
    # Read variable name (until [ or end)
    while i < len(text) and text[i] != "[":
        if text[i] in (" ", "%", "$", "±", "\n"):
            return -1
        i += 1

    if i >= len(text) or text[i] != "[":
        return -1

    # Read [field] segments
    while i < len(text) and text[i] == "[":
        close = text.find("]", i)
        if close < 0:
            return -1
        i = close + 1

    return i
