<!-- Keep this terse; CI does the heavy lifting. -->

## Summary

<!-- One paragraph: what changed and why. -->

## Changes

<!-- Bullet list of the most relevant files / surfaces touched. -->
-

## Verification

- [ ] `uv run pytest -q`
- [ ] `uv run mypy`
- [ ] `uv run ruff check packages`
- [ ] `pnpm --filter @linling/webui-frontend typecheck`
- [ ] If the backend HTTP schema changed: `pnpm --filter @linling/webui-frontend api:update`

## Notes

<!-- Anything reviewers should know — perf concerns, follow-ups,
     tradeoffs deferred. -->
