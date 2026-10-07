# Floor Supervisor

Kiosk application (shared tablet + keyboard-wedge card scanner) that records
employee card scans as immutable events.

```
scanner ─▶ Streamlit input ─▶ ScanService
                                 ├─ CardResolverClient   POST /cards/resolve  → hibob_id, name
                                 ├─ EmployeeService      hibob_etl.employees  (read only)
                                 └─ ScanRepository       floor_supervisor.scan_event (append only)
```

A scan is only a scan: no IN/OUT/shift state is inferred.

## Rules implemented

| Situation | Result | Recorded? |
|---|---|---|
| Card resolved, employee found in HiBob | "Scan registered" | yes (`FOUND`) |
| Card resolved, employee **not** in HiBob snapshot | "Scan registered" + warning | yes (`NOT_FOUND`, HiBob fields NULL) |
| HiBob status not `Active` | "Scan registered" + warning (V1: never blocks) | yes |
| Same device + employee recorded < `DUPLICATE_SCAN_WINDOW_SECONDS` ago | "Already registered" (neutral) | no |
| Card not resolved / resolver down / bad response | error | no (logs only) |
| More than one HiBob row for the ID | error (`DATA_INTEGRITY_ERROR`) | no |
| Database failure | error | no |

Duplicate detection runs in one transaction serialized by
`pg_advisory_xact_lock(hashtextextended('<device_id>|<hibob_id>', 0))`, so
concurrent double-reads cannot both be inserted.

No badge credential material (raw card value, facility code, card number) is
stored. Logs show at most the last 4 characters of a card value.

## Layout

```
app.py                      Streamlit UI (thin)
src/config.py               typed settings from environment / .env
src/db.py                   psycopg 3 connection pool
src/logging_setup.py        JSON logging, card masking
src/bootstrap.py            wiring (composition root)
src/models/                 Employee, scan models
src/repositories/           all SQL (hibob_etl read only, floor_supervisor.scan_event)
src/services/               Card Resolver client, employee lookup, scan orchestration
src/ui/presenter.py         ScanResult → ResultView (tone, copy) + display timings
src/ui/components.py        HTML fragments (all dynamic text escaped)
src/ui/styles.py            official ARRISE tokens → CSS variables
src/ui/kiosk.css            kiosk styling (tokens only, no raw colours)
src/ui/brand.py             wordmark, or assets/brand/arrise-logo.(svg|png) when supplied
src/ui/focus.py             isolated scanner-input focus + no on-screen keyboard
sql/001_create_scan_event.sql
tests/                      unit tests (no PostgreSQL / network needed)
```

## Local development (Windows, PowerShell)

Keep the virtual environment outside synced folders (e.g. OneDrive) to avoid
syncing thousands of files. `<venv-path>` below is any local directory.

```powershell
cd <project-path>
py -3.12 -m venv <venv-path>
& <venv-path>\Scripts\python.exe -m pip install -r requirements-dev.txt

Copy-Item .env.example .env   # then fill DB_PASSWORD and CARD_RESOLVER_BEARER_TOKEN
```

PostgreSQL is reached through an SSH tunnel (keep it open in another terminal).
Never copy the private key into the project directory.

```powershell
ssh -i "<ssh-key-path>" `
  -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=60 `
  -L 5433:127.0.0.1:5432 <ssh-user>@<vm-host>
```

Run the tests and the app:

```powershell
& <venv-path>\Scripts\python.exe -m pytest
& <venv-path>\Scripts\python.exe -m streamlit run app.py
# open http://127.0.0.1:8003/floor-supervisor/
```

`floor_supervisor.scan_event` must exist before scans can be recorded
(see `sql/001_create_scan_event.sql`; apply only after review).

## Configuration

See `.env.example`. Production on the VM uses `DB_PORT=5432`. The bind address,
port and `/floor-supervisor` base path are in `.streamlit/config.toml`.

## Before production

- Restrict Nginx `location /floor-supervisor/` to the allowed site/corporate IP range
  (there is no login).
- Nginx must proxy WebSockets (`/floor-supervisor/_stcore/stream`): `proxy_http_version 1.1`,
  `Upgrade`/`Connection` headers, long `proxy_read_timeout`.
- Validate scanner input + autofocus on the real tablet/browser.
