"""QRDic → linling migrator.

Converts an existing QRDic project (Android QRSpeed scripts + Properties
data files) into the linling format:
  - `.ling` scripts in `out_dir/rules/`
  - SQLite KV store at `out_dir/data/kv.db`
  - Picture resources in `out_dir/files/picture/`
  - `bot.yaml` skeleton at `out_dir/bot.yaml`
  - `migration_report.md` summarising the run.

Script transforms (simple regex rewrites; no AST round-trip):
  1. `$BSH 图文.java imagettftext <TEXT>$` → `$图文 <TEXT>$`
  2. `/storage/emulated/0/QR/QRDic/data/picture/<NAME>.jpg` → `@pic:<NAME>`
  3. Hardcoded admin QQ → `%管理员%` (if admin_qq configured)
  4. Hardcoded main group → `%主群%` (if main_group configured)
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from linling_core.storage.sqlite_kv import SqliteKVStore

# ---------------------------------------------------------------------------
# Config / Report
# ---------------------------------------------------------------------------


@dataclass
class MigrationConfig:
    """Configuration for a migration run."""

    src_dir: Path
    out_dir: Path
    bot_id: str = "linling"
    admin_placeholder: str = "%管理员%"
    main_group_placeholder: str = "%主群%"
    admin_qq: str = ""
    main_group: str = ""


@dataclass
class MigrationReport:
    """Summary of a migration run."""

    rules_written: int = 0
    rules_failed: int = 0
    kv_entries_written: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        """Render as a markdown report for migration_report.md."""
        lines: list[str] = []
        lines.append("# Migration Report")
        lines.append("")
        lines.append("## Summary")
        lines.append("")
        lines.append(f"- Handlers written: **{self.rules_written}**")
        lines.append(f"- Handlers failed: **{self.rules_failed}**")
        lines.append(f"- KV entries written: **{self.kv_entries_written}**")
        lines.append("")

        lines.append("## Warnings")
        lines.append("")
        if self.warnings:
            for w in self.warnings:
                lines.append(f"- {w}")
        else:
            lines.append("_None._")
        lines.append("")

        lines.append("## Errors")
        lines.append("")
        if self.errors:
            for e in self.errors:
                lines.append(f"- {e}")
        else:
            lines.append("_None._")
        lines.append("")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Script migration
# ---------------------------------------------------------------------------

# $BSH 图文.java imagettftext <text>$  — text may not contain $.
_BSH_TUTU_RE = re.compile(r"\$BSH 图文\.java imagettftext ([^$]*)\$")

# /storage/emulated/0/QR/QRDic/data/picture/<NAME>.jpg  — NAME has no slashes
# or common delimiter characters; stop at .jpg.
_PIC_PATH_RE = re.compile(
    r"/storage/emulated/0/QR/QRDic/data/picture/([^/\s±\$]+?)\.jpg",
    re.IGNORECASE,
)


def migrate_script(source: str, config: MigrationConfig) -> tuple[str, list[str]]:
    """Rewrite a QRDic ``dicpro.txt`` source string to .ling format.

    Returns ``(rewritten_source, warnings)``.

    Only the documented transformations are applied; whitespace, comments,
    and all other constructs are preserved verbatim.
    """
    warnings: list[str] = []
    if not source:
        return "", warnings

    # 1. $BSH 图文.java imagettftext X$ → $图文 X$
    result, bsh_n = _BSH_TUTU_RE.subn(r"$图文 \1$", source)
    if bsh_n:
        warnings.append(f"rewrote {bsh_n} BSH 图文 calls → $图文$")

    # 2. Hardcoded picture paths → @pic:<name>
    result, pic_n = _PIC_PATH_RE.subn(r"@pic:\1", result)
    if pic_n:
        warnings.append(f"rewrote {pic_n} hardcoded picture paths → @pic:...")

    # 3. Admin QQ → placeholder  (guard against digit-context matches)
    if config.admin_qq:
        pattern = re.compile(rf"(?<!\d){re.escape(config.admin_qq)}(?!\d)")
        result, admin_n = pattern.subn(config.admin_placeholder, result)
        if admin_n:
            warnings.append(
                f"replaced {admin_n} occurrences of admin QQ "
                f"'{config.admin_qq}' → {config.admin_placeholder}"
            )

    # 4. Main group → placeholder
    if config.main_group:
        pattern = re.compile(rf"(?<!\d){re.escape(config.main_group)}(?!\d)")
        result, grp_n = pattern.subn(config.main_group_placeholder, result)
        if grp_n:
            warnings.append(
                f"replaced {grp_n} occurrences of main group "
                f"'{config.main_group}' → {config.main_group_placeholder}"
            )

    return result, warnings


def _count_handlers(source: str) -> int:
    """Count handler blocks: blank-line-separated blocks whose first non-blank
    line is neither empty nor an ``&&`` config/comment line.
    """
    count = 0
    in_block = False
    for raw in source.split("\n"):
        line = raw.strip()
        if line == "":
            in_block = False
            continue
        if not in_block:
            in_block = True
            # The first line of the block is the trigger.
            if not line.startswith("&&"):
                count += 1
    return count


# ---------------------------------------------------------------------------
# Properties file parsing
# ---------------------------------------------------------------------------

_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")


def _decode_unicode_escapes(text: str) -> str:
    """Decode Java-style ``\\uXXXX`` escapes within a string."""

    def repl(m: re.Match[str]) -> str:
        return chr(int(m.group(1), 16))

    return _UNICODE_ESCAPE_RE.sub(repl, text)


def migrate_properties_file(path: Path) -> dict[str, str]:
    """Parse a Java Properties file into a plain ``{key: value}`` dict.

    Supports:
      * ``#`` and ``!`` comment lines
      * ``key=value`` / ``key:value`` / ``key=``
      * Whitespace separator (``key value``)
      * Backslash line continuations
      * ``\\uXXXX`` Unicode escapes in both keys and values
      * Common escape sequences (``\\n``, ``\\t``, ``\\r``, ``\\\\``)

    Binary files or files with invalid UTF-8 cause a ``UnicodeDecodeError``
    which the caller is expected to handle.
    """
    text = path.read_text(encoding="utf-8")
    return _parse_properties_text(text)


def _parse_properties_text(text: str) -> dict[str, str]:
    """Parse Java Properties content into a dict."""
    result: dict[str, str] = {}

    # Merge backslash line continuations.
    raw_lines = text.split("\n")
    logical: list[str] = []
    buf = ""
    for raw in raw_lines:
        line = raw.rstrip("\r")
        if buf:
            # Previous line ended with a continuation — strip leading
            # whitespace of the continuation line per Java spec.
            line = line.lstrip()
            buf += line
        else:
            buf = line
        # A trailing backslash indicates continuation — but only if not
        # escaped by another backslash. Count trailing backslashes.
        trailing = 0
        i = len(buf) - 1
        while i >= 0 and buf[i] == "\\":
            trailing += 1
            i -= 1
        if trailing % 2 == 1:
            buf = buf[:-1]  # drop the continuation backslash
            continue
        logical.append(buf)
        buf = ""
    if buf:
        logical.append(buf)

    for line in logical:
        stripped = line.lstrip()
        if stripped == "":
            continue
        if stripped[0] in ("#", "!"):
            continue
        key, value = _split_key_value(stripped)
        result[_decode_property(key)] = _decode_property(value)

    return result


def _split_key_value(line: str) -> tuple[str, str]:
    """Split a property line into ``(key, value)``.

    The separator is the first unescaped ``=``, ``:``, or whitespace run.
    """
    key_chars: list[str] = []
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == "\\" and i + 1 < n:
            key_chars.append(ch)
            key_chars.append(line[i + 1])
            i += 2
            continue
        if ch in ("=", ":") or ch in (" ", "\t", "\f"):
            break
        key_chars.append(ch)
        i += 1

    key = "".join(key_chars)

    # Skip whitespace, then optional = or :, then whitespace.
    while i < n and line[i] in (" ", "\t", "\f"):
        i += 1
    if i < n and line[i] in ("=", ":"):
        i += 1
    while i < n and line[i] in (" ", "\t", "\f"):
        i += 1

    value = line[i:]
    return key, value


def _decode_property(text: str) -> str:
    """Decode Java Properties escape sequences: ``\\n``, ``\\t``, ``\\r``,
    ``\\uXXXX``, ``\\\\``, ``\\=``, ``\\:``, ``\\ ``, ``\\#``, ``\\!``.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        nxt = text[i + 1]
        if nxt == "n":
            out.append("\n")
            i += 2
        elif nxt == "t":
            out.append("\t")
            i += 2
        elif nxt == "r":
            out.append("\r")
            i += 2
        elif nxt == "f":
            out.append("\f")
            i += 2
        elif nxt == "u" and i + 5 < n and _is_hex(text[i + 2 : i + 6]):
            out.append(chr(int(text[i + 2 : i + 6], 16)))
            i += 6
        else:
            # \X → X (drop the backslash)
            out.append(nxt)
            i += 2
    return "".join(out)


