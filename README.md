# LiteLLM Key Portal

Self-service-Portal zur Ausgabe von LiteLLM API-Schlüsseln für Studierende der Hochschule.
Admins verwalten das Portal und die ausgegebenen Schlüssel über einen geschützten Admin-Bereich.

Voraussetzung ist eine separat installierte und vollständig eigenständig laufende LiteLLM-Instanz. Von dieser Installation benötigt das Portal die LiteLLM-Base-URL sowie den LiteLLM-Master-Key; sinnvoll ist dabei zum Beispiel eine separate Bereitstellung als Docker-Container.

---

## Architektur

```
Nutzer (Student)
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

Im Self-Service ist aktuell genau **eine Rolle** aktiv:

| URL | Zielgruppe | Status |
|---|---|---|
| `/student` | Studierende | aktiv |

Es gibt derzeit keine Self-Service-Registrierung für weitere Rollen. Der geschützte Admin-Bereich dient nur zur Verwaltung bereits angelegter Einträge und Schlüssel.

LiteLLM User-IDs folgen dem Schema `{rolle}:{email}`. Aktuell wird dabei nur das Präfix `student:` verwendet, z.B.:

```
student:alice@hs-offenburg.de
```

---

## Datenbankmodell

Das Portal verwendet eine **eigene PostgreSQL-Datenbank** (getrennt von LiteLLMs DB). LiteLLM ist die Single Source of Truth für Keys und Budgets.

### `portal_verification_codes`
Temporäre E-Mail-Bestätigungscodes (15 min TTL, HMAC-SHA256-Hash).

| Spalte | Typ | Beschreibung |
|---|---|---|
| `email` | TEXT | Empfänger-E-Mail |
| `role` | TEXT | aktuell nur `student` |
| `hashed_code` | TEXT | HMAC-SHA256 des 6-stelligen Codes |
| `expires_at` | TIMESTAMPTZ | Ablaufzeit (15 min) |
| `used` | BOOLEAN | einmalig verwendbar |

### `portal_users`
Nutzerregister – wer hat sich registriert.

| Spalte | Typ | Beschreibung |
|---|---|---|
| `email` | TEXT | Hochschul-E-Mail |
| `role` | TEXT | aktuell nur `student` |
| `created_at` | TIMESTAMPTZ | Registrierungszeitpunkt |

---

## Sicherheitsmodell

- **E-Mail-Verifikation**: 6-stelliger Code, 15 min TTL, einmalig verwendbar, als HMAC-SHA256 gespeichert
- **Abgesicherte Code-Eingabe**: Das Eingabeformular akzeptiert clientseitig nur Ziffern; serverseitig werden Leerzeichen entfernt und anschließend genau 6 Ziffern erzwungen
- **Budget-Kontrolle**: monatliches Max-Budget pro Nutzer, konfigurierbar per Umgebungsvariable, durchgesetzt von LiteLLM
- **Key-Rotation im Self-Service**: Studierende können erneut einen Verifikationscode anfordern; bei der Bestätigung wird ein vorhandener LiteLLM-Key gelöscht und durch einen neuen ersetzt
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
| `PROFESSOR_BUDGET` | Derzeit ungenutzt / reserviert |
| `ADMIN_BUDGET` | Derzeit ungenutzt / reserviert |
| `RATE_LIMIT_REQUEST_CODE` | Rate Limit Code-Anfrage, z.B. `5/minute` |
| `RATE_LIMIT_VERIFY` | Rate Limit Verifikation, z.B. `10/minute` |
| `CODE_COOLDOWN_MINUTES` | Sperrzeit bis zur nächsten Code-Anfrage, z.B. `5` |
| `DATABASE_URL` | PostgreSQL-URL des Portals |
| `ADMIN_USERNAME` | Benutzername für Admin-Bereich (Basic Auth) |
| `ADMIN_PASSWORD` | Passwort für Admin-Bereich (Basic Auth) |
| `TEST_INFO_EMAIL` | Empfängeradresse für den Admin-Button `Test-Info-Mail senden` |

---

## Setup (Entwicklung)

### 1. Portal-Datenbank starten

```bash
docker compose up -d
```

Startet `portal-db` (PostgreSQL 16) auf Port 5433.

### 2. Python-Umgebung einrichten

Für Installation und Ausführung wird `uv` benötigt.

```bash
uv venv --python 3.12
uv sync --all-groups
```

### 3. Konfiguration

```bash
cp .env.example .env
# .env mit echten Werten befüllen
```

### 4. Server starten

```bash
uv run uvicorn portal:app --reload --port 8080
```

---

## URLs

| URL | Methode | Beschreibung |
|---|---|---|
| `/student` | GET | Registrierungsformular für Studierende |
| `/student/enter-code` | GET | Direkte Eingabeseite für E-Mail-Adresse und Bestätigungscode |
| `/student/request-code` | POST | Bestätigungscode per E-Mail anfordern (auch für erneute Key-Ausstellung) |
| `/student/verify-and-get-key` | POST | Code prüfen, API-Schlüssel erstellen oder bestehenden Schlüssel ersetzen |
| `/admin` | GET | Admin-Dashboard (Basic Auth) |
| `/admin` | POST | Admin-Aktionen (Key löschen, Nutzer löschen, Budget setzen, Nutzer anlegen) |
| `/admin/reset-students` | GET/POST | Geschützte Admin-Seite zum Löschen aller Studierenden |
| `/admin/export` | GET | CSV-Export aller Nutzer (Basic Auth) |
| `/health` | GET | Healthcheck |

---

## Admin-Bereich

`/admin` zeigt für alle registrierten Nutzer:

- E-Mail, Rolle
- LiteLLM-Key (live von LiteLLM abgefragt)
- Code-Status (aktiv mit Restzeit / `-`)
- Registrierungsdatum

Zugang via HTTP Basic Auth (`ADMIN_USERNAME` / `ADMIN_PASSWORD` aus `.env`).

CSV-Export: Button auf der Übersichtsseite oder direkt `/admin/export`.

`Test-Info-Mail senden` verschickt die aktuelle Rundmail-Vorlage aus `rundmail.txt` an genau eine in `.env` konfigurierte Adresse (`TEST_INFO_EMAIL`).

`Info-Mail senden` verschickt dieselbe Vorlage an alle aktuell in `portal_users` registrierten Teilnehmenden.

`Studierende löschen` führt auf eine separate, weiterhin per Basic Auth geschützte Admin-Seite. Auch diese Unterseite ist nicht öffentlich zugänglich.

Wenn sich ein bereits vorhandener Student erneut über den Self-Service verifiziert, rotiert das Portal den LiteLLM-Key: bestehende Keys werden gelöscht und direkt ein neuer Schlüssel generiert.

Für die Code-Eingabe kann direkt `/student/enter-code` verwendet werden. Das Formular filtert Nicht-Ziffern im Browser und der Server normalisiert zusätzlich eingefügte Leerzeichen.

---

## Mail-Skripte

Die gleiche Rundmail-Vorlage aus `rundmail.txt` kann auch direkt per CLI versendet werden. Das ist die Grundlage für eine spätere Ausführung per Cron.

### Test-Info-Mail

```bash
# Vorschau – kein Versand
uv run python scripts/send_test_info_mail.py --dry-run

