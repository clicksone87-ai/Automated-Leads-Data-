# Running on a server

This guide moves the system from a laptop to one always-on Linux server. The repository
holds only code and configuration. Leads live in PostgreSQL, so a new server starts with
an empty database and collects the leads itself (or restores a backup, see step 8).

## 1. Server

| | Minimum | Recommended |
|---|---|---|
| CPU | 4 vCPU | 8 vCPU |
| RAM | 16 GB | 32 GB (a full world collect reads large parquet files in memory) |
| Disk | 200 GB SSD | 500 GB SSD (raw downloads, OSM extracts, database, 14 days of backups) |
| OS | Ubuntu 24.04 LTS or any Linux with Docker Engine + Compose v2.24+ | |

Use an x86-64 (amd64) machine; the PostGIS image has no arm64 build.

Install Docker Engine and the Compose plugin (https://docs.docker.com/engine/install/),
then add your user to the `docker` group.

## 2. Code and configuration

```bash
git clone <repository-url> lead-automation
cd lead-automation
cp .env.example .env
sudo chown -R 1000:1000 data backups   # the containers run as uid 1000
```

Edit `.env`:

- `POSTGRES_PASSWORD`: a long random password. Use it in `DATABASE_URL` too.
- `TZ`: the office time zone (for example `Asia/Karachi`). Use the same value for
  `timezone` in `config/settings.yaml`, because all schedules use it.
- `GROQ_API_KEY`: free key from console.groq.com, used to write emails.
- `HF_TOKEN`: Hugging Face read token with access to `foursquare/fsq-os-places`.
- `GMAIL_ACCOUNT`: the Gmail or Google Workspace address that should hold the drafts.

Edit `config/settings.yaml`:

- `collect.countries`: set `[]` to collect every country in `config/countries.yaml`.
- `country_bbox`: delete the `GB` line. It limits Great Britain to Leeds–Manchester for testing.
- `sender`, `price` and `website.user_agent`: the real company details. Until they are
  filled in, the dashboard warns that a draft should not be sent.

## 3. Google sign-in (Gmail drafts)

1. In Google Cloud Console, enable the **Gmail API** and create an OAuth client ID of
   type **Desktop app**. Save the JSON as `secrets/client_secret.json`.
2. On the OAuth consent screen, choose **Publish app**. In "Testing" mode the sign-in
   expires after 7 days.
3. Run `docker compose run --rm auth`, open the printed URL and approve access. Then
   paste the `http://localhost:8765/?code=…` address back into the terminal. This
   writes `secrets/token.json`.

## 4. Start

```bash
docker compose up -d --build
docker compose run --rm worker alembic upgrade head    # creates the tables
docker compose run --rm worker leads db-check
docker compose ps
```

Running services:

| Service | Does |
|---|---|
| `db` | PostgreSQL 16 + PostGIS |
| `prefect-server` | schedules and run history, UI on port 4200 |
| `worker` | runs the scheduled flows (monthly collect, daily dates/drafts, Gmail sync, backup check) |
| `checker` | checks lead websites all day long |
| `app` | lead dashboard on port 3300 |
| `backup` | nightly `pg_dump` to `./backups` |

## 5. First full load

Start the monthly flow now instead of waiting for the 1st of the month:

```bash
docker compose exec worker prefect deployment run 'monthly-collect/monthly-collect'
```

It collects every source, loads and deduplicates the leads, screens them against the
sanctions list and closes leads that disappeared. A whole-world run takes many hours.
Follow it in the Prefect UI or with `docker compose logs -f worker`. Website checks start
by themselves as leads arrive.

To load a few countries first:

```bash
docker compose run --rm worker leads collect -c AE -c SA     # prints run_id=…
docker compose run --rm worker leads filter <run_id>
```

The monthly run includes OpenStreetMap, which downloads one extract file per country
into `data/osm/`. To load only OpenStreetMap for a country, add `-s osm`.

## 6. Opening the dashboard

The dashboard and the Prefect UI have no login, and both listen on `127.0.0.1` only.
Do not open them to the internet. Reach them through an SSH tunnel from your own computer:

```bash
ssh -L 3300:localhost:3300 -L 4200:localhost:4200 user@server
```

Then open http://localhost:3300 (dashboard) and http://localhost:4200 (Prefect).

For office-wide access, put a reverse proxy with a login (for example Caddy with basic
auth, or a VPN) in front of port 3300.

## 7. Updates

```bash
git pull
docker compose up -d --build
docker compose run --rm worker alembic upgrade head
```

After changing only `config/settings.yaml`, run `docker compose restart worker checker app`.

## 8. Moving existing leads from another machine (optional)

On the old machine, `./backups/` holds a nightly dump (`leads_YYYYMMDD_HHMM.dump`). Copy
one dump to the server's `./backups/` with a file transfer tool (not git). Then restore it:

```bash
docker compose stop worker checker app
docker compose exec -T db pg_restore -U leads -d leads --clean --if-exists < backups/leads_YYYYMMDD_HHMM.dump
docker compose start worker checker app
```

The restored leads show at once. The next monthly run updates them.

## 9. Backups

Dumps older than 14 days are deleted automatically. Copy `./backups/` off the server
regularly, for example to Google Drive or another server. A disk failure loses
everything that exists only on the server.

## 10. Before the first real emails

- The sender name, phone, website, price and postal address are set in `config/settings.yaml`.
- The Gmail account is the company account, and the Google app is published.
- The email rules for each target country have been checked by the company.

The system only creates Gmail drafts. A person reviews and sends each email.
