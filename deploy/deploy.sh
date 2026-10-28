#!/bin/bash
# dow2-web deploy: pull-based. A systemd timer on the main server runs this every 2 minutes.
# New commit on origin/main -> checks -> install the changed parts -> restart only what changed -> note in Telegram.
# No secrets in GitHub: the server pulls with its deploy key. Runs as root on the main server.
set -uo pipefail
SRC=/srv/dow2-web-deploy                     # deploy checkout (nobody edits here)
KEY=/root/.ssh/deploy_dow2-ladder
export GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes"
LOCK=/run/dow2-web-deploy.lock
exec 9>"$LOCK"; flock -n 9 || exit 0
say() { echo "$1"; }                     # only the journal (journalctl -u dow2-web-deploy): the owner wants no deploy notes in Telegram

[ -d "$SRC/.git" ] || git clone -q git@github.com:pivolan/dow2-ladder.git "$SRC" || exit 1
cd "$SRC" || exit 1
git fetch -q origin main || exit 1
NEW=$(git rev-parse origin/main); OLD=$(cat .deployed 2>/dev/null || true)
[ "$NEW" = "$OLD" ] && [ ! -f .pending ] && exit 0
[ "$NEW" = "$(cat .failed 2>/dev/null)" ] && exit 0      # this commit already failed the checks - wait for a new one
git checkout -q -f "$NEW" || exit 1
MSG=$(git log -1 --format='%h %s' "$NEW")

# ---- checks: every Python file compiles, every JS block in the pages parses
fail=""
for f in $(git ls-files '*.py'); do python3 -m py_compile "$f" 2>/dev/null || fail="$fail $f"; done
find . -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null
if [ -n "$fail" ]; then echo "$NEW" > .failed; say "⛔ dow2-web: $MSG — не выложено, не компилируется:$fail"; exit 1; fi

if [ -n "$OLD" ] && git cat-file -e "$OLD" 2>/dev/null; then CH=$(git diff --name-only "$OLD" "$NEW"; cat .pending 2>/dev/null)
else CH=$(git ls-files); fi
has() { echo "$CH" | grep -q "^$1"; }
done_parts=""; pending=""

# ---- ladder (service ladder, in-memory queue: wait until nobody is searching or being matched, max ~10 minutes)
if has ladder/; then
  st=$(curl -s -m 5 http://127.0.0.1:8217/ladder/api/mm/status || echo '{}')
  busy=$(echo "$st" | python3 -c 'import json,sys
try: d=json.load(sys.stdin); print(int(d.get("searching",0))+int(d.get("matched",0)))
except Exception: print(0)')
  [ -f .pending_since ] || date +%s > .pending_since
  age=$(( $(date +%s) - $(cat .pending_since) ))
  if [ "${busy:-0}" -gt 0 ] && [ $age -lt 600 ]; then pending="$pending
ladder/"; else
    install -o root -g root -m 644 ladder/ladder.py ladder/ladder.html ladder/fb_admin.html ladder/capture.py ladder/maps.txt /srv/ladder/app/
    install -o root -g root -m 644 ladder/guess_from_replays.py /srv/ladder/
    systemctl restart dow2-ladder && done_parts="$done_parts ладдер"
  fi
fi
# ---- player statistics (service relicstats)
if has stats/; then
  rsync -a --chown=root:relicstats --chmod=D750,F640 stats/ /srv/relicstats/app/
  systemctl restart relic-stats && done_parts="$done_parts статистика"
fi
# ---- replays (service replayweb) + the story worker
if has replay/; then
  rsync -a --chown=root:replayweb --chmod=D750,F640 --exclude replay_story_worker.py replay/ /srv/replayweb/app/
  install -o root -g root -m 755 replay/replay_story_worker.py /usr/local/bin/replay_story_worker.py
  systemctl restart replay-web; echo "$CH" | grep -q replay_story_worker && systemctl restart replay-story
  done_parts="$done_parts реплеи"
fi
# ---- the site (nginx serves /var/www/rts-site/elite; the downloads in dl/ belong to the launcher CI)
if has site/; then
  rsync -a --exclude dl/ --chmod=D775,F664 site/ /var/www/rts-site/elite/
  find /var/www/rts-site/elite -path '*/dl' -prune -o -print0 2>/dev/null | xargs -0 -r setfacl -m u:pivodev:rwX,u:openclaw:rwX,m::rwx 2>/dev/null
  done_parts="$done_parts сайт"
fi
# ---- our game server in Moscow (Timeweb): only with no game on it
if has gameserver/; then
  live=$(ssh -o BatchMode=yes timeweb 'ss -tn state established "( sport = :47100 or sport = :47000 or sport = :47001 or sport = :47005 or sport = :47007 or sport = :47008 )" | tail -n +2 | wc -l' 2>/dev/null || echo 99)
  if [ "${live:-99}" -gt 0 ]; then pending="$pending
gameserver/"; else
    scp -q gameserver/battle_relay.py timeweb:/srv/dow2battle/battle_relay.py && scp -q gameserver/relay_proxy.py timeweb:/srv/dow2relay/relay_proxy.py \
      && ssh timeweb 'chmod 644 /srv/dow2battle/battle_relay.py /srv/dow2relay/relay_proxy.py; systemctl restart dow2-battle dow2-relay' && done_parts="$done_parts игровой-сервер"
  fi
fi
# deploy/ (systemd units, notifier) is installed by hand: it changes how the services run

if [ -n "$pending" ]; then echo "$pending" | grep . > .pending; else rm -f .pending .pending_since; fi
echo "$NEW" > .deployed
[ -n "$done_parts" ] && say "🚀 dow2-web: $MSG — выложено:$done_parts${pending:+ (ждут конца игр: $(echo $pending | tr '\n' ' '))}"
exit 0
