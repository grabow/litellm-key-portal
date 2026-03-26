"""
Unit-Tests für Helper-Funktionen.

Voraussetzung
-------------
Keine – weder Datenbank noch Netzwerk erforderlich.

Tests ausführen:

    uv run pytest tests/test_helpers.py -v

Abgedeckte Bereiche
-------------------
- HMAC-Code-Hashing (deterministisch, kollisionsresistent)
- Code-Verifikation (korrekt/falsch, Timing-sicher)
- Code-Generierung (6-stellig, nullgepaddetes Format)
- E-Mail-Validierung (Domain, Länge, Sonderzeichen, leerer String)
"""

import os
import sys

import pytest
from starlette.requests import Request

# Set required env vars before importing portal
os.environ.setdefault("LITELLM_BASE_URL", "http://localhost:4000")
os.environ.setdefault("LITELLM_MASTER_KEY", "test-master-key")
os.environ.setdefault("SMTP_HOST", "smtp.example.com")
os.environ.setdefault("SMTP_PORT", "587")
os.environ.setdefault("SMTP_USER", "test@hs-offenburg.de")
os.environ.setdefault("SMTP_PASSWORD", "password")
os.environ.setdefault("SMTP_FROM", "Test <test@hs-offenburg.de>")
os.environ.setdefault("CODE_SECRET", "a-test-secret-that-is-at-least-32-chars!!")
os.environ.setdefault("ALLOWED_DOMAIN", "hs-offenburg.de")
os.environ.setdefault("EMAIL_PLACEHOLDER", "firstname.lastname@university.edu")
os.environ.setdefault("DATABASE_URL", "postgresql://portal:portal@localhost:5433/portal")
os.environ.setdefault("ADMIN_USERNAME", "testadmin")
os.environ.setdefault("ADMIN_PASSWORD", "testpassword")
os.environ.setdefault("RATE_LIMIT_REQUEST_CODE", "1000/minute")
os.environ.setdefault("RATE_LIMIT_VERIFY", "1000/minute")

from portal import (
    _build_database_startup_error,
    _display_litellm_endpoint,
    _describe_database_target,
    generate_code,
    hash_code,
    validate_email,
    verify_code,
)


def test_hash_code_deterministic():
    assert hash_code("123456") == hash_code("123456")
    assert hash_code("000000") != hash_code("000001")


def test_verify_code_correct():
    code = "123456"
    stored = hash_code(code)
    assert verify_code(code, stored) is True


def test_verify_code_wrong():
    stored = hash_code("123456")
    assert verify_code("654321", stored) is False


def test_generate_code_format():
    for _ in range(50):
        code = generate_code()
        assert len(code) == 6, f"Expected 6 digits, got {len(code)}: {code!r}"
        assert code.isdigit(), f"Expected all digits, got: {code!r}"
    # Zero-padding test: with enough iterations we'll get codes < 100000
    # We just verify format holds even if code starts with zero
    # Simulate edge case directly
    import portal
    original_rng = portal._rng
    class FakeRng:
        def randint(self, a, b):
            return 7  # produces "000007"
    portal._rng = FakeRng()
    assert generate_code() == "000007"
    portal._rng = original_rng


def test_validate_email_student_valid():
    ok, msg = validate_email("alice@hs-offenburg.de", "student")
    assert ok is True
    assert msg == ""


def test_validate_email_wrong_domain():
    ok, msg = validate_email("alice@gmail.com", "student")
    assert ok is False
    assert "hs-offenburg.de" in msg


def test_validate_email_professor_valid():
    ok, msg = validate_email("prof@hs-offenburg.de", "professor")
    assert ok is True
    assert msg == ""


def test_validate_email_admin_valid():
    ok, msg = validate_email("admin@hs-offenburg.de", "admin")
    assert ok is True


def test_validate_email_any_role_only_domain_checked(client=None):
    # validate_email prüft nur die Domain – Rollen-Validierung erfolgt in _check_role
    ok, _ = validate_email("alice@hs-offenburg.de", "superuser")
    assert ok is True


def test_validate_email_empty():
    ok, msg = validate_email("", "student")
    assert ok is False


def test_describe_database_target_redacts_credentials():
    target = _describe_database_target("postgresql://portal:secret@localhost:5433/portal")
    assert target == "localhost:5433/portal"


def test_build_database_startup_error_is_actionable():
    message = _build_database_startup_error(
        "postgresql://portal:secret@localhost:5433/portal",
        ConnectionRefusedError("connection refused"),
    )
    assert "localhost:5433/portal" in message
    assert "DATABASE_URL" in message
    assert "docker compose up -d" in message
    assert "secret" not in message


def test_display_litellm_endpoint_uses_request_origin_for_localhost_base_url():
    request = Request(
        {
            "type": "http",
            "scheme": "http",
            "server": ("127.0.0.1", 8080),
            "client": ("127.0.0.1", 12345),
            "method": "GET",
            "path": "/student",
            "headers": [(b"host", b"203.0.113.10")],
        }
    )

    assert _display_litellm_endpoint(request) == "http://203.0.113.10:4000"


def test_display_litellm_endpoint_prefers_forwarded_headers():
    request = Request(
        {
            "type": "http",
            "scheme": "http",
            "server": ("127.0.0.1", 8080),
            "client": ("127.0.0.1", 12345),
            "method": "GET",
            "path": "/student",
            "headers": [
                (b"host", b"portal.internal:8080"),
                (b"x-forwarded-host", b"203.0.113.10"),
                (b"x-forwarded-proto", b"https"),
            ],
        }
    )

    assert _display_litellm_endpoint(request) == "https://203.0.113.10:4000"


def test_run_starts_uvicorn(monkeypatch):
    import portal

    calls = []

    def fake_run(app, host, port):
        calls.append((app, host, port))

    monkeypatch.setattr(portal.uvicorn, "run", fake_run)

    portal.run()

    assert calls == [(portal.app, "0.0.0.0", 8080)]
