"""Rule-file controller for the WebUI.

Exposes a narrow, audited surface for reading, writing, and linting
``.ling`` files on disk. The WebUI's ``/api/rules/files*`` endpoints
delegate to an instance of :class:`RuleFileController`.

Security model — two layers, both enforced by this class, never by the
HTTP layer:

1. **Path confinement**. A path the WebUI hands us must resolve to
   somewhere that matches at least one of the bot's configured rule
   globs (``bot.yaml`` ``rules:`` list). Symlinks that escape the tree
   are rejected.
2. **Suffix allow-list**. Only ``.ling`` files are readable/writable.
   Configuration files, secrets, or arbitrary binaries cannot be
   viewed through this channel.

Together those keep a compromised or malicious WebUI client from using
this as an arbitrary file-read/write primitive.

The controller does **not** own authentication — that's the WebUI's
``require_auth`` dependency. This class trusts its caller to have done
the RBAC check already.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import structlog
from linling_dsl.linter import Diagnostic, Severity, lint_source
from linling_dsl.parser import parse as parse_dsl

logger = structlog.get_logger(__name__)

ReloadFn = Callable[[], Awaitable[object]]


@dataclass(frozen=True)
class FileInfo:
    """Immutable metadata about one rule file."""

    path: str  # bot-relative, forward slashes
    size: int
    handler_count: int


@dataclass(frozen=True)
class LintFinding:
    """Serialisation-friendly copy of :class:`Diagnostic`."""

    line: int
    col: int
    code: str
    severity: str
    message: str

    @classmethod
    def from_diagnostic(cls, d: Diagnostic) -> LintFinding:
        return cls(
            line=d.line,
            col=d.col,
            code=d.code,
            severity=str(d.severity.value),
            message=d.message,
        )


class RuleFileController:
    """File-system facade for one bot's ``.ling`` rule tree."""

    def __init__(
        self,
        *,
        base_dir: Path,
        globs: list[str],
        reload_fn: ReloadFn | None = None,
    ) -> None:
        self._base = base_dir.resolve()
        self._globs = list(globs)
        self._reload_fn = reload_fn

    # ---- listing & reading --------------------------------------------

    def list_files(self) -> list[FileInfo]:
        """Enumerate every ``.ling`` file reachable by the configured globs.

        The handler count is the number of non-internal triggers
        discovered by a lenient parse — cheap (<10ms per file for
        typical scripts) and useful in the UI's file picker.
        """
        out: list[FileInfo] = []
        for path in self._iter_paths():
            try:
                stat = path.stat()
            except OSError:
                continue
            try:
                parsed = parse_dsl(path.read_text(encoding="utf-8"), strict=False)
                handler_count = len(parsed.handlers)
            except Exception:
                handler_count = 0
            out.append(
                FileInfo(
                    path=self._rel(path),
                    size=stat.st_size,
                    handler_count=handler_count,
                )
            )
        out.sort(key=lambda f: f.path)
        return out

    def read(self, rel_path: str) -> str:
        """Read one rule file's content as text."""
        path = self._resolve(rel_path)
        return path.read_text(encoding="utf-8")

    # ---- linting ------------------------------------------------------

    @staticmethod
    def lint(source: str) -> tuple[list[LintFinding], int]:
        """Lint a snippet and return ``(issues, handler_count)``.

        Accepts *any* DSL text — not tied to a particular file — so the
        WebUI can live-lint an editor buffer before the user saves.
        """
        report = lint_source(source)
        findings = [LintFinding.from_diagnostic(d) for d in report.sorted()]
        try:
            parsed = parse_dsl(source, strict=False)
            handler_count = len(parsed.handlers)
        except Exception:
            handler_count = 0
        return findings, handler_count

    # ---- writing ------------------------------------------------------

    async def save(
        self,
        rel_path: str,
        content: str,
        *,
        reload: bool = True,
        lint_first: bool = True,
    ) -> SaveResult:
        """Write ``content`` to ``rel_path``, optionally lint-gated and reload-triggering.

        When ``lint_first`` is True and the content has any ``error``
        severity diagnostic, the save is aborted and the errors are
        returned. Warnings never block a save — operators often want to
        iterate.

        A successful write followed by ``reload=True`` invokes the
        reload callback installed by :func:`attach_bot_to_webui`, which
        hot-swaps the running classifier + DSL dispatcher.
        """
        findings, handlers_in_new = self.lint(content)
        if lint_first and any(f.severity == Severity.ERROR.value for f in findings):
            logger.info(
                "rule_files.save_blocked_by_lint",
                path=rel_path,
                errors=sum(1 for f in findings if f.severity == Severity.ERROR.value),
            )
            return SaveResult(
                saved=False,
                issues=findings,
                reloaded=False,
                handlers=0,
            )

        path = self._resolve(rel_path, allow_missing=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        logger.info(
            "rule_files.saved",
            path=rel_path,
            bytes=len(content.encode("utf-8")),
            handlers_in_file=handlers_in_new,
        )

        reloaded = False
        handlers = 0
        if reload and self._reload_fn is not None:
            result = await self._reload_fn()
            if isinstance(result, dict):
                reloaded = bool(result.get("applied", True))
                raw_handlers = result.get("reloaded", result.get("handlers", 0))
                handlers = int(raw_handlers) if isinstance(raw_handlers, int) else 0
            else:
                reloaded = True
        return SaveResult(
            saved=True,
            issues=findings,
            reloaded=reloaded,
            handlers=handlers,
        )

    # ---- path resolution / confinement --------------------------------

    def _iter_paths(self) -> list[Path]:
        seen: set[Path] = set()
        out: list[Path] = []
        for pattern in self._globs:
            for path in self._base.glob(pattern):
                if not path.is_file():
                    continue
                if path.suffix != ".ling":
                    continue
                resolved = path.resolve()
                if resolved in seen:
                    continue
                if not self._is_within_base(resolved):
                    continue
                seen.add(resolved)
                out.append(resolved)
        return out

    def _resolve(self, rel_path: str, *, allow_missing: bool = False) -> Path:
        """Translate a WebUI-supplied relative path into an on-disk path.

        Raises :class:`PermissionError` on any of: absolute path,
        traversal outside the bot's base directory, or a suffix that
        isn't ``.ling``.
        """
        rel = rel_path.strip().replace("\\", "/")
        if not rel:
            raise PermissionError("empty rule path")
        if rel.startswith("/"):
            raise PermissionError("absolute paths are not allowed")
        if ".." in Path(rel).parts:
            raise PermissionError("path traversal is not allowed")
        if not rel.endswith(".ling"):
            raise PermissionError("only .ling files may be accessed")

        path = (self._base / rel).resolve()
        if not self._is_within_base(path):
            raise PermissionError("path escapes the bot's base directory")

        if not allow_missing and not path.is_file():
            raise FileNotFoundError(rel_path)
        return path

    def _is_within_base(self, path: Path) -> bool:
        try:
            path.relative_to(self._base)
        except ValueError:
            return False
        return True

    def _rel(self, path: Path) -> str:
        return str(path.relative_to(self._base)).replace("\\", "/")


@dataclass(frozen=True)
class SaveResult:
    """Return value of :meth:`RuleFileController.save`."""

    saved: bool
    issues: list[LintFinding]
    reloaded: bool
    handlers: int
