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
from urllib.parse import urlencode, urlsplit

import asyncpg
import httpx
import uvicorn
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
EMAIL_PLACEHOLDER = os.environ.get("EMAIL_PLACEHOLDER", "firstname.lastname@university.edu").strip() or "firstname.lastname@university.edu"
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
INFOMAIL_PATH = os.path.join(os.path.dirname(__file__), "infomail.txt")

ROLE_BUDGETS = {
    "student": STUDENT_BUDGET,
}

SUPPORTED_LANGS = {"de", "en"}
DEFAULT_LANG = "de"

ROLE_LABELS = {
    "student": {"de": "Student:in", "en": "Student"},
    "professor": {"de": "Professor:in", "en": "Professor"},
    "admin": {"de": "Admin", "en": "Admin"},
}

TRANSLATIONS = {
    "app_name": {"de": "LiteLLM Key Portal", "en": "LiteLLM Key Portal"},
    "subtitle": {"de": "Hochschule – KI-Dienste", "en": "University – AI Services"},
    "lang_de": {"de": "DE", "en": "DE"},
    "lang_en": {"de": "EN", "en": "EN"},
    "error_title": {"de": "Fehler – LiteLLM Key Portal", "en": "Error – LiteLLM Key Portal"},
    "back": {"de": "Zurück", "en": "Back"},
    "too_many_requests": {
        "de": "Zu viele Anfragen. Bitte warten Sie einen Moment und versuchen Sie es erneut.",
        "en": "Too many requests. Please wait a moment and try again.",
    },
    "invalid_email": {"de": "Ungültige E-Mail-Adresse.", "en": "Invalid email address."},
    "allowed_domain_only": {
        "de": "Nur E-Mail-Adressen der Domain @{domain} sind erlaubt.",
        "en": "Only email addresses from @{domain} are allowed.",
    },
    "invalid_recipient": {"de": "Ungueltige Empfaengeradresse.", "en": "Invalid recipient address."},
    "verification_email_subject": {
        "de": "Ihr LiteLLM-Bestätigungscode ({code})",
        "en": "Your LiteLLM verification code ({code})",
    },
    "verification_email_body": {
        "de": (
            "Hallo,\n\n"
            "Ihr Bestätigungscode für das LiteLLM API-Portal ({role_label}) lautet:\n\n"
            "  {code}\n\n"
            "Der Code ist {ttl} Minuten gültig.\n\n"
            "Wenn Sie diese Anfrage nicht gestellt haben, ignorieren Sie diese E-Mail.\n\n"
            "Hochschule – KI-Portal"
        ),
        "en": (
            "Hello,\n\n"
            "Your verification code for the LiteLLM API portal ({role_label}) is:\n\n"
            "  {code}\n\n"
            "The code is valid for {ttl} minutes.\n\n"
            "If you did not request this, you can ignore this email.\n\n"
            "University – AI Portal"
        ),
    },
    "infomail_unreadable": {
        "de": "Info-Mail-Datei nicht lesbar: {error}",
        "en": "Info mail file is not readable: {error}",
    },
    "infomail_empty": {
        "de": "Info-Mail-Datei ist leer.",
        "en": "Info mail file is empty.",
    },
    "inform_email_subject": {
        "de": "Wichtige Information zu Ihrem LiteLLM API-Key",
        "en": "Important information about your LiteLLM API key",
    },
    "student_code_hint": {
        "de": "Geben Sie hier Ihre Hochschul-E-Mail-Adresse und den 6-stelligen Bestätigungscode ein.",
        "en": "Enter your university email address and the 6-digit verification code here.",
    },
    "email_label": {"de": "Hochschul-E-Mail-Adresse", "en": "University email address"},
    "email_short_label": {"de": "E-Mail", "en": "Email"},
    "email_placeholder": {
        "de": EMAIL_PLACEHOLDER,
        "en": EMAIL_PLACEHOLDER,
    },
    "request_code": {"de": "Bestätigungscode anfordern", "en": "Request verification code"},
    "existing_key_hint": {
        "de": (
            "Wenn bereits ein API-Schlüssel für diese E-Mail existiert, wird er nach erfolgreicher "
            "Bestätigung durch einen neuen Schlüssel ersetzt."
        ),
        "en": (
            "If an API key already exists for this email address, it will be replaced with a new key "
            "after successful verification."
        ),
    },
    "already_have_code": {
        "de": "Sie haben den Code bereits? <a href=\"{href}\">Code hier eingeben</a>",
        "en": "Already have the code? <a href=\"{href}\">Enter it here</a>",
    },
    "landing_title": {"de": "LiteLLM Key Portal – {label}", "en": "LiteLLM Key Portal – {label}"},
    "enter_code_title": {"de": "Code eingeben – LiteLLM Key Portal", "en": "Enter code – LiteLLM Key Portal"},
    "code_label": {"de": "Bestätigungscode (6 Stellen)", "en": "Verification code (6 digits)"},
    "confirm_code_button": {
        "de": "Code bestätigen &amp; API-Schlüssel erstellen oder erneuern",
        "en": "Verify code &amp; create or renew API key",
    },
    "code_sent_message": {
        "de": "Ein 6-stelliger Bestätigungscode wurde an <strong>{email}</strong> gesendet. Der Code ist {ttl} Minuten gültig.",
        "en": "A 6-digit verification code has been sent to <strong>{email}</strong>. The code is valid for {ttl} minutes.",
    },
    "post_verify_hint": {
        "de": (
            "Nach der Bestätigung wird ein neuer API-Schlüssel erstellt. Ein vorhandener Schlüssel "
            "für diese E-Mail wird dabei automatisch ersetzt."
        ),
        "en": (
            "After verification, a new API key will be created. Any existing key for this email address "
            "will be replaced automatically."
        ),
    },
    "key_issued_title": {
        "de": "API-Schlüssel erhalten – LiteLLM Key Portal",
        "en": "API key received – LiteLLM Key Portal",
    },
    "key_issued_success": {
        "de": "Ihr API-Schlüssel wurde erfolgreich erstellt bzw. erneuert.",
        "en": "Your API key was created or renewed successfully.",
    },
    "your_api_key": {
        "de": "Ihr persönlicher LiteLLM API-Schlüssel:",
        "en": "Your personal LiteLLM API key:",
    },
    "budget_per_month": {"de": "Budget", "en": "Budget"},
    "existing_key_replaced": {
        "de": "Falls bereits ein Schlüssel vorhanden war, wurde er ersetzt.",
        "en": "If a key already existed, it has been replaced.",
    },
    "save_key_securely": {
        "de": "Bitte speichern Sie diesen Schlüssel sicher.",
        "en": "Please store this key securely.",
    },
    "shown_once": {
        "de": "Er wird Ihnen nur einmal angezeigt.",
        "en": "It is shown only once.",
    },
    "api_endpoint": {"de": "API-Endpunkt", "en": "API endpoint"},
    "unknown_role": {"de": "Unbekannte Rolle: {role}", "en": "Unknown role: {role}"},
    "admin_realm": {"de": "Admin-Bereich", "en": "Admin Area"},
    "code_active": {"de": "aktiv ({minutes} min)", "en": "active ({minutes} min)"},
    "confirm_delete_key": {"de": "Key löschen?", "en": "Delete key?"},
    "confirm_delete_user": {"de": "Nutzer komplett löschen?", "en": "Delete user completely?"},
    "set": {"de": "Setzen", "en": "Set"},
    "csv_download": {"de": "CSV herunterladen", "en": "Download CSV"},
    "delete_students": {"de": "Studierende löschen", "en": "Delete students"},
    "send_test_info_mail": {"de": "Test-Info-Mail senden", "en": "Send test info email"},
    "confirm_send_info_mail": {
        "de": "Info-E-Mail an alle registrierten Teilnehmenden senden?",
        "en": "Send the info email to all registered participants?",
    },
    "send_info_mail": {"de": "Info-Mail senden", "en": "Send info email"},
    "entries_count": {"de": "<strong>{count}</strong> Einträge", "en": "<strong>{count}</strong> entries"},
    "template_label": {"de": "Vorlage", "en": "Template"},
    "column_role": {"de": "Rolle", "en": "Role"},
    "column_key": {"de": "LiteLLM-Key", "en": "LiteLLM key"},
    "column_code_status": {"de": "Code-Status", "en": "Code status"},
    "column_available": {"de": "Verfügbar", "en": "Available"},
    "column_max_limit": {"de": "Max-Limit", "en": "Max limit"},
    "column_registered": {"de": "Registriert", "en": "Registered"},
    "column_set_max_limit": {"de": "Max-Limit setzen", "en": "Set max limit"},
    "no_entries": {"de": "Keine Einträge vorhanden.", "en": "No entries available."},
    "admin_add_user_heading": {"de": "Nutzer manuell hinzufügen", "en": "Add user manually"},
    "role_label": {"de": "Rolle", "en": "Role"},
    "add_user_button": {"de": "Hinzufügen &amp; Key generieren", "en": "Add &amp; generate key"},
    "bulk_set_limit_heading": {
        "de": "Max-Limit für alle Studierenden setzen",
        "en": "Set max limit for all students",
    },
    "confirm_bulk_set_limit": {
        "de": "Max-Limit fuer alle Studierenden setzen?",
        "en": "Set the max limit for all students?",
    },
    "new_max_limit": {"de": "Neues Max-Limit", "en": "New max limit"},
    "admin_overview_title": {"de": "Admin-Übersicht – LiteLLM Key Portal", "en": "Admin overview – LiteLLM Key Portal"},
    "reset_copy": {
        "de": (
            "Diese Aktion verwendet den bestehenden Student-Reset und löscht zuerst alle studentischen "
            "Keys in LiteLLM und danach die zugehörigen Nutzer. Anschließend werden die studentischen "
            "Portal-Einträge aus der Datenbank entfernt."
        ),
        "en": (
            "This action uses the existing student reset and first deletes all student keys in LiteLLM, "
            "then the associated users. Afterwards, the student portal entries are removed from the database."
        ),
    },
    "confirm_delete_all_students": {
        "de": "Wirklich alle Studierenden und deren Keys löschen?",
        "en": "Really delete all students and their keys?",
    },
    "confirmation_label": {"de": "Bestätigung", "en": "Confirmation"},
    "delete_all_students": {"de": "Alle Studierenden löschen", "en": "Delete all students"},
    "delete_all_hint": {
        "de": "Wenn du wirklich alles löschen willst, tippe bitte <code>delete_all</code>.",
        "en": "If you really want to delete everything, please type <code>delete_all</code>.",
    },
    "back_to_admin": {"de": "Zurück zur Admin-Übersicht", "en": "Back to admin overview"},
    "reset_students_title": {
        "de": "Studierende löschen – LiteLLM Key Portal",
        "en": "Delete students – LiteLLM Key Portal",
    },
    "flash_key_deleted": {"de": "Key erfolgreich gelöscht.", "en": "Key deleted successfully."},
    "flash_user_deleted": {"de": "Nutzer erfolgreich gelöscht.", "en": "User deleted successfully."},
    "flash_budget_updated": {"de": "Max-Limit erfolgreich aktualisiert.", "en": "Max limit updated successfully."},
    "flash_student_budgets_updated": {
        "de": "Max-Limit fuer alle Studierenden erfolgreich aktualisiert.",
        "en": "Max limit updated successfully for all students.",
    },
    "flash_user_added": {"de": "Nutzer erfolgreich angelegt.", "en": "User created successfully."},
    "flash_students_reset": {"de": "Alle Studierenden wurden erfolgreich gelöscht.", "en": "All students were deleted successfully."},
    "flash_test_info_mail_sent": {
        "de": "Test-Info-E-Mail erfolgreich gesendet.",
        "en": "Test info email sent successfully.",
    },
    "flash_info_mail_sent": {
        "de": "Info-E-Mail an {count} Empfaenger gesendet.",
        "en": "Info email sent to {count} recipients.",
    },
    "invalid_budget": {"de": "Ungültiger Budget-Wert.", "en": "Invalid budget value."},
    "budget_negative": {"de": "Budget darf nicht negativ sein.", "en": "Budget must not be negative."},
    "error_delete_key": {"de": "Fehler beim Löschen des Keys: {error}", "en": "Error deleting key: {error}"},
    "error_delete_user": {"de": "Fehler beim Löschen des Nutzers: {error}", "en": "Error deleting user: {error}"},
    "error_update_budget": {"de": "Fehler beim Aktualisieren des Budgets: {error}", "en": "Error updating budget: {error}"},
    "no_students_found": {"de": "Keine Studierenden gefunden.", "en": "No students found."},
    "error_update_student_budgets": {
        "de": "Fehler beim Aktualisieren der Studierenden-Budgets: {error}",
        "en": "Error updating student budgets: {error}",
    },
    "error_create_user": {"de": "Fehler beim Erstellen des Nutzers: {error}", "en": "Error creating user: {error}"},
    "error_update_existing_user": {
        "de": "Fehler beim Aktualisieren des bestehenden Nutzers: {error}",
        "en": "Error updating existing user: {error}",
    },
    "error_rotate_keys": {
        "de": "Fehler beim Rotieren bestehender API-Schlüssel: {error}",
        "en": "Error rotating existing API keys: {error}",
    },
    "error_create_api_key": {
        "de": "Fehler beim Erstellen des API-Schlüssels: {error}",
        "en": "Error creating API key: {error}",
    },
    "user_already_exists": {"de": "Nutzer existiert bereits.", "en": "User already exists."},
    "add_user_success": {
        "de": "Nutzer angelegt. API-Schlüssel für <strong>{email}</strong>:",
        "en": "User created. API key for <strong>{email}</strong>:",
    },
    "role_budget_endpoint": {
        "de": "Rolle: <strong>{role}</strong> | Budget: <strong>{budget:.2f} €/Monat</strong><br>API-Endpunkt: <code>{endpoint}</code>",
        "en": "Role: <strong>{role}</strong> | Budget: <strong>{budget:.2f} €/month</strong><br>API endpoint: <code>{endpoint}</code>",
    },
    "back_to_overview": {"de": "Zurück zur Übersicht", "en": "Back to overview"},
    "user_created_title": {"de": "Nutzer angelegt – Admin", "en": "User created – Admin"},
    "no_recipients": {"de": "Keine registrierten Empfaenger vorhanden.", "en": "No registered recipients available."},
    "error_send_info_mail": {
        "de": "Fehler beim Versenden der Info-E-Mail: {error}",
        "en": "Error sending info email: {error}",
    },
    "test_info_email_missing": {
        "de": "TEST_INFO_EMAIL ist nicht gesetzt. Bitte in der .env konfigurieren.",
        "en": "TEST_INFO_EMAIL is not set. Please configure it in .env.",
    },
    "error_send_test_info_mail": {
        "de": "Fehler beim Versenden der Test-Info-E-Mail: {error}",
        "en": "Error sending test info email: {error}",
    },
    "test_info_email_count_error": {
        "de": "Test-Info-E-Mail konnte nicht eindeutig an eine Adresse gesendet werden.",
        "en": "Test info email could not be sent cleanly to exactly one address.",
    },
    "unknown_action": {"de": "Unbekannte Aktion: {action}", "en": "Unknown action: {action}"},
    "delete_all_missing": {
        "de": "Bestaetigung fehlt. Bitte genau 'delete_all' eingeben.",
        "en": "Confirmation missing. Please enter exactly 'delete_all'.",
    },
    "error_run_student_reset": {
        "de": "Fehler beim Ausfuehren des Student-Resets: {error}",
        "en": "Error running the student reset: {error}",
    },
    "student_reset_partial_error": {
        "de": "Student-Reset wurde mit Fehlern abgeschlossen. Pruefen Sie LiteLLM und die Portal-Datenbank.",
        "en": "Student reset finished with errors. Please check LiteLLM and the portal database.",
    },
    "student_reset_failed": {
        "de": "Student-Reset konnte nicht erfolgreich ausgefuehrt werden.",
        "en": "Student reset could not be completed successfully.",
    },
    "cooldown_error": {
        "de": "Ein Code wurde bereits gesendet. Bitte warten Sie {minutes} Minuten oder prüfen Sie Ihr Postfach.",
        "en": "A code was already sent. Please wait {minutes} minutes or check your inbox.",
    },
    "email_send_failed": {"de": "E-Mail konnte nicht gesendet werden: {error}", "en": "Email could not be sent: {error}"},
    "invalid_code": {
        "de": "Ungültiger Code. Bitte geben Sie genau 6 Ziffern ein.",
        "en": "Invalid code. Please enter exactly 6 digits.",
    },
    "no_valid_code": {
        "de": "Kein gültiger Code gefunden. Der Code ist möglicherweise abgelaufen oder wurde bereits verwendet. Bitte fordern Sie einen neuen Code an.",
        "en": "No valid code was found. The code may have expired or was already used. Please request a new code.",
    },
    "wrong_code": {"de": "Falscher Bestätigungscode.", "en": "Incorrect verification code."},
    "error_litellm_check": {"de": "Fehler bei der LiteLLM-Prüfung: {error}", "en": "Error during LiteLLM check: {error}"},
    "error_fetch_keys": {
        "de": "Fehler beim Abrufen bestehender LiteLLM-Keys: {error}",
        "en": "Error fetching existing LiteLLM keys: {error}",
    },
    "error_update_litellm_user": {
        "de": "Fehler beim Aktualisieren des LiteLLM-Nutzers: {error}",
        "en": "Error updating LiteLLM user: {error}",
    },
    "error_create_litellm_user": {
        "de": "Fehler beim Erstellen des LiteLLM-Benutzers: {error}",
        "en": "Error creating LiteLLM user: {error}",
    },
}


