# HSOG LiteLLM Key Portal

Self-service portal zur Ausgabe von LiteLLM API-Schlüsseln für Studierende und Admins der Hochschule Offenburg.

---

## Architektur

```
Nutzer (Student / Admin)
        |
        v
FastAPI Portal (nur VPN)       ← portal.py
        |
        v
LiteLLM Proxy (Admin-API, intern)
        |
        v
OpenAI / Campus-GPT
```

Der Portal-Prozess kommuniziert mit LiteLLM über den `MASTER_KEY`. Dieser wird nie an Endnutzer weitergegeben.

---

## Rollenmodell

Rollen werden über **URLs** gesteuert – kein Dropdown im Formular:

| URL | Zielgruppe | Status |
|---|---|---|
| `/student` | Studierende | aktiv |
| `/professor` | Professorinnen / Professoren | **deaktiviert** (404) |
| `/admin` | Administratoren | aktiv |

> **Professoren-Route deaktiviert:** `/professor` ist derzeit abgeschaltet (liefert 404).
> Da sich anhand der E-Mail-Adresse nicht automatisch entscheiden lässt, wer Professor ist,
> erfolgt die Registrierung ausschließlich manuell durch den Admin über `/admin/overview`
> (Rolle `professor` auswählen, Key wird generiert und dem Betroffenen direkt mitgeteilt).
>
> Zum späteren Reaktivieren des Self-Service: `"professor"` in `ROLE_BUDGETS` und `ROLE_LABELS`
> in `portal.py` wieder eintragen sowie `PROFESSOR_BUDGET` in `.env` setzen.

LiteLLM User-IDs folgen dem Schema `{rolle}:{email}`, z.B.:

```
student:alice@hs-offenburg.de
admin:carol@hs-offenburg.de
```

---

## Datenbankmodell

Das Portal verwendet eine **eigene PostgreSQL-Datenbank** (getrennt von LiteLLMs DB). LiteLLM ist die Single Source of Truth für Keys und Budgets.

### `portal_verification_codes`
Temporäre E-Mail-Bestätigungscodes (15 min TTL, HMAC-SHA256-Hash).

| Spalte | Typ | Beschreibung |
|---|---|---|
| `email` | TEXT | Empfänger-E-Mail |
| `role` | TEXT | student / professor / admin |
| `hashed_code` | TEXT | HMAC-SHA256 des 6-stelligen Codes |
| `expires_at` | TIMESTAMPTZ | Ablaufzeit (15 min) |
| `used` | BOOLEAN | einmalig verwendbar |

### `portal_users`
Nutzerregister – wer hat sich registriert.

| Spalte | Typ | Beschreibung |
|---|---|---|
| `email` | TEXT | Hochschul-E-Mail |
| `role` | TEXT | student / professor / admin |
| `created_at` | TIMESTAMPTZ | Registrierungszeitpunkt |

---

## Sicherheitsmodell

- **E-Mail-Verifikation**: 6-stelliger Code, 15 min TTL, einmalig verwendbar, als HMAC-SHA256 gespeichert
- **Budget-Kontrolle**: monatliches Max-Budget pro Nutzer, konfigurierbar per Umgebungsvariable, durchgesetzt von LiteLLM
- **Domain-Check**: nur `@hs-offenburg.de`-Adressen werden akzeptiert
- **Rate Limiting**: konfigurierbare Limits pro Minute (slowapi)
- **Admin-Bereich**: HTTP Basic Auth (Credentials in `.env`)
- **Secrets**: ausschließlich über Umgebungsvariablen – keine Secrets im Repository

---

## Umgebungsvariablen (`.env`)

```bash
cp .env.example .env
# dann Werte eintragen
```

| Variable | Beschreibung |
|---|---|
| `LITELLM_BASE_URL` | URL des LiteLLM-Proxy, z.B. `http://localhost:4000` |
| `LITELLM_MASTER_KEY` | LiteLLM Master-Key |
| `SMTP_HOST` | SMTP-Server |
| `SMTP_PORT` | SMTP-Port (Standard: 587) |
| `SMTP_USER` | SMTP-Benutzername |
| `SMTP_PASSWORD` | SMTP-Passwort |
| `SMTP_FROM` | Absenderadresse |
| `CODE_SECRET` | Geheimnis für HMAC (mind. 32 Zeichen) |
| `GMAIL_USER` | Gmail-Adresse (optional, hat Vorrang vor SMTP) |
| `GMAIL_APP_KEY` | Gmail App-Passwort (Google-Konto → Sicherheit → App-Passwörter) |
| `ALLOWED_DOMAIN` | Erlaubte E-Mail-Domain, z.B. `hs-offenburg.de` |
| `STUDENT_BUDGET` | Monatliches Budget Studierende (€) |
| `PROFESSOR_BUDGET` | Monatliches Budget Professoren (€, für spätere Aktivierung) |
| `ADMIN_BUDGET` | Monatliches Budget Admins (€) |
| `RATE_LIMIT_REQUEST_CODE` | Rate Limit Code-Anfrage, z.B. `5/minute` |
| `RATE_LIMIT_VERIFY` | Rate Limit Verifikation, z.B. `10/minute` |
| `DATABASE_URL` | PostgreSQL-URL des Portals |
| `ADMIN_USERNAME` | Benutzername für Admin-Bereich (Basic Auth) |
| `ADMIN_PASSWORD` | Passwort für Admin-Bereich (Basic Auth) |

