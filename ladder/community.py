# Launcher community: Steam sign-in, who has the launcher open, the players' chat. Part of the ladder service
# (ladder.py routes /ladder/api/c/* here). Reading is open to everyone; writing needs a Steam sign-in.
#
# Sign-in (Steam OpenID 2.0, no API key): the launcher makes a random nonce and opens the browser at
#   /ladder/api/c/login?nonce=N -> Steam -> /ladder/api/c/return?nonce=N (checked with Steam: check_authentication)
# then asks /ladder/api/c/claim?nonce=N (once) for its session token. Names and avatars: users.py (from memory, Steam is
# asked in the background, never inside a request).
#
# GET  login?nonce=          -> redirect to Steam            GET  return?...      <- Steam comes back here
# GET  claim?nonce=          -> {token, steam, name, avatar} (once, 10 minutes)
# POST local   {steam, name, device[, token]} -> sign-in with the Steam account logged in on this computer, no browser
#      (steamcommunity.com is blocked in Russia: the OpenID page never comes back there). The Steam id is the player's own
#      claim, so the account is bound to the first computer (device = hash of the machine id) that signs in with it; another
#      computer needs one sign-in through the browser (token of that session) to be bound too.
# Every step is written to the journal (journalctl -u dow2-ladder | grep community).
# POST me      {token}       -> the signed-in profile (name, avatar, rating, stats: 1v1 games / wins / losses this season; rank: null)
# POST hello   {token?, state, uid?}  "I'm here" (old launchers every ~30 s, new ones when menu / game changes) -> {online: [...]}
# GET  online                -> who has the launcher open (signed in, seen in the last 90 s)
# GET  live                  -> the Online block: searching / ingame / launcher + users (ladder.live_view)
# GET  stream?after=all:N,ru:N,en:N  (headers X-Token, X-State) -> SSE, see stream.py
# GET  msgs?room=&after=&limit=&before= -> {msgs: [...]}: the last `limit` (1000 at most and by default), the ones after
#      an id (200 at most) or before one (paging up). RU and EN are one feed in two languages: text is in the language of
#      the asked room, orig / lang / ru / en carry the original and the translations.
# POST say     {token, room, text, cid?} -> {ok, id, msg} | {ok: false, error[, retry_after ms][, retry]}
# POST logout  {token}
# POST mod     {admin, op: del|ban|unban, id|steam}   (admin = the fb-admin token)
import collections, html, json, os, re, secrets, sqlite3, threading, time, urllib.parse, urllib.request
import stream, users

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = ROOT + '/data/community.sqlite'
BASE = 'https://dow2.fintebtc.de'                    # where Steam sends the player back (OpenID realm)
ROOMS = ('all', 'ru', 'en')
FEED = {'all': ('all',), 'ru': ('ru', 'en'), 'en': ('ru', 'en')}   # RU and EN: one feed, the room only picks the language
ONLINE_SECS = 90
MM_STATE = None                                     # ladder.py: steam -> 'search' | 'match' | None
ADMIN_OK = None                                     # ladder.py: admin token -> bool
STATS = None                                        # ladder.py: steam -> 1v1 stats of the season for the profile
LIVE = None                                         # ladder.py: live_view()
LOCK = threading.Lock()
PRESENCE = {}                                       # steam -> {'seen', 'state'}  (in memory: who is here now; old endpoints)
RATE = {}                                           # steam -> [times of the last messages]
INFLIGHT = {}                                       # (steam, cid) -> Event while that message is being checked

def log(event, **kv):
    print('community: %s %s' % (event, ' '.join('%s=%s' % (k, str(v)[:120].replace(' ', '_')) for k, v in kv.items())), flush=True)

def db():
    c = sqlite3.connect(DB, timeout=30); c.execute('PRAGMA journal_mode=WAL'); return c

