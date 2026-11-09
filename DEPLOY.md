# Putting Pipeline Tracker on the company platform

This guide is for whoever adds the tracker to the internal platform. Reps don't need to read it. Once it's live, they open the tracker, click **⚙ Settings**, and paste their own Trellus API key and Google app password.

## How it works
- **One server for the whole team.** The tracker doesn't have its own login. The platform signs people in and tells the tracker who each person is.
- **Everyone's data is separate.** Each person gets their own database file, created on their first visit. Reps only ever see their own clients, calls, emails, to-dos and stats.
- **Managers** (listed in `ADMIN_EMAILS`) get a **Team** tab with everyone's numbers. They can open anyone's pipeline **read-only**: the server refuses every change while they're viewing someone else.
- **Keys are per person.** Each rep's Trellus key and Google app password are saved in their own database. They're never sent back to the browser, and no one else can see them, managers included. Each person's calls and emails sync in the background every 5 minutes.

## What the platform needs to do
The tracker is a small web app with no dependencies (Python 3.10+ standard library only). The platform should **reverse-proxy** to it and add two headers to every request it forwards:

| Header | Value |
|---|---|
| `X-User-Email` | The signed-in person's email, e.g. `jane.doe@corgi.insure` |
| `X-Proxy-Secret` | A long random string, the same as `PROXY_SECRET` below |

Important:
1. **Overwrite or strip any `X-User-Email` / `X-Proxy-Secret` the browser sends.** Only the platform may set them.
2. **Don't expose the tracker's port directly.** People should only reach it through the platform. The secret header is a second lock: without it, every request (including the page itself) gets a 401.
3. Header names can be changed with `USER_HEADER` and `PROXY_SECRET_HEADER` if the platform already uses different ones (e.g. `X-Forwarded-Email`).

## Settings (environment variables)
| Variable | Required | Example | What it does |
|---|---|---|---|
| `AUTH_MODE` | yes | `header` | Turns on team mode. Without it the app runs in single-person Mac mode. |
| `PROXY_SECRET` | yes | 32+ random characters | Must match the `X-Proxy-Secret` header. The server won't start without it. Generate one with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `DATA_DIR` | yes | `/data` | Where everyone's databases live. **Must be a persistent volume.** |
| `ADMIN_EMAILS` | no | `lead@corgi.insure,vp@corgi.insure` | Managers who get the Team tab. |
| `ALLOWED_EMAIL_DOMAINS` | recommended | `corgi.insure` | Refuse anyone outside these domains. |
| `PORT` | no | `8765` | Port to listen on (default 8765). |
| `HOST` | no | `0.0.0.0` | Address to bind (team mode default: all interfaces). |
| `USER_HEADER` | no | `X-Forwarded-Email` | Header with the person's email (default `X-User-Email`). |
| `PROXY_SECRET_HEADER` | no | `X-Proxy-Secret` | Header carrying the secret. |
| `ALLOW_OTHER_TRELLUS_EMAIL` | no | `1` | By default each person can only sync the Trellus account matching their login email. A Trellus API key can see teammates' calls, so this stops anyone pulling someone else's calls. Only set this if people's Trellus emails differ from their login emails. |

Put them in the platform's secret/env settings. Don't commit a `.env` with real values.

## Run it
Plain Python:
```
AUTH_MODE=header PROXY_SECRET=... DATA_DIR=/data ADMIN_EMAILS=lead@corgi.insure \
ALLOWED_EMAIL_DOMAINS=corgi.insure python3 server.py
```
Or Docker (a `Dockerfile` is included):
```
docker build -t pipeline-tracker .
docker run -p 8765:8765 -v tracker-data:/data \
  -e AUTH_MODE=header -e PROXY_SECRET=... -e ADMIN_EMAILS=lead@corgi.insure \
  -e ALLOWED_EMAIL_DOMAINS=corgi.insure pipeline-tracker
```
- **Run exactly one instance.** The data is SQLite files and the syncs run inside the process, so two copies would sync everyone twice and could clash.
- **Health check:** `GET /healthz` returns `ok`. It's the only URL that works without the headers, and it reveals nothing.
- **Outbound network:** `api.trellus.ai` and `rpt-2.trellus.ai` (HTTPS), `imap.gmail.com` port 993 (email sync), and `fonts.googleapis.com` / `fonts.gstatic.com` from people's browsers (font only).

## Data, backups and security
- `DATA_DIR/users.db` lists who has used the tracker. `DATA_DIR/users/*.db` holds one database per person.
- **Back up `DATA_DIR`** regularly. For a consistent copy while it's running: `sqlite3 file.db ".backup copy.db"`.
- Keys are stored in plain text inside each person's database. The app never shows them, but anyone with access to the server's disk could read them. Keep `DATA_DIR` on an encrypted, access-controlled volume, the same as any other credential store.
- Email sync is **read-only**: it opens the mailbox read-only and never sends, moves, deletes or marks anything read. Trellus sync only reads.

## Google app passwords
Email sync needs each rep to create a Google app password. In Google Workspace that requires **2-Step Verification**, and the Workspace admin must not have disabled app passwords. If reps see "app passwords aren't available", that's the setting to check.

## Moving someone's existing data in
If someone already used the tracker on their Mac, copy their `pipeline.db` to the server and run:
```
AUTH_MODE=header DATA_DIR=/data python3 server.py import pipeline.db person@corgi.insure
```
This replaces whatever is in that person's account. Their keys don't come across, so they enter them again in Settings.
