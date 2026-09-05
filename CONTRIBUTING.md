# Contributing to linling

Thank you for your interest in contributing to **linling**! linling is a modular, high-performance bot engine combining rule-based DSL logic with ReAct LLM agent intelligence.

We welcome contributions of all kinds: bug fixes, new features, documentation improvements, tooling integrations, and community rules.

---

## Code of Conduct

Please help us foster an open, welcoming, diverse, and inclusive community. Be respectful and constructive in discussions, code reviews, and issue threads.

---

## Repository Structure

linling is organized as a monorepo:

```
linling/
├── packages/
│   ├── core/           # Core models, Event protocol, KV storage, Metrics
│   ├── dsl/            # Ling DSL lexer, parser, interpreter, AST
│   ├── agent/          # ReAct agent loop, tool dispatch, memory & profile
│   ├── adapters-onebot/# OneBot v11 WebSocket client
│   ├── adapters-cli/   # Interactive terminal adapter for testing
│   ├── tools-stdlib/   # Built-in tool library (gacha, search, weather, etc.)
│   ├── cli/            # `linling` CLI commands (run, check, lint, etc.)
│   └── webui/          # FastAPI backend & Vue 3 / Vite frontend
├── bot/                # Default production bot configuration and rules
├── docs/               # Comprehensive documentation and specifications
└── tests/              # End-to-end integration test suite
```

---

## Development Setup

### Prerequisites

- **Python**: 3.11, 3.12, or 3.13
- **uv**: Fast Python package and workspace manager ([astral.sh/uv](https://docs.astral.sh/uv/))
- **Node.js**: >= 20 (Node 22 recommended) and **pnpm** >= 9 (only needed for WebUI frontend work)

### 1. Clone & Install Dependencies

```bash
# Clone the repository
git clone https://github.com/your-username/linling.git
cd linling

# Install Python workspace dependencies and set up virtual environment
uv sync --all-packages

# Set up local environment file
cp .env.example .env
```

If you are developing the WebUI frontend:

```bash
# Install frontend dependencies
pnpm install
```

### 2. Running a Bot Locally (CLI Interactive Mode)

You can run the bot locally in interactive CLI mode without requiring a QQ account or OneBot server:

```bash
uv run linling run bot/bot.yaml
```

Type messages into your terminal to interact with the bot directly.

---

## Quality Standards & Tooling

All pull requests must pass our automated quality checks. You can run them locally:

### 1. Code Formatting

We use [Ruff](https://astral.sh/ruff) for code formatting:

```bash
# Check formatting
uv run ruff format --check packages

# Apply formatting
uv run ruff format packages
```

### 2. Linting

```bash
# Check linter rules
uv run ruff check packages

# Automatically fix fixable issues
uv run ruff check --fix packages
```

### 3. Type Checking

We use [mypy](https://mypy.readthedocs.io/) in strict mode:

```bash
uv run mypy
```

### 4. Automated Tests

```bash
# Run the full test suite
uv run pytest

# Run a specific package or test file
uv run pytest packages/agent/tests/
uv run pytest tests/test_e2e_router.py
```

### 5. Frontend Checks (WebUI)

```bash
# Frontend linting
pnpm --filter @linling/webui-frontend lint

# Frontend typechecking
pnpm --filter @linling/webui-frontend typecheck
```

---

## Pull Request Guidelines

1. **Create a branch**: Use descriptive branch names:
   - `feature/your-feature-name`
   - `fix/issue-description`
   - `docs/what-changed`
2. **Follow style conventions**:
   - Write clear, idiomatic Python with type hints.
   - Keep functions focused and well-scoped.
   - Use `structlog` for structured logging.
3. **Add tests**: Add unit tests for any new features or bug fixes.
4. **Run all checks**: Ensure `ruff`, `mypy`, and `pytest` pass before opening your PR.
5. **Write descriptive PR descriptions**: Explain *why* the change is needed and *what* was done.

---

## Security Disclosures

If you discover a security vulnerability, please do **not** open a public issue. Review our [Security Policy](SECURITY.md) for details on how to responsibly disclose vulnerabilities.
