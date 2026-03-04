"""
LiteLLM Key Portal
Self-service portal for issuing LiteLLM API keys to students, professors, and admins.

URLs:
  GET  /student                  → Student landing page
  GET  /professor                → Professor landing page
  POST /{role}/request-code
  POST /{role}/verify-and-get-key
  GET  /admin           → Admin dashboard (Nutzerliste + CSV-Export)
  POST /admin           → Admin-Aktionen (delete-key, delete-user, update-budget, add-user)
  GET  /admin/reset-students
  POST /admin/reset-students
  GET  /admin/export    → CSV-Download
  GET  /health
"""

from __future__ import annotations

import asyncio
import base64
import csv
import hmac
import hashlib
import html
import io
import logging
import math
import os
import random
import smtplib
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import asyncpg
import httpx
from httpx import HTTPStatusError
from dotenv import load_dotenv
from fastapi import FastAPI, Form, Path, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from scripts import reset_students as reset_students_script

# ---------------------------------------------------------------------------
# Config – fail fast at startup
# ---------------------------------------------------------------------------
load_dotenv()


def _require(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise ValueError(f"Missing required environment variable: {key}")
    return val


LITELLM_BASE_URL = _require("LITELLM_BASE_URL").rstrip("/")
LITELLM_MASTER_KEY = _require("LITELLM_MASTER_KEY")

# E-Mail – Gmail hat Vorrang vor SMTP wenn beides gesetzt ist
GMAIL_USER = os.environ.get("GMAIL_USER", "").strip()
GMAIL_APP_KEY = os.environ.get("GMAIL_APP_KEY", "").replace(" ", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "").strip()
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "").strip()
SMTP_FROM = os.environ.get("SMTP_FROM", "").strip()

if GMAIL_USER and GMAIL_APP_KEY:
    _EMAIL_METHOD = "gmail"
elif SMTP_HOST and SMTP_USER and SMTP_PASSWORD and SMTP_FROM:
    _EMAIL_METHOD = "smtp"
else:
    raise ValueError(
        "E-Mail nicht konfiguriert: Setze GMAIL_USER+GMAIL_APP_KEY "
        "oder SMTP_HOST+SMTP_USER+SMTP_PASSWORD+SMTP_FROM."
    )

CODE_SECRET = _require("CODE_SECRET")
assert len(CODE_SECRET) >= 32, "CODE_SECRET must be at least 32 characters long"
ALLOWED_DOMAIN = _require("ALLOWED_DOMAIN")
STUDENT_BUDGET = float(os.environ.get("STUDENT_BUDGET", "5.00"))
PROFESSOR_BUDGET = float(os.environ.get("PROFESSOR_BUDGET", "20.00"))
ADMIN_BUDGET = float(os.environ.get("ADMIN_BUDGET", "50.00"))
RATE_LIMIT_REQUEST_CODE = os.environ.get("RATE_LIMIT_REQUEST_CODE", "5/minute")
RATE_LIMIT_VERIFY = os.environ.get("RATE_LIMIT_VERIFY", "10/minute")
DATABASE_URL = _require("DATABASE_URL")
ADMIN_USERNAME = _require("ADMIN_USERNAME")
ADMIN_PASSWORD = _require("ADMIN_PASSWORD")
TEST_INFO_EMAIL = os.environ.get("TEST_INFO_EMAIL", "").strip().lower()

# Debug-Logging
DEBUG = os.environ.get("DEBUG", "false").strip().lower() == "true"
_log_handlers: list[logging.Handler] = [logging.StreamHandler()]
if DEBUG:
    _log_handlers.append(logging.FileHandler("logging.txt", encoding="utf-8"))
logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.WARNING,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    handlers=_log_handlers,
    force=True,
)
logger = logging.getLogger("portal")

CODE_TTL_MINUTES = 15
CODE_COOLDOWN_MINUTES = int(os.environ.get("CODE_COOLDOWN_MINUTES", "5"))
ROUNDMAIL_PATH = os.path.join(os.path.dirname(__file__), "rundmail.txt")

ROLE_BUDGETS = {
    "student": STUDENT_BUDGET,
}

ROLE_LABELS = {
    "student": "Student:in",
}

