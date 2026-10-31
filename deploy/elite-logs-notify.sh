#!/bin/bash
# Новые логи из лаунчера Elite (/srv/relicstats/logs) → Telegram, тема dow_replay (18850). Запускается systemd path-юнитом.
# 31.10.2026: к каждому логу - короткая сводка /srv/relicstats/logsummary.py (только новое с прошлого лога этого игрока,
# известные сигнатуры объяснены, шум отброшен); то же уходит всем, кто написал боту @elite_launcher_logs_bot.
SENT=/var/lib/elite-logs-notify.sent; touch "$SENT"
for f in /srv/relicstats/logs/*.txt.gz; do
  [ -e "$f" ] || continue
  b=$(basename "$f"); grep -qxF "$b" "$SENT" && continue
  gzip -t "$f" 2>/dev/null || continue          # still being written: the next change event picks it up
  t=/tmp/elite-logs-$$-${b%.gz}; zcat "$f" > "$t"
  who=${b#*_}; who=${who%.txt.gz}
  if [ $(stat -c%s "$t") -gt 45000000 ]; then rm -f "$t"; t=/tmp/elite-logs-$$-$b; cp "$f" "$t"; fi   # с дампами — сжатым
  rid=${b%%_*}; uid=$(cat "/srv/relicstats/logs/$rid.uid" 2>/dev/null)
  steam=$(zcat "$f" | grep -a -m1 -o "start, steam=[0-9]*" | cut -d= -f2)
  [ -z "$steam" ] && steam=$(zcat "$f" | grep -a -m1 -o "Found profile: /steam/[0-9]*" | grep -o "[0-9]*$")
  nick=""; [ -n "$steam" ] && nick=$(sqlite3 /srv/ladder/data/ladder.sqlite "SELECT alias FROM profiles WHERE steam='$steam' LIMIT 1" 2>/dev/null)
  cap="📋 Логи из лаунчера Elite от «$who»${nick:+ · ник $nick}${steam:+ · steam $steam} ($(du -h "$t" | cut -f1)) · id $rid${uid:+ · чат ${uid:0:8} — ответить: rts.fintebtc.de/ladder/fb-admin}"
  telegram_send --chat -1003866396190 --thread 18850 file "$t" "$cap" >/dev/null 2>&1 && echo "$b" >> "$SENT"
  s=/tmp/elite-logsum-$$-$rid.txt
  python3 /srv/relicstats/logsummary.py "$f" > "$s" 2>/dev/null || /usr/local/bin/elite-crash-digest.sh "$f" > "$s" 2>/dev/null
  [ -n "$uid" ] && printf '\nчат %s — ответить: rts.fintebtc.de/ladder/fb-admin\n' "${uid:0:8}" >> "$s"
  if [ -s "$s" ]; then
    telegram_send --chat -1003866396190 --thread 18850 text "$(cat "$s")" >/dev/null 2>&1
    python3 /srv/elite-logbot/notify.py "$t" "$cap" "$s" >> /var/log/elite-logbot-notify.log 2>&1
  fi
  rm -f "$t" "$s"
done