def init_db():  # once at start, not on every connection
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS device (steam TEXT, device TEXT, first INTEGER, last INTEGER, PRIMARY KEY (steam, device));
        CREATE TABLE IF NOT EXISTS sess (token TEXT PRIMARY KEY, steam TEXT, ts INTEGER);
        CREATE TABLE IF NOT EXISTS pending (nonce TEXT PRIMARY KEY, token TEXT, ts INTEGER);
        CREATE TABLE IF NOT EXISTS prof (steam TEXT PRIMARY KEY, name TEXT, avatar TEXT, ts INTEGER, banned INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS msg (id INTEGER PRIMARY KEY AUTOINCREMENT, room TEXT, steam TEXT, text TEXT, ts INTEGER, deleted INTEGER DEFAULT 0);
        CREATE INDEX IF NOT EXISTS msg_room ON msg(room, id);
        ''')
        have = {r[1] for r in c.execute('PRAGMA table_info(msg)')}
        # one row per message: the original (text) in its language (lang: ru / en / other) and the translation to the
        # other language (NULL when not needed or the same as the original); cid - the launcher's id against duplicates
        for col in ('lang TEXT', 'text_ru TEXT', 'text_en TEXT', 'cid TEXT'):
            if col.split()[0] not in have: c.execute('ALTER TABLE msg ADD COLUMN ' + col)
        c.execute('CREATE UNIQUE INDEX IF NOT EXISTS msg_cid ON msg(steam, cid) WHERE cid IS NOT NULL')
    users.DB = DB
    users.load()

def _get(url, data=None, timeout=15):
    r = urllib.request.Request(url, data=data, headers={'User-Agent': 'EliteLadder/1.0'})
    with urllib.request.urlopen(r, timeout=timeout) as f: return f.read().decode('utf-8', 'replace')

def _session(token):
    if not token or len(str(token)) > 80: return None
    with db() as c:
        r = c.execute('SELECT steam FROM sess WHERE token=?', (str(token),)).fetchone()
    return r and r[0]

def _user(steam):
    u = users.get(steam)
    return {'steam': steam, 'name': u['name'], 'avatar': u['avatar']}

# ---------- sign-in ----------
def login_url(nonce):
    ret = BASE + '/ladder/api/c/return?' + urllib.parse.urlencode({'nonce': nonce})
    q = {'openid.ns': 'http://specs.openid.net/auth/2.0', 'openid.mode': 'checkid_setup', 'openid.return_to': ret,
         'openid.realm': BASE + '/', 'openid.identity': 'http://specs.openid.net/auth/2.0/identifier_select',
         'openid.claimed_id': 'http://specs.openid.net/auth/2.0/identifier_select'}
    return 'https://steamcommunity.com/openid/login?' + urllib.parse.urlencode(q)

def openid_return(query):
    q = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
    nonce = q.get('nonce', '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,64}', nonce): return None, 'bad nonce'
    if q.get('openid.mode') == 'cancel': return None, 'cancelled on the Steam page'
    if not q.get('openid.return_to', '').startswith(BASE + '/ladder/api/c/return'): return None, 'bad return_to'
    m = re.fullmatch(r'https://steamcommunity\.com/openid/id/(\d{17})', q.get('openid.claimed_id', ''))
    if not m: return None, 'no Steam id'
    check = {k: v for k, v in q.items() if k.startswith('openid.')}; check['openid.mode'] = 'check_authentication'
    try: ok = 'is_valid:true' in _get('https://steamcommunity.com/openid/login', urllib.parse.urlencode(check).encode())
    except Exception as e: return None, 'Steam did not answer (%s)' % type(e).__name__
    if not ok: return None, 'Steam did not confirm the sign-in'
    steam = m.group(1); token = secrets.token_urlsafe(32); now = int(time.time())
    with db() as c:
        c.execute('INSERT INTO sess VALUES(?,?,?)', (token, steam, now))
        c.execute('INSERT OR REPLACE INTO pending VALUES(?,?,?)', (nonce, token, now))
        c.execute('DELETE FROM pending WHERE ts < ?', (now - 600,))
    return steam, None

def claim(nonce):
    with db() as c:
        r = c.execute('SELECT token, ts FROM pending WHERE nonce=?', (str(nonce)[:64],)).fetchone()
        if not r: return {'ok': False, 'wait': True}
        c.execute('DELETE FROM pending WHERE nonce=?', (str(nonce)[:64],))
    if time.time() - r[1] > 600: return {'ok': False, 'error': 'expired'}
    steam = _session(r[0])
    return dict(_user(steam), ok=True, token=r[0], rating=users.rating(steam))

def local(d, ip=''):
    steam = str(d.get('steam', '')); dev = str(d.get('device', ''))[:64].lower()
    if not re.fullmatch(r'7656119\d{10}', steam): log('local-bad', ip=ip, steam=steam); return {'ok': False, 'error': 'no Steam account found on this computer'}
    if not re.fullmatch(r'[0-9a-f]{32,64}', dev): log('local-bad', ip=ip, steam=steam, device=dev[:12]); return {'ok': False, 'error': 'bad device id'}
    now = int(time.time())
    with db() as c:
        known = [r[0] for r in c.execute('SELECT device FROM device WHERE steam=?', (steam,))]
        if known and dev not in known and _session(d.get('token')) != steam:
            log('local-other-computer', ip=ip, steam=steam, device=dev[:12], known=len(known))
            return {'ok': False, 'need_browser': True, 'error': 'this Steam account signed in from another computer: confirm once through Steam in the browser'}
        c.execute('INSERT INTO device VALUES(?,?,?,?) ON CONFLICT(steam, device) DO UPDATE SET last=excluded.last', (steam, dev, now, now))
        token = secrets.token_urlsafe(32)
        c.execute('INSERT INTO sess VALUES(?,?,?)', (token, steam, now))
    users.fetch_now(steam)
    users.set_alias(steam, d.get('name'))           # the name of the Steam client here: shown if Steam gives none
    log('local-ok', ip=ip, steam=steam, device=dev[:12], first=not known)
    return dict(_user(steam), ok=True, token=token, rating=users.rating(steam))

# ---------- presence ----------
def hello(d):
    steam = _session(d.get('token'))
    state = d.get('state') if d.get('state') in ('menu', 'game') else 'menu'
    if steam:
        with LOCK: PRESENCE[steam] = {'seen': time.time(), 'state': state}
        users.launcher_seen(steam, state)
    return {'ok': True, 'signed_in': bool(steam), 'online': online()}

def online():
    now = time.time()
    with LOCK: here = [(s, p) for s, p in PRESENCE.items() if now - p['seen'] < ONLINE_SECS]
    out = []
    for s, p in here:
        u = users.get(s, True); st = p['state']
        try: st = (MM_STATE and MM_STATE(s)) or st
        except Exception: pass
        out.append({'steam': s, 'name': u['name'], 'avatar': u['avatar'], 'state': st, 'rating': u['rating']})
    out.sort(key=lambda u: u['name'].lower())
    return out

# ---------- moderation and translation ----------
# One call per message to the checking service (data/chat_check.json: {"url": ..., "token": ...}), POST
# {text, lang, translate: [ru|en, ...]} -> {allowed, reason?, ru?, en?}. The language is found here (share of Cyrillic),
# only the other language is asked for ("other": both); texts with 3 letters or fewer (gg, +, smileys, numbers, links)
# are neither checked nor translated; short repeated texts come from a small cache. No file: everything is allowed and
# nothing is translated. 4 calls at a time, 30 s per message in all; no answer in time = not published, the author retries.
CHECK_FILE = ROOT + '/data/chat_check.json'
CHECK_SEM = threading.Semaphore(4)
BUDGET = 30
CACHE = collections.OrderedDict()                   # short text -> result (500 at most)

def lang_of(text):
    cyr = len(re.findall(r'[а-яёА-ЯЁ]', text)); lat = len(re.findall(r'[A-Za-z]', text))
    return 'other' if not cyr and not lat else 'ru' if cyr >= lat else 'en'

def trivial(text):
    return len(re.findall(r'[^\W\d_]', re.sub(r'https?://\S+', '', text))) <= 3

def _check_cfg():
    try:
        with open(CHECK_FILE) as f: cfg = json.load(f)
        return cfg if cfg.get('url') else None
    except Exception:
        return None

def check(text, moderate=True):
    # {allowed, reason, lang, ru, en}, or None when the service gave no answer in time
    lang = lang_of(text); out = {'allowed': True, 'reason': '', 'lang': lang, 'ru': None, 'en': None}
    cfg = _check_cfg()
    if not cfg or trivial(text): return out
    key = text.strip().lower() if len(text) <= 40 else None
    with LOCK:
        if key in CACHE: CACHE.move_to_end(key); return dict(CACHE[key])
    need = ['en'] if lang == 'ru' else ['ru'] if lang == 'en' else ['ru', 'en']
    end = time.time() + BUDGET
    if not CHECK_SEM.acquire(timeout=BUDGET): return None
    try:
        left = end - time.time()
        if left < 1: return None
        body = json.dumps({'text': text, 'lang': lang, 'translate': need, 'moderate': moderate}, ensure_ascii=False).encode('utf-8')
        r = urllib.request.Request(cfg['url'], data=body, headers={'Content-Type': 'application/json', 'Authorization': 'Bearer %s' % cfg.get('token', '')})
        with urllib.request.urlopen(r, timeout=left) as f: res = json.load(f)
    except Exception as e:
        log('check-fail', error=type(e).__name__); return None
    finally:
        CHECK_SEM.release()
    out['allowed'] = bool(res.get('allowed', True)); out['reason'] = str(res.get('reason') or '')[:200]
    for l in need:
        tr = str(res.get(l) or '').strip()[:1000]
        out[l] = tr if tr and tr != text else None
    if key and out['allowed']:
        with LOCK:
            CACHE[key] = dict(out)
            while len(CACHE) > 500: CACHE.popitem(last=False)
    return out

def backfill():
    # once at start: the last 1000 RU/EN messages without a language get one and their translation, a message a second;
    # each translated one goes out (msg_tr), until then the original is shown
    if not _check_cfg(): return
    with db() as c:
        rows = c.execute("SELECT id, room, text FROM msg WHERE room IN ('ru','en') AND deleted=0 AND lang IS NULL ORDER BY id DESC LIMIT 1000").fetchall()
    for mid, room, text in rows:
        r = check(text, moderate=False)
        if r is None: time.sleep(10); continue      # the service is down: the rest waits for the next start
        with db() as c: c.execute('UPDATE msg SET lang=?, text_ru=?, text_en=? WHERE id=?', (r['lang'], r['ru'], r['en'], mid))
        if r['ru'] or r['en']: stream.publish('msg_tr', {'id': mid, 'room': room, 'ru': r['ru'], 'en': r['en']})
        time.sleep(1)

# ---------- chat ----------
COLS = 'id, room, steam, text, ts, lang, text_ru, text_en, cid'

def _int(v):
    try: return max(0, int(v))
    except Exception: return 0

def _msg(r, room=None):
    # room: the asked one (its language picks text; old launchers show text as is); None = the stream event (room it was written in)
    u = users.get(r[2]); orig, ru, en = r[3], r[6], r[7]
    text = (ru or orig) if room == 'ru' else (en or orig) if room == 'en' else orig
    return {'id': r[0], 'room': room or r[1], 'steam': r[2], 'name': u['name'], 'avatar': u['avatar'], 'text': text, 'ts': r[4],
            'cid': r[8], 'lang': r[5] or '', 'orig': orig, 'ru': ru, 'en': en}

def msgs(room, after, limit=None, before=None):
    room = room if room in ROOMS else 'all'; rooms = FEED[room]
    after, before = _int(after), _int(before); limit = min(1000, _int(limit) or 1000)
    q = 'SELECT %s FROM msg WHERE room IN (%s) AND deleted=0' % (COLS, ','.join('?' * len(rooms)))
    with db() as c:
        if after > 0: rows = c.execute(q + ' AND id>? ORDER BY id LIMIT 200', (*rooms, after)).fetchall()
        elif before > 0: rows = c.execute(q + ' AND id<? ORDER BY id DESC LIMIT ?', (*rooms, before, limit)).fetchall()[::-1]
        else: rows = c.execute(q + ' ORDER BY id DESC LIMIT ?', (*rooms, limit)).fetchall()[::-1]
        dead = [r[0] for r in c.execute('SELECT id FROM msg WHERE room IN (%s) AND deleted=1 AND id>? ORDER BY id DESC LIMIT 50' % ','.join('?' * len(rooms)),
                                        (*rooms, max(0, after - 500)))]
    return {'room': room, 'msgs': [_msg(r, room) for r in rows], 'deleted': dead}

def missed(after):
    # for the stream: after = "all:123,ru:45,en:67" (the last ids the launcher has); RU and EN share a feed (the smaller id).
    # More than 200 missed in a feed: a reset for it instead, the launcher reads its last 100 again
    got = {}
    for part in str(after or '').split(','):
        k, _, v = part.partition(':')
        if k in ROOMS and v.isdigit(): got[k] = int(v)
    out = []
    feeds = [('all', got.get('all'))] + [('ru', min(v for v in (got.get('ru'), got.get('en')) if v is not None) if 'ru' in got or 'en' in got else None)]
    for room, a in feeds:
        if a is None: continue
        rooms = FEED[room]
        with db() as c:
            rows = c.execute('SELECT %s FROM msg WHERE room IN (%s) AND deleted=0 AND id>? ORDER BY id LIMIT 201' % (COLS, ','.join('?' * len(rooms))), (*rooms, a)).fetchall()
        if len(rows) > 200: out.append(('reset', {'room': room})); continue
        out += [('msg', _msg(r)) for r in rows]
    return out

def _by_cid(steam, cid):
    with db() as c:
        r = c.execute('SELECT %s FROM msg WHERE steam=? AND cid=?' % COLS, (steam, cid)).fetchone()
    return r

def say(d):
    steam = _session(d.get('token'))
    if not steam: return {'ok': False, 'error': 'sign in with Steam to write'}
    if users.get(steam)['banned']: return {'ok': False, 'error': 'you cannot write in the chat'}
    room = d.get('room') if d.get('room') in ROOMS else 'all'
    text = ''.join(ch for ch in str(d.get('text', '')) if ch == '\n' or ch.isprintable()).strip()[:500]
    if not text: return {'ok': False, 'error': 'empty'}
    cid = str(d.get('cid') or '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', cid): cid = None   # optional: old launchers send none
    mine = None
    if cid:
        # the same message again (a retry): the stored one, or wait for the check already running
        r = _by_cid(steam, cid)
        if r: return {'ok': True, 'id': r[0], 'msg': _msg(r, room)}
        with LOCK:
            ev = INFLIGHT.get((steam, cid))
            if not ev: mine = INFLIGHT[(steam, cid)] = threading.Event()
        if ev:
            ev.wait(BUDGET + 5); r = _by_cid(steam, cid)
            return {'ok': True, 'id': r[0], 'msg': _msg(r, room)} if r else {'ok': False, 'error': 'timeout', 'retry': True}
    try:
        now = time.time()
        with LOCK:
            t = [x for x in RATE.get(steam, []) if now - x < 60]
            if t and now - t[-1] < 1.5: return {'ok': False, 'error': 'too fast', 'retry_after': int((1.5 - (now - t[-1])) * 1000) + 50}
            if len(t) >= 20: return {'ok': False, 'error': 'too fast', 'retry_after': int((60 - (now - t[0])) * 1000) + 50}
            RATE[steam] = t + [now]
            PRESENCE.setdefault(steam, {'state': 'menu'})['seen'] = now
        users.launcher_seen(steam)
        chk = check(text)
        if chk is None: return {'ok': False, 'error': 'timeout', 'retry': True}
        if not chk['allowed']:
            log('rejected', steam=steam, reason=chk['reason']); return {'ok': False, 'error': chk['reason'] or 'rejected'}
        try:
            with db() as c:
                cur = c.execute('INSERT INTO msg(room, steam, text, ts, lang, text_ru, text_en, cid) VALUES(?,?,?,?,?,?,?,?)',
                                (room, steam, text, int(now), chk['lang'], chk['ru'], chk['en'], cid))
                r = c.execute('SELECT %s FROM msg WHERE id=?' % COLS, (cur.lastrowid,)).fetchone()
        except sqlite3.IntegrityError:              # the same cid stored a moment ago
            r = _by_cid(steam, cid)
        stream.publish('msg', _msg(r))
        return {'ok': True, 'id': r[0], 'msg': _msg(r, room)}
    finally:
        if mine:
            with LOCK: INFLIGHT.pop((steam, cid), None)
            mine.set()

def me(d):
    steam = _session(d.get('token'))
    if not steam: return {'ok': False, 'signed_in': False}
    try: stats = STATS and STATS(steam)
    except Exception: stats = None
    return dict(_user(steam), ok=True, signed_in=True, rating=users.rating(steam), stats=stats)

def me_event(steam):  # the stream's own card: rating and the season's 1v1 games
    try: st = (STATS and STATS(steam)) or {}
    except Exception: st = {}
    return {'rating': users.rating(steam), 'games': st.get('games', 0), 'wins': st.get('wins', 0), 'losses': st.get('losses', 0)}

def logout(d):
    with db() as c: c.execute('DELETE FROM sess WHERE token=?', (str(d.get('token', ''))[:80],))
    return {'ok': True}

def mod(d):
    if not (ADMIN_OK and ADMIN_OK(d.get('admin'))): return {'ok': False, 'error': 'token'}
    op = d.get('op')
    if op == 'del':
        mid = _int(d.get('id'))
        with db() as c:
            c.execute('UPDATE msg SET deleted=1 WHERE id=?', (mid,))
            r = c.execute('SELECT room FROM msg WHERE id=?', (mid,)).fetchone()
        if r: stream.publish('msg_del', {'id': mid, 'room': r[0]})
    elif op in ('ban', 'unban'):
        steam = str(d.get('steam', ''))
        users.set_banned(steam, op == 'ban')
        with db() as c: c.execute('UPDATE prof SET banned=? WHERE steam=?', (1 if op == 'ban' else 0, steam))
    else: return {'ok': False, 'error': 'op'}
    return {'ok': True}

PAGE = '''<!doctype html><meta charset="utf-8"><title>Elite Launcher</title>
<style>body{background:#101614;color:#e8e2cf;font:16px system-ui,sans-serif;display:grid;place-items:center;height:100vh;margin:0}
div{max-width:420px;text-align:center;line-height:1.5}b{color:#d4b25c}</style><div>%s</div>'''

def handle(h, method, u, d):
    p = u.path[len('/ladder/api/c/'):]
    ip = h.headers.get('X-Real-IP') or h.headers.get('X-Forwarded-For', '').split(',')[0] or h.client_address[0]
    if method == 'GET':
        qs = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if p == 'login':
            if not re.fullmatch(r'[A-Za-z0-9_-]{16,64}', qs.get('nonce', '')): return h.send(400, {'error': 'nonce'}, cache=0)
            log('login', ip=ip, nonce=qs['nonce'][:8], ua=h.headers.get('User-Agent', '')[:60])
            h.send_response(302); h.send_header('Location', login_url(qs['nonce'])); h.send_header('Cache-Control', 'no-store')
            h.send_header('Content-Length', '0'); h.end_headers(); return
        if p == 'return':
            steam, err = openid_return(query=u.query)
            log('return', ip=ip, nonce=qs.get('nonce', '')[:8], steam=steam or '-', error=err or '-')
            if err: return h.send_html(PAGE % ('Не получилось войти / Sign-in failed: %s' % html.escape(err)), cache=0)
            users.fetch_now(steam)
            name = html.escape(users.get(steam)['name'])
            return h.send_html(PAGE % ('Вы вошли как <b>%s</b>. Вернитесь в лаунчер.<br><br>Signed in as <b>%s</b>. Go back to the launcher.' % (name, name)), cache=0)
        if p == 'claim':
            r = claim(qs.get('nonce', ''))
            if not r.get('wait'): log('claim', ip=ip, nonce=qs.get('nonce', '')[:8], ok=r.get('ok'), steam=r.get('steam', '-'), error=r.get('error', '-'))
            return h.send(200, r, cache=0)
        if p == 'online': return h.send(200, {'online': online()}, cache=0)
        if p == 'live': return h.send(200, LIVE(), cache=0)
        if p == 'msgs': return h.send(200, msgs(qs.get('room', 'all'), qs.get('after', '0'), qs.get('limit'), qs.get('before')), cache=0)
        if p == 'stream':
            # the token only in a header, never in the address (addresses end up in logs)
            steam = _session(h.headers.get('X-Token'))
            state = h.headers.get('X-State') if h.headers.get('X-State') in ('menu', 'game') else 'menu'
            if steam: users.launcher_seen(steam, state)
            return stream.serve(h, ip, steam, lambda: users.launcher_seen(steam), lambda: missed(qs.get('after')), lambda: me_event(steam))
        return h.send(404, {'error': 'not found'}, cache=0)
    if p == 'local': return h.send(200, local(d or {}, ip), cache=0)
    fn = {'hello': hello, 'say': say, 'me': me, 'logout': logout, 'mod': mod}.get(p)
    if not fn: return h.send(404, {'error': 'not found'}, cache=0)
    return h.send(200, fn(d or {}), cache=0)

init_db()
threading.Thread(target=backfill, daemon=True).start()