# ---------------------------------------------------------------------------
# PostgreSQL schema
# ---------------------------------------------------------------------------
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS portal_verification_codes (
    id          SERIAL PRIMARY KEY,
    email       TEXT NOT NULL,
    role        TEXT NOT NULL,
    hashed_code TEXT NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    used        BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pvc_email_role ON portal_verification_codes(email, role);

CREATE TABLE IF NOT EXISTS portal_users (
    id         SERIAL PRIMARY KEY,
    email      TEXT NOT NULL,
    role       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (email, role)
);
"""


# ---------------------------------------------------------------------------
# Lifespan – DB pool init
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)
    app.state.pool = pool
    yield
    await pool.close()


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------
limiter = Limiter(key_func=get_remote_address)

app = FastAPI(lifespan=lifespan, title="LiteLLM Key Portal")
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> Response:
    return HTMLResponse(
        render_error("Zu viele Anfragen. Bitte warten Sie einen Moment und versuchen Sie es erneut."),
        status_code=429,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_rng = random.SystemRandom()


def generate_code() -> str:
    return f"{_rng.randint(0, 999999):06d}"


def hash_code(code: str) -> str:
    return hmac.new(CODE_SECRET.encode(), code.encode(), hashlib.sha256).hexdigest()


def verify_code(submitted: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_code(submitted), stored_hash)


def validate_email(email: str, role: str) -> tuple[bool, str]:
    if not email or len(email) > 254:
        return False, "Ungültige E-Mail-Adresse."
    if "@" not in email:
        return False, "Ungültige E-Mail-Adresse."
    if any(c in email for c in ("\r", "\n", "\x00")):
        return False, "Ungültige E-Mail-Adresse."
    domain = email.split("@", 1)[1]
    if domain != ALLOWED_DOMAIN:
        return False, f"Nur E-Mail-Adressen der Domain @{ALLOWED_DOMAIN} sind erlaubt."
    return True, ""


def send_plain_email(to: str, subject: str, text_body: str) -> None:
    if not to or any(c in to for c in ("\r", "\n", "\x00")):
        raise ValueError("Ungueltige Empfaengeradresse.")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["To"] = to
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    if _EMAIL_METHOD == "gmail":
        msg["From"] = GMAIL_USER
        logger.debug("Sende E-Mail via Gmail an %s", to)
        with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
            smtp.starttls()
            smtp.login(GMAIL_USER, GMAIL_APP_KEY)
            smtp.sendmail(GMAIL_USER, [to], msg.as_string())
    else:
        msg["From"] = SMTP_FROM
        logger.debug("Sende E-Mail via SMTP an %s", to)
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
            smtp.starttls()
            smtp.login(SMTP_USER, SMTP_PASSWORD)
            smtp.sendmail(SMTP_FROM, [to], msg.as_string())


def send_verification_email(to: str, code: str, role: str) -> None:
    text_body = (
        f"Hallo,\n\n"
        f"Ihr Bestätigungscode für das LiteLLM API-Portal ({ROLE_LABELS.get(role, role)}) lautet:\n\n"
        f"  {code}\n\n"
        f"Der Code ist {CODE_TTL_MINUTES} Minuten gültig.\n\n"
        f"Wenn Sie diese Anfrage nicht gestellt haben, ignorieren Sie diese E-Mail.\n\n"
        f"Hochschule – KI-Portal"
    )
    send_plain_email(to, f"Ihr LiteLLM-Bestätigungscode ({code})", text_body)
    logger.info("Bestätigungscode gesendet: email=%s role=%s", to, role)


def _load_roundmail_text() -> str:
    try:
        with open(ROUNDMAIL_PATH, "r", encoding="utf-8") as handle:
            text_body = handle.read().strip()
    except OSError as exc:
        raise RuntimeError(f"Rundmail-Datei nicht lesbar: {exc}") from exc
    if not text_body:
        raise ValueError("Rundmail-Datei ist leer.")
    return text_body


def send_inform_email(recipients: list[str]) -> int:
    unique_recipients = sorted({email.strip().lower() for email in recipients if email and email.strip()})
    if not unique_recipients:
        return 0

    text_body = _load_roundmail_text()
    subject = "Wichtige Information zu Ihrem LiteLLM API-Key"

    for recipient in unique_recipients:
        send_plain_email(recipient, subject, text_body)

    logger.info("Info-E-Mails gesendet: count=%s", len(unique_recipients))
    return len(unique_recipients)


async def litellm_user_exists(user_id: str) -> bool:
    headers = {"Authorization": f"Bearer {LITELLM_MASTER_KEY}"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{LITELLM_BASE_URL}/user/info",
            params={"user_id": user_id},
            headers=headers,
        )
        return resp.status_code == 200


async def litellm_create_user(user_id: str, budget: float) -> dict:
    headers = {"Authorization": f"Bearer {LITELLM_MASTER_KEY}"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{LITELLM_BASE_URL}/user/new",
            json={"user_id": user_id, "max_budget": budget, "budget_duration": "30d"},
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json()


async def litellm_generate_key(user_id: str, budget: float) -> str:
    headers = {"Authorization": f"Bearer {LITELLM_MASTER_KEY}"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{LITELLM_BASE_URL}/key/generate",
            json={
                "user_id": user_id,
                "max_budget": budget,
                "budget_duration": "30d",
                "metadata": {"portal": "hsog-litellm-key-portal"},
            },
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json()["key"]


async def litellm_get_user_key_tokens(user_id: str) -> list[str]:
    headers = {"Authorization": f"Bearer {LITELLM_MASTER_KEY}"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{LITELLM_BASE_URL}/user/info",
            params={"user_id": user_id},
            headers=headers,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        keys = data.get("keys", [])
        return [k["token"] if isinstance(k, dict) else k for k in keys]


async def litellm_delete_keys(tokens: list[str]) -> None:
    if not tokens:
        return
    headers = {"Authorization": f"Bearer {LITELLM_MASTER_KEY}"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{LITELLM_BASE_URL}/key/delete",
            json={"keys": tokens},
            headers=headers,
        )
        resp.raise_for_status()


async def litellm_delete_user(user_id: str) -> None:
    headers = {"Authorization": f"Bearer {LITELLM_MASTER_KEY}"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{LITELLM_BASE_URL}/user/delete",
            json={"user_ids": [user_id]},
            headers=headers,
        )
        resp.raise_for_status()


async def litellm_update_budget(user_id: str, budget: float) -> None:
    headers = {"Authorization": f"Bearer {LITELLM_MASTER_KEY}"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{LITELLM_BASE_URL}/user/update",
            json={"user_id": user_id, "max_budget": budget, "budget_duration": "30d"},
            headers=headers,
        )
        resp.raise_for_status()


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: system-ui, sans-serif; background: #f4f6f8; color: #222;
       display: flex; justify-content: center; align-items: flex-start;
       min-height: 100vh; padding: 2rem 1rem; }
.card { background: #fff; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,.12);
        max-width: 480px; width: 100%; padding: 2rem; }
h1 { font-size: 1.4rem; margin-bottom: 0.25rem; color: #1a4f8a; }
.subtitle { font-size: 0.85rem; color: #666; margin-bottom: 0.5rem; }
.role-badge { display: inline-block; background: #e8f0fe; color: #1a4f8a;
              border-radius: 4px; padding: 0.2rem 0.6rem; font-size: 0.8rem;
              font-weight: 600; margin-bottom: 1.5rem; }
label { display: block; font-size: 0.9rem; font-weight: 600; margin-bottom: 0.25rem; }
input { width: 100%; padding: 0.55rem 0.75rem; border: 1px solid #ccc;
        border-radius: 5px; font-size: 1rem; margin-bottom: 1rem; }
button { width: 100%; padding: 0.65rem; background: #1a4f8a; color: #fff; border: none;
         border-radius: 5px; font-size: 1rem; cursor: pointer; font-weight: 600; }
button:hover { background: #163f6e; }
.error { background: #fde8e8; border: 1px solid #f5aca6; border-radius: 5px;
         padding: 0.75rem 1rem; margin-bottom: 1rem; color: #8b1a1a; }
.success { background: #e6f4ea; border: 1px solid #a8d5b5; border-radius: 5px;
           padding: 0.75rem 1rem; margin-bottom: 1rem; color: #1a5c2a; }
.key-box { background: #f0f4ff; border: 1px solid #b0c4ef; border-radius: 5px;
           padding: 1rem; font-family: monospace; word-break: break-all;
           font-size: 0.9rem; margin: 1rem 0; }
.hint { font-size: 0.8rem; color: #555; margin-top: 0.5rem; }
"""


def render_base(title: str, role: str, body_html: str) -> str:
    label = ROLE_LABELS.get(role, role)
    return f"""<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>{_CSS}</style>
</head>
<body>
  <div class="card">
    <h1>LiteLLM Key Portal</h1>
    <p class="subtitle">Hochschule – KI-Dienste</p>
    <span class="role-badge">{label}</span>
    {body_html}
  </div>
</body>
</html>"""


def render_error(msg: str, role: str = "") -> str:
    back = f'<a href="/{html.escape(role)}">← Zurück</a>' if role else '<a href="/">← Zurück</a>'
    return render_base(
        "Fehler – LiteLLM Key Portal", role or "student",
        f'<div class="error">{html.escape(msg)}</div>{back}',
    )


def render_landing(role: str) -> str:
    label = ROLE_LABELS.get(role, role)
    body = f"""
<form method="post" action="/{role}/request-code">
  <label for="email">Hochschul-E-Mail-Adresse</label>
  <input type="email" id="email" name="email" required
         placeholder="vorname.nachname@hs-offenburg.de">
  <button type="submit">Bestätigungscode anfordern</button>
</form>
<p class="hint">
  Wenn bereits ein API-Schlüssel für diese E-Mail existiert, wird er nach erfolgreicher
  Bestätigung durch einen neuen Schlüssel ersetzt.
</p>
<p class="hint">
  Sie haben den Code bereits? <a href="/{role}/enter-code">Code hier eingeben</a>
</p>
"""
    return render_base(f"LiteLLM Key Portal – {label}", role, body)


def render_enter_code(role: str, email: str = "") -> str:
    esc = html.escape(email, quote=True)
    body = f"""
<p class="hint">
  Geben Sie hier Ihre Hochschul-E-Mail-Adresse und den 6-stelligen Bestätigungscode ein.
</p>
<form method="post" action="/{role}/verify-and-get-key">
  <label for="email">Hochschul-E-Mail-Adresse</label>
  <input type="email" id="email" name="email" required
         value="{esc}" placeholder="vorname.nachname@hs-offenburg.de">
  <label for="code">Bestätigungscode (6 Stellen)</label>
  <input type="text" id="code" name="code" required maxlength="6" pattern="[0-9]{{6}}"
         placeholder="123456" inputmode="numeric" autocomplete="one-time-code"
         oninput="this.value=this.value.replace(/\\D/g,'').slice(0,6)">
  <button type="submit">Code bestätigen &amp; API-Schlüssel erstellen oder erneuern</button>
</form>
"""
    return render_base("Code eingeben – LiteLLM Key Portal", role, body)


def render_code_sent(role: str, email: str) -> str:
    esc = html.escape(email, quote=True)
    body = f"""
<div class="success">
  Ein 6-stelliger Bestätigungscode wurde an <strong>{esc}</strong> gesendet.
  Der Code ist {CODE_TTL_MINUTES} Minuten gültig.
</div>
<p class="hint">
  Nach der Bestätigung wird ein neuer API-Schlüssel erstellt. Ein vorhandener Schlüssel
  für diese E-Mail wird dabei automatisch ersetzt.
</p>
<form method="post" action="/{role}/verify-and-get-key">
  <input type="hidden" name="email" value="{esc}">
  <label for="code">Bestätigungscode (6 Stellen)</label>
  <input type="text" id="code" name="code" required maxlength="6" pattern="[0-9]{{6}}"
         placeholder="123456" inputmode="numeric" autocomplete="one-time-code"
         oninput="this.value=this.value.replace(/\\D/g,'').slice(0,6)">
  <button type="submit">Code bestätigen &amp; API-Schlüssel erstellen oder erneuern</button>
</form>
"""
    return render_base("Code eingeben – LiteLLM Key Portal", role, body)


def render_key_issued(role: str, email: str, key: str) -> str:
    budget = ROLE_BUDGETS.get(role, 0)
    esc_email = html.escape(email)
    esc_key = html.escape(key)
    body = f"""
<div class="success">Ihr API-Schlüssel wurde erfolgreich erstellt bzw. erneuert.</div>
<p>Ihr persönlicher LiteLLM API-Schlüssel:</p>
<div class="key-box">{esc_key}</div>
<p class="hint">
  E-Mail: {esc_email}<br>
  Budget: <strong>{budget:.2f} €/Monat</strong><br><br>
  Falls bereits ein Schlüssel vorhanden war, wurde er ersetzt.<br><br>
  <strong>Bitte speichern Sie diesen Schlüssel sicher.</strong>
  Er wird Ihnen nur einmal angezeigt.<br><br>
  API-Endpunkt: <code>{LITELLM_BASE_URL}</code>
</p>
"""
    return render_base("API-Schlüssel erhalten – LiteLLM Key Portal", role, body)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

# Rollen die sich selbst registrieren dürfen (Self-Service)
SELF_SERVICE_ROLES = {"student"}


def _check_role(role: str) -> HTMLResponse | None:
    if role not in SELF_SERVICE_ROLES:
        return HTMLResponse(render_error(f"Unbekannte Rolle: {role}"), status_code=404)
    return None


@app.get("/health")
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Basic Auth helper
# ---------------------------------------------------------------------------

_UNAUTHORIZED = Response(
    status_code=401,
    headers={"WWW-Authenticate": 'Basic realm="Admin-Bereich"'},
)


def _check_basic_auth(request: Request) -> bool:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode()
        username, _, password = decoded.partition(":")
    except Exception:
        return False
    return hmac.compare_digest(username, ADMIN_USERNAME) and hmac.compare_digest(password, ADMIN_PASSWORD)


# ---------------------------------------------------------------------------
# Admin overview helpers
# ---------------------------------------------------------------------------

async def _fetch_litellm_info(user_id: str) -> dict:
    """Gibt masked key_name, verfügbares Budget und Max-Limit eines LiteLLM-Users zurück."""
    headers = {"Authorization": f"Bearer {LITELLM_MASTER_KEY}"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{LITELLM_BASE_URL}/user/info",
                params={"user_id": user_id},
                headers=headers,
            )
            if resp.status_code != 200:
                return {"key": "-", "available_budget": "-", "max_budget": "-"}
            data = resp.json()
            keys = data.get("keys", [])
            first = keys[0] if keys else {}
            key_name = (first.get("key_name") or first.get("token") or "-") if isinstance(first, dict) else "-"
            user_info = data.get("user_info") or data
            max_budget = user_info.get("max_budget")
            spend = user_info.get("spend")

            max_budget_str = f"{max_budget:.2f} €" if max_budget is not None else "-"
            if max_budget is not None and spend is not None:
                available_budget = max_budget - spend
                available_budget_str = f"{available_budget:.2f} €"
            else:
                available_budget_str = "-"

            return {
                "key": key_name,
                "available_budget": available_budget_str,
                "max_budget": max_budget_str,
            }
    except Exception:
        return {"key": "-", "available_budget": "-", "max_budget": "-"}


async def _build_rows(pool: asyncpg.Pool) -> list[dict]:
    """Fetch all portal_users, active codes, and masked keys from LiteLLM."""
    now = datetime.now(timezone.utc)

    async with pool.acquire() as conn:
        users = await conn.fetch(
            "SELECT email, role, created_at FROM portal_users ORDER BY email ASC, role ASC"
        )
        codes = await conn.fetch(
            """
            SELECT DISTINCT ON (email, role) email, role, expires_at
            FROM portal_verification_codes
            WHERE used = FALSE AND expires_at > $1
            ORDER BY email, role, id DESC
            """,
            now,
        )

    active_codes = {(r["email"], r["role"]): r["expires_at"] for r in codes}

    # Key + Budget parallel von LiteLLM abrufen
    user_ids = [f"{u['role']}:{u['email']}" for u in users]
    infos = await asyncio.gather(*[_fetch_litellm_info(uid) for uid in user_ids])

    rows = []
    for user, info in zip(users, infos):
        expires_at = active_codes.get((user["email"], user["role"]))
        if expires_at:
            remaining = math.ceil((expires_at.replace(tzinfo=timezone.utc) - now).total_seconds() / 60)
            code_status = f"aktiv ({remaining} min)"
        else:
            code_status = "-"
        rows.append({
            "email": user["email"],
            "role": user["role"],
            "key": info["key"],
            "available_budget": info["available_budget"],
            "max_budget": info["max_budget"],
            "code_status": code_status,
            "created_at": user["created_at"].strftime("%Y-%m-%d %H:%M"),
        })
    return rows


async def _get_roundmail_recipients(pool: asyncpg.Pool) -> list[str]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT DISTINCT email FROM portal_users WHERE email <> '' ORDER BY email"
        )
    return [row["email"] for row in rows]


