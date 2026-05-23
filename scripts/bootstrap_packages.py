#!/usr/bin/env python3
"""Scaffold remaining workspace packages.

Creates each package with:
- pyproject.toml (package-level)
- src/<pkg>/__init__.py
- src/<pkg>/version.py
- src/<pkg>/py.typed (marker)
- tests/__init__.py
- tests/test_smoke.py

Run once during P0. Idempotent; will skip existing files.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ROOT / "packages"

# pkg dir, dist name, import name, description, deps (beyond linling-core)
SPECS = [
    (
        "dsl",
        "linling-dsl",
        "linling_dsl",
        "linling DSL: parser, VM, migrator",
        ["linling-core", "lark>=1.1"],
    ),
    (
        "agent",
        "linling-agent",
        "linling_agent",
        "linling Agent framework: LLM providers, memory, tool-calling loop",
        ["linling-core", "httpx>=0.27"],
    ),
    (
        "adapters/onebot",
        "linling-adapter-onebot",
        "linling_adapter_onebot",
        "OneBot v11 (QQ) platform adapter",
        ["linling-core", "websockets>=12.0", "httpx>=0.27"],
    ),
    (
        "adapters/cli",
        "linling-adapter-cli",
        "linling_adapter_cli",
        "Local CLI adapter for debugging",
        ["linling-core"],
    ),
    (
        "tools-stdlib",
        "linling-tools-stdlib",
        "linling_tools_stdlib",
        "linling standard tool library (http, json, codec, image-text, …)",
        ["linling-core", "httpx>=0.27", "pillow>=10.4"],
    ),
    (
        "cli",
        "linling-cli",
        "linling_cli",
        "linling command-line entry point (`linling`)",
        ["linling-core", "linling-dsl", "linling-agent", "typer>=0.12", "rich>=13.7"],
    ),
]

PYPROJECT_TMPL = """\
[project]
name = "{dist}"
version = "0.0.0"
description = "{desc}"
requires-python = ">=3.11"
license = {{ text = "MIT" }}
dependencies = [
{deps_block}
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/{imp}"]
"""

INIT_TMPL = '''\
"""{dist} — part of linling.

{desc}
"""

from {imp}.version import __version__

__all__ = ["__version__"]
'''

VERSION_TMPL = '__version__ = "0.0.0"\n'

SMOKE_TMPL = """\
from {imp} import __version__


def test_version_is_a_string() -> None:
    assert isinstance(__version__, str)
"""

CLI_ENTRY = """\
[project.scripts]
ap = "linling_cli.main:app"
"""


def write(path: Path, content: str) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    print(f"  + {path.relative_to(ROOT)}")


def render_deps(deps: list[str]) -> str:
    return "\n".join(f'    "{d}",' for d in deps)


def bootstrap(pkg_dir: str, dist: str, imp: str, desc: str, deps: list[str]) -> None:
    print(f"[bootstrap] {dist}")
    base = PACKAGES / pkg_dir

    py_toml = PYPROJECT_TMPL.format(dist=dist, desc=desc, deps_block=render_deps(deps), imp=imp)
    # cli package gets an entry point
    if dist == "linling-cli":
        py_toml += "\n" + CLI_ENTRY

    write(base / "pyproject.toml", py_toml)
    write(base / "src" / imp / "__init__.py", INIT_TMPL.format(dist=dist, desc=desc, imp=imp))
    write(base / "src" / imp / "version.py", VERSION_TMPL)
    write(base / "src" / imp / "py.typed", "")
    write(base / "tests" / "__init__.py", "")
    write(base / "tests" / "test_smoke.py", SMOKE_TMPL.format(imp=imp))

    # linling-cli needs a main module so the entry point resolves
    if dist == "linling-cli":
        main_src = dedent(
            """\
            \"\"\"`linling` command-line entry point.\"\"\"

            from __future__ import annotations

            import typer

            from linling_cli import __version__

            app = typer.Typer(
                help="linling command-line interface",
                no_args_is_help=True,
            )


            @app.command()
            def version() -> None:
                \"\"\"Print the platform version.\"\"\"
                typer.echo(__version__)


            @app.command()
            def info() -> None:
                \"\"\"Show basic platform info.\"\"\"
                typer.echo(f"linling {__version__}")


            if __name__ == "__main__":
                app()
            """
        )
        write(base / "src" / imp / "main.py", main_src)


def main() -> None:
    for spec in SPECS:
        bootstrap(*spec)


if __name__ == "__main__":
    main()
