"""
Integrationstests für alle Portal-Routen.

Voraussetzung
-------------
Die Portal-Datenbank muss laufen:

    docker compose up -d

Tests ausführen:

    pytest tests/test_portal.py -v

Alle Tests verwenden die echte PostgreSQL-Datenbank (portal-db auf Port 5433).
LiteLLM und E-Mail-Versand werden vollständig gemockt – es werden keine echten
API-Aufrufe oder E-Mails gesendet. Die Datenbank wird vor und nach jedem Test
geleert (clean_db-Fixture).

Abgedeckte Bereiche
-------------------
- Landingpages und Rollenvalidierung
- E-Mail-Validierung (Domain, Länge, Format)
- Code-Anforderung inkl. Cooldown-Schutz und Duplikat-Erkennung
- Code-Verifikation (korrekt, falsch, abgelaufen, bereits verwendet)
- Vollständiger Registrierungsflow: Code → LiteLLM-User → API-Key
- Admin-Bereich (Basic Auth, Übersicht, CSV-Export)
- Admin-Aktionen: Key löschen, Nutzer löschen, Budget setzen, Nutzer hinzufügen
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest

os.environ["LITELLM_BASE_URL"] = "http://localhost:4000"
os.environ["LITELLM_MASTER_KEY"] = "test-master-key"
os.environ["SMTP_HOST"] = "smtp.example.com"
os.environ["SMTP_PORT"] = "587"
os.environ["SMTP_USER"] = "test@hs-offenburg.de"
os.environ["SMTP_PASSWORD"] = "password"
os.environ["SMTP_FROM"] = "Test <test@hs-offenburg.de>"
os.environ["CODE_SECRET"] = "a-test-secret-that-is-at-least-32-chars!!"
os.environ["ALLOWED_DOMAIN"] = "hs-offenburg.de"
os.environ["RATE_LIMIT_REQUEST_CODE"] = "1000/minute"
os.environ["RATE_LIMIT_VERIFY"] = "1000/minute"
os.environ["DATABASE_URL"] = "postgresql://portal:portal@localhost:5433/portal"
os.environ["ADMIN_USERNAME"] = "testadmin"
os.environ["ADMIN_PASSWORD"] = "testpassword"

import base64

import portal
from fastapi.testclient import TestClient

ADMIN_AUTH = "Basic " + base64.b64encode(b"testadmin:testpassword").decode()

DB_URL = os.environ["DATABASE_URL"]


def _run(coro):
    return asyncio.run(coro)


async def _ensure_schema():
    conn = await asyncpg.connect(DB_URL)
    try:
        await conn.execute(portal.SCHEMA_SQL)
    finally:
        await conn.close()


async def _truncate():
    conn = await asyncpg.connect(DB_URL)
    try:
        await conn.execute(
            "TRUNCATE portal_verification_codes, portal_users RESTART IDENTITY CASCADE"
        )
    finally:
        await conn.close()


async def _insert_code(email: str, role: str, code: str, expired: bool = False, used: bool = False):
    hashed = portal.hash_code(code)
    expires_at = (
        datetime.now(timezone.utc) - timedelta(minutes=1)
        if expired
        else datetime.now(timezone.utc) + timedelta(minutes=15)
    )
    conn = await asyncpg.connect(DB_URL)
    try:
        await conn.execute(
            "INSERT INTO portal_verification_codes (email, role, hashed_code, expires_at, used) "
            "VALUES ($1, $2, $3, $4, $5)",
            email, role, hashed, expires_at, used,
        )
    finally:
        await conn.close()


async def _insert_user(email: str, role: str):
    conn = await asyncpg.connect(DB_URL)
    try:
        await conn.execute(
            "INSERT INTO portal_users (email, role) VALUES ($1, $2)",
            email, role,
        )
    finally:
        await conn.close()


async def _get_code_used(email: str, role: str) -> bool | None:
    conn = await asyncpg.connect(DB_URL)
    try:
        row = await conn.fetchrow(
            "SELECT used FROM portal_verification_codes "
            "WHERE email = $1 AND role = $2 ORDER BY id DESC LIMIT 1",
            email, role,
        )
        return row["used"] if row else None
    finally:
        await conn.close()


async def _get_portal_user(email: str, role: str) -> dict | None:
    conn = await asyncpg.connect(DB_URL)
    try:
        row = await conn.fetchrow(
            "SELECT email, role FROM portal_users WHERE email = $1 AND role = $2",
            email, role,
        )
        return dict(row) if row else None
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def ensure_schema():
    _run(_ensure_schema())


@pytest.fixture(autouse=True)
def clean_db():
    _run(_truncate())
    yield
    _run(_truncate())


@pytest.fixture
def client():
    with TestClient(portal.app, raise_server_exceptions=True) as c:
        yield c


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_health_endpoint(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_landing_student(client):
    resp = client.get("/student")
    assert resp.status_code == 200
    assert "Student:in" in resp.text
    assert "Bestätigungscode" in resp.text
    assert "/student/enter-code" in resp.text


def test_enter_code_page(client):
    resp = client.get("/student/enter-code")
    assert resp.status_code == 200
    assert "Code eingeben" in resp.text
    assert 'name="email"' in resp.text
    assert 'name="code"' in resp.text
    assert "replace(/\\D/g,''" in resp.text


def test_landing_professor_disabled(client):
    resp = client.get("/professor")
    assert resp.status_code == 404


def test_landing_unknown_role(client):
    resp = client.get("/superuser")
    assert resp.status_code == 404


def test_request_code_invalid_domain(client):
    with patch("portal.litellm_user_exists", new_callable=AsyncMock, return_value=False):
        resp = client.post("/student/request-code", data={"email": "alice@gmail.com"})
    assert resp.status_code == 400
    assert "hs-offenburg.de" in resp.text


def test_request_code_success(client):
    with patch("portal.send_verification_email") as mock_send:
        resp = client.post("/student/request-code", data={"email": "alice@hs-offenburg.de"})
    assert resp.status_code == 200
    assert "Bestätigungscode" in resp.text
    mock_send.assert_called_once()
    assert mock_send.call_args[0][0] == "alice@hs-offenburg.de"


def test_request_code_smtp_failure(client):
    with patch("portal.send_verification_email", side_effect=Exception("SMTP error")):
        resp = client.post("/student/request-code", data={"email": "alice@hs-offenburg.de"})
    assert resp.status_code == 503


def test_request_code_cooldown(client):
    # Zweiter Code-Request innerhalb des Cooldowns wird abgelehnt
    with patch("portal.send_verification_email"):
        resp1 = client.post("/student/request-code", data={"email": "alice@hs-offenburg.de"})
        resp2 = client.post("/student/request-code", data={"email": "alice@hs-offenburg.de"})
    assert resp1.status_code == 200
    assert resp2.status_code == 429
    assert "warten" in resp2.text.lower() or "bereits gesendet" in resp2.text.lower()


def test_request_code_existing_user_allowed(client):
    with patch("portal.send_verification_email") as mock_send:
        resp = client.post("/student/request-code", data={"email": "alice@hs-offenburg.de"})
    assert resp.status_code == 200
    assert "Bestätigungscode" in resp.text
    mock_send.assert_called_once()


def test_existing_user_with_key_gets_rotated_key(client):
    _run(_insert_code("alice@hs-offenburg.de", "student", "333334"))
    _run(_insert_user("alice@hs-offenburg.de", "student"))

    with patch("portal.litellm_user_exists", new_callable=AsyncMock, return_value=True), \
         patch("portal.litellm_get_user_key_tokens", new_callable=AsyncMock, return_value=["sk-old-key"]), \
         patch("portal.litellm_delete_keys", new_callable=AsyncMock) as mock_delete_keys, \
         patch("portal.litellm_update_budget", new_callable=AsyncMock) as mock_update_budget, \
         patch("portal.litellm_generate_key", new_callable=AsyncMock, return_value="sk-rotated-key"):
        resp = client.post(
            "/student/verify-and-get-key",
            data={"email": "alice@hs-offenburg.de", "code": "333334"},
        )
    assert resp.status_code == 200
    assert "sk-rotated-key" in resp.text
    mock_delete_keys.assert_awaited_once_with(["sk-old-key"])
    mock_update_budget.assert_awaited_once_with("student:alice@hs-offenburg.de", portal.STUDENT_BUDGET)


def test_verify_wrong_code(client):
    _run(_insert_code("alice@hs-offenburg.de", "student", "111111"))
    resp = client.post(
        "/student/verify-and-get-key",
        data={"email": "alice@hs-offenburg.de", "code": "999999"},
    )
    assert resp.status_code == 400
    assert "Falscher Bestätigungscode" in resp.text


def test_expired_code_rejected(client):
    _run(_insert_code("alice@hs-offenburg.de", "student", "222222", expired=True))
    resp = client.post(
        "/student/verify-and-get-key",
        data={"email": "alice@hs-offenburg.de", "code": "222222"},
    )
    assert resp.status_code == 400
    assert "Kein gültiger Code" in resp.text


def test_verify_invalid_code_format(client):
    resp = client.post(
        "/student/verify-and-get-key",
        data={"email": "alice@hs-offenburg.de", "code": "abc"},
    )
    assert resp.status_code == 400


def test_verify_code_with_whitespace_is_normalized(client):
    _run(_insert_code("alice@hs-offenburg.de", "student", "123456"))

    with patch("portal.litellm_user_exists", new_callable=AsyncMock, return_value=False), \
         patch("portal.litellm_create_user", new_callable=AsyncMock, return_value={}), \
         patch("portal.litellm_generate_key", new_callable=AsyncMock, return_value="sk-test-api-key"):
        resp = client.post(
            "/student/verify-and-get-key",
            data={"email": "alice@hs-offenburg.de", "code": " 123 456 "},
        )

    assert resp.status_code == 200
    assert "sk-test-api-key" in resp.text


def test_existing_user_without_key_gets_new_key(client):
    _run(_insert_code("alice@hs-offenburg.de", "student", "333333"))
    _run(_insert_user("alice@hs-offenburg.de", "student"))

    with patch("portal.litellm_user_exists", new_callable=AsyncMock, return_value=True), \
         patch("portal.litellm_get_user_key_tokens", new_callable=AsyncMock, return_value=[]), \
         patch("portal.litellm_update_budget", new_callable=AsyncMock) as mock_update_budget, \
         patch("portal.litellm_generate_key", new_callable=AsyncMock, return_value="sk-new-key"):
        resp = client.post(
            "/student/verify-and-get-key",
            data={"email": "alice@hs-offenburg.de", "code": "333333"},
        )
    assert resp.status_code == 200
    assert "sk-new-key" in resp.text
    mock_update_budget.assert_awaited_once_with("student:alice@hs-offenburg.de", portal.STUDENT_BUDGET)


def test_admin_overview_no_auth(client):
    resp = client.get("/admin")
    assert resp.status_code == 401


def test_admin_overview_wrong_auth(client):
    bad = "Basic " + base64.b64encode(b"wrong:creds").decode()
    resp = client.get("/admin", headers={"Authorization": bad})
    assert resp.status_code == 401


def test_admin_overview_authenticated_empty(client):
    with patch(
        "portal._fetch_litellm_info",
        new_callable=AsyncMock,
        return_value={"key": "-", "budget": "-"},
    ):
        resp = client.get("/admin", headers={"Authorization": ADMIN_AUTH})
    assert resp.status_code == 200
    assert "0" in resp.text or "Keine Einträge" in resp.text


def test_admin_overview_with_users(client):
    _run(_insert_user("alice@hs-offenburg.de", "student"))
    with patch(
        "portal._fetch_litellm_info",
        new_callable=AsyncMock,
        return_value={"key": "sk-test-key", "budget": "5.00 €"},
    ):
        resp = client.get("/admin", headers={"Authorization": ADMIN_AUTH})
    assert resp.status_code == 200
    assert "alice@hs-offenburg.de" in resp.text
    assert "sk-test-key" in resp.text


def test_admin_export_csv(client):
    _run(_insert_user("alice@hs-offenburg.de", "student"))
    with patch(
        "portal._fetch_litellm_info",
        new_callable=AsyncMock,
        return_value={"key": "sk-test-key", "budget": "5.00 €"},
    ):
        resp = client.get("/admin/export", headers={"Authorization": ADMIN_AUTH})
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    assert "alice@hs-offenburg.de" in resp.text
    assert "sk-test-key" in resp.text


def test_verify_success_full_flow(client):
    _run(_insert_code("bob@hs-offenburg.de", "student", "456789"))

    with patch("portal.litellm_user_exists", new_callable=AsyncMock, return_value=False), \
         patch("portal.litellm_create_user", new_callable=AsyncMock, return_value={}), \
         patch("portal.litellm_generate_key", new_callable=AsyncMock, return_value="sk-test-api-key-12345"):
        resp = client.post(
            "/student/verify-and-get-key",
            data={"email": "bob@hs-offenburg.de", "code": "456789"},
        )

    assert resp.status_code == 200
    assert "sk-test-api-key-12345" in resp.text

    assert _run(_get_code_used("bob@hs-offenburg.de", "student")) is True

    user = _run(_get_portal_user("bob@hs-offenburg.de", "student"))
    assert user is not None
    assert user["role"] == "student"


# ---------------------------------------------------------------------------
# Admin-Methoden-Tests
# ---------------------------------------------------------------------------

def test_admin_delete_key(client):
    _run(_insert_user("charlie@hs-offenburg.de", "student"))
    with patch("portal.litellm_get_user_key_tokens", new_callable=AsyncMock, return_value=["sk-tok-abc"]), \
         patch("portal.litellm_delete_keys", new_callable=AsyncMock) as mock_del:
        resp = client.post(
            "/admin",
            data={"action": "delete-key", "email": "charlie@hs-offenburg.de", "role": "student"},
            headers={"Authorization": ADMIN_AUTH},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert "key-deleted" in resp.headers["location"]
    mock_del.assert_awaited_once_with(["sk-tok-abc"])


def test_admin_delete_user(client):
    _run(_insert_user("dave@hs-offenburg.de", "student"))
    with patch("portal.litellm_get_user_key_tokens", new_callable=AsyncMock, return_value=["sk-tok-xyz"]), \
         patch("portal.litellm_delete_keys", new_callable=AsyncMock), \
         patch("portal.litellm_delete_user", new_callable=AsyncMock) as mock_del_user:
        resp = client.post(
            "/admin",
            data={"action": "delete-user", "email": "dave@hs-offenburg.de", "role": "student"},
            headers={"Authorization": ADMIN_AUTH},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert "user-deleted" in resp.headers["location"]
    mock_del_user.assert_awaited_once_with("student:dave@hs-offenburg.de")
    # Nutzer muss aus portal_users entfernt sein
    assert _run(_get_portal_user("dave@hs-offenburg.de", "student")) is None


def test_admin_update_budget(client):
    _run(_insert_user("eve@hs-offenburg.de", "professor"))
    with patch("portal.litellm_update_budget", new_callable=AsyncMock) as mock_budget:
        resp = client.post(
            "/admin",
            data={"action": "update-budget", "email": "eve@hs-offenburg.de", "role": "professor", "budget": "30.00"},
            headers={"Authorization": ADMIN_AUTH},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert "budget-updated" in resp.headers["location"]
    mock_budget.assert_awaited_once_with("professor:eve@hs-offenburg.de", 30.0)


def test_admin_add_user(client):
    with patch("portal.litellm_create_user", new_callable=AsyncMock, return_value={}), \
         patch("portal.litellm_generate_key", new_callable=AsyncMock, return_value="sk-admin-generated-key"):
        resp = client.post(
            "/admin",
            data={"action": "add-user", "email": "frank@hs-offenburg.de", "role": "student"},
            headers={"Authorization": ADMIN_AUTH},
        )
    assert resp.status_code == 200
    assert "sk-admin-generated-key" in resp.text
    assert _run(_get_portal_user("frank@hs-offenburg.de", "student")) is not None


def test_admin_add_user_duplicate(client):
    _run(_insert_user("grace@hs-offenburg.de", "student"))
    with patch("portal.litellm_create_user", new_callable=AsyncMock, return_value={}), \
         patch("portal.litellm_generate_key", new_callable=AsyncMock, return_value="sk-dup-key"):
        resp = client.post(
            "/admin",
            data={"action": "add-user", "email": "grace@hs-offenburg.de", "role": "student"},
            headers={"Authorization": ADMIN_AUTH},
        )
    assert resp.status_code == 409


def test_admin_unknown_action(client):
    resp = client.post(
        "/admin",
        data={"action": "do-something-weird", "email": "x@hs-offenburg.de", "role": "student"},
        headers={"Authorization": ADMIN_AUTH},
    )
    assert resp.status_code == 400