async def _get_student_emails(pool: asyncpg.Pool) -> list[str]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT DISTINCT email FROM portal_users WHERE role = 'student' AND email <> '' ORDER BY email"
        )
    return [row["email"] for row in rows]


def render_admin_overview(rows: list[dict], flash: str = "") -> str:
    count = len(rows)
    table_rows = ""
    for r in rows:
        email = r["email"]
        role = r["role"]
        key = r["key"]
        key_short = key[:20] + "…" if len(key) > 20 else key
        role_label = ROLE_LABELS.get(role, role)
        # Escape all user-controlled values for safe HTML embedding
        available_budget = r["available_budget"]
        max_budget = r["max_budget"]
        e = html.escape(email, quote=True)
        k = html.escape(key, quote=True)
        ks = html.escape(key_short)
        cs = html.escape(r["code_status"])
        abs_ = html.escape(available_budget)
        mbs = html.escape(max_budget)
        table_rows += (
            f"<tr>"
            f"<td>{e}</td>"
            f"<td>{role_label}</td>"
            f"<td class='mono' title='{k}'>{ks}</td>"
            f"<td>{cs}</td>"
            f"<td>{abs_}</td>"
            f"<td>{mbs}</td>"
            f"<td>{r['created_at']}</td>"
            f"<td>"
            f"<form method='post' action='/admin'"
            f" onsubmit=\"return confirm('Key löschen?')\">"
            f"<input type='hidden' name='action' value='delete-key'>"
            f"<input type='hidden' name='email' value='{e}'>"
            f"<input type='hidden' name='role' value='{role}'>"
            f"<button class='btn-sm btn-warn'>Key löschen</button></form>"
            f"</td>"
            f"<td>"
            f"<form method='post' action='/admin'"
            f" onsubmit=\"return confirm('Nutzer komplett löschen?')\">"
            f"<input type='hidden' name='action' value='delete-user'>"
            f"<input type='hidden' name='email' value='{e}'>"
            f"<input type='hidden' name='role' value='{role}'>"
            f"<button class='btn-sm btn-danger'>Nutzer löschen</button></form>"
            f"</td>"
            f"<td>"
            f"<form method='post' action='/admin' style='display:flex;gap:4px'>"
            f"<input type='hidden' name='action' value='update-budget'>"
            f"<input type='hidden' name='email' value='{e}'>"
            f"<input type='hidden' name='role' value='{role}'>"
            f"<input type='number' name='budget' min='0' step='0.01' placeholder='€'"
            f" style='width:70px;padding:2px 4px;font-size:0.8rem'>"
            f"<button class='btn-sm btn-neutral'>Setzen</button></form>"
            f"</td>"
            f"</tr>\n"
        )

    role_options = "".join(
        f'<option value="{role}">{label}</option>' for role, label in ROLE_LABELS.items()
    )

    flash_html = f'<div class="success" style="margin-bottom:1rem">{flash}</div>' if flash else ""

    css_extra = """
body { max-width: 100%; }
.card { max-width: 1200px; }
.table-scroll { margin-top: 1rem; max-height: 70vh; overflow-y: auto; overflow-x: auto;
                border: 1px solid #d9e2ef; border-radius: 6px; }
table { width: 100%; border-collapse: collapse; margin-top: 0; font-size: 0.83rem; }
th { background: #1a4f8a; color: #fff; padding: 0.5rem 0.6rem; text-align: left;
     white-space: nowrap; position: sticky; top: 0; z-index: 1; }
td { padding: 0.4rem 0.6rem; border-bottom: 1px solid #e0e0e0; vertical-align: middle; }
tr:hover td { background: #f0f4ff; }
.mono { font-family: monospace; }
.topbar { display: flex; gap: 0.75rem; align-items: center; margin-bottom: 1rem; flex-wrap: wrap; }
.btn-csv { padding: 0.4rem 0.9rem; background: #2e7d32; color: #fff; border: none;
           border-radius: 5px; font-size: 0.85rem; cursor: pointer; text-decoration: none; }
.btn-link-danger { padding: 0.4rem 0.9rem; background: #b71c1c; color: #fff; border: none;
                   border-radius: 5px; font-size: 0.85rem; cursor: pointer; text-decoration: none; }
.btn-mail { padding: 0.4rem 0.9rem; background: #6a1b9a; color: #fff; border: none;
            border-radius: 5px; font-size: 0.85rem; cursor: pointer; }
.count { font-size: 0.9rem; color: #555; }
.btn-sm { padding: 0.25rem 0.5rem; border: none; border-radius: 4px;
          font-size: 0.78rem; cursor: pointer; white-space: nowrap; }
.btn-warn   { background: #e65100; color: #fff; }
.btn-danger { background: #b71c1c; color: #fff; }
.btn-neutral { background: #1a4f8a; color: #fff; }
.btn-add    { background: #1a4f8a; color: #fff; padding: 0.4rem 0.9rem;
              border: none; border-radius: 5px; font-size: 0.85rem; cursor: pointer; }
.add-section { margin-top: 2rem; padding-top: 1.5rem; border-top: 1px solid #ddd; }
.add-section h2 { font-size: 1rem; margin-bottom: 1rem; color: #1a4f8a; }
.add-form { display: flex; gap: 0.5rem; flex-wrap: wrap; align-items: flex-end; }
.add-form label { font-size: 0.85rem; font-weight: 600; }
.add-form input, .add-form select { padding: 0.4rem 0.6rem; border: 1px solid #ccc;
  border-radius: 4px; font-size: 0.85rem; width: auto; margin-bottom: 0; }
.bulk-form { display: flex; gap: 0.5rem; flex-wrap: wrap; align-items: flex-end; }
.bulk-form label { font-size: 0.85rem; font-weight: 600; }
.bulk-form input { padding: 0.4rem 0.6rem; border: 1px solid #ccc;
  border-radius: 4px; font-size: 0.85rem; width: 110px; margin-bottom: 0; }
"""

    body = f"""
<style>{css_extra}</style>
{flash_html}
<div class="topbar">
  <a class="btn-csv" href="/admin/export">CSV herunterladen</a>
  <a class="btn-link-danger" href="/admin/reset-students">Studierende löschen</a>
  <form method="post" action="/admin">
    <input type="hidden" name="action" value="send-test-inform-email">
    <button class="btn-mail" type="submit">Test-Info-Mail senden</button>
  </form>
  <form method="post" action="/admin"
        onsubmit="return confirm('Info-E-Mail an alle registrierten Teilnehmenden senden?')">
    <input type="hidden" name="action" value="send-inform-email">
    <button class="btn-mail" type="submit">Info-Mail senden</button>
  </form>
  <span class="count"><strong>{count}</strong> Einträge</span>
  <span class="count">Vorlage: <code>rundmail.txt</code></span>
</div>
<div class="table-scroll">
  <table>
    <thead>
      <tr>
        <th>E-Mail</th><th>Rolle</th><th>LiteLLM-Key</th><th>Code-Status</th>
        <th>Verfügbar</th><th>Max-Limit</th><th>Registriert</th><th></th><th></th><th>Max-Limit setzen</th>
      </tr>
    </thead>
    <tbody>
      {table_rows or '<tr><td colspan="10">Keine Einträge vorhanden.</td></tr>'}
    </tbody>
  </table>
</div>

<div class="add-section">
  <h2>Nutzer manuell hinzufügen</h2>
  <form method="post" action="/admin" class="add-form">
    <input type="hidden" name="action" value="add-user">
    <div>
      <label>E-Mail</label><br>
      <input type="email" name="email" required placeholder="vorname.nachname@hs-offenburg.de"
             style="width:260px">
    </div>
    <div>
      <label>Rolle</label><br>
      <select name="role">{role_options}</select>
    </div>
    <div>
      <label>&nbsp;</label><br>
      <button class="btn-add" type="submit">Hinzufügen &amp; Key generieren</button>
    </div>
  </form>
</div>

<div class="add-section">
  <h2>Max-Limit für alle Studierenden setzen</h2>
  <form method="post" action="/admin" class="bulk-form"
        onsubmit="return confirm('Max-Limit fuer alle Studierenden setzen?')">
    <input type="hidden" name="action" value="update-student-budgets">
    <div>
      <label>Neues Max-Limit</label><br>
      <input type="number" name="budget" min="0" step="0.01" required placeholder="€">
    </div>
    <div>
      <label>&nbsp;</label><br>
      <button class="btn-mail" type="submit">Max-Limit setzen</button>
    </div>
  </form>
</div>
"""
    return render_base("Admin-Übersicht – LiteLLM Key Portal", "admin", body)