# Echte Ausführung
uv run python scripts/send_test_info_mail.py --confirm
```

Das Script verwendet `TEST_INFO_EMAIL` aus der `.env` und sendet genau eine Mail.

### Rundmail an alle Teilnehmenden

```bash
# Vorschau – kein Versand
uv run python scripts/send_info_mail.py --dry-run

# Echte Ausführung
uv run python scripts/send_info_mail.py --confirm
```

Das Script liest alle aktuell registrierten Empfänger aus `portal_users` und versendet an diese die gleiche Vorlage aus `rundmail.txt`.

Hinweis: Die eigentliche Anbindung an Cron-Jobs ist noch nicht umgesetzt. Die dafür vorbereiteten CLI-Skripte sind mit `scripts/send_test_info_mail.py`, `scripts/send_info_mail.py` und `scripts/reset_students.py` bereits vorhanden.

---

## Semester-Reset (Studierende)

```bash
# Vorschau – keine Änderungen
uv run python scripts/reset_students.py --dry-run

# Echte Ausführung
uv run python scripts/reset_students.py --confirm
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

# 2. Abhängigkeiten synchronisieren
uv sync --all-groups
```

Kein LiteLLM, kein echtes SMTP erforderlich – beides wird in den Tests vollständig gemockt.

### Ausführen

```bash
# Alle Tests
uv run pytest tests/ -v

# Nur Unit-Tests (kein Docker nötig)
uv run pytest tests/test_helpers.py -v

# Nur Integrationstests
uv run pytest tests/test_portal.py -v
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
pyproject.toml          # Projekt- und Abhängigkeitsdefinition für uv
uv.lock                 # Lockfile für reproduzierbare Installationen
.env.example            # Konfigurationsvorlage (kein Secret)
docker-compose.yml      # Portal-Datenbank (PostgreSQL)
scripts/
  reset_students.py     # Semester-Reset
tests/
  test_helpers.py       # Unit-Tests (HMAC, Validierung)
  test_portal.py        # Integrationstests (Routen)
```
