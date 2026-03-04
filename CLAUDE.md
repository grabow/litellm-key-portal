# CLAUDE.md

This repository uses `AGENTS.md` as the primary, tool-agnostic guidance for coding agents.
Claude Code should follow `AGENTS.md` first.

## Claude-Specific Notes

- Keep changes localized and avoid introducing extra abstraction unless it clearly reduces complexity.
- Prefer updating existing tests over adding duplicate coverage.
- When behavior or wording changes, keep `README.md` and visible UI text aligned with the implementation.
