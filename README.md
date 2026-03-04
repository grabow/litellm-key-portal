# LiteLLM Key Portal

Self-service portal for issuing LiteLLM API keys to students. Admins manage users, keys, mail actions, and resets through a protected admin area.

This project is intentionally separate from LiteLLM itself. A working LiteLLM installation is required, and this portal only needs the LiteLLM base URL and the LiteLLM master key. Running LiteLLM separately, for example in Docker, is recommended.

The web UI supports German and English. German remains the default for internal use; switch to English with `?lang=en` and back with `?lang=de`.

---

## Overview

- Self-service flow for students via email verification code
- Key rotation when a user verifies again
- Separate PostgreSQL database for portal state
- Admin UI for deleting keys, deleting users, setting max limits, exporting CSV, sending info mails, and deleting all students
- CLI scripts for test mail, bulk info mail, and student reset

---

## Requirements

- `uv` for installation and execution
- PostgreSQL for the portal database
- A separately running LiteLLM instance
- SMTP or Gmail credentials for email delivery

---

## Quick Start

```bash
# 1. Start the portal database
docker compose up -d

# 2. Create the Python environment and install dependencies
uv venv --python 3.12
uv sync --all-groups

# 3. Create the configuration
cp .env.example .env

# 4. Start the app
uv run uvicorn portal:app --reload --port 8080
```

Open locally:

- `http://127.0.0.1:8080/student`
- `http://127.0.0.1:8080/student?lang=en`
- `http://127.0.0.1:8080/admin`

---

## Configuration

Copy `.env.example` to `.env` and set real values.

Core variables:

- `LITELLM_BASE_URL`: LiteLLM base URL, for example `http://localhost:4000`
- `LITELLM_MASTER_KEY`: LiteLLM master key
- `DATABASE_URL`: PostgreSQL URL for this portal
- `ALLOWED_DOMAIN`: allowed email domain, for example `hs-offenburg.de`
- `STUDENT_BUDGET`: monthly max limit for student users
- `ADMIN_USERNAME` / `ADMIN_PASSWORD`: HTTP Basic Auth for `/admin`
- `TEST_INFO_EMAIL`: target address for the admin test mail button and the CLI test mail script
- `CODE_SECRET`: HMAC secret for verification codes, at least 32 characters

For email delivery, configure either:

- `GMAIL_USER` + `GMAIL_APP_KEY`
- or `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`

---

## Routes

| Route | Method | Purpose |
|---|---|---|
| `/student` | GET | Student landing page |
| `/student/enter-code` | GET | Direct code entry page |
| `/student/request-code` | POST | Send verification code |
| `/student/verify-and-get-key` | POST | Verify code and create or rotate key |
| `/admin` | GET | Admin dashboard (Basic Auth) |
| `/admin` | POST | Admin actions |
| `/admin/reset-students` | GET/POST | Separate protected delete-all-students page |
| `/admin/export` | GET | CSV export (Basic Auth) |
| `/health` | GET | Health check |

---

## Admin Area

`/admin` shows:

- email
- role
- current LiteLLM key (fetched live)
- code status
- available budget
- max limit
- registration timestamp

The table is visually limited to a scrollable area with a sticky header so large user lists remain manageable.

The admin UI also includes:

- `Send test info email`: sends `infomail.txt` to `TEST_INFO_EMAIL`
- `Send info email`: sends `infomail.txt` to all registered recipients
- `Delete students`: opens a separate protected page with the required `delete_all` confirmation
- `Set max limit for all students`: bulk-updates all student budgets in LiteLLM

---

## CLI Scripts

The mail and reset flows are already prepared for later cron usage. The cron integration itself is not configured yet, but the scripts are ready:

- `scripts/send_test_info_mail.py`
- `scripts/send_info_mail.py`
- `scripts/reset_students.py`

Examples:

```bash
# Preview only
uv run python scripts/send_test_info_mail.py --dry-run
uv run python scripts/send_info_mail.py --dry-run
uv run python scripts/reset_students.py --dry-run

# Live execution
uv run python scripts/send_test_info_mail.py --confirm
uv run python scripts/send_info_mail.py --confirm
uv run python scripts/reset_students.py --confirm
```

`infomail.txt` is the shared template file for both admin mail actions and the CLI mail scripts.

---

## Tests

```bash
docker compose up -d
uv sync --all-groups
uv run pytest tests/ -v
```

LiteLLM and email delivery are mocked in the test suite. Only the portal PostgreSQL database is required.
