#!/bin/bash
# Новые ТЕКСТОВЫЕ ответы игроков в чате лаунчера (feedback.sqlite, author=player) -> Telegram, тема dow_replay (18850).
# Загрузки логов (текст начинается с 📋) не дублируем. Мьют: data/fb_mod.json {all_muted, muted:[uid...]}.
set -uo pipefail
DB=/srv/ladder/data/feedback.sqlite
LADDER=/srv/ladder/data/ladder.sqlite
MOD=/srv/ladder/data/fb_mod.json
STATE=/var/lib/elite-fb-notify.lastid
CHAT=-1003866396190; THREAD=18850
[ -e "$DB" ] || exit 0
if [ ! -f "$STATE" ]; then sqlite3 "$DB" "SELECT COALESCE(MAX(id),0) FROM fb" > "$STATE"; fi
last=$(cat "$STATE" 2>/dev/null); last=${last:-0}
ALLMUTED=$(python3 -c "import json;print('1' if json.load(open('$MOD')).get('all_muted') else '0')" 2>/dev/null || echo 0)
sqlite3 -separator $'\t' "$DB" \
  "SELECT id, uid, COALESCE(name,''), COALESCE(meta,''), replace(replace(text,char(13),' '),char(10),' ') \
   FROM fb WHERE id>$last AND author='player' AND text NOT LIKE '📋%' ORDER BY id" \
| while IFS=$'\t' read -r id uid name meta text; do
    [ -n "$id" ] || continue
    # мьют: весь чат или конкретный uid -> пропускаем, но двигаем lastid
    if [ "$ALLMUTED" = "1" ]; then echo "$id" > "$STATE"; continue; fi
    muted=$(python3 -c "import json,sys;print('1' if '$uid' in (json.load(open('$MOD')).get('muted') or []) else '0')" 2>/dev/null || echo 0)
    if [ "$muted" = "1" ]; then echo "$id" > "$STATE"; continue; fi
    steam=$(printf '%s' "$meta" | grep -o '"steam"[^0-9]*[0-9]*' | grep -o '[0-9]\{5,\}' | head -1)
    nick=""
    [ -n "$steam" ] && nick=$(sqlite3 "$LADDER" "SELECT alias FROM profiles WHERE steam='$steam' LIMIT 1" 2>/dev/null)
    who=${nick:-${name:-anon}}
    safe=$(printf '%s' "$text" | tr -d '_*`[]')
    whos=$(printf '%s' "$who" | tr -d '_*`[]')
    cap="💬 Ответ игрока «$whos»${steam:+ · steam $steam} · чат ${uid:0:8} — ответить: rts.fintebtc.de/ladder/fb-admin
$safe"
    if telegram_send --chat "$CHAT" --thread "$THREAD" text "$cap" >/dev/null 2>&1; then
        echo "$id" > "$STATE"
    else
        echo "elite-fb-notify: send failed for id $id" >&2; exit 1
    fi
done
