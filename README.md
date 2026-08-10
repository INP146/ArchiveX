# ArchiveX

ArchiveX is a self-hosted archive for selected X accounts. The current
foundation provides configuration validation, persistent local storage setup,
a health endpoint, post synchronization, and local media downloads. It uses
`twscrape` to discover posts and `gallery-dl` to download accessible media.

Archive records and task lifecycle records share `ARCHIVE_DB_PATH`. The
`queue_tasks` and `queue_attempts` tables therefore live beside accounts,
posts, media, and synchronization runs in the same SQLite database. The
twscrape `accounts.db` remains separate because its authentication and request
state is owned by twscrape.

## Local development

```sh
python3 scripts/setup_venv.py
python3 scripts/dev.py
```

The setup script creates `.env` from `.env.example` only when it is missing,
updates the editable Python development dependencies, and installs the locked
frontend dependencies. It never overwrites an existing `.env`.

The development script checks the host Redis instance configured by
`TASK_REDIS_URL` (the default is `redis://127.0.0.1:6379/0`), then starts the
API, crawl worker, media worker, single scheduler, and Vite frontend. Open
`http://localhost:5173`. Press `Ctrl+C` to stop only those five development
processes; the host Redis process is not managed by this script.

For backend-only development, run `python3 scripts/start_backend.py`. It starts
the API, both workers, and the scheduler without checking or starting the
frontend. The API address defaults to `http://localhost:8000`.

## Docker Compose deployment

```sh
docker compose up --build
```

This starts the frontend, API, Redis, both workers, and the scheduler. No `.env`
file is required: container paths, queue topology, Redis connectivity, and task
settings are declared in `docker-compose.yml`. Open `http://localhost:8000`.

The Compose file contains a development authentication-token default so the
stack can start by itself. Set `ARCHIVEX_WEB_AUTH_TOKEN` in the shell before
starting a public deployment. The `./data` directory is mounted at `/data` in
the Python containers and holds the SQLite databases, archive files, and
twscrape session data. Keep this directory when recreating containers.

## Configuration

`.env.example` is exclusively the host-development template and therefore uses
`./data/...` paths plus host Redis/API addresses. Container configuration lives
in `docker-compose.yml`; it does not use the host `.env` as a service env file.
Optional deployment-only substitutions use the `ARCHIVEX_WEB_*` prefix.
Archive targets are added in the Web UI and stored by stable X user ID.

The application uses Taskiq and Redis for durable background work. A single
scheduler enqueues enabled accounts at `ARCHIVE_SYNC_INTERVAL_SECONDS`, the
crawl worker archives posts, and separate media workers download attachments.
Redis Stream acknowledgements recover tasks after a worker exits, duplicate
account/media submissions are coalesced, and transient failures use delayed
retries with jitter. Set
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
history (`GET /api/sync-runs`). Post lists support `account_x_user_id`, `q`, `from`,
`to`, `has_media`, `post_type`, `exclude_post_type`, `limit`, and `offset` query
parameters. Post types are `original`, `reply`, `repost`, and `quote`.

The protected `GET /api/crawler-accounts` endpoint lists twscrape login
accounts without exposing cookies or proxy credentials. Assign or clear an
account-specific HTTP proxy with `PATCH /api/crawler-accounts/{username}` and a
JSON body such as `{"proxy":"http://user:pass@host:port"}` or `{"proxy":null}`.
The Web **Settings** page provides the same controls and only displays a masked
proxy URL after it is saved.

Account onboarding is a two-step operation. `POST /api/accounts/resolve` resolves
a username or profile URL without persisting it. `POST /api/accounts` confirms
the returned `x_user_id` and immediately queues its initial synchronization.
`POST /api/accounts/{x_user_id}/sync` returns `202 Accepted` with a task ID;
repeated requests for an account that is already queued or running return the
existing task ID. Account detail, pause/resume, manual sync, username history,
post ownership, and archive paths all use the string `x_user_id`.

## Task queue and task center

The Compose deployment contains six runtime services:

```text
redis
web            built React frontend and API reverse proxy
archivex       FastAPI and task lifecycle API
worker-crawl   one concurrent account synchronization
worker-media   four concurrent media downloads
scheduler      the single Taskiq scheduler
```

After signing in to ArchiveX, use **任务中心** in the Web sidebar. In Compose it
is available at `http://localhost:8000/tasks`; local Vite development uses
`http://localhost:5173/tasks`. The integrated page shows logical tasks and every
execution attempt, including queued, running, waiting-to-retry, completed,
failed, and abandoned states. Manual retries create a new task linked to their
source; automatic retries remain attempts of the original task. Task lifecycle
events are written directly to the unified archive database and do not depend
on the API process being available.

Compose queue settings are declared in `docker-compose.yml`. Important
operational limits include `TASK_SYNC_TIMEOUT_SECONDS`,
`TASK_MEDIA_TIMEOUT_SECONDS`, retry count and delay settings, and
`TASK_DEDUPE_TTL_SECONDS`. Redis is kept internal to the Compose network and
persists its append-only log in the `redis_data` volume.

The API no longer starts an in-process periodic synchronization loop.

## Web frontend

The React frontend lives in `frontend/`. It uses Vite, TypeScript, TanStack
Router, TanStack Query, and Tailwind CSS. The `scripts/dev.py` entry point
launches it with the backend development processes. To run it separately for
frontend-only work, use:

```sh
cd frontend
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

An HTTP proxy can also be assigned to an existing twscrape login account. The
interactive prompt keeps credentials out of shell history:

```sh
.venv/bin/archivex-session --session-path ./data/twscrape proxy --username pni146
```

Use `proxy --username pni146 --clear` to return that account to a direct
connection.

Then start ArchiveX and add public accounts from the Web account-management
page. These targets do not need to be the account used for the session.

## Tests

Use Python 3.11 or newer:

```sh
python -m pip install -e '.[dev]'
pytest
```
