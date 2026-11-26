# Runbook

For moving to an always-on server, see [server-setup.md](server-setup.md).

## 1. First-time setup

1. `cp .env.example .env` and set a real `POSTGRES_PASSWORD` (use the same password in
   `DATABASE_URL`). Set `TZ` to your IANA time zone, and use the same value for
   `timezone` in `config/settings.yaml`.
2. `uv sync`
3. `docker compose up -d --build`, then `docker compose ps`. `db` and `prefect-server`
   should be `healthy` and `worker` and `backup` should be `running`.
4. `uv run alembic upgrade head`. This creates the PostGIS extension and all tables.
5. `uv run leads db-check`
6. Fill in `sender`, `price` and `website.user_agent` in `config/settings.yaml`.
7. Google sign-in (see README), then set `GOOGLE_SHEET_ID` in `.env` and run
   `docker compose up -d` so the worker picks up the change.

If port 5432 is already used on the host, set `DB_PORT=5433` in `.env` and use
`localhost:5433` in `DATABASE_URL`.

## 2. Google OAuth

- The OAuth client must be of type **Desktop app**. While the consent screen is in
  "Testing", refresh tokens expire after **7 days**. Publish the app (no verification
  is needed for personal use with your own account) or re-run the sign-in weekly.
- Token location: `secrets/token.json`. The worker mounts `secrets/` read-only, so a
  refreshed access token is kept in memory and the refresh token keeps working.
- Re-authorise (for example after changing scopes): delete `secrets/token.json` and run
  `docker compose run --rm auth`.
- Error `invalid_grant`: the token expired or was revoked. Re-authorise.

### Foursquare OS Places

The dataset is gated. Request access to `foursquare/fsq-os-places` on Hugging Face, create
a read token and put it in `.env` as `HF_TOKEN`. The
newest release is used unless `sources.foursquare.uri` points at other files. A release
whose columns changed fails with the missing column names. Restart the worker after
changing `.env`.

## 3. Schedules

Defined in `config/settings.yaml` → `schedules` and evaluated in `timezone`.
`python -m leads.deploy` (the worker command) registers them on start.

| Deployment | Cron (default) | Does |
|---|---|---|
| monthly-collect | `0 2 1 * *` | collect (`collect.countries`) → process → sanctions → close missing leads |
| daily-drafts | `0 7 * * *` | opening dates → drafts if `outreach.auto_drafts` → Sheet |
| gmail-sync | `*/30 * * * *` | sent / replied / bounced / opt-out, follow-up drafts |
| backup-check | `30 3 * * *` | verify the 03:00 dump from the `backup` container |

- Change a schedule: edit the YAML, then `docker compose restart worker`.
- Run a flow now: Prefect UI → Deployments → *Quick run*, or
  `docker compose exec worker prefect deployment run 'monthly-collect/monthly-collect'`.
- Pause everything: pause the deployments in the UI, or `docker compose stop worker`.
- Every flow writes a row to the `runs` table (status, counts) as well as to Prefect logs.

## 4. Backups

- The `backup` container runs `pg_dump -Fc` at 03:00 (`TZ`) into
  `./backups/leads_YYYYMMDD_HHMM.dump` and deletes dumps older than 14 days.
- The `backup-check` flow fails (and shows red in Prefect) if the newest dump is older
  than 26 h or smaller than 1 KB.
- Manual backup:
  `docker compose exec db pg_dump -U leads -Fc leads > backups/manual.dump`
- Restore into the running DB (this overwrites data):
  ```bash
  docker compose stop worker
  docker compose exec -T db pg_restore -U leads -d leads --clean --if-exists < backups/leads_YYYYMMDD_HHMM.dump
  docker compose start worker
  ```
- Copy `./backups` off the machine regularly. It is git-ignored.

## 5. Data locations

| Path | Contents |
|---|---|
| `data/raw/source=<name>/run=<run_id>/` | raw parquet per source and run |
| `data/exports/` | CSV exports |
| `data/sanctions/` | downloaded OFAC SDN.CSV / ADD.CSV |
| `data/registries/<name>/` | registry bulk files |
| Docker volume `pgdata` | PostgreSQL data |
| Docker volume `prefect-data` | Prefect server DB (flow run history) |

Raw parquet can be deleted after a run has been processed. Keep the last 2 runs for
debugging.

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| `worker` restarting | `docker compose logs worker`. Usually the Prefect server isn't healthy yet, or there's a bad cron string in settings.yaml |
| `relation "leads" does not exist` | `uv run alembic upgrade head` |
| `connection refused` on localhost:5432 | `docker compose ps db`; check `DB_PORT` / `DATABASE_URL` |
| Permission denied writing `data/` (Linux) | `sudo chown -R 1000:1000 data` (the worker runs as uid 1000) |
| CLI prints `Not implemented yet` | that step is still a stub; see the build order in README |
| Gmail `insufficientPermissions` | re-authorise (scopes changed) |
| Many checks end in `timeout` or `dns lookup failed` | lower `batch.website_concurrency`; check your network/DNS |
| Prefect UI shows no deployments | worker not running or `PREFECT_API_URL` wrong; `docker compose logs worker` |

## 7. Compliance notes

- Drafts only. The code never calls `messages.send` and never sends WhatsApp messages.
- WhatsApp: the dashboard button appears for leads that replied to an email or show a
  WhatsApp number themselves. It opens wa.me with the text filled in; the person presses
  Send in their own WhatsApp Business app. If someone asks to stop, add the number on the
  Opt-outs page.
- Pages that need JavaScript are opened in headless Chromium only after a normal visit
  found no readable text. It loads no images, media or fonts and reaches public
  addresses only.
- WhatsApp numbers come only from the business's own public pages, with an evidence URL.
- Opt-outs and hard bounces are permanent (`suppression`). Remove a row by hand only
  if the person asks to be contacted again.
- Website checks respect robots.txt, fetch at most the home page and one contact page
  per domain, and identify the tool in the User-Agent.
- Exports carry the Overture / Foursquare / OpenStreetMap attribution footer.