def render_admin_reset_students() -> str:
    css_extra = """
.danger-box { background: #fff4f4; border: 1px solid #f1b5b5; border-radius: 6px; padding: 1rem; }
.danger-copy { font-size: 0.9rem; color: #555; margin-bottom: 0.9rem; }
.danger-form { display: flex; gap: 0.75rem; flex-wrap: wrap; align-items: flex-end; }
.danger-form label { font-size: 0.85rem; font-weight: 600; }
.danger-form input { padding: 0.55rem 0.7rem; border: 1px solid #d9a2a2;
  border-radius: 4px; font-size: 0.9rem; width: 220px; margin-bottom: 0; }
.btn-danger-wide { background: #b71c1c; color: #fff; padding: 0.65rem 0.9rem;
                   border: none; border-radius: 5px; font-size: 0.9rem; cursor: pointer; }
.back-link { display: inline-block; margin-top: 1rem; font-size: 0.9rem; }
"""
    body = f"""
<style>{css_extra}</style>
<div class="danger-box">
  <p class="danger-copy">
    Diese Aktion verwendet den bestehenden Student-Reset und löscht zuerst alle studentischen
    Keys in LiteLLM und danach die zugehörigen Nutzer. Anschließend werden die studentischen
    Portal-Einträge aus der Datenbank entfernt.
  </p>
  <form method="post" action="/admin/reset-students" class="danger-form"
        onsubmit="return confirm('Wirklich alle Studierenden und deren Keys löschen?')">
    <div>
      <label>Bestätigung</label><br>
      <input type="text" name="delete_confirmation" required placeholder="delete_all"
             autocomplete="off" spellcheck="false">
    </div>
    <div>
      <label>&nbsp;</label><br>
      <button class="btn-danger-wide" type="submit">Alle Studierenden löschen</button>
    </div>
  </form>
  <p class="danger-copy">
    Wenn du wirklich alles löschen willst, tippe bitte <code>delete_all</code>.
  </p>
</div>
<a class="back-link" href="/admin">← Zurück zur Admin-Übersicht</a>
"""
    return render_base("Studierende löschen – LiteLLM Key Portal", "admin", body)


