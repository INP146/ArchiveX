# ArchiveX

ArchiveX is a self-hosted, read-only archive for selected X accounts. The current
foundation provides configuration validation, persistent local storage setup, and
a health endpoint. It uses `twscrape` to archive posts; media downloading will
be added in a later step.

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
at `ARCHIVE_SYNC_INTERVAL_SECONDS`. `TWSCRAPE_SESSION_PATH` is either a
twscrape account database file or a directory containing `accounts.db`. Its
accounts and login state must be provisioned before ArchiveX starts; credentials
are not accepted by the web service or stored in this repository.

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