def _is_hex(s: str) -> bool:
    return len(s) == 4 and all(c in "0123456789abcdefABCDEF" for c in s)


# ---------------------------------------------------------------------------
# Data tree migration
# ---------------------------------------------------------------------------


async def migrate_data_tree(
    src: Path,
    kv: SqliteKVStore,
    *,
    skip_suffixes: tuple[str, ...] = (".bak",),
    skip_dirs: tuple[str, ...] = ("picture",),
) -> int:
    """Walk ``src/data/**`` and import every Properties file into ``kv``.

    Directory layout: ``src/data/<scope_parts...>/<file_name>``
      → scope ``= "/".join(scope_parts)``
      → file ``= file_name``
      → each ``K=V`` line becomes a KV row.

    Files whose name ends with any element of ``skip_suffixes`` are skipped
    silently; ``skip_dirs`` skips entire subtrees (e.g. ``picture/`` which
    holds binary assets).

    Returns the number of KV rows written.
    """
    data_root = src / "data"
    if not data_root.is_dir():
        return 0

    written = 0
    for file_path in sorted(data_root.rglob("*")):
        if not file_path.is_file():
            continue
        if file_path.suffix in skip_suffixes:
            continue
        # Skip files under any blacklisted subdir (e.g. picture/).
        rel_parts = file_path.relative_to(data_root).parts
        if any(part in skip_dirs for part in rel_parts[:-1]):
            continue

        *scope_parts, file_name = rel_parts
        if not scope_parts:
            # Top-level file directly under data/; skip — schema requires a
            # non-empty scope (data/<scope>/<file>).
            continue
        scope = "/".join(scope_parts)

        try:
            props = migrate_properties_file(file_path)
        except (UnicodeDecodeError, OSError):
            continue
        if not props:
            continue

        async with kv.transaction() as tx:
            for key, value in props.items():
                await tx.write(scope, file_name, key, value)
                written += 1

    return written