@app.get("/admin", response_class=HTMLResponse)
async def admin_overview(request: Request, flash: str = "", count: int = 0):
    if not _check_basic_auth(request):
        return _UNAUTHORIZED
    flash_msg = {
        "key-deleted": "Key erfolgreich gelöscht.",
        "user-deleted": "Nutzer erfolgreich gelöscht.",
        "budget-updated": "Max-Limit erfolgreich aktualisiert.",
        "student-budgets-updated": "Max-Limit fuer alle Studierenden erfolgreich aktualisiert.",
        "user-added": "Nutzer erfolgreich angelegt.",
        "students-reset": "Alle Studierenden wurden erfolgreich gelöscht.",
        "test-inform-email-sent": "Test-Info-E-Mail erfolgreich gesendet.",
    }.get(flash, "")
    if flash == "inform-email-sent":
        flash_msg = f"Info-E-Mail an {count} Empfaenger gesendet."
    rows = await _build_rows(request.app.state.pool)
    return HTMLResponse(render_admin_overview(rows, flash_msg))


@app.get("/admin/reset-students", response_class=HTMLResponse)
async def admin_reset_students_page(request: Request):
    if not _check_basic_auth(request):
        return _UNAUTHORIZED
    return HTMLResponse(render_admin_reset_students())


