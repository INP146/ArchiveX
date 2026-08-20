#!/bin/sh
set -eu

mkdir -p /data/archive /data/twscrape
if [ ! -f /data/.archivex-owner-10001 ]; then
    chown -R appuser:appuser /data
    touch /data/.archivex-owner-10001
    chown appuser:appuser /data/.archivex-owner-10001
fi

exec gosu appuser "$@"
