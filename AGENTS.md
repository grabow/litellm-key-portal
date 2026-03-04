# AGENTS.md

This file is the primary guidance for coding agents working in this repository.

## Project Overview

LiteLLM Key Portal is a FastAPI self-service portal for issuing LiteLLM API keys.
Users request a verification code via institutional email, confirm the code, and receive a virtual API key managed by LiteLLM.

The current public self-service flow is limited to the `student` role.
An authenticated admin area is available for managing existing users and keys.

## Architecture

```text
User (student)
        |
        v
FastAPI Portal (VPN only)   <- portal.py
        |
        v
LiteLLM Proxy (Admin API, internal)
        |
        v
OpenAI / other configured providers
```

- `portal.py` contains the main FastAPI app, HTML rendering, mail delivery, validation, and LiteLLM integration.
- PostgreSQL stores verification codes and tracked portal users.
- LiteLLM user IDs are deterministic and use the pattern `student:<email>`.

## Operational Constraints

- Keep the LiteLLM admin API private. This service is intended to run behind VPN or equivalent network restrictions.
- Never store secrets in the repository. All credentials and tokens must stay in environment variables.
- When adding or changing environment variables, update `.env.example`.
- Preserve the email verification flow semantics unless the task explicitly changes them:
  - 6-digit code
  - 15-minute TTL
  - single-use codes
  - rate-limited endpoints

## Key Files

```text
portal.py                  # Main FastAPI application
pyproject.toml             # uv project and dependency definition
uv.lock                    # Locked dependency resolution
.env.example               # Environment variable template
tests/test_portal.py       # Main application tests
tests/test_helpers.py      # Helper-level tests
scripts/reset_students.py  # Semester cleanup utility
docker-compose.yml         # Local stack definition
README.md                  # User-facing project documentation
```

## Development

```bash
uv venv --python 3.12
uv sync --all-groups
cp .env.example .env
uv run uvicorn portal:app --host 0.0.0.0 --port 8080
```

Common commands:

```bash
uv run uvicorn portal:app --reload --port 8080
uv run pytest
uv run pytest tests/test_portal.py -v
```

## Change Guidelines

- Prefer small, explicit changes over broad refactors.
- Keep the generated HTML and copy consistent with the current product name: `LiteLLM Key Portal`.
- If you change behavior, update or add tests in `tests/`.
- If you change setup, deployment, or branding, update `README.md`.
