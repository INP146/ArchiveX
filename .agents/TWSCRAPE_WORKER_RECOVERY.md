# twscrape Worker Recovery

## Symptom

When the only active twscrape account is locked, the crawler used to wait 30
seconds for every account task. A crawl child that exited while holding a
twscrape request lock left that lock in `accounts.db` for twscrape's fixed
15-minute lease. The container itself stayed healthy because Taskiq restarted
only the child process.

## Runtime behavior

- `TWSCRAPE_WAIT_TIMEOUT_SECONDS` defaults to `0.5`; a task fails fast when no
  account is available instead of occupying the crawl slot for 30 seconds.
- `AccountPoolUnavailableError` carries the earliest lock expiry. The retry
  middleware schedules all affected account tasks for that time, so they wake
  together rather than creating a 30-second retry ladder.
- twscrape 0.20.1 reports GraphQL error 336 as `GqlFeaturesOutdatedError`
  instead of calling `exit(1)`. The ArchiveX adapter preserves that failure as
  a retryable error while its context wrapper releases the account lock.
- Crawl workers hold an advisory lock beside `accounts.db`. A replacement
  child clears a twscrape lock only when the previous holder left an unclean
  marker and no other crawler currently holds the advisory lock. Recovery
  matches the account username, endpoint, and original lock timestamp, so it
  cannot remove another endpoint's lock or a newer rate-limit lock. Failures
  while persisting a rate-limit or inactive-account state are never treated as
  ownerless request locks. A clean shutdown preserves legitimate rate limits.
- Forked children immediately close inherited advisory-lock descriptors and
  discard inherited in-memory tracking. The supported crawl topology remains
  one worker process with one async task, as fixed by the development launcher
  and both Compose files.

## Deployment note

The image-only Compose file must be downloaded at the matching release tag so it
uses the corresponding backend image and includes the `state-migrate` service.
Existing containers are intentionally not restarted by development tools. After
a controlled deployment, verify `/ready` and inspect crawl logs
for `No account available` events; a healthy retry should be one grouped retry
at the reported lock expiry, not one 30-second wait per account.

The crawl worker remains serial by default (`--max-async-tasks 1`). Increasing
that value is only useful when the twscrape session database has multiple
active crawler accounts and should be done deliberately to avoid X rate
limits.

The account-sync API reports when tasks are queued; it is not the completion
time for the whole batch. With one active crawler credential, 40 independent
account requests are intentionally serialized and a roughly one-minute batch
is expected. Reaching a materially lower wall time requires at least two
independent active crawler credentials plus an explicit concurrency change;
raising the async-task flag alone makes one credential contend on twscrape's
account lock.
