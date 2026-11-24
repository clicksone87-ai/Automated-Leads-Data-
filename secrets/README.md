# secrets/

Git-ignored. Put here:

- `client_secret.json`: Google OAuth client of type **Desktop app** (Google Cloud Console →
  APIs & Services → Credentials). Enable the Gmail API and Google Sheets API.
- `token.json`: written by `python -m leads.google_auth` (see README).

Mounted read-only into the worker container.
