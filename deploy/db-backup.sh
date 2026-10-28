#!/bin/bash
# DoW2 web databases -> Cloudflare R2 (rclone remote r3:, bucket backup/dow2/<date>/). Only the SQLite databases:
# ladder (matches, ratings, automatch history), launcher chat, player stats, usage counts, replay index. Daily, kept 14 days.
# A consistent copy via VACUUM INTO (one read transaction; the services keep running and writing), zstd, upload, then the local copy goes.
set -uo pipefail
DBS="/srv/ladder/data/ladder.sqlite /srv/ladder/data/feedback.sqlite /srv/relicstats/data/relic.sqlite /srv/relicstats/data/usage.db /srv/replayweb/data/index.sqlite"
DAY=$(date +%F); TMP=$(mktemp -d /srv/.dbbackup.XXXXXX); trap 'rm -rf "$TMP"' EXIT
say() { telegram_send --chat -1003866396190 --thread 18850 text "$1" >/dev/null 2>&1 || true; echo "$1"; }
fail=""; total=0
for db in $DBS; do
  [ -f "$db" ] || continue
  name=$(echo "${db#/srv/}" | tr / _)
  if sqlite3 "$db" ".timeout 60000" "VACUUM INTO '$TMP/$name'" && zstd -q -T0 -9 --rm "$TMP/$name" -o "$TMP/$name.zst" \
     && rclone copyto "$TMP/$name.zst" "r3:backup/dow2/$DAY/$name.zst" --s3-no-check-bucket; then
    total=$(( total + $(stat -c %s "$TMP/$name.zst") )); rm -f "$TMP/$name.zst"
  else fail="$fail $name"; fi
done
rclone delete r3:backup/dow2 --min-age 14d --s3-no-check-bucket 2>/dev/null; rclone rmdirs r3:backup/dow2 --leave-root 2>/dev/null
if [ -n "$fail" ]; then say "⛔ бэкап баз DoW2 ($DAY): не вышло:$fail"; exit 1; fi
echo "backup $DAY ok, $(( total / 1048576 )) MB"
