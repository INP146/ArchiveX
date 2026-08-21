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
cp docker-compose.build.yml docker-compose.yml
docker compose up -d --build
```

`docker-compose.build.yml` is the tracked build template. The copied
`docker-compose.yml` is intentionally local-only and ignored by Git, similar
to `.env`; keep your deployment-specific values there. Before exposing
ArchiveX outside a trusted local network, change `WEB_AUTH_TOKEN` in the local
`docker-compose.yml`.

This starts the frontend, API, Redis, both workers, and the scheduler. No
separate Python, Node.js, or `.env` setup is required. Open
`http://localhost:8000` and sign in with the Compose authentication token. The
example token is `change-me-before-production`.

SQLite WAL files and twscrape session state live in the Compose-managed
`state_data` volume on Docker's Linux filesystem. Archived media remains
visible on the host under `./data/archive`; it is mounted over
`/data/archive` inside the state volume. On the first start after upgrading,
the `state-migrate` init service uses SQLite's backup API to copy
`./data/archive.sqlite3` and `./data/twscrape/accounts.db` into `state_data`.
The old host files are not modified or deleted. Redis queue data uses the
separate `redis_data` volume.

Stop the old foreground stack before the first start with this layout. Never
use `docker compose down -v` for routine cleanup: `-v` deletes both named
volumes, including the primary SQLite state. The verified migration and
rollback procedure is documented in
[`.agents/DOCKER_SQLITE_RECOVERY.md`](.agents/DOCKER_SQLITE_RECOVERY.md).

## Configuration

`.env.example` is only for local development with `scripts/setup_venv.py`. The
Docker deployment does not use it: paths, queue topology, Redis connectivity,
authentication, and runtime settings are all declared directly in
the local `docker-compose.yml` copied from `docker-compose.build.yml`. Archive
targets are added in the Web UI and stored by stable X user ID.

The application uses Taskiq and Redis for durable background work. A single
scheduler enqueues enabled accounts at `ARCHIVE_SYNC_INTERVAL_SECONDS`, the
crawl worker archives posts, and separate media workers download attachments.
Redis Stream acknowledgements recover tasks after a worker exits, duplicate
account/media submissions are coalesced, and transient failures use delayed
retries with jitter. Set
`ARCHIVE_MEDIA_ENABLED=false` to archive post metadata without downloading
files. `TWSCRAPE_SESSION_PATH` is either a
twscrape account database file or a directory containing `accounts.db`. After
the Compose stack is running, open **Settings** in the Web UI to add a crawler
account: enter the X login username and paste the browser cookie string
containing `auth_token` and `ct0`. The cookie is written directly to the
persistent twscrape database, is never displayed again, and is only accepted
for the authenticated Web session. Enable **replace existing session** only
when deliberately rotating an account session. Credentials are not stored in
this repository.

## Authentication and API

Browser clients establish a session with `POST /api/auth/session` and then use a
signed, HttpOnly cookie. `DELETE /api/auth/session` logs out and `GET
/api/auth/session` reports whether the browser is authenticated. Script clients
can continue to use `Authorization: Bearer <WEB_AUTH_TOKEN>` for protected API
routes. `WEB_SESSION_SECRET` is optional; when omitted, ArchiveX derives it from
`WEB_AUTH_TOKEN`. Set `WEB_COOKIE_SECURE=true` when serving over HTTPS.
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

Compose queue settings are declared in the local `docker-compose.yml` (copied
from `docker-compose.build.yml`). Important
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

The stable Docker workflow is Web-only. Start the Compose stack, sign in, and
open **Settings**. Under **添加采集账号**, enter the X login username and paste
the browser cookie string containing `auth_token` and `ct0`, then save. The
session status and request count appear in the same page. HTTP proxies can be
added or cleared from each account row.

To obtain the cookie string, use the browser's developer tools on `x.com`,
copy the value from an authenticated request's `Cookie` header, and paste it
directly into the form. Treat it like a password: use HTTPS for any non-local
deployment, never put it in chat or shell history, and rotate the session in
Settings when it expires. The ArchiveX web service stores it only in the
Docker `state_data` volume and never displays it again.

These crawler login accounts are separate from the public X accounts added on
the account-management page.

## Published container images

Pushing a version tag such as `v0.1.2-rc1` publishes two multi-platform images to
GitHub Container Registry:

```text
ghcr.io/inp146/archivex:0.1.2-rc1
ghcr.io/inp146/archivex-web:0.1.2-rc1
```

The repository path is derived by the workflow and converted to lowercase, so
the workflow does not hard-code an owner account. The repository includes
`docker-compose.ghcr.yml` for an image-only deployment. From the separate
deployment directory, download the versioned file as `./docker-compose.yml`:

```sh
curl -fsSL https://raw.githubusercontent.com/INP146/ArchiveX/v0.1.2-rc1/docker-compose.ghcr.yml -o ./docker-compose.yml
```

Replace the example `WEB_AUTH_TOKEN`, then deploy without the source tree:

```sh
docker compose pull
docker compose up -d
```

This deployment file has no `build` or `.env` dependency. Workers, the
scheduler, and `tools` all use the backend image. It pins version `0.1.2-rc1`
instead of relying on `latest`; change both image tags when upgrading. If the
packages are private, log in to `ghcr.io` before pulling.

## Backup, restore, and upgrades

Use `docker compose run --rm tools backup` to create a verified archive under
`backups/`; the maintenance command runs entirely from the Docker image.
The restore command must target a stopped host directory. It deliberately
rejects `/data` when that path is a Docker named-volume mountpoint, because a
mountpoint cannot be atomically replaced; restore to a host staging directory
and run the documented state migration instead.
Restore and upgrade procedures, security requirements, known limitations, and
the stable-release checklist are documented in
[`.agents/RELEASE_V0.1.1.md`](.agents/RELEASE_V0.1.1.md).

## Tests

Use Python 3.11 or newer:

```sh
python -m pip install -e '.[dev]'
pytest
```
