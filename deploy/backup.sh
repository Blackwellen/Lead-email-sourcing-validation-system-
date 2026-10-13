#!/bin/sh
# Daily compressed logical backup with rotation. Restore: see docs/OPERATIONS.md
set -eu
while true; do
  f="/backups/lead-$(date -u +%Y%m%dT%H%M%SZ).dump"
  if pg_dump -Fc -f "$f.tmp"; then mv "$f.tmp" "$f"; echo "backup ok: $f"; else echo "backup FAILED" >&2; rm -f "$f.tmp"; fi
  find /backups -name 'lead-*.dump' -mtime +"${BACKUP_KEEP_DAYS:-14}" -delete
  sleep 86400
done