@app.post("/admin", response_class=HTMLResponse)
async def admin_overview_post(
    request: Request,
    action: str = Form(...),
    email: str = Form(""),
    role: str = Form(""),
    budget: str = Form(""),
):
    if not _check_basic_auth(request):
        return _UNAUTHORIZED

    email = email.strip().lower()
    role = role.strip().lower()
    user_id = f"{role}:{email}"
    pool: asyncpg.Pool = request.app.state.pool

    logger.info("Admin-Aktion: action=%s email=%s role=%s", action, email, role)

    if action == "delete-key":
        try:
            tokens = await litellm_get_user_key_tokens(user_id)
            await litellm_delete_keys(tokens)
        except Exception as exc:
            logger.error("delete-key Fehler: %s", exc)
            return HTMLResponse(render_error(f"Fehler beim Löschen des Keys: {exc}", "admin"), status_code=502)
        return RedirectResponse("/admin?flash=key-deleted", status_code=303)

    elif action == "delete-user":
        try:
            tokens = await litellm_get_user_key_tokens(user_id)
            await litellm_delete_keys(tokens)
            await litellm_delete_user(user_id)
        except Exception as exc:
            logger.error("delete-user Fehler: %s", exc)
            return HTMLResponse(render_error(f"Fehler beim Löschen des Nutzers: {exc}", "admin"), status_code=502)
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM portal_users WHERE email = $1 AND role = $2", email, role)
            await conn.execute(
                "UPDATE portal_verification_codes SET used = TRUE WHERE email = $1 AND role = $2",
                email, role,
            )
        return RedirectResponse("/admin?flash=user-deleted", status_code=303)

    elif action == "update-budget":
        try:
            budget_val = float(budget)
        except (ValueError, TypeError):
            return HTMLResponse(render_error("Ungültiger Budget-Wert.", "admin"), status_code=400)
        if budget_val < 0:
            return HTMLResponse(render_error("Budget darf nicht negativ sein.", "admin"), status_code=400)
        try:
            await litellm_update_budget(user_id, budget_val)
        except Exception as exc:
            logger.error("update-budget Fehler: %s", exc)
            return HTMLResponse(render_error(f"Fehler beim Aktualisieren des Budgets: {exc}", "admin"), status_code=502)
        return RedirectResponse("/admin?flash=budget-updated", status_code=303)

    elif action == "update-student-budgets":
        try:
            budget_val = float(budget)
        except (ValueError, TypeError):
            return HTMLResponse(render_error("Ungültiger Budget-Wert.", "admin"), status_code=400)
        if budget_val < 0:
            return HTMLResponse(render_error("Budget darf nicht negativ sein.", "admin"), status_code=400)
        student_emails = await _get_student_emails(pool)
        if not student_emails:
            return HTMLResponse(render_error("Keine Studierenden gefunden.", "admin"), status_code=400)
        try:
            await asyncio.gather(*[
                litellm_update_budget(f"student:{student_email}", budget_val)
                for student_email in student_emails
            ])
        except Exception as exc:
            logger.error("update-student-budgets Fehler: %s", exc)
            return HTMLResponse(
                render_error(f"Fehler beim Aktualisieren der Studierenden-Budgets: {exc}", "admin"),
                status_code=502,
            )
        return RedirectResponse("/admin?flash=student-budgets-updated", status_code=303)

    elif action == "add-user":
        if role not in ROLE_BUDGETS:
            return HTMLResponse(render_error(f"Unbekannte Rolle: {role}", "admin"), status_code=400)
        try:
            await litellm_create_user(user_id, ROLE_BUDGETS[role])
        except HTTPStatusError as exc:
            if exc.response.status_code != 409:
                return HTMLResponse(render_error(f"Fehler beim Erstellen des Nutzers: {exc}", "admin"), status_code=502)
            try:
                await litellm_update_budget(user_id, ROLE_BUDGETS[role])
            except Exception as update_exc:
                return HTMLResponse(
                    render_error(f"Fehler beim Aktualisieren des bestehenden Nutzers: {update_exc}", "admin"),
                    status_code=502,
                )
        except Exception as exc:
            return HTMLResponse(render_error(f"Fehler beim Erstellen des Nutzers: {exc}", "admin"), status_code=502)
        try:
            existing_tokens = await litellm_get_user_key_tokens(user_id)
            if existing_tokens:
                await litellm_delete_keys(existing_tokens)
        except Exception as exc:
            return HTMLResponse(
                render_error(f"Fehler beim Rotieren bestehender API-Schlüssel: {exc}", "admin"),
                status_code=502,
            )
        try:
            api_key = await litellm_generate_key(user_id, ROLE_BUDGETS[role])
        except Exception as exc:
            return HTMLResponse(render_error(f"Fehler beim Erstellen des API-Schlüssels: {exc}", "admin"), status_code=502)
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO portal_users (email, role) VALUES ($1, $2) ON CONFLICT (email, role) DO NOTHING",
                    email,
                    role,
                )
        except asyncpg.UniqueViolationError:
            return HTMLResponse(render_error("Nutzer existiert bereits.", "admin"), status_code=409)
        body = (
            f"<div class='success'>Nutzer angelegt. API-Schlüssel für <strong>{email}</strong>:</div>"
            f"<div class='key-box'>{api_key}</div>"
            f"<p class='hint'>Rolle: <strong>{ROLE_LABELS.get(role, role)}</strong> | "
            f"Budget: <strong>{ROLE_BUDGETS[role]:.2f} €/Monat</strong><br>"
            f"API-Endpunkt: <code>{LITELLM_BASE_URL}</code></p>"
            f"<br><a href='/admin'>← Zurück zur Übersicht</a>"
        )
        return HTMLResponse(render_base("Nutzer angelegt – Admin", "admin", body))

    elif action == "send-inform-email":
        recipients = await _get_roundmail_recipients(pool)
        if not recipients:
            return HTMLResponse(render_error("Keine registrierten Empfaenger vorhanden.", "admin"), status_code=400)
        try:
            sent_count = await asyncio.to_thread(send_inform_email, recipients)
        except Exception as exc:
            logger.error("send-inform-email Fehler: %s", exc)
            return HTMLResponse(
                render_error(f"Fehler beim Versenden der Info-E-Mail: {exc}", "admin"),
                status_code=502,
            )
        return RedirectResponse(f"/admin?flash=inform-email-sent&count={sent_count}", status_code=303)

    elif action == "send-test-inform-email":
        if not TEST_INFO_EMAIL:
            return HTMLResponse(
                render_error("TEST_INFO_EMAIL ist nicht gesetzt. Bitte in der .env konfigurieren.", "admin"),
                status_code=400,
            )
        try:
            sent_count = await asyncio.to_thread(send_inform_email, [TEST_INFO_EMAIL])
        except Exception as exc:
            logger.error("send-test-inform-email Fehler: %s", exc)
            return HTMLResponse(
                render_error(f"Fehler beim Versenden der Test-Info-E-Mail: {exc}", "admin"),
                status_code=502,
            )
        if sent_count != 1:
            return HTMLResponse(
                render_error("Test-Info-E-Mail konnte nicht eindeutig an eine Adresse gesendet werden.", "admin"),
                status_code=502,
            )
        return RedirectResponse("/admin?flash=test-inform-email-sent", status_code=303)

    return HTMLResponse(render_error(f"Unbekannte Aktion: {action}", "admin"), status_code=400)