def _normalize_lang(lang: str) -> str:
    return lang if lang in SUPPORTED_LANGS else DEFAULT_LANG


def _t(key: str, lang: str, **kwargs) -> str:
    entry = TRANSLATIONS.get(key)
    if not entry:
        raise KeyError(f"Missing translation key: {key}")
    template = entry.get(lang) or entry[DEFAULT_LANG]
    return template.format(**kwargs)


def _role_label(role: str, lang: str) -> str:
    entry = ROLE_LABELS.get(role)
    if not entry:
        return role
    return entry.get(lang) or entry[DEFAULT_LANG]


def _with_lang(path: str, lang: str, **params: str) -> str:
    query_params = {"lang": _normalize_lang(lang)}
    for key, value in params.items():
        if value:
            query_params[key] = value
    return f"{path}?{urlencode(query_params)}"

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


def _describe_database_target(database_url: str) -> str:
    parsed = urlsplit(database_url)
    host = parsed.hostname or "<unknown-host>"
    port = f":{parsed.port}" if parsed.port else ""
    db_name = parsed.path.lstrip("/") or "<unknown-db>"
    return f"{host}{port}/{db_name}"


def _build_database_startup_error(database_url: str, exc: Exception) -> str:
    target = _describe_database_target(database_url)
    return (
        f"Failed to connect to PostgreSQL at {target}. "
        "Check that the database is running and that DATABASE_URL is correct. "
        "For local development, start it with `docker compose up -d`. "
        f"Original error: {type(exc).__name__}: {exc}"
    )


