# ArchiveX

ArchiveX is a self-hosted, read-only archive for selected X accounts. The current
foundation provides configuration validation, persistent local storage setup,
a health endpoint, post synchronization, and local media downloads. It uses
`twscrape` to discover posts and `gallery-dl` to download accessible media.

## Run with Docker

```sh
cp .env.example .env
docker compose up --build
```

Open `http://localhost:8000/health`.

The `./data` directory is mounted at `/data` in the container and holds the
SQLite database, archive files, and future twscrape session data. Keep this
directory when recreating the container.

## Configuration

All settings are documented in `.env.example`. `WEB_AUTH_TOKEN`,
`ARCHIVE_DB_PATH`, and `ARCHIVE_DATA_DIR` are required. `ARCHIVE_ACCOUNTS` may
be empty to run the web service without crawling.

When accounts are configured, the application starts one sequential sync loop:
it imports the configured history on the first run and then checks for updates
at `ARCHIVE_SYNC_INTERVAL_SECONDS`. New posts create media download records;
failed media downloads are retried during later scans. Set
`ARCHIVE_MEDIA_ENABLED=false` to archive post metadata without downloading
files. `TWSCRAPE_SESSION_PATH` is either a
twscrape account database file or a directory containing `accounts.db`. Its
accounts and login state must be provisioned before ArchiveX starts; credentials
are not accepted by the web service or stored in this repository.

## Authentication and API

Browser clients establish a session with `POST /api/auth/session` and then use a
signed, HttpOnly cookie. `DELETE /api/auth/session` logs out and `GET
/api/auth/session` reports whether the browser is authenticated. Script clients
can continue to use `Authorization: Bearer <WEB_AUTH_TOKEN>` for protected API
routes. Set a separate random `WEB_SESSION_SECRET` of at least 32 characters in
production, and set `WEB_COOKIE_SECURE=true` when serving over HTTPS.
`WEB_AUTH_DISPLAY_NAME`, `WEB_AUTH_USERNAME`, and `WEB_AUTH_AVATAR_URL` define
the identity shown for the current token-authenticated user in the web sidebar;
they are independent from the X accounts being archived.

Protected routes expose archived accounts (`GET /api/accounts`), posts (`GET /api/posts` and
`GET /api/posts/{tweet_id}`), downloaded media (`GET /api/media/{id}`), and sync
history (`GET /api/sync-runs`). Post lists support `account_id`, `q`, `from`,
`to`, `has_media`, `post_type`, `exclude_post_type`, `limit`, and `offset` query
parameters. Post types are `original`, `reply`, `repost`, and `quote`.

## Web frontend

The React frontend lives in `frontend/`. It uses Vite, TypeScript, TanStack
Router, TanStack Query, and Tailwind CSS. Start the API, then run:

```sh
cd frontend
npm install
npm run dev
```

Vite proxies `/api` and `/health` to the port in the repository `.env` file.
Set `VITE_API_PROXY_TARGET` to override it. `npm run generate:api` can regenerate
TypeScript types from the FastAPI OpenAPI document when the API changes.

## X Session Setup

Copy and run this command first:

```sh
.venv/bin/archivex-session --session-path ./data/twscrape cookies --username your_x_login --from-clipboard
```

While it is waiting, copy the cookie string from the browser. ArchiveX detects
the new clipboard content and imports it automatically; there is nothing to
paste into the terminal and no second Enter press.

Check that the account is active:

```sh
.venv/bin/archivex-session --session-path ./data/twscrape status
```

Then set `ARCHIVE_ACCOUNTS` to the public accounts to archive and start the
service. These targets do not need to be the account used for the session.

## Tests

Use Python 3.11 or newer:

```sh
python -m pip install -e '.[dev]'
pytest
```