@app.post("/admin/reset-students", response_class=HTMLResponse)
async def admin_reset_students_post(
    request: Request,
    delete_confirmation: str = Form(""),
):
    if not _check_basic_auth(request):
        return _UNAUTHORIZED
    if delete_confirmation.strip() != "delete_all":
        return HTMLResponse(
            render_error("Bestaetigung fehlt. Bitte genau 'delete_all' eingeben.", "admin"),
            status_code=400,
        )
    try:
        exit_code = await reset_students_script.run_student_reset(dry_run=False, confirm=True)
    except Exception as exc:
        logger.error("reset-students Fehler: %s", exc)
        return HTMLResponse(
            render_error(f"Fehler beim Ausfuehren des Student-Resets: {exc}", "admin"),
            status_code=502,
        )
    if exit_code == 0:
        return RedirectResponse("/admin?flash=students-reset", status_code=303)
    if exit_code == 2:
        return HTMLResponse(
            render_error(
                "Student-Reset wurde mit Fehlern abgeschlossen. Pruefen Sie LiteLLM und die Portal-Datenbank.",
                "admin",
            ),
            status_code=502,
        )
    return HTMLResponse(
        render_error("Student-Reset konnte nicht erfolgreich ausgefuehrt werden.", "admin"),
        status_code=502,
    )


@app.get("/admin/export")
async def admin_overview_export(request: Request):
    if not _check_basic_auth(request):
        return _UNAUTHORIZED
    rows = await _build_rows(request.app.state.pool)
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=["email", "role", "key", "code_status", "created_at"],
        extrasaction="ignore",
    )
    writer.writeheader()
    writer.writerows(rows)
    buf.seek(0)
    filename = f"portal-users-{datetime.now().strftime('%Y%m%d-%H%M')}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/{role}", response_class=HTMLResponse)
