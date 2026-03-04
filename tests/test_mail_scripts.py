import asyncio
import os
from pathlib import Path

from scripts import send_info_mail, send_test_info_mail


def test_send_test_info_mail_dry_run(monkeypatch):
    monkeypatch.setenv("TEST_INFO_EMAIL", "test@hs-offenburg.de")
    monkeypatch.setattr(send_test_info_mail, "_load_env", lambda: None)
    monkeypatch.setattr(send_test_info_mail, "TEMPLATE_PATH", Path("infomail.txt"))
    monkeypatch.setattr(send_test_info_mail, "_validate_template", lambda: None)

    class LoaderCalled(Exception):
        pass

    def should_not_load():
        raise LoaderCalled()

    monkeypatch.setattr(send_test_info_mail, "_load_portal_module", should_not_load)

    assert send_test_info_mail.run(dry_run=True, confirm=False) == 0


def test_send_test_info_mail_confirm(monkeypatch):
    monkeypatch.setenv("TEST_INFO_EMAIL", "test@hs-offenburg.de")
    monkeypatch.setattr(send_test_info_mail, "_load_env", lambda: None)
    monkeypatch.setattr(send_test_info_mail, "_validate_template", lambda: None)

    calls = []

    class FakePortal:
        @staticmethod
        def send_inform_email(recipients):
            calls.append(recipients)
            return 1

    monkeypatch.setattr(send_test_info_mail, "_load_portal_module", lambda: FakePortal)

    assert send_test_info_mail.run(dry_run=False, confirm=True) == 0
    assert calls == [["test@hs-offenburg.de"]]


def test_send_test_info_mail_missing_recipient(monkeypatch):
    monkeypatch.delenv("TEST_INFO_EMAIL", raising=False)
    monkeypatch.setattr(send_test_info_mail, "_load_env", lambda: None)

    assert send_test_info_mail.run(dry_run=False, confirm=True) == 1


def test_send_info_mail_dry_run(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://portal:portal@localhost:5433/portal")
    monkeypatch.setattr(send_info_mail, "_load_env", lambda: None)
    monkeypatch.setattr(send_info_mail, "_validate_template", lambda: None)

    async def fake_get_recipients(database_url: str):
        assert database_url
        return ["alice@hs-offenburg.de", "bob@hs-offenburg.de"]

    monkeypatch.setattr(send_info_mail, "_get_recipients", fake_get_recipients)

    assert asyncio.run(send_info_mail.run(dry_run=True, confirm=False)) == 0


def test_send_info_mail_confirm(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://portal:portal@localhost:5433/portal")
    monkeypatch.setattr(send_info_mail, "_load_env", lambda: None)
    monkeypatch.setattr(send_info_mail, "_validate_template", lambda: None)

    async def fake_get_recipients(database_url: str):
        assert database_url
        return ["alice@hs-offenburg.de", "bob@hs-offenburg.de"]

    calls = []

    class FakePortal:
        @staticmethod
        def send_inform_email(recipients):
            calls.append(recipients)
            return 2

    monkeypatch.setattr(send_info_mail, "_get_recipients", fake_get_recipients)
    monkeypatch.setattr(send_info_mail, "_load_portal_module", lambda: FakePortal)

    assert asyncio.run(send_info_mail.run(dry_run=False, confirm=True)) == 0
    assert calls == [["alice@hs-offenburg.de", "bob@hs-offenburg.de"]]


def test_send_info_mail_no_recipients(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://portal:portal@localhost:5433/portal")
    monkeypatch.setattr(send_info_mail, "_load_env", lambda: None)
    monkeypatch.setattr(send_info_mail, "_validate_template", lambda: None)

    async def fake_get_recipients(database_url: str):
        assert database_url
        return []

    monkeypatch.setattr(send_info_mail, "_get_recipients", fake_get_recipients)

    assert asyncio.run(send_info_mail.run(dry_run=False, confirm=True)) == 0


def test_send_info_mail_db_error(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://portal:portal@localhost:5433/portal")
    monkeypatch.setattr(send_info_mail, "_load_env", lambda: None)
    monkeypatch.setattr(send_info_mail, "_validate_template", lambda: None)

    async def fake_get_recipients(database_url: str):
        raise RuntimeError("db down")

    monkeypatch.setattr(send_info_mail, "_get_recipients", fake_get_recipients)

    assert asyncio.run(send_info_mail.run(dry_run=False, confirm=True)) == 1
