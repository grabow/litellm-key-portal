# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Self-service portal for issuing LiteLLM API keys to students, professors, and administrators at Hochschule Offenburg. The portal verifies institutional email addresses, creates LiteLLM users with role prefixes, assigns monthly budgets, generates virtual API keys, and supports semester-based cleanup of student accounts.

## Architecture

```
User (student / professor / admin)
        |
        v
FastAPI Portal (VPN only)   ← portal.py
        |
        v
LiteLLM Proxy (Admin API, internal)
        |
        v
OpenAI
```

The portal communicates with LiteLLM via `MASTER_KEY` (never exposed to users). The portal must run inside VPN — LiteLLM admin port must not be publicly accessible.

## Role Model

LiteLLM user IDs follow this naming convention, enabling deterministic semester cleanup:

```
student:<email>    # e.g. student:alice@hs-offenburg.de
professor:<email>
admin:<email>
```

## Security Model

- **Email verification:** 6-digit code, 15-minute TTL, single-use, stored as HMAC hash
- **Budget control:** Monthly max budget per user, configurable via env vars, enforced by LiteLLM
- **Semester reset:** All `student:*` users and keys deleted via script/scheduled job
- **Secrets:** SMTP credentials and `MASTER_KEY` via environment variables only — no secrets in repo

## Repository Structure

```
portal.py          # Main FastAPI application
requirements.txt
.env.example       # Template — update when adding new env vars
scripts/           # Semester reset and maintenance scripts
docs/
```

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then fill in secrets
uvicorn portal:app --host 0.0.0.0 --port 8080
```

## Common Commands

```bash
# Development server with auto-reload
uvicorn portal:app --reload --port 8080

# Run tests
pytest

# Run a single test
pytest tests/test_foo.py::test_bar -v
```

## Production Notes

- SQLite is used for development; consider PostgreSQL for production
- Implement rate limiting before production rollout
- Portal must be VPN-restricted
