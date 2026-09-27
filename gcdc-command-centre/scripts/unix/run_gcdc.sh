#!/usr/bin/env sh
# Runs one gcdc command (from cron) and appends its output to logs/gcdc-YYYY-MM.log.
set -u
HERE=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$HERE" || exit 1
mkdir -p logs
LOG="logs/gcdc-$(date +%Y-%m).log"
GCDC="${GCDC_BIN:-$HERE/.venv/bin/gcdc}"
echo "==== $(date '+%Y-%m-%d %H:%M:%S') gcdc $*" >> "$LOG"
"$GCDC" "$@" >> "$LOG" 2>&1
rc=$?
echo "==== exit $rc" >> "$LOG"
exit $rc