---

## Setup (Entwicklung)

### 1. Portal-Datenbank starten

```bash
docker compose up -d
```

Startet `portal-db` (PostgreSQL 16) auf Port 5433.

### 2. Python-Umgebung einrichten

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip install -r requirements.txt -r requirements-dev.txt
```

### 3. Konfiguration

```bash
cp .env.example .env
# .env mit echten Werten befüllen
```

### 4. Server starten

```bash
uvicorn portal:app --reload --port 8080
```

---

## URLs

| URL | Methode | Beschreibung |
|---|---|---|
| `/{role}` | GET | Registrierungsformular (role: `student` / `admin`; `/professor` deaktiviert) |
| `/{role}/request-code` | POST | Bestätigungscode per E-Mail anfordern |
| `/{role}/verify-and-get-key` | POST | Code prüfen, API-Schlüssel erstellen |
| `/admin/overview` | GET | Admin-Dashboard (Basic Auth) |
| `/admin/overview/export` | GET | CSV-Export aller Nutzer (Basic Auth) |
| `/health` | GET | Healthcheck |

---

## Admin-Bereich

`/admin/overview` zeigt für alle registrierten Nutzer:

- E-Mail, Rolle
- LiteLLM-Key (live von LiteLLM abgefragt)
- Code-Status (aktiv mit Restzeit / `-`)
- Registrierungsdatum

Zugang via HTTP Basic Auth (`ADMIN_USERNAME` / `ADMIN_PASSWORD` aus `.env`).

CSV-Export: Button auf der Übersichtsseite oder direkt `/admin/overview/export`.

---

## Semester-Reset (Studierende)

```bash
# Vorschau – keine Änderungen
python scripts/reset_students.py --dry-run

# Echte Ausführung
python scripts/reset_students.py --confirm
```

Das Script:
1. Holt alle `student:*`-User von LiteLLM
2. Löscht deren Keys bei LiteLLM
3. Löscht die User bei LiteLLM
4. Bereinigt `portal_users` und `portal_verification_codes` in der Portal-DB

Exit-Code `2` bei Fehlern während der LiteLLM-Löschvorgänge.

---

## Tests

### Voraussetzungen

```bash
# 1. Portal-Datenbank starten
docker compose up -d

# 2. Python-Umgebung aktivieren
source .venv/bin/activate
```

Kein LiteLLM, kein echtes SMTP erforderlich – beides wird in den Tests vollständig gemockt.

### Ausführen

```bash
# Alle Tests
pytest tests/ -v

# Nur Unit-Tests (kein Docker nötig)
pytest tests/test_helpers.py -v

# Nur Integrationstests
pytest tests/test_portal.py -v
```

### Abdeckung

| Datei | Art | Inhalt |
|---|---|---|
| `tests/test_helpers.py` | Unit | HMAC-Hashing, Code-Format, E-Mail-Validierung |
| `tests/test_portal.py` | Integration | Alle Routen, Admin-Aktionen, Full-Flow |

Die Integrationstests schreiben in die echte `portal-db` (PostgreSQL auf Port 5433) und leeren die Tabellen vor und nach jedem Test automatisch. Es entstehen keine Seiteneffekte.

---

## Repository-Struktur

```
portal.py               # FastAPI-Anwendung
requirements.txt        # Laufzeit-Abhängigkeiten
requirements-dev.txt    # Test-Abhängigkeiten
.env.example            # Konfigurationsvorlage (kein Secret)
docker-compose.yml      # Portal-Datenbank (PostgreSQL)
scripts/
  reset_students.py     # Semester-Reset
tests/
  test_helpers.py       # Unit-Tests (HMAC, Validierung)
  test_portal.py        # Integrationstests (Routen)
```
