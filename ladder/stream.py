# Launcher live stream (SSE): one open GET /ladder/api/c/stream per launcher instead of polling. Part of the ladder service.
# The server sends only what changed: who is looking for a game, playing, or has the launcher open (presence, match,
# match_end), their names, avatars and ratings (user), the chat (msg, msg_tr, msg_del). On connect: the messages missed
# since the ids the launcher already has, a full snapshot (online), the own profile (me) when signed in.
#
# Every event: "event: <name>\ndata: <JSON on one line>\n\n". A ": ping" comment every 20 s keeps the connection (and the
# presence of the signed-in player) alive. A subscriber that falls 1000 events behind is dropped: it reconnects and gets
# a new snapshot. At most 5 connections per IP and 1000 in all, else 503 with "retry: 30000".
import json, queue, threading, time

PING = 20
MAX_PER_IP = 5
MAX_ALL = 1000
LIVE = None                                         # ladder.py: live_view() - the Online block from memory
LOCK = threading.Lock()
SUBS = set()

class Sub:
    def __init__(self, ip, steam):
        self.q = queue.Queue(maxsize=1000); self.ip = ip; self.steam = steam; self.dead = False

def frame(event, data):
    return ('event: %s\ndata: %s\n\n' % (event, json.dumps(data, ensure_ascii=False, separators=(',', ':')))).encode('utf-8')

def publish(event, data, to=None):
    # to: a Steam id - only the connections of that player
    with LOCK: subs = list(SUBS)
    for s in subs:
        if to and s.steam != to: continue
        try: s.q.put_nowait((event, data))
        except queue.Full: s.dead = True            # too slow: closed, it comes back and gets a snapshot

def _subscribe(ip, steam):
    with LOCK:
        if len(SUBS) >= MAX_ALL or sum(1 for s in SUBS if s.ip == ip) >= MAX_PER_IP: return None
        s = Sub(ip, steam); SUBS.add(s); return s

def serve(h, ip, steam, seen, missed, me):
    # seen(): the signed-in player is here (on connect and on every ping); missed(): [(event, data)] since the client's ids;
    # me(): the own profile event data or None
    s = _subscribe(ip, steam)
    h.close_connection = True
    if not s:
        b = b'retry: 30000\n\n'
        h.send_response(503); h.send_header('Content-Type', 'text/event-stream; charset=utf-8'); h.send_header('Cache-Control', 'no-store')
        h.send_header('Content-Length', str(len(b))); h.end_headers(); h.wfile.write(b); return
    try:
        # no Content-Length: the response lasts as long as the connection; X-Accel-Buffering: nginx passes every line at once
        h.send_response(200)
        h.send_header('Content-Type', 'text/event-stream; charset=utf-8'); h.send_header('Cache-Control', 'no-store')
        h.send_header('X-Accel-Buffering', 'no'); h.end_headers()
        w = h.wfile
        w.write(b'retry: 5000\n\n')
        for ev, d in missed(): w.write(frame(ev, d))
        w.write(frame('online', LIVE()))
        if steam:
            seen(); m = me()
            if m: w.write(frame('me', m))
        w.flush()
        while not s.dead:
            try: ev, d = s.q.get(timeout=PING)
            except queue.Empty:
                w.write(b': ping\n\n'); w.flush()
                if steam: seen()
                continue
            w.write(frame(ev, d))
            if ev == 'user' and steam and d.get('steam') == steam:   # own rating changed: the own card follows
                m = me()
                if m: w.write(frame('me', m))
            w.flush()
    except OSError:                                 # BrokenPipe, ConnectionReset/Aborted: the launcher is gone
        pass
    finally:
        with LOCK: SUBS.discard(s)

def _where(v):  # steam -> its presence event data in one snapshot (each person in one group only)
    out = {}
    for p in v['searching']: out[p['steam']] = dict(p, group='searching')
    for m in v['ingame']:
        for x in m['team1'] + m['team2']: out[x['steam']] = {'steam': x['steam'], 'group': 'ingame', 'match': m['id'], 'race': x.get('race')}
        for w in m['watchers']: out[w] = {'steam': w, 'group': 'ingame', 'match': m['id'], 'watcher': True}
    for p in v['launcher']: out[p['steam']] = dict(p, group='launcher')
    return out

def publisher():
    # once a second: the Online block against the previous one, only the differences go out; nothing changed - nothing sent
    prev = None
    while True:
        time.sleep(1)
        if not SUBS: prev = None; continue          # nobody listens: the next one starts from a snapshot anyway
        try: v = LIVE()
        except Exception as e: print('stream: live %s' % e, flush=True); continue
        if prev is not None:
            for st, u in v['users'].items():         # first: a new person's name, or a name / avatar / rating changed
                if prev['users'].get(st) != u: publish('user', dict(u, steam=st))
            a, b = _where(prev), _where(v)
            for st in a.keys() | b.keys():
                if a.get(st) != b.get(st): publish('presence', b.get(st) or {'steam': st, 'group': None})
            ma, mb = {m['id']: m for m in prev['ingame']}, {m['id']: m for m in v['ingame']}
            for mid, m in mb.items():
                if ma.get(mid) != m: publish('match', m)
            for mid in ma.keys() - mb.keys(): publish('match_end', {'id': mid})
        prev = v
