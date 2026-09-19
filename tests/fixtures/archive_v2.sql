-- Frozen v2 schema: migration input, never used by runtime.

CREATE TABLE IF NOT EXISTS accounts (
    x_user_id TEXT PRIMARY KEY,
    current_username TEXT,
    display_name TEXT,
    archive_enabled INTEGER NOT NULL DEFAULT 1 CHECK (archive_enabled IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'active',
    last_sync_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS account_username_history (
    id INTEGER PRIMARY KEY,
    x_user_id TEXT NOT NULL REFERENCES accounts(x_user_id),
    username TEXT NOT NULL,
    observed_from TEXT NOT NULL,
    observed_to TEXT,
    last_observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
    tweet_id TEXT PRIMARY KEY,
    account_x_user_id TEXT NOT NULL REFERENCES accounts(x_user_id),
    post_type TEXT NOT NULL,
    text TEXT NOT NULL,
    posted_at TEXT NOT NULL,
    permalink TEXT NOT NULL,
    raw_json_path TEXT NOT NULL,
    media_scanned_at TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS media (
    id TEXT PRIMARY KEY,
    tweet_id TEXT NOT NULL REFERENCES posts(tweet_id) ON DELETE CASCADE,
    media_type TEXT NOT NULL,
    source_url TEXT NOT NULL,
    local_path TEXT,
    download_status TEXT NOT NULL DEFAULT 'pending',
    sha256 TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(tweet_id, source_url)
);
CREATE TABLE IF NOT EXISTS sync_runs (
    id TEXT PRIMARY KEY,
    account_x_user_id TEXT NOT NULL REFERENCES accounts(x_user_id),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    posts_seen INTEGER NOT NULL DEFAULT 0,
    posts_new INTEGER NOT NULL DEFAULT 0,
    media_new INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_posts_account_posted_at
    ON posts(account_x_user_id, posted_at DESC, tweet_id DESC);
CREATE INDEX IF NOT EXISTS idx_media_tweet_id ON media(tweet_id);
CREATE INDEX IF NOT EXISTS idx_media_download_status ON media(download_status, updated_at);
CREATE INDEX IF NOT EXISTS idx_sync_runs_account_started_at
    ON sync_runs(account_x_user_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_username_history_account_observed
    ON account_username_history(x_user_id, observed_from DESC);
CREATE INDEX IF NOT EXISTS idx_username_history_username_observed
    ON account_username_history(username COLLATE NOCASE, observed_from DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_username_history_current
    ON account_username_history(x_user_id) WHERE observed_to IS NULL;
PRAGMA user_version = 2;


CREATE TABLE IF NOT EXISTS queue_tasks (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    worker TEXT NOT NULL,
    account_x_user_id TEXT REFERENCES accounts(x_user_id) ON DELETE SET NULL,
    media_id TEXT REFERENCES media(id) ON DELETE SET NULL,
    parent_task_id TEXT REFERENCES queue_tasks(id) ON DELETE SET NULL,
    trigger TEXT,
    context TEXT NOT NULL,
    args TEXT NOT NULL,
    kwargs TEXT NOT NULL,
    labels TEXT NOT NULL,
    result TEXT,
    error TEXT,
    queued_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    next_retry_at TEXT,
    current_attempt INTEGER NOT NULL DEFAULT 1,
    max_attempts INTEGER NOT NULL DEFAULT 1,
    retry_of TEXT REFERENCES queue_tasks(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS queue_attempts (
    task_id TEXT NOT NULL REFERENCES queue_tasks(id) ON DELETE CASCADE,
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,
    labels TEXT NOT NULL,
    result TEXT,
    error TEXT,
    queued_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    next_retry_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (task_id, attempt)
);
CREATE INDEX IF NOT EXISTS idx_queue_tasks_status_updated
    ON queue_tasks(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_queue_tasks_target
    ON queue_tasks(name, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_queue_tasks_account
    ON queue_tasks(account_x_user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_queue_tasks_media
    ON queue_tasks(media_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_queue_tasks_parent
    ON queue_tasks(parent_task_id, updated_at DESC);
