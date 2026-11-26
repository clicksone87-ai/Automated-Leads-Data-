# lead-automation

Automated lead collection and outreach preparation for new businesses (created on or
after 2018-01-01) in the countries listed in [config/countries.yaml](config/countries.yaml).

It collects businesses from open data, deduplicates them into one lead per business,
checks each website, finds public emails and publicly shown WhatsApp numbers, screens
names against the OFAC SDN list, and every day creates personalised **Gmail drafts** that
you review and send yourself. **Nothing is ever sent automatically**, and no WhatsApp
API is ever called.

![Architecture](docs/architecture.png)

| Step | What happens | When |
|---|---|---|
| Collect | Overture Maps, Foursquare OS Places, company registries, OSM, licensed feeds → raw parquet | 1st of month 02:00 |
| Process | Normalise → filter countries → dedupe/match → age resolver (keep ≥ 2018-01-01) → sanctions | after collect |
| Refresh | Update `last_seen`, close leads missing for 2 runs, re-check websites older than 30 days | after collect |
| Website check | working / broken / parked / redirect_social / none, plus public emails, WhatsApp and socials | daily (new leads) + monthly |
| Drafts | Up to 100 Gmail drafts/day, priority none > broken > parked > working, nested labels | 07:00 daily |
| Sheet | Full sorted lead list with filter views and data attribution | 07:00 daily |
| Gmail sync | Sent / replied / bounced / opt-out detection, one follow-up draft after 6 days | every 30 min |
| Backup | `pg_dump` to `./backups` (03:00) + verification flow (03:30) | daily |

To run it on an always-on server, follow [docs/server-setup.md](docs/server-setup.md).

Everything runs locally in Docker Compose: PostgreSQL 16 + PostGIS, the Prefect 3 server,
a worker that serves the scheduled flows, a nightly backup container, and an optional
Metabase dashboard.

## Setup