# ---------------------------------------------------------------------------
# Lifespan – DB pool init
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    except Exception as exc:
        raise RuntimeError(_build_database_startup_error(DATABASE_URL, exc)) from exc
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


def _get_lang(request: Request) -> str:
    return _normalize_lang(request.query_params.get("lang", DEFAULT_LANG))


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> Response:
    lang = _get_lang(request)
    return HTMLResponse(
        render_error(_t("too_many_requests", lang), lang=lang),
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


def validate_email(email: str, role: str, lang: str = DEFAULT_LANG) -> tuple[bool, str]:
    if not email or len(email) > 254:
        return False, _t("invalid_email", lang)
    if "@" not in email:
        return False, _t("invalid_email", lang)
    if any(c in email for c in ("\r", "\n", "\x00")):
        return False, _t("invalid_email", lang)
    domain = email.split("@", 1)[1]
    if domain != ALLOWED_DOMAIN:
        return False, _t("allowed_domain_only", lang, domain=ALLOWED_DOMAIN)
    return True, ""


def send_plain_email(to: str, subject: str, text_body: str) -> None:
    if not to or any(c in to for c in ("\r", "\n", "\x00")):
        raise ValueError(_t("invalid_recipient", DEFAULT_LANG))

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


def send_verification_email(to: str, code: str, role: str, lang: str = DEFAULT_LANG) -> None:
    text_body = _t(
        "verification_email_body",
        lang,
        role_label=_role_label(role, lang),
        code=code,
        ttl=CODE_TTL_MINUTES,
    )
    subject = _t("verification_email_subject", lang, code=code)
    send_plain_email(to, subject, text_body)
    logger.info("Bestätigungscode gesendet: email=%s role=%s", to, role)


def _load_infomail_text(lang: str = DEFAULT_LANG) -> str:
    try:
        with open(INFOMAIL_PATH, "r", encoding="utf-8") as handle:
            text_body = handle.read().strip()
    except OSError as exc:
        raise RuntimeError(_t("infomail_unreadable", lang, error=exc)) from exc
    if not text_body:
        raise ValueError(_t("infomail_empty", lang))
    return text_body


def send_inform_email(recipients: list[str], lang: str = DEFAULT_LANG) -> int:
    unique_recipients = sorted({email.strip().lower() for email in recipients if email and email.strip()})
    if not unique_recipients:
        return 0

    text_body = _load_infomail_text(lang)
    subject = _t("inform_email_subject", lang)

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
.header-row { display: flex; justify-content: space-between; gap: 1rem; align-items: flex-start; }
.lang-switch { font-size: 0.8rem; white-space: nowrap; margin-top: 0.2rem; }
.lang-switch a { color: #1a4f8a; text-decoration: none; }
.lang-switch strong { color: #163f6e; }
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


def render_base(
    title: str,
    role: str,
    body_html: str,
    lang: str = DEFAULT_LANG,
    switch_path: str = "/student",
    **switch_params: str,
) -> str:
    label = _role_label(role, lang)
    de_link = _with_lang(switch_path, "de", **switch_params)
    en_link = _with_lang(switch_path, "en", **switch_params)
    de_toggle = f"<strong>{_t('lang_de', lang)}</strong>" if lang == "de" else f"<a href=\"{de_link}\">{_t('lang_de', lang)}</a>"
    en_toggle = f"<strong>{_t('lang_en', lang)}</strong>" if lang == "en" else f"<a href=\"{en_link}\">{_t('lang_en', lang)}</a>"
    return f"""<!DOCTYPE html>
<html lang="{lang}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>{_CSS}</style>
</head>
<body>
  <div class="card">
    <div class="header-row">
      <div>
        <h1>{_t("app_name", lang)}</h1>
        <p class="subtitle">{_t("subtitle", lang)}</p>
      </div>
      <div class="lang-switch">{de_toggle} | {en_toggle}</div>
    </div>
    <span class="role-badge">{label}</span>
    {body_html}
  </div>
</body>
</html>"""


def render_error(msg: str, role: str = "", lang: str = DEFAULT_LANG, back_path: str = "") -> str:
    target = back_path or (f"/{role}" if role else "/student")
    back = f'<a href="{html.escape(_with_lang(target, lang), quote=True)}">← {_t("back", lang)}</a>'
    return render_base(
        _t("error_title", lang),
        role or "student",
        f'<div class="error">{html.escape(msg)}</div>{back}',
        lang=lang,
        switch_path=target,
    )


def render_landing(role: str, lang: str) -> str:
    label = _role_label(role, lang)
    enter_code_href = _with_lang(f"/{role}/enter-code", lang)
    body = f"""
<form method="post" action="{html.escape(_with_lang(f'/{role}/request-code', lang), quote=True)}">
  <label for="email">{_t("email_label", lang)}</label>
  <input type="email" id="email" name="email" required
         placeholder="{_t("email_placeholder", lang)}">
  <button type="submit">{_t("request_code", lang)}</button>
</form>
<p class="hint">
  {_t("existing_key_hint", lang)}
</p>
<p class="hint">
  {_t("already_have_code", lang, href=html.escape(enter_code_href, quote=True))}
</p>
"""
    return render_base(_t("landing_title", lang, label=label), role, body, lang=lang, switch_path=f"/{role}")


def render_enter_code(role: str, email: str = "", lang: str = DEFAULT_LANG) -> str:
    esc = html.escape(email, quote=True)
    body = f"""
<p class="hint">
  {_t("student_code_hint", lang)}
</p>
<form method="post" action="{html.escape(_with_lang(f'/{role}/verify-and-get-key', lang), quote=True)}">
  <label for="email">{_t("email_label", lang)}</label>
  <input type="email" id="email" name="email" required
         value="{esc}" placeholder="{_t("email_placeholder", lang)}">
  <label for="code">{_t("code_label", lang)}</label>
  <input type="text" id="code" name="code" required maxlength="6" pattern="[0-9]{{6}}"
         placeholder="123456" inputmode="numeric" autocomplete="one-time-code"
         oninput="this.value=this.value.replace(/\\D/g,'').slice(0,6)">
  <button type="submit">{_t("confirm_code_button", lang)}</button>
</form>
"""
    return render_base(_t("enter_code_title", lang), role, body, lang=lang, switch_path=f"/{role}/enter-code", email=email)


def render_code_sent(role: str, email: str, lang: str = DEFAULT_LANG) -> str:
    esc = html.escape(email, quote=True)
    body = f"""
<div class="success">
  {_t("code_sent_message", lang, email=esc, ttl=CODE_TTL_MINUTES)}
</div>
<p class="hint">
  {_t("post_verify_hint", lang)}
</p>
<form method="post" action="{html.escape(_with_lang(f'/{role}/verify-and-get-key', lang), quote=True)}">
  <input type="hidden" name="email" value="{esc}">
  <label for="code">{_t("code_label", lang)}</label>
  <input type="text" id="code" name="code" required maxlength="6" pattern="[0-9]{{6}}"
         placeholder="123456" inputmode="numeric" autocomplete="one-time-code"
         oninput="this.value=this.value.replace(/\\D/g,'').slice(0,6)">
  <button type="submit">{_t("confirm_code_button", lang)}</button>
</form>
"""
    return render_base(_t("enter_code_title", lang), role, body, lang=lang, switch_path=f"/{role}/enter-code", email=email)


def render_key_issued(role: str, email: str, key: str, lang: str = DEFAULT_LANG) -> str:
    budget = ROLE_BUDGETS.get(role, 0)
    esc_email = html.escape(email)
    esc_key = html.escape(key)
    body = f"""
<div class="success">{_t("key_issued_success", lang)}</div>
<p>{_t("your_api_key", lang)}</p>
<div class="key-box">{esc_key}</div>
<p class="hint">
  {_t("email_short_label", lang)}: {esc_email}<br>
  {_t("budget_per_month", lang)}: <strong>{budget:.2f} €/{"Monat" if lang == "de" else "month"}</strong><br><br>
  {_t("existing_key_replaced", lang)}<br><br>
  <strong>{_t("save_key_securely", lang)}</strong>
  {_t("shown_once", lang)}<br><br>
  {_t("api_endpoint", lang)}: <code>{LITELLM_BASE_URL}</code>
</p>
"""
    return render_base(_t("key_issued_title", lang), role, body, lang=lang, switch_path=f"/{role}")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

# Rollen die sich selbst registrieren dürfen (Self-Service)
SELF_SERVICE_ROLES = {"student"}


def _check_role(role: str, lang: str = DEFAULT_LANG) -> HTMLResponse | None:
    if role not in SELF_SERVICE_ROLES:
        return HTMLResponse(
            render_error(_t("unknown_role", lang, role=role), lang=lang, back_path="/student"),
            status_code=404,
        )
    return None


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/")
async def root():
    return RedirectResponse("/student?lang=de", status_code=302)


# ---------------------------------------------------------------------------
# Basic Auth helper
# ---------------------------------------------------------------------------

_UNAUTHORIZED = Response(
    status_code=401,
    headers={"WWW-Authenticate": f'Basic realm="{_t("admin_realm", DEFAULT_LANG)}"'},
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


async def _build_rows(pool: asyncpg.Pool, lang: str = DEFAULT_LANG) -> list[dict]:
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
            code_status = _t("code_active", lang, minutes=remaining)
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


async def _get_infomail_recipients(pool: asyncpg.Pool) -> list[str]:
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


def render_admin_overview(rows: list[dict], flash: str = "", lang: str = DEFAULT_LANG) -> str:
    count = len(rows)
    table_rows = ""
    admin_action = html.escape(_with_lang("/admin", lang), quote=True)
    for r in rows:
        email = r["email"]
        role = r["role"]
        key = r["key"]
        key_short = key[:20] + "…" if len(key) > 20 else key
        role_label = _role_label(role, lang)
        # Escape all user-controlled values for safe HTML embedding
        available_budget = r["available_budget"]
        max_budget = r["max_budget"]
        e = html.escape(email, quote=True)
        role_escaped = html.escape(role, quote=True)
        k = html.escape(key, quote=True)
        ks = html.escape(key_short)
        rs = html.escape(role_label)
        cs = html.escape(r["code_status"])
        abs_ = html.escape(available_budget)
        mbs = html.escape(max_budget)
        table_rows += (
            f"<tr>"
            f"<td>{e}</td>"
            f"<td>{rs}</td>"
            f"<td class='mono' title='{k}'>{ks}</td>"
            f"<td>{cs}</td>"
            f"<td>{abs_}</td>"
            f"<td>{mbs}</td>"
            f"<td>{r['created_at']}</td>"
            f"<td>"
            f"<form method='post' action='{admin_action}'"
            f" onsubmit=\"return confirm('{html.escape(_t('confirm_delete_key', lang), quote=True)}')\">"
            f"<input type='hidden' name='action' value='delete-key'>"
            f"<input type='hidden' name='email' value='{e}'>"
            f"<input type='hidden' name='role' value='{role_escaped}'>"
            f"<button class='btn-sm btn-warn'>{_t('confirm_delete_key', lang).rstrip('?')}</button></form>"
            f"</td>"
            f"<td>"
            f"<form method='post' action='{admin_action}'"
            f" onsubmit=\"return confirm('{html.escape(_t('confirm_delete_user', lang), quote=True)}')\">"
            f"<input type='hidden' name='action' value='delete-user'>"
            f"<input type='hidden' name='email' value='{e}'>"
            f"<input type='hidden' name='role' value='{role_escaped}'>"
            f"<button class='btn-sm btn-danger'>{'Nutzer löschen' if lang == 'de' else 'Delete user'}</button></form>"
            f"</td>"
            f"<td>"
            f"<form method='post' action='{admin_action}' style='display:flex;gap:4px'>"
            f"<input type='hidden' name='action' value='update-budget'>"
            f"<input type='hidden' name='email' value='{e}'>"
            f"<input type='hidden' name='role' value='{role_escaped}'>"
            f"<input type='number' name='budget' min='0' step='0.01' placeholder='€'"
            f" style='width:70px;padding:2px 4px;font-size:0.8rem'>"
            f"<button class='btn-sm btn-neutral'>{_t('set', lang)}</button></form>"
            f"</td>"
            f"</tr>\n"
        )

    role_options = "".join(
        f'<option value="{html.escape(role, quote=True)}">{html.escape(_role_label(role, lang))}</option>'
        for role in ROLE_BUDGETS
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
  <a class="btn-csv" href="{html.escape(_with_lang('/admin/export', lang), quote=True)}">{_t("csv_download", lang)}</a>
  <a class="btn-link-danger" href="{html.escape(_with_lang('/admin/reset-students', lang), quote=True)}">{_t("delete_students", lang)}</a>
  <form method="post" action="{admin_action}">
    <input type="hidden" name="action" value="send-test-inform-email">
    <button class="btn-mail" type="submit">{_t("send_test_info_mail", lang)}</button>
  </form>
  <form method="post" action="{admin_action}"
        onsubmit="return confirm('{html.escape(_t("confirm_send_info_mail", lang), quote=True)}')">
    <input type="hidden" name="action" value="send-inform-email">
    <button class="btn-mail" type="submit">{_t("send_info_mail", lang)}</button>
  </form>
  <span class="count">{_t("entries_count", lang, count=count)}</span>
  <span class="count">{_t("template_label", lang)}: <code>infomail.txt</code></span>
</div>
<div class="table-scroll">
  <table>
    <thead>
      <tr>
        <th>{_t("email_short_label", lang)}</th><th>{_t("column_role", lang)}</th><th>{_t("column_key", lang)}</th><th>{_t("column_code_status", lang)}</th>
        <th>{_t("column_available", lang)}</th><th>{_t("column_max_limit", lang)}</th><th>{_t("column_registered", lang)}</th><th></th><th></th><th>{_t("column_set_max_limit", lang)}</th>
      </tr>
    </thead>
    <tbody>
      {table_rows or f'<tr><td colspan="10">{_t("no_entries", lang)}</td></tr>'}
    </tbody>
  </table>
</div>

<div class="add-section">
  <h2>{_t("admin_add_user_heading", lang)}</h2>
  <form method="post" action="{admin_action}" class="add-form">
    <input type="hidden" name="action" value="add-user">
    <div>
      <label>{_t("email_short_label", lang)}</label><br>
      <input type="email" name="email" required placeholder="{_t("email_placeholder", lang)}"
             style="width:260px">
    </div>
    <div>
      <label>{_t("role_label", lang)}</label><br>
      <select name="role">{role_options}</select>
    </div>
    <div>
      <label>&nbsp;</label><br>
      <button class="btn-add" type="submit">{_t("add_user_button", lang)}</button>
    </div>
  </form>
</div>

<div class="add-section">
  <h2>{_t("bulk_set_limit_heading", lang)}</h2>
  <form method="post" action="{admin_action}" class="bulk-form"
        onsubmit="return confirm('{html.escape(_t("confirm_bulk_set_limit", lang), quote=True)}')">
    <input type="hidden" name="action" value="update-student-budgets">
    <div>
      <label>{_t("new_max_limit", lang)}</label><br>
      <input type="number" name="budget" min="0" step="0.01" required placeholder="€">
    </div>
    <div>
      <label>&nbsp;</label><br>
      <button class="btn-mail" type="submit">{_t("column_set_max_limit", lang)}</button>
    </div>
  </form>
</div>
"""
    return render_base(_t("admin_overview_title", lang), "admin", body, lang=lang, switch_path="/admin")


def render_admin_reset_students(lang: str = DEFAULT_LANG) -> str:
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
    {_t("reset_copy", lang)}
  </p>
  <form method="post" action="{html.escape(_with_lang('/admin/reset-students', lang), quote=True)}" class="danger-form"
        onsubmit="return confirm('{html.escape(_t("confirm_delete_all_students", lang), quote=True)}')">
    <div>
      <label>{_t("confirmation_label", lang)}</label><br>
      <input type="text" name="delete_confirmation" required placeholder="delete_all"
             autocomplete="off" spellcheck="false">
    </div>
    <div>
      <label>&nbsp;</label><br>
      <button class="btn-danger-wide" type="submit">{_t("delete_all_students", lang)}</button>
    </div>
  </form>
  <p class="danger-copy">
    {_t("delete_all_hint", lang)}
  </p>
</div>
<a class="back-link" href="{html.escape(_with_lang('/admin', lang), quote=True)}">← {_t("back_to_admin", lang)}</a>
"""
    return render_base(_t("reset_students_title", lang), "admin", body, lang=lang, switch_path="/admin/reset-students")


@app.get("/admin", response_class=HTMLResponse)
async def admin_overview(request: Request, flash: str = "", count: int = 0):
    if not _check_basic_auth(request):
        return _UNAUTHORIZED
    lang = _get_lang(request)
    flash_msg = {
        "key-deleted": _t("flash_key_deleted", lang),
        "user-deleted": _t("flash_user_deleted", lang),
        "budget-updated": _t("flash_budget_updated", lang),
        "student-budgets-updated": _t("flash_student_budgets_updated", lang),
        "user-added": _t("flash_user_added", lang),
        "students-reset": _t("flash_students_reset", lang),
        "test-inform-email-sent": _t("flash_test_info_mail_sent", lang),
    }.get(flash, "")
    if flash == "inform-email-sent":
        flash_msg = _t("flash_info_mail_sent", lang, count=count)
    rows = await _build_rows(request.app.state.pool, lang)
    return HTMLResponse(render_admin_overview(rows, flash_msg, lang))


@app.get("/admin/reset-students", response_class=HTMLResponse)
async def admin_reset_students_page(request: Request):
    if not _check_basic_auth(request):
        return _UNAUTHORIZED
    return HTMLResponse(render_admin_reset_students(_get_lang(request)))


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

    lang = _get_lang(request)
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
            return HTMLResponse(render_error(_t("error_delete_key", lang, error=exc), "admin", lang), status_code=502)
        return RedirectResponse(_with_lang("/admin", lang, flash="key-deleted"), status_code=303)

    elif action == "delete-user":
        try:
            tokens = await litellm_get_user_key_tokens(user_id)
            await litellm_delete_keys(tokens)
            await litellm_delete_user(user_id)
        except Exception as exc:
            logger.error("delete-user Fehler: %s", exc)
            return HTMLResponse(render_error(_t("error_delete_user", lang, error=exc), "admin", lang), status_code=502)
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM portal_users WHERE email = $1 AND role = $2", email, role)
            await conn.execute(
                "UPDATE portal_verification_codes SET used = TRUE WHERE email = $1 AND role = $2",
                email, role,
            )
        return RedirectResponse(_with_lang("/admin", lang, flash="user-deleted"), status_code=303)

    elif action == "update-budget":
        try:
            budget_val = float(budget)
        except (ValueError, TypeError):
            return HTMLResponse(render_error(_t("invalid_budget", lang), "admin", lang), status_code=400)
        if budget_val < 0:
            return HTMLResponse(render_error(_t("budget_negative", lang), "admin", lang), status_code=400)
        try:
            await litellm_update_budget(user_id, budget_val)
        except Exception as exc:
            logger.error("update-budget Fehler: %s", exc)
            return HTMLResponse(render_error(_t("error_update_budget", lang, error=exc), "admin", lang), status_code=502)
        return RedirectResponse(_with_lang("/admin", lang, flash="budget-updated"), status_code=303)

    elif action == "update-student-budgets":
        try:
            budget_val = float(budget)
        except (ValueError, TypeError):
            return HTMLResponse(render_error(_t("invalid_budget", lang), "admin", lang), status_code=400)
        if budget_val < 0:
            return HTMLResponse(render_error(_t("budget_negative", lang), "admin", lang), status_code=400)
        student_emails = await _get_student_emails(pool)
        if not student_emails:
            return HTMLResponse(render_error(_t("no_students_found", lang), "admin", lang), status_code=400)
        try:
            await asyncio.gather(*[
                litellm_update_budget(f"student:{student_email}", budget_val)
                for student_email in student_emails
            ])
        except Exception as exc:
            logger.error("update-student-budgets Fehler: %s", exc)
            return HTMLResponse(
                render_error(_t("error_update_student_budgets", lang, error=exc), "admin", lang),
                status_code=502,
            )
        return RedirectResponse(_with_lang("/admin", lang, flash="student-budgets-updated"), status_code=303)

    elif action == "add-user":
        if role not in ROLE_BUDGETS:
            return HTMLResponse(render_error(_t("unknown_role", lang, role=role), "admin", lang), status_code=400)
        try:
            await litellm_create_user(user_id, ROLE_BUDGETS[role])
        except HTTPStatusError as exc:
            if exc.response.status_code != 409:
                return HTMLResponse(render_error(_t("error_create_user", lang, error=exc), "admin", lang), status_code=502)
            try:
                await litellm_update_budget(user_id, ROLE_BUDGETS[role])
            except Exception as update_exc:
                return HTMLResponse(
                    render_error(_t("error_update_existing_user", lang, error=update_exc), "admin", lang),
                    status_code=502,
                )
        except Exception as exc:
            return HTMLResponse(render_error(_t("error_create_user", lang, error=exc), "admin", lang), status_code=502)
        try:
            existing_tokens = await litellm_get_user_key_tokens(user_id)
            if existing_tokens:
                await litellm_delete_keys(existing_tokens)
        except Exception as exc:
            return HTMLResponse(
                render_error(_t("error_rotate_keys", lang, error=exc), "admin", lang),
                status_code=502,
            )
        try:
            api_key = await litellm_generate_key(user_id, ROLE_BUDGETS[role])
        except Exception as exc:
            return HTMLResponse(render_error(_t("error_create_api_key", lang, error=exc), "admin", lang), status_code=502)
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO portal_users (email, role) VALUES ($1, $2) ON CONFLICT (email, role) DO NOTHING",
                    email,
                    role,
                )
        except asyncpg.UniqueViolationError:
            return HTMLResponse(render_error(_t("user_already_exists", lang), "admin", lang), status_code=409)
        body = (
            f"<div class='success'>{_t('add_user_success', lang, email=html.escape(email))}</div>"
            f"<div class='key-box'>{api_key}</div>"
            f"<p class='hint'>{_t('role_budget_endpoint', lang, role=html.escape(_role_label(role, lang)), budget=ROLE_BUDGETS[role], endpoint=html.escape(LITELLM_BASE_URL))}</p>"
            f"<br><a href='{html.escape(_with_lang('/admin', lang), quote=True)}'>← {_t('back_to_overview', lang)}</a>"
        )
        return HTMLResponse(render_base(_t("user_created_title", lang), "admin", body, lang=lang, switch_path="/admin"))

    elif action == "send-inform-email":
        recipients = await _get_infomail_recipients(pool)
        if not recipients:
            return HTMLResponse(render_error(_t("no_recipients", lang), "admin", lang), status_code=400)
        try:
            sent_count = await asyncio.to_thread(send_inform_email, recipients, lang)
        except Exception as exc:
            logger.error("send-inform-email Fehler: %s", exc)
            return HTMLResponse(
                render_error(_t("error_send_info_mail", lang, error=exc), "admin", lang),
                status_code=502,
            )
        return RedirectResponse(_with_lang("/admin", lang, flash="inform-email-sent", count=str(sent_count)), status_code=303)

    elif action == "send-test-inform-email":
        if not TEST_INFO_EMAIL:
            return HTMLResponse(
                render_error(_t("test_info_email_missing", lang), "admin", lang),
                status_code=400,
            )
        try:
            sent_count = await asyncio.to_thread(send_inform_email, [TEST_INFO_EMAIL], lang)
        except Exception as exc:
            logger.error("send-test-inform-email Fehler: %s", exc)
            return HTMLResponse(
                render_error(_t("error_send_test_info_mail", lang, error=exc), "admin", lang),
                status_code=502,
            )
        if sent_count != 1:
            return HTMLResponse(
                render_error(_t("test_info_email_count_error", lang), "admin", lang),
                status_code=502,
            )
        return RedirectResponse(_with_lang("/admin", lang, flash="test-inform-email-sent"), status_code=303)

    return HTMLResponse(render_error(_t("unknown_action", lang, action=action), "admin", lang), status_code=400)


@app.post("/admin/reset-students", response_class=HTMLResponse)
async def admin_reset_students_post(
    request: Request,
    delete_confirmation: str = Form(""),
):
    if not _check_basic_auth(request):
        return _UNAUTHORIZED
    lang = _get_lang(request)
    if delete_confirmation.strip() != "delete_all":
        return HTMLResponse(
            render_error(_t("delete_all_missing", lang), "admin", lang),
            status_code=400,
        )
    try:
        exit_code = await reset_students_script.run_student_reset(dry_run=False, confirm=True)
    except Exception as exc:
        logger.error("reset-students Fehler: %s", exc)
        return HTMLResponse(
            render_error(_t("error_run_student_reset", lang, error=exc), "admin", lang),
            status_code=502,
        )
    if exit_code == 0:
        return RedirectResponse(_with_lang("/admin", lang, flash="students-reset"), status_code=303)
    if exit_code == 2:
        return HTMLResponse(
            render_error(
                _t("student_reset_partial_error", lang),
                "admin",
                lang,
            ),
            status_code=502,
        )
    return HTMLResponse(
        render_error(_t("student_reset_failed", lang), "admin", lang),
        status_code=502,
    )


@app.get("/admin/export")
async def admin_overview_export(request: Request):
    if not _check_basic_auth(request):
        return _UNAUTHORIZED
    rows = await _build_rows(request.app.state.pool, _get_lang(request))
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
async def landing(request: Request, role: str = Path(...)):
    lang = _get_lang(request)
    if err := _check_role(role, lang):
        return err
    return render_landing(role, lang)


@app.get("/{role}/enter-code", response_class=HTMLResponse)
async def enter_code(request: Request, role: str = Path(...), email: str = ""):
    lang = _get_lang(request)
    if err := _check_role(role, lang):
        return err
    return render_enter_code(role, email.strip().lower(), lang)


@app.post("/{role}/request-code", response_class=HTMLResponse)
@limiter.limit(RATE_LIMIT_REQUEST_CODE)
async def request_code(
    request: Request,
    role: str = Path(...),
    email: str = Form(...),
):
    lang = _get_lang(request)
    if err := _check_role(role, lang):
        return err

    email = email.strip().lower()
    logger.debug("request_code: email=%s role=%s", email, role)

    ok, err_msg = validate_email(email, role, lang)
    if not ok:
        logger.debug("Validierungsfehler: %s", err_msg)
        return HTMLResponse(render_error(err_msg, role, lang), status_code=400)

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
                    _t("cooldown_error", lang, minutes=CODE_COOLDOWN_MINUTES),
                    role,
                    lang,
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
        await asyncio.to_thread(send_verification_email, email, code, role, lang)
    except Exception as exc:
        logger.error("E-Mail-Fehler: %s", exc)
        return HTMLResponse(
            render_error(_t("email_send_failed", lang, error=exc), role, lang),
            status_code=503,
        )

    logger.info("Code angefordert: email=%s role=%s", email, role)
    return HTMLResponse(render_code_sent(role, email, lang))


@app.post("/{role}/verify-and-get-key", response_class=HTMLResponse)
@limiter.limit(RATE_LIMIT_VERIFY)
async def verify_and_get_key(
    request: Request,
    role: str = Path(...),
    email: str = Form(...),
    code: str = Form(...),
):
    lang = _get_lang(request)
    if err := _check_role(role, lang):
        return err

    email = email.strip().lower()
    code = "".join(code.split())

    if len(email) > 254:
        return HTMLResponse(render_error(_t("invalid_email", lang), role, lang), status_code=400)

    if not code.isdigit() or len(code) != 6:
        return HTMLResponse(
            render_error(_t("invalid_code", lang), role, lang),
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
                    _t("no_valid_code", lang),
                    role,
                    lang,
                ),
                status_code=400,
            )

        if not verify_code(code, row["hashed_code"]):
            return HTMLResponse(render_error(_t("wrong_code", lang), role, lang), status_code=400)

        await conn.execute(
            "UPDATE portal_verification_codes SET used = TRUE WHERE id = $1", row["id"]
        )

        user_id = f"{role}:{email}"
        budget = ROLE_BUDGETS[role]

        try:
            exists = await litellm_user_exists(user_id)
        except Exception as exc:
            return HTMLResponse(
                render_error(_t("error_litellm_check", lang, error=exc), role, lang),
                status_code=502,
            )

        if exists:
            try:
                tokens = await litellm_get_user_key_tokens(user_id)
            except Exception as exc:
                return HTMLResponse(
                    render_error(_t("error_fetch_keys", lang, error=exc), role, lang),
                    status_code=502,
                )
            try:
                if tokens:
                    await litellm_delete_keys(tokens)
                await litellm_update_budget(user_id, budget)
            except Exception as exc:
                return HTMLResponse(
                    render_error(_t("error_update_litellm_user", lang, error=exc), role, lang),
                    status_code=502,
                )
        else:
            try:
                await litellm_create_user(user_id, budget)
            except Exception as exc:
                return HTMLResponse(
                    render_error(_t("error_create_litellm_user", lang, error=exc), role, lang),
                    status_code=502,
                )

        try:
            api_key = await litellm_generate_key(user_id, budget)
        except Exception as exc:
            return HTMLResponse(
                render_error(_t("error_create_api_key", lang, error=exc), role, lang),
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
    return HTMLResponse(render_key_issued(role, email, api_key, lang))


def run() -> None:
    uvicorn.run(app, host="0.0.0.0", port=8080)


if __name__ == "__main__":
    run()
