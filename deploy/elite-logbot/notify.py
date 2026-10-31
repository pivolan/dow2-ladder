#!/usr/bin/env python3
# notify.py <file> <caption> <summary.txt> - the same "player sent logs" notice to everyone in chats.json (people who wrote to
# @elite_launcher_logs_bot): the log file (Telegram bots: up to 50 MB) with a short caption, then the summary as text.
import json, subprocess, sys

TOKEN = open('/root/.elite_logbot_token').read().strip()
API = 'https://api.telegram.org/bot%s/' % TOKEN
f, caption, summ = sys.argv[1], sys.argv[2][:1000], open(sys.argv[3]).read()[:3900]
try: chats = json.load(open('/srv/elite-logbot/chats.json'))
except Exception: chats = {}
for cid in chats:
    for args in (['-F', 'chat_id=%s' % cid, '-F', 'document=@%s' % f, '-F', 'caption=%s' % caption, API + 'sendDocument'],
                 ['--data-urlencode', 'chat_id=%s' % cid, '--data-urlencode', 'text=%s' % summ, API + 'sendMessage']):
        r = subprocess.run(['curl', '-s', '-m', '120'] + args, capture_output=True, text=True)
        if '"ok":true' not in r.stdout: print('chat %s: %s' % (cid, r.stdout[:200]), file=sys.stderr)