Prerequisites: Docker Desktop (or Docker Engine + Compose v2.24+), [uv](https://docs.astral.sh/uv/), git.

```bash
cp .env.example .env              # then edit POSTGRES_PASSWORD + DATABASE_URL, TZ, GOOGLE_SHEET_ID
uv sync                           # Python 3.12 venv with all deps (uv downloads 3.12 if needed)
docker compose up -d --build      # db, prefect-server, worker, backup
uv run alembic upgrade head       # create tables (runs against localhost:5432)
uv run leads db-check             # PostgreSQL + PostGIS versions
uv run leads --help               # all commands
```

- Prefect UI: http://localhost:4200 (deployments, schedules, run logs and retries)
- Lead dashboard: `docker compose up -d --build db app`, then http://localhost:3300
  (or without Docker: `uv run leads dashboard`). It has no login, so keep it on this computer.
- Metabase (optional): `docker compose --profile dashboard up -d`, then http://localhost:3000
  (change with `METABASE_PORT` in `.env`).
  Connect it to host `db`, port 5432, with the credentials from `.env`.

Then edit [config/settings.yaml](config/settings.yaml): `timezone`, the `sender` block,
`price`, `website.user_agent`, and `sources.*` (Overture release, Foursquare path, which
sources are enabled). Restart the worker after changing schedules:
`docker compose restart worker`.

## Google OAuth sign-in (Gmail + Sheets)

1. In Google Cloud Console create a project, enable the **Gmail API** (and the
   **Google Sheets API** if you use a Sheet), configure the OAuth consent screen (External, add yourself as
   a test user), and create an **OAuth client ID** of type **Desktop app**.
2. Download the JSON and save it as `secrets/client_secret.json`.
3. Sign in once. This writes `secrets/token.json`:

   ```bash
   docker compose run --rm auth
   ```

   Inside Docker this is a copy/paste flow: open the printed URL, approve access, then
   copy the full `http://localhost:8765/?code=…` address your browser ends up on (the
   page itself shows an error, which is expected) and paste it back into the terminal.
   `auth` is a one-off service (profile `tools`) using the worker image with `secrets/`
   writable; the long-running worker mounts `secrets/` read-only.

   Alternative on the host, which opens your browser directly:
   `uv run python -m leads.google_auth`.

4. Set `GMAIL_ACCOUNT` in `.env`; drafts are refused in any other account. Set
   `GOOGLE_SHEET_ID` (before signing in) only if you use a Sheet.

Scopes: `gmail.modify` (create drafts, read replies, apply labels) and, with a Sheet, `spreadsheets`.
The code never calls `messages.send`.

## CLI

```
uv run leads collect -s overture -c GB -c IE   # sources -> data/raw (prints run_id)
uv run leads filter <run_id>                  # normalise, filter, dedupe, load into Postgres
uv run leads check-websites --limit 500
uv run leads drafts --dry-run
uv run leads sync
uv run leads sheet
uv run leads refresh
uv run leads sanctions
uv run leads export-csv
uv run leads db-check | show-config
```

Inside Docker: `docker compose run --rm worker leads <command>`.
Commands whose module is still a stub exit with code 2 and print `Not implemented yet`.

## Development

```bash
uv run pytest          # tests (no network, no DB)
# database tests (the database is wiped; its name must end in _test):
# TEST_DATABASE_URL=postgresql+psycopg://leads:...@localhost:5452/leads_test uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pre-commit install
uv run alembic revision --autogenerate -m "describe change"   # after editing models.py
```

Layout: `src/leads/{sources,pipeline,enrich,outreach,export,flows}`. See
[docs/runbook.md](docs/runbook.md) for operations, schedules, backups and troubleshooting.

## Build order / TODO

Each item maps to `TODO(build step N)` markers in the code.

- [x] **1. Project setup**: repo, uv, Docker Compose, settings, DB schema + Alembic, CI (ruff, mypy, pytest)
- [ ] **2. Overture + Foursquare loaders** for 2–3 test countries, normalise, country filter (`sources/overture.py`, `sources/foursquare.py`, `pipeline/__init__.py`) — Overture done (tested on MT, IE); Foursquare tested on MT
- [ ] **3. Age resolver + Companies House**, then other registries (`pipeline/age.py`, `sources/registries/*`) — loaders written; UK (Leeds–Manchester) tested end to end, Canada file loaded, Norway, France and Australia not yet loaded
- [x] **4. Website checker + email/WhatsApp extractor** (`enrich/website.py`, `enrich/check.py`); a status is set only on proof, anything unclear stays empty. JavaScript-only pages are opened in headless Chromium (`enrich/browser.py`). Leads without a website are tried at their email domain, then at the .com of an Instagram/X/TikTok/YouTube/Pinterest profile name, kept only if that site links back to the profile or shows the lead's phone or email. Facebook and Instagram pages themselves are never fetched: their robots.txt forbids it
- [x] **5. Dedupe/match + CSV export + dashboard**: stable lead ids, `merge_fields`, upserts (`pipeline/match.py`)
- [x] **6. Prefect flows + schedules + monthly refresh** (`flows/*`, `pipeline/refresh.py`; deployments are registered already)
- [ ] **7. AI emails, Gmail drafts, labels, Gmail sync, follow-ups, opt-outs, sanctions check: done for English-speaking countries. WhatsApp from the dashboard after a reply, or when the business shows a WhatsApp number: opens wa.me with the text filled in, the person presses Send (`outreach/whatsapp.py`). Left: other languages. The Google Sheet view is replaced by the dashboard** (`outreach/*`, `export/sheets.py`, `pipeline/sanctions.py`, `config/templates/i18n/`)
- [ ] **8. Scale to all countries**, more registries. OpenStreetMap done (`sources/osm.py`); add each new country's extract to `EXTRACTS`
- [ ] **9. Licensed directory feeds** (optional, `sources/directories.py`)

Already working: config loading, DB connection/run bookkeeping, schema, CLI, Docker,
phone/domain/name/postcode normalisation, the website classifier and contact extraction,
the in-memory matcher, age rules, Gmail label helpers, template rendering, opt-out and
bounce detection, SDN fuzzy matching, raw-zone parquet writer, CSV export.

## Data attribution and rules

- Exports and the Sheet carry a footer crediting Overture Maps Foundation, Foursquare
  Open Source Places and OpenStreetMap contributors (`leads.export.ATTRIBUTION`).
- WhatsApp numbers are stored only when the business shows them publicly (wa.me /
  api.whatsapp.com links, WhatsApp buttons, "WhatsApp: +…" text), together with the URL
  of the page that shows them.
- Opt-outs ("no thanks", "unsubscribe", "stop", "remove" and translations) and hard
  bounces go to the `suppression` table; suppressed contacts never get drafts.
- Leads whose name matches an OFAC SDN entry (score ≥ 92, same country) are skipped.
