#!/bin/sh
set -eu

mkdir -p /data/archive /data/twscrape
# Only claim the small state paths. /data/archive may contain tens of
# gigabytes of host media and must never be walked recursively here. These
# operations stay constant-time and also repair files created by root-owned
# one-shot migration containers.
chown appuser:appuser /data /data/twscrape
for state_file in \
    /data/archive.sqlite3 \
    /data/archive.sqlite3-wal \
    /data/archive.sqlite3-shm \
    /data/twscrape/accounts.db \
    /data/twscrape/accounts.db-wal \
    /data/twscrape/accounts.db-shm
do
    if [ -e "$state_file" ]; then
        chown appuser:appuser "$state_file"
    fi
done
if [ ! -f /data/.archivex-owner-10001 ]; then
    touch /data/.archivex-owner-10001
    chown appuser:appuser /data/.archivex-owner-10001
fi

# /data/archive can be a nested host bind mount over the /data state volume.
# Its existing contents were prepared by earlier releases; only claim the root
# here so a large media archive is never recursively chowned on every startup.
chown appuser:appuser /data/archive
if [ ! -f /data/archive/.archivex-media-owner-10001 ]; then
    touch /data/archive/.archivex-media-owner-10001
    chown appuser:appuser /data/archive/.archivex-media-owner-10001
fi

exec gosu appuser "$@"
