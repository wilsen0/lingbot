#!/usr/bin/env python3
"""QRDic → linling migration tool.

Migrates a full QRDic project (the legacy Android QRSpeed bot code) into
the linling project layout:

    bot/
      rules/main.ling        ← the migrated DSL script
      data.sqlite            ← KV store populated from Properties files
      migration_report.md    ← what migrated, what didn't, TODOs

Usage:

    python scripts/migrate_qrdic.py --src QRDic --out bot \\
        [--bot-id linling]

This script is read-only with respect to the source directory and
idempotent: re-running overwrites outputs cleanly. Designed to finish
under 30 seconds on the real QRDic tree (~2200 Properties files,
~10k-line dicpro.txt).
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import typer
from linling_core.storage.sqlite_kv import SqliteKVStore
from linling_dsl import parse
from linling_dsl.migrator import _parse_properties_text
from linling_dsl.parser import ParseError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Hardcoded substitutions that the task spec requires us to apply unconditionally.
DEFAULT_ADMIN_QQ = "2078123478"
ADMIN_PLACEHOLDER = "%管理员%"

# $BSH 图文.java imagettftext <text>$ — <text> may not contain $.
_BSH_TUTU_RE = re.compile(r"\$BSH 图文\.java imagettftext ([^$]*)\$")

# /storage/emulated/0/QR/QRDic/data/picture/<NAME>.<ext>  — leave extension so
# that downstream file-store can resolve per-format resources.
_PIC_PATH_RE = re.compile(
    r"/storage/emulated/0/QR/QRDic/data/picture/([^/\s±\$]+?\.(?:jpg|jpeg|png|gif))",
    re.IGNORECASE,
)

# .bak files are backups, skip entirely.
_SKIPPED_SUFFIXES = (".bak",)
# picture/ holds binary assets, not Properties — skip during KV ingestion.
_SKIPPED_DIRS = ("picture",)


# ---------------------------------------------------------------------------
# Report dataclass
# ---------------------------------------------------------------------------


@dataclass
class ParseFailure:
    """Record a handler block that failed to reparse."""

    index: int
    line: int
    trigger: str
    error: str
    source: str


@dataclass
class Substitution:
    """Record a single textual substitution applied to the script."""

    line: int
    kind: str  # "admin" | "pic" | "bsh"
    before: str
    after: str
    trigger: str


@dataclass
class MigrationReport:
    """Summary of a migration run."""

    kv_files_total: int = 0
    kv_files_migrated: int = 0
    kv_files_skipped: int = 0
    kv_rows_inserted: int = 0
    handlers_total: int = 0
    handlers_migrated: int = 0
    orphan_blocks_merged: int = 0
    parse_failures: list[ParseFailure] = field(default_factory=list)
    substitutions: list[Substitution] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # derived
    @property
    def substitution_counts(self) -> dict[str, int]:
        counts = {"admin": 0, "pic": 0, "bsh": 0}
        for s in self.substitutions:
            counts[s.kind] = counts.get(s.kind, 0) + 1
        return counts


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def scope_and_file_from_path(data_root: Path, file_path: Path) -> tuple[str, str] | None:
    """Given `QRDic/data/A/B/file`, return `("A/B", "file")`.

    Returns None if the file is directly under data/ (no scope) or under a
    skipped subtree.
    """
    rel_parts = file_path.relative_to(data_root).parts
    if len(rel_parts) < 2:
        return None
    if any(part in _SKIPPED_DIRS for part in rel_parts[:-1]):
        return None
    *scope_parts, file_name = rel_parts
    return "/".join(scope_parts), file_name


# ---------------------------------------------------------------------------
# Properties → KV
# ---------------------------------------------------------------------------


async def migrate_kv(src: Path, kv: SqliteKVStore, report: MigrationReport) -> None:
    """Walk `src/data/**` and import all non-`.bak` Properties files into `kv`.

    Each file is written inside a single transaction for performance.
    Errors are logged to the report; no file causes a crash.
    """
    data_root = src / "data"
    if not data_root.is_dir():
        report.warnings.append(f"no data directory at {data_root}; skipped KV migration")
        return

    for file_path in sorted(data_root.rglob("*")):
        if not file_path.is_file():
            continue
        if file_path.suffix in _SKIPPED_SUFFIXES:
            continue

        scope_file = scope_and_file_from_path(data_root, file_path)
        if scope_file is None:
            continue

        report.kv_files_total += 1
        scope, file_name = scope_file

        try:
            text = file_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            report.kv_files_skipped += 1
            report.warnings.append(f"skipped {file_path}: {exc}")
            continue

        try:
            props = _parse_properties_text(text)
        except Exception as exc:
            report.kv_files_skipped += 1
            report.warnings.append(f"parse error in {file_path}: {exc}")
            continue

        if not props:
            report.kv_files_migrated += 1
            continue

        try:
            async with kv.transaction() as tx:
                for key, value in props.items():
                    await tx.write(scope, file_name, key, value)
            report.kv_rows_inserted += len(props)
            report.kv_files_migrated += 1
        except Exception as exc:
            report.kv_files_skipped += 1
            report.warnings.append(f"write error in {file_path}: {exc}")


# ---------------------------------------------------------------------------
# DSL rewriting
# ---------------------------------------------------------------------------


def rewrite_block(
    block: str,
    block_start: int,
    trigger: str,
    report: MigrationReport,
) -> str:
    """Apply the four spec-mandated rewrites to a handler block.

    Logs each substitution to the report with line numbers adjusted to the
    position in the original `dicpro.txt`.
    """
    lines = block.split("\n")

    # Apply per-line so we can record accurate line numbers in the report.
    for i, line in enumerate(lines):
        original = line
        new_line = line

        # 1. $BSH 图文.java imagettftext <text>$ → $图文 <text>$
        new_line, n = _BSH_TUTU_RE.subn(r"$图文 \1$", new_line)
        if n:
            report.substitutions.append(
                Substitution(
                    line=block_start + i,
                    kind="bsh",
                    before="$BSH 图文.java imagettftext …$",
                    after="$图文 …$",
                    trigger=trigger,
                )
            )

        # 2. Picture path → @pic:<name>.<ext>
        new_line, n = _PIC_PATH_RE.subn(r"@pic:\1", new_line)
        if n:
            report.substitutions.append(
                Substitution(
                    line=block_start + i,
                    kind="pic",
                    before="/storage/emulated/0/QR/QRDic/data/picture/…",
                    after="@pic:…",
                    trigger=trigger,
                )
            )

        # 3. Hardcoded admin QQ. Guard against partial digit matches.
        new_line, n = re.subn(
            rf"(?<!\d){re.escape(DEFAULT_ADMIN_QQ)}(?!\d)",
            ADMIN_PLACEHOLDER,
            new_line,
        )
        if n:
            for _ in range(n):
                report.substitutions.append(
                    Substitution(
                        line=block_start + i,
                        kind="admin",
                        before=DEFAULT_ADMIN_QQ,
                        after=ADMIN_PLACEHOLDER,
                        trigger=trigger,
                    )
                )

        if new_line != original:
            lines[i] = new_line

    return "\n".join(lines)


def split_into_blocks(source: str) -> list[tuple[str, int]]:
    """Split a DSL source into handler-like blocks (blank-line separated).

    Returns list of ``(block_text, 1-based_start_line)``. Preserves all
    non-blank content, so round-tripping back to a file concatenates the
    blocks with blank separators.
    """
    blocks: list[tuple[str, int]] = []
    current: list[str] = []
    start = 1
    for i, line in enumerate(source.split("\n")):
        lineno = i + 1
        if line.strip() == "":
            if current:
                blocks.append(("\n".join(current), start))
                current = []
        else:
            if not current:
                start = lineno
            current.append(line)
    if current:
        blocks.append(("\n".join(current), start))
    return blocks


# Markers that can never legitimately be a handler trigger. When a block
# begins with one of these, it is almost certainly a QRDic handler body that
# got severed from its predecessor by a whitespace-only line or similar
# oddity; merge it back into the preceding block.
_ORPHAN_LINE_MARKERS: tuple[str, ...] = (
    "如果:",
    "正则:",
    "如果尾",
    "$jump",
    "$跳",
    "返回",
    "完成",
)


def _block_first_nonempty(block: str) -> str:
    for line in block.split("\n"):
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _looks_like_orphan(block: str) -> bool:
    """Return True if *block* cannot stand on its own as a handler.

    An orphan block starts with a body-only marker (``如果:``, ``返回`` …) or a
    bare label like ``:形象标记`` — things that make sense only when glued to
    the handler that came just before.
    """
    first = _block_first_nonempty(block)
    if not first:
        return False
    for marker in _ORPHAN_LINE_MARKERS:
        if first.startswith(marker):
            return True
    # Bare label: ":name" (not "::" or a free-standing colon).
    return first.startswith(":") and len(first) > 1 and first[1] != " "


def _merge_orphan_blocks(
    blocks: list[tuple[str, int]],
) -> tuple[list[tuple[str, int]], int]:
    """Collapse orphan blocks into their immediate predecessor.

    Returns ``(merged_blocks, merge_count)``. The predecessor's ``start_line``
    is preserved. Orphans with no predecessor are left in place (they will
    surface as parse errors just like before, which is the right signal).
    """
    merged: list[tuple[str, int]] = []
    merge_count = 0
    for block, start in blocks:
        if merged and _looks_like_orphan(block):
            prev_block, prev_start = merged[-1]
            merged[-1] = (prev_block + "\n" + block, prev_start)
            merge_count += 1
        else:
            merged.append((block, start))
    return merged, merge_count


def _block_trigger(block: str) -> str:
    """Extract a best-effort trigger string for reporting purposes."""
    first = block.split("\n", 1)[0]
    return first[:80]


def migrate_dsl(source: str, report: MigrationReport) -> str:
    """Rewrite every handler block, validating each via the parser.

    Returns the rewritten `.ling` text. Blocks that fail to reparse are
    still emitted verbatim (with rewrites applied) and recorded in the
    report as TODOs — dropping them would silently hide bugs.
    """
    raw_blocks = split_into_blocks(source)
    blocks, merged = _merge_orphan_blocks(raw_blocks)
    report.orphan_blocks_merged += merged
    report.handlers_total = sum(1 for b, _ in blocks if not b.lstrip().startswith("&&"))

    rewritten_blocks: list[str] = []
    for idx, (block, start_line) in enumerate(blocks, 1):
        trigger = _block_trigger(block)
        rewritten = rewrite_block(block, start_line, trigger, report)
        rewritten_blocks.append(rewritten)

        # && configuration lines are not handlers; do not count or validate.
        if block.lstrip().startswith("&&"):
            continue

        try:
            parse(rewritten, strict=False)
        except ParseError as exc:
            report.parse_failures.append(
                ParseFailure(
                    index=idx,
                    line=start_line,
                    trigger=trigger,
                    error=str(exc),
                    source=block,
                )
            )
            continue
        except Exception as exc:
            report.parse_failures.append(
                ParseFailure(
                    index=idx,
                    line=start_line,
                    trigger=trigger,
                    error=f"unexpected: {type(exc).__name__}: {exc}",
                    source=block,
                )
            )
            continue

        report.handlers_migrated += 1

    return "\n\n".join(rewritten_blocks) + "\n"


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def render_report(report: MigrationReport, bot_id: str, elapsed: float) -> str:
    """Render MigrationReport → markdown."""
    counts = report.substitution_counts
    ts = datetime.now().isoformat(timespec="seconds")
    lines: list[str] = []
    lines.append("# QRDic → linling migration report")
    lines.append("")
    lines.append(f"Generated: {ts}")
    lines.append(f"Bot ID: `{bot_id}`")
    lines.append(f"Elapsed: {elapsed:.2f}s")
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append(
        f"- KV files migrated: {report.kv_files_migrated} / {report.kv_files_total} "
        f"({report.kv_rows_inserted} rows inserted, {report.kv_files_skipped} skipped)"
    )
    lines.append(
        f"- Handlers migrated: {report.handlers_migrated} / {report.handlers_total}  "
        f"({len(report.parse_failures)} parse errors)"
    )
    lines.append(f"- Orphan blocks merged: {report.orphan_blocks_merged}")
    lines.append(
        f"- Hardcoded substitutions: {counts['admin']} author IDs, "
        f"{counts['pic']} picture paths, "
        f"{counts['bsh']} BSH 图文 calls"
    )
    lines.append("")

    lines.append("## Parse errors")
    lines.append("")
    if report.parse_failures:
        for fail in report.parse_failures:
            lines.append(f"### Handler #{fail.index} (line {fail.line})")
            lines.append(f"**Trigger**: `{fail.trigger}`")
            lines.append(f"**Error**: {fail.error}")
            lines.append("```dsl")
            lines.append(fail.source)
            lines.append("```")
            lines.append("")
    else:
        lines.append("_None._")
        lines.append("")

    lines.append("## Substitution log")
    lines.append("")
    if report.substitutions:
        # Group by kind for readability; within each kind, show up to 50 entries.
        for kind, label in (
            ("admin", "Admin QQ"),
            ("pic", "Picture path"),
            ("bsh", "BSH 图文 call"),
        ):
            items = [s for s in report.substitutions if s.kind == kind]
            if not items:
                continue
            lines.append(f"### {label} ({len(items)})")
            for s in items[:50]:
                lines.append(
                    f"- Line {s.line}: `{s.before}` → `{s.after}`  (in handler `{s.trigger}`)"
                )
            if len(items) > 50:
                lines.append(f"- …and {len(items) - 50} more.")
            lines.append("")
    else:
        lines.append("_None._")
        lines.append("")

    lines.append("## Warnings")
    lines.append("")
    if report.warnings:
        for w in report.warnings[:100]:
            lines.append(f"- {w}")
        if len(report.warnings) > 100:
            lines.append(f"- …and {len(report.warnings) - 100} more.")
    else:
        lines.append("_None._")
    lines.append("")

    lines.append("## Next steps")
    lines.append("")
    lines.append("- Review parse-error handlers above and fix DSL syntax.")
    lines.append("- Confirm that `@pic:<name>` references resolve via the future file store.")
    lines.append(
        "- Set `admin_users` in `bot.yaml` so `%管理员%` binds correctly."
    )
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Top-level migrate()
# ---------------------------------------------------------------------------


async def run_migration(src: Path, out: Path, bot_id: str) -> MigrationReport:
    """Execute the full migration. Idempotent — overwrites outputs cleanly."""
    if not src.is_dir():  # noqa: ASYNC240 — batch script, not a hot async loop
        raise typer.BadParameter(f"source not a directory: {src}")

    started = time.perf_counter()

    out.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 — batch script
    rules_dir = out / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)

    # Start clean for idempotency.
    db_path = out / "data.sqlite"
    if db_path.exists():
        db_path.unlink()
    # aiosqlite may leave WAL sidecars; remove them too.
    for sidecar in (db_path.with_suffix(".sqlite-wal"), db_path.with_suffix(".sqlite-shm")):
        if sidecar.exists():
            sidecar.unlink()

    report = MigrationReport()

    # --- Part 1: KV migration ---
    kv = SqliteKVStore(bot_id=bot_id, db_path=db_path)
    try:
        await migrate_kv(src, kv, report)
    finally:
        await kv.close()

    # --- Part 2: DSL rewrite ---
    dicpro = src / "dicpro.txt"
    if dicpro.is_file():
        try:
            raw = dicpro.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            report.warnings.append(f"failed to read {dicpro}: {exc}")
            raw = ""
        if raw:
            rewritten = migrate_dsl(raw, report)
            (rules_dir / "main.ling").write_text(rewritten, encoding="utf-8")
    else:
        report.warnings.append(f"no dicpro.txt at {dicpro}; skipped DSL migration")

    # --- Part 3: Report ---
    elapsed = time.perf_counter() - started
    (out / "migration_report.md").write_text(
        render_report(report, bot_id, elapsed), encoding="utf-8"
    )

    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

app = typer.Typer(
    help="Migrate a QRDic project into linling format.",
    add_completion=False,
)


@app.command()
def main(
    src: Path = typer.Option(  # noqa: B008
        ...,
        "--src",
        help="Path to the QRDic/ source directory (read-only).",
    ),
    out: Path = typer.Option(  # noqa: B008
        ...,
        "--out",
        help="Output directory (e.g. bot).",
    ),
    bot_id: str = typer.Option(
        "linling",
        "--bot-id",
        help="bot_id to tag KV rows with.",
    ),
) -> None:
    """Run the migration."""
    report = asyncio.run(run_migration(src, out, bot_id))
    typer.echo(
        f"Migrated {report.handlers_migrated}/{report.handlers_total} handlers, "
        f"{report.kv_rows_inserted} KV rows across "
        f"{report.kv_files_migrated}/{report.kv_files_total} files."
    )
    if report.parse_failures:
        typer.echo(
            f"⚠ {len(report.parse_failures)} handler(s) failed to reparse — "
            f"see {out / 'migration_report.md'}"
        )


if __name__ == "__main__":
    try:
        app()
    except typer.BadParameter as exc:
        typer.echo(f"error: {exc}", err=True)
        sys.exit(2)
