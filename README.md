# ArchiveX

ArchiveX is a self-hosted, read-only archive for selected X accounts. The current
foundation provides configuration validation, persistent local storage setup, and
a health endpoint. Crawling and media downloading will be added in later steps.

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
be empty until crawling is configured.

## Tests

Use Python 3.11 or newer:

```sh
python -m pip install -e '.[dev]'
pytest
```