async def landing(role: str = Path(...)):
    if err := _check_role(role):
        return err
    return render_landing(role)


@app.get("/{role}/enter-code", response_class=HTMLResponse)
async def enter_code(role: str = Path(...), email: str = ""):
    if err := _check_role(role):
        return err
    return render_enter_code(role, email.strip().lower())


@app.post("/{role}/request-code", response_class=HTMLResponse)
@limiter.limit(RATE_LIMIT_REQUEST_CODE)
async def request_code(
    request: Request,
    role: str = Path(...),
    email: str = Form(...),
):
    if err := _check_role(role):
        return err

    email = email.strip().lower()
    logger.debug("request_code: email=%s role=%s", email, role)

    ok, err_msg = validate_email(email, role)
    if not ok:
        logger.debug("Validierungsfehler: %s", err_msg)
        return HTMLResponse(render_error(err_msg, role), status_code=400)

    pool: asyncpg.Pool = request.app.state.pool
    async with pool.acquire() as conn:
        # Cooldown: keinen neuen Code senden wenn bereits kürzlich einer gesendet wurde
        recent = await conn.fetchval(
            """
            SELECT id FROM portal_verification_codes
            WHERE email = $1 AND role = $2 AND used = FALSE
              AND expires_at > $3 AND created_at > $4
            LIMIT 1
            """,
            email, role,
            datetime.now(timezone.utc),
            datetime.now(timezone.utc) - timedelta(minutes=CODE_COOLDOWN_MINUTES),
        )
        if recent is not None:
            return HTMLResponse(
                render_error(
                    f"Ein Code wurde bereits gesendet. Bitte warten Sie {CODE_COOLDOWN_MINUTES} Minuten "
                    f"oder prüfen Sie Ihr Postfach.",
                    role,
                ),
                status_code=429,
            )

        await conn.execute(
            "UPDATE portal_verification_codes SET used = TRUE "
            "WHERE email = $1 AND role = $2 AND used = FALSE",
            email, role,
        )
        code = generate_code()
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=CODE_TTL_MINUTES)
        await conn.execute(
            "INSERT INTO portal_verification_codes (email, role, hashed_code, expires_at) "
            "VALUES ($1, $2, $3, $4)",
            email, role, hash_code(code), expires_at,
        )

    try:
        await asyncio.to_thread(send_verification_email, email, code, role)
    except Exception as exc:
        logger.error("E-Mail-Fehler: %s", exc)
        return HTMLResponse(
            render_error(f"E-Mail konnte nicht gesendet werden: {exc}", role),
            status_code=503,
        )

    logger.info("Code angefordert: email=%s role=%s", email, role)
    return HTMLResponse(render_code_sent(role, email))


@app.post("/{role}/verify-and-get-key", response_class=HTMLResponse)
@limiter.limit(RATE_LIMIT_VERIFY)
async def verify_and_get_key(
    request: Request,
    role: str = Path(...),
    email: str = Form(...),
    code: str = Form(...),
):
    if err := _check_role(role):
        return err

    email = email.strip().lower()
    code = "".join(code.split())

    if len(email) > 254:
        return HTMLResponse(render_error("Ungültige E-Mail-Adresse.", role), status_code=400)

    if not code.isdigit() or len(code) != 6:
        return HTMLResponse(
            render_error("Ungültiger Code. Bitte geben Sie genau 6 Ziffern ein.", role),
            status_code=400,
        )

    pool: asyncpg.Pool = request.app.state.pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, hashed_code FROM portal_verification_codes
            WHERE email = $1 AND role = $2 AND used = FALSE AND expires_at > $3
            ORDER BY id DESC LIMIT 1
            """,
            email, role, datetime.now(timezone.utc),
        )
        if row is None:
            return HTMLResponse(
                render_error(
                    "Kein gültiger Code gefunden. Der Code ist möglicherweise abgelaufen "
                    "oder wurde bereits verwendet. Bitte fordern Sie einen neuen Code an.",
                    role,
                ),
                status_code=400,
            )

        if not verify_code(code, row["hashed_code"]):
            return HTMLResponse(render_error("Falscher Bestätigungscode.", role), status_code=400)

        await conn.execute(
            "UPDATE portal_verification_codes SET used = TRUE WHERE id = $1", row["id"]
        )

        user_id = f"{role}:{email}"
        budget = ROLE_BUDGETS[role]

        try:
            exists = await litellm_user_exists(user_id)
        except Exception as exc:
            return HTMLResponse(
                render_error(f"Fehler bei der LiteLLM-Prüfung: {exc}", role),
                status_code=502,
            )

        if exists:
            try:
                tokens = await litellm_get_user_key_tokens(user_id)
            except Exception as exc:
                return HTMLResponse(
                    render_error(f"Fehler beim Abrufen bestehender LiteLLM-Keys: {exc}", role),
                    status_code=502,
                )
            try:
                if tokens:
                    await litellm_delete_keys(tokens)
                await litellm_update_budget(user_id, budget)
            except Exception as exc:
                return HTMLResponse(
                    render_error(f"Fehler beim Aktualisieren des LiteLLM-Nutzers: {exc}", role),
                    status_code=502,
                )
        else:
            try:
                await litellm_create_user(user_id, budget)
            except Exception as exc:
                return HTMLResponse(
                    render_error(f"Fehler beim Erstellen des LiteLLM-Benutzers: {exc}", role),
                    status_code=502,
                )

        try:
            api_key = await litellm_generate_key(user_id, budget)
        except Exception as exc:
            return HTMLResponse(
                render_error(f"Fehler beim Erstellen des API-Schlüssels: {exc}", role),
                status_code=502,
            )

        await conn.execute(
            """
            INSERT INTO portal_users (email, role)
            VALUES ($1, $2)
            ON CONFLICT (email, role) DO NOTHING
            """,
            email, role,
        )

    logger.info("Key ausgestellt: email=%s role=%s", email, role)
    return HTMLResponse(render_key_issued(role, email, api_key))