# ---------------------------------------------------------------------------
# Top-level migrate()
# ---------------------------------------------------------------------------


_BOT_YAML_TEMPLATE = """# linling bot config (migrated from QRDic)
bot_id: "{bot_id}"
name: "{bot_id}"
admin_users: [{admin}]
main_group: "{main_group}"

storage:
  kv: "sqlite:///./data/kv.db"
  files: "./files"

adapters: []

rules:
  - "rules/**/*.ling"
"""


async def migrate(config: MigrationConfig) -> MigrationReport:
    """Run the full migration from a QRDic project to linling format."""
    report = MigrationReport()

    out = config.out_dir
    src = config.src_dir

    rules_dir = out / "rules"
    data_dir = out / "data"
    files_picture_dir = out / "files" / "picture"
    rules_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    files_picture_dir.mkdir(parents=True, exist_ok=True)

    # --- 1. Migrate dicpro.txt ---------------------------------------------
    dicpro = src / "dicpro.txt"
    if dicpro.is_file():
        try:
            raw = dicpro.read_text(encoding="utf-8")
            rewritten, warn = migrate_script(raw, config)
            (rules_dir / "main.ling").write_text(rewritten, encoding="utf-8")
            report.rules_written = _count_handlers(rewritten)
            report.warnings.extend(warn)
        except (OSError, UnicodeDecodeError) as e:
            report.errors.append(f"failed to read {dicpro}: {e}")
            report.rules_failed += 1
    else:
        report.warnings.append(f"no dicpro.txt at {dicpro} — skipped script migration")

    # --- 2. Migrate Properties data tree -----------------------------------
    db_path = data_dir / "kv.db"
    kv = SqliteKVStore(bot_id=config.bot_id, db_path=db_path)
    try:
        try:
            report.kv_entries_written = await migrate_data_tree(src, kv)
        except Exception as e:
            report.errors.append(f"data tree migration error: {e}")
    finally:
        await kv.close()

    # --- 3. Copy picture files ---------------------------------------------
    src_pictures = src / "data" / "picture"
    if src_pictures.is_dir():
        for pic in src_pictures.iterdir():
            if not pic.is_file():
                continue
            dest = files_picture_dir / pic.name
            try:
                shutil.copyfile(pic, dest)
            except OSError as e:
                report.warnings.append(f"could not copy picture {pic.name}: {e}")

    # --- 4. Write bot.yaml skeleton ----------------------------------------
    admin = f'"{config.admin_qq}"' if config.admin_qq else ""
    bot_yaml = _BOT_YAML_TEMPLATE.format(
        bot_id=config.bot_id,
        admin=admin,
        main_group=config.main_group,
    )
    (out / "bot.yaml").write_text(bot_yaml, encoding="utf-8")

    # --- 5. Write migration_report.md --------------------------------------
    (out / "migration_report.md").write_text(report.to_markdown(), encoding="utf-8")

    return report
