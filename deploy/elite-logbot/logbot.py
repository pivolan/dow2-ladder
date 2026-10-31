#!/usr/bin/env python3
# Elite log bot (31.10.2026): long-polls the Telegram bot, remembers everyone who writes to it (chat id, name) in chats.json
# and answers with their chat id. Later the "player sent logs" notices are duplicated to the chats listed there.
# Token: /root/.elite_logbot_token (never on a command line).
import json, os, time, urllib.request

DIR = '/srv/elite-logbot'
CHATS = os.path.join(DIR, 'chats.json')
TOKEN = open('/root/.elite_logbot_token').read().strip()
API = 'https://api.telegram.org/bot%s/' % TOKEN


def call(method, **params):
    req = urllib.request.Request(API + method, json.dumps(params).encode(), {'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.load(r)


def load():
    try: return json.load(open(CHATS))
    except Exception: return {}


def save(chats):
    tmp = CHATS + '.tmp'
    json.dump(chats, open(tmp, 'w'), ensure_ascii=False, indent=2)
    os.replace(tmp, CHATS)


def main():
    os.makedirs(DIR, exist_ok=True)
    offset = 0
    while True:
        try:
            res = call('getUpdates', offset=offset, timeout=25, allowed_updates=['message'])
        except Exception as e:
            print('getUpdates failed:', str(e)[:120], flush=True); time.sleep(5); continue
        for u in res.get('result', []):
            offset = u['update_id'] + 1
            m = u.get('message') or {}
            chat, frm = m.get('chat') or {}, m.get('from') or {}
            if not chat.get('id'): continue
            chats = load()
            cid = str(chat['id'])
            new = cid not in chats
            chats[cid] = {'type': chat.get('type'), 'username': frm.get('username') or chat.get('username'),
                          'name': ' '.join(x for x in (frm.get('first_name'), frm.get('last_name')) if x) or chat.get('title'),
                          'first_seen': chats.get(cid, {}).get('first_seen') or int(time.time()), 'last_text': (m.get('text') or '')[:200]}
            save(chats)
            print('chat %s @%s %s: %s' % (cid, chats[cid]['username'], chats[cid]['name'], chats[cid]['last_text']), flush=True)
            if new or (m.get('text') or '').startswith('/start'):
                try: call('sendMessage', chat_id=chat['id'], text='Ваш chat id: %s\nСюда будут приходить уведомления о присланных логах Elite.' % cid)
                except Exception as e: print('reply failed:', str(e)[:120], flush=True)


if __name__ == '__main__':
    main()
