"""``linling lint`` — static analysis for ``.ling`` files."""

from __future__ import annotations

from pathlib import Path

import typer
from linling_dsl.linter import Diagnostic, Severity, lint_source
from rich.console import Console

console = Console()

_STYLE = {
    Severity.ERROR: "bold red",
    Severity.WARNING: "yellow",
    Severity.INFO: "cyan",
}


def _iter_ling_files(path: Path) -> list[Path]:
    """Return the ``.ling`` files reachable from ``path`` (recursive)."""
    if path.is_file():
        return [path]
    if path.is_dir():
        return sorted(path.rglob("*.ling"))
    return []


def _format_diagnostic(file: Path, diag: Diagnostic) -> tuple[str, str]:
    """Render one diagnostic as ``(styled, plain)`` strings for the console."""
    location = f"{file}:{diag.line}"
    style = _STYLE.get(diag.severity, "")
    styled = f"[{style}]{location}: {diag.severity.value} {diag.code}[/{style}] {diag.message}"
    plain = f"{location}: {diag.severity.value} {diag.code} {diag.message}"
    return styled, plain


def lint(
    paths: list[Path] = typer.Argument(  # noqa: B008
        ..., help="要检查的 .ling 文件或目录"
    ),
    strict_errors: bool = typer.Option(
        False,
        "--strict",
        help="遇到警告时也以非零状态退出",
    ),
) -> None:
    """Lint ``.ling`` rule files and exit non-zero on problems."""
    files: list[Path] = []
    for raw in paths:
        files.extend(_iter_ling_files(raw))

    if not files:
        console.print("[yellow]未找到 .ling 文件[/yellow]")
        raise typer.Exit(code=0)

    total_errors = 0
    total_warnings = 0
    total_infos = 0

    for file in files:
        try:
            source = file.read_text(encoding="utf-8")
        except OSError as exc:
            console.print(f"[bold red]{file}: read error: {exc}[/bold red]")
            total_errors += 1
            continue

        report = lint_source(source, filename=str(file))
        for diag in report.sorted():
            styled, _plain = _format_diagnostic(file, diag)
            console.print(styled)
            if diag.severity == Severity.ERROR:
                total_errors += 1
            elif diag.severity == Severity.WARNING:
                total_warnings += 1
            else:
                total_infos += 1

    console.print(
        f"\n[bold]共检查 {len(files)} 个文件:[/bold] "
        f"{total_errors} errors, {total_warnings} warnings, {total_infos} infos"
    )

    if total_errors > 0 or (strict_errors and total_warnings > 0):
        raise typer.Exit(code=1)
